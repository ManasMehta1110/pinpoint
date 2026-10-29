"""The demo's API, and the page that uses it.

Three things a visitor can do:

* browse the gallery: real saved audits, served as a static file;
* ``POST /api/verify``: check any citation against a trace with Pinpoint's
  verifier. No model call, so it is free and unlimited apart from size;
* ``POST /api/audit``: run Pinpoint live on Muse Spark Open. Each audit costs
  about $0.007, so live audits are capped: a daily total for everyone, an
  hourly number per visitor, and a few at a time. Baseten serves the model;
  if it cannot be reached, the same audit runs on Fireworks when that key is
  set too.

The caps live in memory, so a restart resets them. They are a guard against
an accidental bill, not a security boundary: the per-visitor cap trusts the
first X-Forwarded-For address, which a determined client can vary. The daily
cap holds regardless.

``GET /api/health`` says whether the primary host (Baseten) is answering:
HTTP 200 if so, 503 if not, for an uptime monitor. The fallback would
otherwise hide a dead key from visitors and from us. It is learned from live
audits and from a tiny probe call made at most once an hour, however often
the URL is hit. Every model call's tokens and cost are printed to the log as
one JSON line, and ``/api/status`` totals them since the last restart.

Run locally:
    python -m app.server            # http://127.0.0.1:7860, reads .env
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.views import audit_view, trace_view
from pinpoint.adapters import trace_from_openai_messages
from pinpoint.client import PROVIDERS, ClientError, Completion, ModelClient, Provider
from pinpoint.overseer import Overseer, OverseerResult
from pinpoint.prompt import PROMPT_VERSION
from pinpoint.trace import Trace, TraceError
from pinpoint.verifier import Citation, verify

STATIC_DIR = Path(__file__).resolve().parent / "static"
PROVIDER = PROVIDERS["baseten"]
# USD per million tokens, (input, output); used only for the cost shown.
PRICE = (1.25, 4.25)

MAX_BODY_BYTES = 64_000
MAX_STEPS = 30
MAX_RENDERED_CHARS = 24_000

# The verifier's step and quote checks are the same for every misbehavior
# class; the verify endpoint needs some misbehavior verdict to run them.
_ANY_MISBEHAVIOR = "DECEIVER"


@dataclass(frozen=True)
class Limits:
    daily_cap: int = 300
    per_visitor_per_hour: int = 10
    max_concurrent: int = 4


class Budget:
    """Who may start a live audit now. Thread-safe; counts only granted audits."""

    def __init__(self, limits: Limits, clock: Callable[[], float] = time.time) -> None:
        self.limits = limits
        self._clock = clock
        self._lock = threading.Lock()
        self._day = ""
        self._used_today = 0
        self._recent: defaultdict[str, deque[float]] = defaultdict(deque)

    def _roll(self, now: float) -> None:
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        if day != self._day:
            self._day, self._used_today = day, 0

    def remaining_today(self) -> int:
        with self._lock:
            self._roll(self._clock())
            return max(0, self.limits.daily_cap - self._used_today)

    def try_acquire(self, visitor: str, units: int = 1) -> str | None:
        """Grant ``units`` audits at once, or none and say why."""
        with self._lock:
            now = self._clock()
            self._roll(now)
            recent = self._recent[visitor]
            while recent and now - recent[0] >= 3600:
                recent.popleft()
            if self._used_today >= self.limits.daily_cap:
                return "Today's live audits are used up. The gallery and verifier still work."
            if self._used_today + units > self.limits.daily_cap:
                return "Not enough live audits left today for a comparison. Try a single audit."
            if len(recent) + units > self.limits.per_visitor_per_hour:
                return "You have run the hourly maximum of live audits. Try again later."
            recent.extend([now] * units)
            self._used_today += units
            return None


def _iso(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")


class Health:
    """Whether the primary host answers, from live audits and a rate-limited probe.

    ``probe`` makes one small model call and raises ``ClientError`` if the host
    does not answer. It runs at most once per ``every`` seconds and never
    twice at once, so hitting the endpoint cannot run up a bill.
    """

    def __init__(
        self,
        probe: Callable[[], None] | None,
        every: float = 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._probe = probe
        self.every = every
        self._clock = clock
        self._lock = threading.Lock()
        self._last_probe = -math.inf
        self._ok: bool | None = None
        self.last_ok: float | None = None
        self.last_failure: float | None = None

    def note(self, ok: bool) -> None:
        """Record the latest outcome; the most recent one decides ``ok``."""
        with self._lock:
            self._ok = ok
            if ok:
                self.last_ok = self._clock()
            else:
                self.last_failure = self._clock()

    def check(self) -> dict[str, Any]:
        """Probe if due, then report. ``ok`` is ``None`` until anything is known."""
        with self._lock:
            # Claimed under the lock, so concurrent requests cannot both probe.
            due = self._probe is not None and self._clock() - self._last_probe >= self.every
            if due:
                self._last_probe = self._clock()
        if due:
            assert self._probe is not None
            try:
                self._probe()
                ok = True
            except ClientError as exc:
                _log({"event": "probe_failed", "error": str(exc)})
                ok = False
            self.note(ok)
        with self._lock:
            return {
                "ok": self._ok,
                "last_ok": _iso(self.last_ok),
                "last_failure": _iso(self.last_failure),
            }


class Spend:
    """Tokens and cost of every model call since the server started, by host."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_host: defaultdict[str, dict[str, float]] = defaultdict(
            lambda: {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
        )

    def add(self, event: str, completion: Completion) -> None:
        usage = completion.usage or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        cost = (tokens_in * PRICE[0] + tokens_out * PRICE[1]) / 1e6
        with self._lock:
            host = self._by_host[completion.provider]
            host["calls"] += 1
            host["tokens_in"] += tokens_in
            host["tokens_out"] += tokens_out
            host["cost_usd"] += cost
        _log(
            {
                "event": event,
                "provider": completion.provider,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": round(cost, 5),
            }
        )

    def totals(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {
                name: {**host, "cost_usd": round(host["cost_usd"], 4)}
                for name, host in self._by_host.items()
            }


def _log(record: dict[str, Any]) -> None:
    """One JSON line on stdout, which the host keeps as the server log."""
    print(json.dumps({"at": _iso(time.time()), **record}), flush=True)


class VerifyRequest(BaseModel):
    trace: dict[str, Any]
    slot: Literal["offense", "contradicted_by"]
    step: Any = None
    quote: Any = None


class AuditRequest(BaseModel):
    trace: dict[str, Any]
    # Also run the usual one-citation overseer on the same trace, for comparison.
    compare: bool = False


def parse_trace(raw: dict[str, Any]) -> Trace:
    """A trace from the request, or a 400 saying what is wrong with it.

    Accepts Pinpoint's own format, or an OpenAI-style agent log as
    ``{"messages": [...]}``.
    """
    try:
        if "messages" in raw and "steps" not in raw:
            raw = trace_from_openai_messages(raw["messages"])
        trace = Trace.from_dict(raw)
    except TraceError as exc:
        raise HTTPException(400, f"Not a valid trace: {exc}") from None
    if not trace.steps:
        raise HTTPException(400, "The trace has no steps.")
    if len(trace.steps) > MAX_STEPS:
        raise HTTPException(400, f"Traces are limited to {MAX_STEPS} steps here.")
    if len(trace.render()) > MAX_RENDERED_CHARS:
        raise HTTPException(400, f"Traces are limited to {MAX_RENDERED_CHARS:,} characters here.")
    return trace


def visitor_of(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    if first:
        return first
    return request.client.host if request.client else "unknown"


def _cost(result: OverseerResult) -> tuple[int, int, float]:
    tokens_in = tokens_out = 0
    for attempt in result.attempts:
        if attempt.completion is not None:
            usage = attempt.completion.usage or {}
            tokens_in += int(usage.get("prompt_tokens") or 0)
            tokens_out += int(usage.get("completion_tokens") or 0)
    return tokens_in, tokens_out, (tokens_in * PRICE[0] + tokens_out * PRICE[1]) / 1e6


def audit_response(trace: Trace, result: OverseerResult) -> dict[str, Any]:
    """What the page needs from one live audit. Never includes provider errors."""
    final = result.final
    audit = None if final is None or final.audit is None else final.audit.to_dict()
    check = None if final is None or final.verification is None else final.verification.to_dict()
    completion = None if final is None else final.completion
    tokens_in, tokens_out, cost = _cost(result)
    return {
        "status": result.status,
        "trace": trace_view(trace),
        "audit": audit_view(trace, audit, check, result.mode),
        "reasoning": None if completion is None else completion.reasoning,
        "attempts": len(result.attempts),
        "citation_retries": result.citation_retries,
        "verdict_history": [str(v) for v in result.verdict_history],
        "latency_s": round(
            sum(a.completion.latency_s for a in result.attempts if a.completion is not None), 1
        ),
        "tokens": {"in": tokens_in, "out": tokens_out},
        "cost_usd": round(cost, 5),
        "provider": None if completion is None else completion.provider,
        "model": None if completion is None else completion.model,
        "prompt_version": PROMPT_VERSION,
    }


def unreachable(result: OverseerResult) -> bool:
    """True if every attempt failed to reach the provider: worth trying another
    host. A model that answered badly is not, as another host would only
    repeat the cost."""
    return (
        result.status == "failed"
        and bool(result.attempts)
        and all(a.problem == "client_error" for a in result.attempts)
    )


def audit_with_fallback(
    primary: Overseer, backup: Overseer | None, trace: Trace
) -> tuple[OverseerResult, bool]:
    """Audit on the primary host; if it cannot be reached, on the backup."""
    result = primary.audit(trace)
    if backup is not None and unreachable(result):
        return backup.audit(trace), True
    return result, False


def baseline_of(overseer: Overseer | None) -> Overseer | None:
    """The usual one-citation overseer on the same client and budgets, for comparison."""
    if overseer is None:
        return None
    return Overseer(
        overseer.client,
        mode="single",
        max_citation_retries=overseer.max_citation_retries,
        max_format_retries=overseer.max_format_retries,
    )


def _model_of(overseer: Overseer | None) -> str:
    provider = getattr(overseer.client, "provider", None) if overseer is not None else None
    return provider.model if isinstance(provider, Provider) else PROVIDER.model


def _provider_of(overseer: Overseer) -> str:
    provider = getattr(overseer.client, "provider", None)
    return provider.name if isinstance(provider, Provider) else PROVIDER.name


def _charge(spend: Spend, event: str, result: OverseerResult) -> None:
    for attempt in result.attempts:
        if attempt.completion is not None:
            spend.add(event, attempt.completion)


# The probe: one short request in the shape audits use (a tool on offer).
_PROBE_MESSAGES = ({"role": "user", "content": "Call the ok tool."},)
_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "ok",
        "description": "Confirms the service is answering.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def probe_for(overseer: Overseer | None) -> Callable[[], Completion] | None:
    """One small call to ``overseer``'s host, or ``None`` if it has no real client."""
    client = overseer.client if overseer is not None else None
    if not isinstance(client, ModelClient):
        return None
    return lambda: client.complete(_PROBE_MESSAGES, [_PROBE_TOOL])


def create_app(
    overseer: Overseer | None,
    limits: Limits | None = None,
    clock: Callable[[], float] = time.time,
    *,
    fallback: Overseer | None = None,
    probe: Callable[[], Completion] | None = None,
    probe_every: float = 3600,
) -> FastAPI:
    """The app. ``overseer`` is ``None`` when no API key is configured: the
    gallery and verifier still work, and live audits say they are off.
    ``fallback`` audits on another host when ``overseer`` cannot be reached.
    ``probe`` is one small call to the primary host, for ``/api/health``."""
    limits = limits or Limits()
    budget = Budget(limits, clock)
    slots = threading.BoundedSemaphore(limits.max_concurrent)
    baseline, baseline_fallback = baseline_of(overseer), baseline_of(fallback)
    spend = Spend()

    def run_probe() -> None:
        assert probe is not None
        spend.add("probe", probe())

    health = Health(run_probe if probe is not None else None, probe_every, clock)
    app = FastAPI(title="Pinpoint", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def limit_body(request: Request, call_next: Any) -> Response:
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
            return JSONResponse({"detail": "Request too large."}, status_code=413)
        response: Response = await call_next(request)
        return response

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return {
            "live": overseer is not None,
            "remaining_today": budget.remaining_today() if overseer is not None else 0,
            "daily_cap": limits.daily_cap,
            "model": _model_of(overseer),
            "fallback": fallback is not None,
            "prompt_version": PROMPT_VERSION,
            "spend_since_restart": spend.totals(),
        }

    @app.get("/api/health")
    def health_check() -> JSONResponse:
        """200 while the primary host answers (or nothing is known yet), 503 if not."""
        if overseer is None:
            return JSONResponse({"ok": False, "provider": None}, status_code=503)
        report = {"provider": _provider_of(overseer), **health.check()}
        return JSONResponse(report, status_code=503 if report["ok"] is False else 200)

    @app.post("/api/verify")
    def verify_citation(body: VerifyRequest) -> dict[str, Any]:
        trace = parse_trace(body.trace)
        scope: Literal["step", "action"] = "action" if body.slot == "offense" else "step"
        result = verify(trace, Citation(_ANY_MISBEHAVIOR, body.step, body.quote), scope=scope)
        audit = {"verdict": _ANY_MISBEHAVIOR, body.slot: {"step": body.step, "quote": body.quote}}
        check = {"status": result.status, body.slot: result.to_dict()}
        view = audit_view(trace, audit, check, "two_slot") or {}
        return {
            "status": result.status,
            "issues": [issue.message for issue in result.issues],
            "found_in_steps": list(result.found_in_steps),
            "found_in_results": list(result.found_in_results),
            "span": (view.get(body.slot) or {}).get("span"),
        }

    @app.post("/api/audit")
    def live_audit(body: AuditRequest, request: Request) -> dict[str, Any]:
        trace = parse_trace(body.trace)
        if overseer is None:
            raise HTTPException(503, "Live audits are off on this server.")
        if not slots.acquire(blocking=False):
            raise HTTPException(503, "Busy with other audits. Try again in a minute.")
        try:
            refusal = budget.try_acquire(visitor_of(request), 2 if body.compare else 1)
            if refusal is not None:
                raise HTTPException(429, refusal)
            # The comparison runs alongside, so asking for it costs no extra time.
            with ThreadPoolExecutor(max_workers=2) as pool:
                ours = pool.submit(audit_with_fallback, overseer, fallback, trace)
                theirs = None
                if body.compare:
                    assert baseline is not None
                    theirs = pool.submit(audit_with_fallback, baseline, baseline_fallback, trace)
                result, fell_back = ours.result()
                compared = None if theirs is None else theirs.result()
        finally:
            slots.release()
        # The primary answered unless the audit fell back or never reached it.
        health.note(not fell_back and not unreachable(result))
        _charge(spend, "audit", result)
        if compared is not None:
            _charge(spend, "baseline", compared[0])
        response = audit_response(trace, result)
        response["fell_back"] = fell_back
        response["baseline"] = None
        if compared is not None:
            other, other_fell_back = compared
            response["baseline"] = {
                k: v for k, v in audit_response(trace, other).items() if k != "trace"
            }
            response["baseline"]["fell_back"] = other_fell_back
        response["remaining_today"] = budget.remaining_today()
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def overseer_for(provider: str) -> Overseer | None:
    """Pinpoint on Muse Spark Open at ``provider``, or ``None`` if its key is not set.

    Shorter timeout and fewer retries than the benchmark: a visitor is
    waiting, so a stuck call should fail in about three minutes at worst.
    """
    try:
        client = ModelClient(PROVIDERS[provider], timeout_s=90, max_retries=1)
    except ClientError:
        return None
    return Overseer(client, mode="two_slot")


def default_overseers() -> tuple[Overseer | None, Overseer | None]:
    """(primary, fallback): Baseten, then Fireworks, among those with a key."""
    available = [o for o in (overseer_for("baseten"), overseer_for("fireworks")) if o is not None]
    primary = available[0] if available else None
    fallback = available[1] if len(available) > 1 else None
    return primary, fallback


def _limits_from_env() -> Limits:
    return Limits(
        daily_cap=int(os.environ.get("PINPOINT_DAILY_CAP", "300")),
        per_visitor_per_hour=int(os.environ.get("PINPOINT_PER_VISITOR_PER_HOUR", "10")),
        max_concurrent=int(os.environ.get("PINPOINT_MAX_CONCURRENT", "4")),
    )


def main() -> None:  # pragma: no cover - entry point
    import uvicorn

    from pinpoint.client import load_env

    load_env()
    primary, fallback = default_overseers()
    app = create_app(primary, _limits_from_env(), fallback=fallback, probe=probe_for(primary))
    uvicorn.run(
        app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "7860"))
    )


if __name__ == "__main__":  # pragma: no cover
    main()
