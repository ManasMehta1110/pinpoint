"""The `pinpoint` command: audit an agent's log from a terminal or a CI job.

    pinpoint audit LOG.json [--json | --markdown] [--provider auto|baseten|fireworks|meta]
    pinpoint verify LOG.json --step N --quote TEXT [--slot offense|contradicted_by]

LOG.json may be a Pinpoint trace (``question`` and ``steps``), an agent log in
OpenAI chat format (a list of messages, or ``{"messages": [...]}``), or a
recorded run that holds such a log under ``messages``.

``audit`` asks Muse Spark Open for a verdict and citations and verifies every
citation against the log. Exit codes, for scripts and CI:

    0  no misbehavior found
    1  misbehavior found, with citations verified against the log
    2  the log could not be read
    3  no verified answer: no API key, the provider failed, or a citation did
       not check out (shown as unverified, never as a finding)

``verify`` checks one citation against the log without calling any model:
exit 0 if it is real, 1 if not.

API keys come from the environment, or from a ``.env`` file in the current
directory: ``BASETEN_API_KEY``, ``FIREWORKS_API_KEY`` or ``MODEL_API_KEY``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pinpoint.adapters import trace_from_openai_messages
from pinpoint.audit import MODES, Mode
from pinpoint.client import PROVIDERS, ClientError, ModelClient
from pinpoint.overseer import CompletionSource, Overseer, OverseerResult
from pinpoint.prompt import PROMPT_VERSION
from pinpoint.trace import Trace, TraceError
from pinpoint.verifier import Citation, Scope, verify

EXIT_CLEAN, EXIT_FOUND, EXIT_BAD_INPUT, EXIT_NO_ANSWER = 0, 1, 2, 3
AUTO_ORDER = ("baseten", "fireworks", "meta")

# The step-and-quote checks are the same for every misbehavior class; verify
# needs some misbehavior verdict to run them.
_ANY_MISBEHAVIOR = "DECEIVER"


class InputError(ValueError):
    """The log could not be read as a trace."""


def load_trace(path: Path) -> Trace:
    """Read a log file in any supported format as a trace."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror or exc}") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InputError(f"{path} is not valid JSON: {exc}") from None
    try:
        if isinstance(data, list):
            data = trace_from_openai_messages(data)
        elif isinstance(data, dict) and "messages" in data and "steps" not in data:
            data = trace_from_openai_messages(data["messages"])
        trace = Trace.from_dict(data)
    except TraceError as exc:
        raise InputError(f"{path} is not an agent log Pinpoint can read: {exc}") from None
    if not trace.steps:
        raise InputError(f"{path} has no steps to audit")
    return trace


def make_client(provider: str) -> CompletionSource:
    """A model client for ``provider``. Raises ClientError if its key is not set."""
    return ModelClient(PROVIDERS[provider], timeout_s=180, max_retries=2)


def _clients(
    provider: str, factory: Callable[[str], CompletionSource]
) -> list[tuple[str, CompletionSource]]:
    """The clients to try, in order: one named provider, or every one with a key."""
    names = AUTO_ORDER if provider == "auto" else (provider,)
    clients = []
    for name in names:
        try:
            clients.append((name, factory(name)))
        except ClientError:
            continue
    return clients


def run_audit(
    trace: Trace,
    mode: Mode,
    clients: Sequence[tuple[str, CompletionSource]],
) -> tuple[OverseerResult | None, str | None]:
    """Audit on the first provider that answers. Returns the result and its provider."""
    result: OverseerResult | None = None
    for name, client in clients:
        result = Overseer(client, mode=mode).audit(trace)
        unreachable = result.status == "failed" and all(
            a.problem == "client_error" for a in result.attempts
        )
        if not unreachable:
            return result, name
    return result, None


def exit_code(result: OverseerResult | None) -> int:
    if result is None or result.status in ("failed", "unverified"):
        return EXIT_NO_ANSWER
    return EXIT_FOUND if result.status == "verified" else EXIT_CLEAN


# --- reports ---------------------------------------------------------------------------


def report(trace: Trace, result: OverseerResult | None, provider: str | None) -> dict[str, Any]:
    """Everything worth showing about one audit, as plain data."""
    if result is None or result.audit is None:
        return {"status": "failed" if result is None else result.status, "verdict": None,
                "citations": [], "provider": provider}  # fmt: skip
    audit = result.audit
    check = result.verification
    citations = []
    for slot in ("offense", "contradicted_by"):
        cited = getattr(audit, slot)
        if cited is None:
            continue
        slot_check = getattr(check, slot) if check is not None else None
        step = trace.get_step(cited.step) if isinstance(cited.step, int) else None
        citations.append({
            "slot": slot if result.mode != "single" else "evidence",
            "step": cited.step,
            "tool": step.tool if step is not None else None,
            "quote": cited.quote,
            "verified": bool(slot_check is not None and slot_check.verified),
            "issues": [i.message for i in slot_check.issues] if slot_check is not None else [],
        })  # fmt: skip
    usage = [a.completion.usage for a in result.attempts if a.completion is not None]
    return {
        "status": result.status,
        "verdict": audit.verdict,
        "confidence": audit.confidence,
        "rationale": audit.rationale,
        "citations": citations,
        "provider": provider,
        "model": PROVIDERS[provider].model if provider in PROVIDERS else None,
        "mode": result.mode,
        "prompt_version": PROMPT_VERSION,
        "model_calls": len([a for a in result.attempts if a.completion is not None]),
        "tokens": {
            "in": sum(int(u.get("prompt_tokens") or 0) for u in usage),
            "out": sum(int(u.get("completion_tokens") or 0) for u in usage),
        },
    }


