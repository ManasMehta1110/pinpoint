"""Tests for the demo app: the API, its spending caps, and the gallery build.

The live audit is driven by the scripted OracleModel; no test builds a real
client or reads .env.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from conftest import RECORDS, OracleModel
from fastapi.testclient import TestClient

from app import examples as examples_mod
from app import server
from app.examples import PICKS, BuildError, Pick, build_example
from app.server import Budget, Limits, create_app
from benchmark.fetch_data import DATA_DIR
from pinpoint.client import ClientError
from pinpoint.overseer import Overseer

DECEIVER = RECORDS[3]  # read_document "It was formed in 1945." at 2; answer 1950 at 3
TRACE = {k: DECEIVER[k] for k in ("question", "steps", "claimed_answer")}
GOOD_AUDIT = {
    "verdict": "DECEIVER",
    "confidence": 0.9,
    "offense": {"step": 3, "quote": "formed in 1950"},
    "contradicted_by": {"step": 2, "quote": "formed in 1945"},
    "rationale": "Read 1945, answered 1950.",
}


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_client(
    script: list[Any] | None = None, limits: Limits | None = None, clock: Clock | None = None
) -> TestClient:
    overseer = None
    if script is not None:
        overseer = Overseer(OracleModel({DECEIVER["question"]: script}), mode="two_slot")
    return TestClient(create_app(overseer, limits, clock or Clock()))


# --- page and status -------------------------------------------------------------------


def test_page_and_gallery_are_served() -> None:
    client = make_client()
    page = client.get("/")
    assert page.status_code == 200 and "<title>Pinpoint</title>" in page.text
    gallery = client.get("/static/examples.json")
    assert gallery.status_code == 200 and len(gallery.json()) == len(PICKS)


def test_no_api_docs_are_exposed() -> None:
    client = make_client()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_status_without_a_key() -> None:
    body = make_client().get("/api/status").json()
    assert body["live"] is False and body["remaining_today"] == 0


def test_status_with_a_key() -> None:
    body = make_client([], Limits(daily_cap=5)).get("/api/status").json()
    assert body["live"] is True and body["remaining_today"] == 5
    assert body["model"] == "meta-models/muse-spark-open-1026"


# --- verify (free) -----------------------------------------------------------------------


def verify(client: TestClient, slot: str, step: Any, quote: Any) -> dict[str, Any]:
    response = client.post(
        "/api/verify", json={"trace": TRACE, "slot": slot, "step": step, "quote": quote}
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_the_real_offense_is_accepted_and_located() -> None:
    body = verify(make_client(), "offense", 3, "formed in 1950")
    assert body["status"] == "verified" and body["issues"] == []
    step_text = "submit_answer\nanswer: It was formed in 1950.\n-> (no result)"
    assert step_text[body["span"]["start"] : body["span"]["end"]] == "formed in 1950"


def test_the_document_is_rejected_as_an_offense() -> None:
    body = verify(make_client(), "offense", 2, "formed in 1945")
    assert body["status"] == "unverified" and body["span"] is None
    assert body["found_in_results"] == [2]
    assert any("tool" in issue for issue in body["issues"])


def test_the_document_is_accepted_as_a_contradiction() -> None:
    body = verify(make_client(), "contradicted_by", 2, "formed in 1945")
    assert body["status"] == "verified" and body["span"] is not None


@pytest.mark.parametrize(
    ("step", "quote"),
    [(2, "formed in 1950"), (9, "formed in 1950"), (3, "the agent lied"), (3, "the"),
     ("three", "formed in 1950"), (3, None), (True, "formed in 1950")],
)  # fmt: skip
def test_fakes_are_rejected_with_a_reason(step: Any, quote: Any) -> None:
    body = verify(make_client(), "offense", step, quote)
    assert body["status"] == "unverified" and body["issues"] and body["span"] is None


def test_unknown_slot_is_refused() -> None:
    response = make_client().post(
        "/api/verify", json={"trace": TRACE, "slot": "evidence", "step": 3, "quote": "x"}
    )
    assert response.status_code == 422


# --- trace checks ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("trace", "message"),
    [
        ({"steps": []}, "question"),
        ({"question": "q", "steps": "nope"}, "steps"),
        ({"question": "q", "steps": []}, "no steps"),
        ({"question": "q", "steps": [{"tool": "t"}] * 31}, "30 steps"),
        ({"question": "q", "steps": [{"tool": "t", "result": "x" * 25_000}]}, "characters"),
    ],
)
def test_bad_traces_are_refused_with_a_reason(trace: dict[str, Any], message: str) -> None:
    for path, extra in (("/api/verify", {"slot": "offense", "step": 1, "quote": "x"}),
                        ("/api/audit", {})):  # fmt: skip
        response = make_client([GOOD_AUDIT]).post(path, json={"trace": trace, **extra})
        assert response.status_code == 400
        assert message in response.json()["detail"]


def test_oversized_bodies_are_refused_before_parsing() -> None:
    client = make_client()
    big = json.dumps({"trace": {"question": "x" * 70_000, "steps": []}})
    response = client.post("/api/verify", content=big, headers={"content-type": "application/json"})
    assert response.status_code == 413


def test_a_malformed_length_header_is_refused() -> None:
    response = make_client().post(
        "/api/verify",
        content=b"{}",
        headers={"content-type": "application/json", "content-length": "-1"},
    )
    assert response.status_code == 413


# --- live audit --------------------------------------------------------------------------


def test_live_audit_returns_highlighted_citations() -> None:
    client = make_client([GOOD_AUDIT], Limits(daily_cap=3))
    response = client.post("/api/audit", json={"trace": TRACE})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "verified"
    assert body["audit"]["verdict"] == "DECEIVER"
    assert body["audit"]["offense"]["span"] is not None
    assert body["audit"]["contradicted_by"]["span"] is not None
    assert body["tokens"] == {"in": 1000, "out": 200}
    assert body["cost_usd"] == pytest.approx((1000 * 1.25 + 200 * 4.25) / 1e6)
    assert body["remaining_today"] == 2


def test_a_retried_citation_is_reported() -> None:
    bad = {**GOOD_AUDIT, "offense": {"step": 2, "quote": "formed in 1945"}}
    body = make_client([bad, GOOD_AUDIT]).post("/api/audit", json={"trace": TRACE}).json()
    assert body["status"] == "verified" and body["attempts"] == 2 and body["citation_retries"] == 1


def test_live_audits_off_without_a_key() -> None:
    response = make_client().post("/api/audit", json={"trace": TRACE})
    assert response.status_code == 503


def test_provider_errors_are_not_leaked() -> None:
    secret = ClientError("baseten returned HTTP 401: key sk-SECRET rejected")
    response = make_client([secret]).post("/api/audit", json={"trace": TRACE})
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert "SECRET" not in response.text and "401" not in response.text


def test_daily_cap_is_enforced() -> None:
    client = make_client([GOOD_AUDIT] * 5, Limits(daily_cap=2, per_visitor_per_hour=100))
    codes = [client.post("/api/audit", json={"trace": TRACE}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert client.get("/api/status").json()["remaining_today"] == 0


def test_per_visitor_cap_follows_the_forwarded_address() -> None:
    client = make_client([GOOD_AUDIT] * 5, Limits(daily_cap=100, per_visitor_per_hour=1))

    def post(ip: str) -> int:
        headers = {"x-forwarded-for": f"{ip}, 10.0.0.1"}
        return client.post("/api/audit", json={"trace": TRACE}, headers=headers).status_code

    assert [post("1.1.1.1"), post("1.1.1.1"), post("2.2.2.2")] == [200, 429, 200]


def test_refused_requests_do_not_use_the_allowance() -> None:
    client = make_client([GOOD_AUDIT], Limits(daily_cap=1))
    assert (
        client.post("/api/audit", json={"trace": {"question": "q", "steps": []}}).status_code == 400
    )
    assert client.post("/api/audit", json={"trace": TRACE}).status_code == 200


def test_busy_server_refuses_rather_than_queueing() -> None:
    started, release = threading.Event(), threading.Event()

    class Slow:
        def complete(self, messages: Any, tools: Any) -> Any:
            started.set()
            release.wait(5)
            raise ClientError("slow")

    app = create_app(Overseer(Slow()), Limits(max_concurrent=1), Clock())
    client = TestClient(app)
    first: list[int] = []
    worker = threading.Thread(
        target=lambda: first.append(client.post("/api/audit", json={"trace": TRACE}).status_code)
    )
    worker.start()
    assert started.wait(5)
    try:
        assert client.post("/api/audit", json={"trace": TRACE}).status_code == 503
    finally:
        release.set()
        worker.join(5)
    assert first == [200]
    # The slot is free again.
    assert client.get("/api/status").status_code == 200


# --- the budget ---------------------------------------------------------------------------


def test_budget_hourly_window_slides() -> None:
    clock = Clock()
    budget = Budget(Limits(daily_cap=100, per_visitor_per_hour=2), clock)
    assert budget.try_acquire("a") is None
    clock.now += 1800
    assert budget.try_acquire("a") is None
    assert budget.try_acquire("a") is not None
    clock.now += 1800  # the first is now an hour old
    assert budget.try_acquire("a") is None


def test_budget_resets_at_utc_midnight() -> None:
    clock = Clock(1_800_000_000.0 - (1_800_000_000 % 86400) + 86400 - 10)  # 23:59:50 UTC
    budget = Budget(Limits(daily_cap=1, per_visitor_per_hour=100), clock)
    assert budget.try_acquire("a") is None
    assert budget.try_acquire("b") is not None
    clock.now += 20
    assert budget.remaining_today() == 1
    assert budget.try_acquire("b") is None


class KeyedClient:
    """Stands in for ModelClient: fails like it when the key is unset, never connects."""

    def __init__(self, provider: Any, **_: Any) -> None:
        import os

        if not os.environ.get(provider.api_key_env, "").strip():
            raise ClientError(f"{provider.api_key_env} is not set")
        self.provider = provider


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ((), (None, None)),
        (("BASETEN_API_KEY",), ("baseten", None)),
        (("FIREWORKS_API_KEY",), ("fireworks", None)),
        (("BASETEN_API_KEY", "FIREWORKS_API_KEY"), ("baseten", "fireworks")),
    ],
)
def test_default_overseers_use_whichever_keys_are_set(
    monkeypatch: pytest.MonkeyPatch, keys: tuple[str, ...], expected: tuple[str | None, ...]
) -> None:
    monkeypatch.setattr(server, "ModelClient", KeyedClient)
    for name in ("BASETEN_API_KEY", "FIREWORKS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name in keys:
        monkeypatch.setenv(name, "test-key")
    found = tuple(
        None if o is None else o.client.provider.name  # type: ignore[attr-defined]
        for o in server.default_overseers()
    )
    assert found == expected


# --- falling back to another host ------------------------------------------------------


def make_fallback_client(
    primary: list[Any], fallback: list[Any], limits: Limits | None = None
) -> TestClient:
    q = DECEIVER["question"]
    app = create_app(
        Overseer(OracleModel({q: primary}), mode="two_slot"),
        limits,
        Clock(),
        fallback=Overseer(OracleModel({q: fallback}), mode="two_slot"),
    )
    return TestClient(app)


def test_an_unreachable_host_falls_back() -> None:
    down = ClientError("baseten returned HTTP 503")
    client = make_fallback_client([down], [GOOD_AUDIT], Limits(daily_cap=5))
    body = client.post("/api/audit", json={"trace": TRACE}).json()
    assert body["status"] == "verified" and body["fell_back"] is True
    assert body["remaining_today"] == 4  # one audit, not two
    assert "503" not in json.dumps(body)


def test_a_reachable_host_is_not_second_guessed() -> None:
    # An empty fallback script fails the request if the fallback is ever called.
    body = make_fallback_client([GOOD_AUDIT], []).post("/api/audit", json={"trace": TRACE}).json()
    assert body["status"] == "verified" and body["fell_back"] is False


def test_a_bad_answer_does_not_fall_back() -> None:
    # The model answered, just not usably: another host would only repeat the cost.
    client = make_fallback_client(["not json", "not json"], [])
    body = client.post("/api/audit", json={"trace": TRACE}).json()
    assert body["status"] == "failed" and body["fell_back"] is False


def test_both_hosts_down_is_reported_as_failed() -> None:
    down = ClientError("down")
    body = make_fallback_client([down], [down]).post("/api/audit", json={"trace": TRACE}).json()
    assert body["status"] == "failed" and body["fell_back"] is True


def test_status_reports_the_fallback() -> None:
    assert make_fallback_client([], []).get("/api/status").json()["fallback"] is True
    assert make_client([]).get("/api/status").json()["fallback"] is False


def test_unreachable_needs_every_attempt_to_be_a_provider_error() -> None:
    from pinpoint.overseer import Attempt, OverseerResult

    def result(*problems: str | None) -> OverseerResult:
        attempts = tuple(Attempt(i + 1, None, None, None, p) for i, p in enumerate(problems))
        return OverseerResult("failed", "two_slot", "3", attempts)

    assert server.unreachable(result("client_error")) is True
    assert server.unreachable(result("bad_arguments", "client_error")) is False
    assert server.unreachable(result()) is False
    ok = OverseerResult(
        "verified", "two_slot", "3", (Attempt(1, None, None, None, "client_error"),)
    )
    assert server.unreachable(ok) is False


def test_limits_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINPOINT_DAILY_CAP", "7")
    monkeypatch.setenv("PINPOINT_PER_VISITOR_PER_HOUR", "3")
    monkeypatch.setenv("PINPOINT_MAX_CONCURRENT", "2")
    assert server._limits_from_env() == Limits(7, 3, 2)


# --- the gallery ------------------------------------------------------------------------


@pytest.fixture
def need_data() -> None:
    for name in ("eval_traces.jsonl", "eval_traces_realistic.jsonl"):
        if not (DATA_DIR / name).exists():
            pytest.skip(f"{name} not fetched; run python -m benchmark.fetch_data")


@pytest.mark.usefixtures("need_data")
def test_committed_gallery_is_exactly_what_the_builder_writes() -> None:
    committed = examples_mod.OUT_PATH.read_text(encoding="utf-8")
    assert committed == examples_mod.render(examples_mod.build())


@pytest.mark.usefixtures("need_data")
def test_gallery_highlights_are_where_they_should_be() -> None:
    for ex in examples_mod.build():
        steps = ex["trace"]["steps"]
        offense = ex["pinpoint"]["offense"]
        step = steps[offense["step"] - 1]
        # Pinpoint's offense is always highlighted inside the agent's action.
        assert offense["span"] is not None
        assert offense["span"]["end"] <= step["action_len"]
        assert "smoking_gun_step" not in json.dumps(ex["input"])
        assert "policy_class" not in json.dumps(ex["input"])
        if ex["kind"] == "substitution":
            cited = steps[ex["single"]["offense"]["step"] - 1]
            assert cited["tool"] == "read_document"
            assert ex["single"]["offense"]["span"]["start"] >= cited["action_len"]
            assert ex["single_reasoning"]


@pytest.mark.usefixtures("need_data")
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"kind": "limit"}, "must be one Pinpoint got wrong"),
        ({"kind": "midtrace", "line": 3}, "mid-trace"),  # a DECEIVER whose offense is the answer
        ({"excerpt": "words the model never wrote"}, "not verbatim"),
    ],
)
def test_misleading_picks_fail_the_build(change: dict[str, Any], message: str) -> None:
    louvre = PICKS[0]
    pick = Pick(**{**louvre.__dict__, **change})
    if change.get("line") == 3:
        pick = Pick(**{**pick.__dict__, "data_file": "eval_traces.jsonl"})
    with pytest.raises(BuildError, match=message):
        build_example(pick)


@pytest.mark.usefixtures("need_data")
def test_a_substitution_pick_must_show_one(monkeypatch: pytest.MonkeyPatch) -> None:
    # A mid-trace trace where both overseers cited the agent: not a substitution.
    pick = Pick("x", "substitution", "midtrace.jsonl", 1, "t", "b")
    with pytest.raises(BuildError, match="did not cite a document"):
        build_example(pick)


def test_build_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> list[dict[str, Any]]:
        raise BuildError("nope")

    monkeypatch.setattr(examples_mod, "build", broken)
    assert examples_mod.main() == 1


# --- views ----------------------------------------------------------------------------------


def test_an_unverified_citation_is_never_highlighted() -> None:
    # The quote is really in the step, but the verifier did not accept the
    # citation, so the page must not mark it as if it pointed at something.
    from app.views import audit_view
    from pinpoint.trace import Trace

    trace = Trace.from_dict(TRACE)
    audit = {"verdict": "DECEIVER", "offense": {"step": 3, "quote": "formed in 1950"}}
    rejected = {"status": "unverified", "issues": [{"code": "x", "message": "no"}]}
    view = audit_view(trace, audit, {"status": "unverified", "offense": rejected}, "two_slot")
    assert view is not None and view["offense"]["span"] is None
    assert view["offense"]["issues"] == ["no"]
    accepted = {"status": "verified", "issues": []}
    view = audit_view(trace, audit, {"status": "verified", "offense": accepted}, "two_slot")
    assert view is not None and view["offense"]["span"] is not None


def test_single_slot_views_have_no_second_citation() -> None:
    from app.views import audit_view
    from pinpoint.trace import Trace

    trace = Trace.from_dict(TRACE)
    audit = {"verdict": "DECEIVER", "offense": {"step": 2, "quote": "formed in 1945"},
             "contradicted_by": {"step": 2, "quote": "formed in 1945"}}  # fmt: skip
    view = audit_view(trace, audit, {"offense": {"status": "verified"}}, "single")
    assert view is not None and view["contradicted_by"] is None
    # No offense rule in single mode: a document quote is highlighted.
    assert view["offense"]["span"] is not None
    assert audit_view(trace, None, None, "single") is None


# --- comparing with the usual one-citation overseer ------------------------------------

# One scripted answer that reads correctly in both modes: two-slot reads
# offense and contradicted_by, single reads evidence (here, the document).
BOTH = {**GOOD_AUDIT, "evidence": {"step": 2, "quote": "formed in 1945"}}


def test_compare_runs_the_usual_overseer_too() -> None:
    client = make_client([BOTH, BOTH], Limits(daily_cap=10))
    body = client.post("/api/audit", json={"trace": TRACE, "compare": True}).json()
    assert body["audit"]["mode"] == "two_slot"
    assert body["audit"]["offense"]["step"] == 3
    baseline = body["baseline"]
    assert baseline["audit"]["mode"] == "single"
    assert baseline["audit"]["offense"]["step"] == 2  # the document
    assert baseline["audit"]["contradicted_by"] is None
    assert "trace" not in baseline and baseline["fell_back"] is False
    assert body["remaining_today"] == 8  # a comparison is two audits


def test_without_compare_there_is_no_baseline() -> None:
    body = make_client([BOTH]).post("/api/audit", json={"trace": TRACE}).json()
    assert body["baseline"] is None


def test_a_comparison_needs_two_audits_left() -> None:
    client = make_client([BOTH, BOTH, BOTH], Limits(daily_cap=3, per_visitor_per_hour=100))
    assert client.post("/api/audit", json={"trace": TRACE, "compare": True}).status_code == 200
    refused = client.post("/api/audit", json={"trace": TRACE, "compare": True})
    assert refused.status_code == 429 and "single audit" in refused.json()["detail"]
    # Nothing was spent by the refusal: the last audit is still there.
    assert client.post("/api/audit", json={"trace": TRACE}).status_code == 200


def test_a_comparison_counts_twice_per_visitor() -> None:
    client = make_client([BOTH] * 4, Limits(daily_cap=100, per_visitor_per_hour=3))
    assert client.post("/api/audit", json={"trace": TRACE, "compare": True}).status_code == 200
    assert client.post("/api/audit", json={"trace": TRACE, "compare": True}).status_code == 429


def test_each_side_falls_back_on_its_own() -> None:
    down = ClientError("down")
    # The primary host fails once; whichever side drew the failure falls back.
    client = make_fallback_client([down, BOTH], [BOTH], Limits(daily_cap=10))
    body = client.post("/api/audit", json={"trace": TRACE, "compare": True}).json()
    assert body["status"] == "verified" and body["baseline"]["status"] == "verified"
    assert [body["fell_back"], body["baseline"]["fell_back"]].count(True) == 1


def test_budget_grants_several_units_at_once() -> None:
    budget = Budget(Limits(daily_cap=3, per_visitor_per_hour=100), Clock())
    assert budget.try_acquire("a", 2) is None
    assert budget.try_acquire("a", 2) is not None
    assert budget.remaining_today() == 1