_HEADLINES = {
    "no_finding": "No misbehavior found",
    "verified": "Misbehavior found, citations verified",
    "unverified": "Inconclusive: a citation did not check out against the log",
    "failed": "Inconclusive: no readable audit (no API key, or the provider failed)",
}


def render_text(rep: dict[str, Any]) -> str:
    lines = [f"Pinpoint: {_HEADLINES.get(rep['status'], rep['status'])}"]
    if rep.get("verdict"):
        lines[0] += f"  [{rep['verdict']}]"
    for c in rep["citations"]:
        name = c["slot"].replace("_", " ")
        mark = "verified" if c["verified"] else "UNVERIFIED"
        lines.append(f"  {name:<16} step {c['step']} ({c['tool']}), {mark}")
        lines.append(f'    "{c["quote"]}"')
        lines.extend(f"    ! {issue}" for issue in c["issues"])
    if rep.get("rationale"):
        lines.append(f"  why: {rep['rationale']}")
    if rep.get("model"):
        tokens = rep["tokens"]
        lines.append(
            f"  {rep['model']} via {rep['provider']}, {rep['model_calls']} model call(s), "
            f"{tokens['in']:,} tokens in, {tokens['out']:,} out, prompt v{rep['prompt_version']}"
        )
    return "\n".join(lines)


def _md(text: object) -> str:
    """Inline code on one line; a double-backtick fence if the text holds a backtick."""
    s = str(text).replace("\n", " ")
    return f"`` {s} ``" if "`" in s else f"`{s}`"


def render_markdown(rep: dict[str, Any]) -> str:
    lines = [f"### Pinpoint: {_HEADLINES.get(rep['status'], rep['status'])}"]
    if rep.get("verdict"):
        lines.append(f"Verdict: **{rep['verdict']}**")
    if rep["citations"]:
        lines += ["", "| citation | step | tool | quote | checked |", "|---|---|---|---|---|"]
        for c in rep["citations"]:
            quote = _md(c["quote"]).replace("|", "\\|")
            mark = "verified" if c["verified"] else "**unverified**"
            lines.append(f"| {c['slot']} | {c['step']} | {_md(c['tool'])} | {quote} | {mark} |")
    if rep.get("rationale"):
        lines += ["", f"> {rep['rationale']}"]
    if rep.get("model"):
        lines += [
            "",
            f"<sub>{rep['model']} via {rep['provider']}, prompt v{rep['prompt_version']}. "
            "Every citation is checked against the log before it is shown.</sub>",
        ]
    return "\n".join(lines) + "\n"


# --- commands ------------------------------------------------------------------------------


def _load_dotenv() -> None:
    from dotenv import load_dotenv

    load_dotenv(Path.cwd() / ".env", override=False)


def cmd_audit(args: argparse.Namespace, factory: Callable[[str], CompletionSource]) -> int:
    trace = load_trace(args.log)
    clients = _clients(args.provider, factory)
    if not clients:
        keys = ", ".join(PROVIDERS[p].api_key_env for p in AUTO_ORDER)
        print(f"error: no API key found; set one of {keys}", file=sys.stderr)
        return EXIT_NO_ANSWER
    result, provider = run_audit(trace, args.mode, clients)
    rep = report(trace, result, provider)
    if args.format == "json":
        print(json.dumps(rep, indent=2, ensure_ascii=False))
    elif args.format == "markdown":
        print(render_markdown(rep), end="")
    else:
        print(render_text(rep))
    return exit_code(result)


def cmd_verify(args: argparse.Namespace) -> int:
    trace = load_trace(args.log)
    scope: Scope = "action" if args.slot == "offense" else "step"
    result = verify(trace, Citation(_ANY_MISBEHAVIOR, args.step, args.quote), scope=scope)
    if result.verified:
        print(
            f"real: step {args.step} contains that quote"
            + (" in the agent's own action" if scope == "action" else "")
        )
        return 0
    print("not verified:")
    for issue in result.issues:
        print(f"  - {issue.message}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pinpoint",
        description="Audit an AI agent's log: a verdict, plus the exact steps that prove it, "
        "checked against the log.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="audit a log with Muse Spark Open")
    audit.add_argument("log", type=Path)
    audit.add_argument("--provider", default="auto", choices=["auto", *AUTO_ORDER])
    audit.add_argument("--mode", default="two_slot", choices=MODES)
    fmt = audit.add_mutually_exclusive_group()
    fmt.add_argument("--json", dest="format", action="store_const", const="json")
    fmt.add_argument("--markdown", dest="format", action="store_const", const="markdown")

    check = sub.add_parser("verify", help="check one citation against a log (no model call)")
    check.add_argument("log", type=Path)
    check.add_argument("--step", type=int, required=True)
    check.add_argument("--quote", required=True)
    check.add_argument("--slot", default="offense", choices=["offense", "contradicted_by"])
    return parser


def main(
    argv: Sequence[str] | None = None, factory: Callable[[str], CompletionSource] | None = None
) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify":
            return cmd_verify(args)
        _load_dotenv()
        return cmd_audit(args, factory or make_client)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
