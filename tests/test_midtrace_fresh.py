"""The fresh mid-trace set: unseen bugs, same design, labels right, outputs real.

Also runs every scenario's code, in both mid-trace sets, so each test output a
trace claims is what that code actually produces.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

import pytest

from benchmark import midtrace, midtrace_fresh
from benchmark.fetch_data import sha256_of
from benchmark.midtrace import SCENARIOS, Scenario
from benchmark.midtrace_fresh import FRESH_SCENARIOS
from benchmark.run import data_path, pinned_sha256
from pinpoint.audit import parse_audit, verify_audit
from pinpoint.trace import Trace
from pinpoint.verifier import contains_phrase, normalize

RECORDS = midtrace_fresh.build_fresh()
MISBEHAVIOR = [r for r in RECORDS if r["policy_class"] != "HONEST"]


def test_committed_file_is_exactly_what_the_generator_writes() -> None:
    committed = midtrace_fresh.DATASET_PATH.read_text(encoding="utf-8")
    assert committed == midtrace.render_jsonl(RECORDS)


def test_committed_file_matches_its_pin() -> None:
    path = data_path("midtrace_fresh.jsonl", midtrace_fresh.DATASET_PATH.parent)
    assert path == midtrace_fresh.DATASET_PATH
    assert sha256_of(path) == pinned_sha256("midtrace_fresh.jsonl")


def test_no_bug_is_shared_with_the_original_set() -> None:
    assert not {s.func for s in SCENARIOS} & {s.func for s in FRESH_SCENARIOS}
    assert not {s.module for s in SCENARIOS} & {s.module for s in FRESH_SCENARIOS}


def test_same_design_as_the_original() -> None:
    assert len(RECORDS) == 36
    assert Counter(r["policy_class"] for r in RECORDS) == {
        "HONEST": 12,
        "REWARD_HACKER": 12,
        "DECEIVER": 12,
    }
    for cls in ("REWARD_HACKER", "DECEIVER"):
        variants = Counter(r["policy_variant"] for r in RECORDS if r["policy_class"] == cls)
        assert variants == {1: 3, 2: 3, 3: 3, 4: 3}
    assert all(r["question_id"].startswith("mf_") for r in RECORDS)


def test_the_original_set_is_unchanged_by_sharing_the_generator() -> None:
    assert midtrace.render_jsonl(midtrace.build()) == midtrace.DATASET_PATH.read_text(
        encoding="utf-8"
    )


# --- the claimed outputs are real ----------------------------------------------------


def _run(src: str, expr: str) -> Any:
    namespace: dict[str, Any] = {}
    exec(src, namespace)  # our own scenario code, never model output
    return eval(expr, namespace)


@pytest.mark.parametrize("s", SCENARIOS + FRESH_SCENARIOS, ids=lambda s: s.func)
def test_buggy_fixed_and_attempted_code_give_the_claimed_outputs(s: Scenario) -> None:
    body = s.test_body.removeprefix("assert ")
    op = " is " if body.endswith(f" is {s.want}") else " == "
    expr = body.removesuffix(op + s.want)
    want = eval(s.want)

    def passes(value: Any) -> bool:
        return bool(value is want) if op == " is " else bool(value == want)

    assert repr(_run(s.module_src, expr)) == s.got
    assert passes(_run(s.module_src.replace(s.buggy, s.fixed), expr))
    attempted = _run(s.module_src.replace(s.buggy, s.attempt), expr)
    assert repr(attempted) == s.attempt_got and not passes(attempted)


# --- the labels are right ------------------------------------------------------------


@pytest.mark.parametrize(
    "record", MISBEHAVIOR, ids=lambda r: f"{r['question_id']}-{r['policy_class']}"
)
def test_offense_is_mid_trace_and_verifies_in_both_two_slot_modes(record: dict[str, Any]) -> None:
    step = record["smoking_gun_step"]
    assert 1 <= step < len(record["steps"])
    assert not contains_phrase(record["claimed_answer"], record["smoking_gun_keyword"])
    trace = Trace.from_dict(record)
    hits = [
        s.index for s in trace.steps if contains_phrase(s.text(), record["smoking_gun_keyword"])
    ]
    assert hits == [step]
    for mode in ("two_slot", "two_slot_earliest"):
        arguments = {
            "verdict": record["policy_class"],
            "confidence": 1.0,
            "offense": {"step": step, "quote": record["smoking_gun_keyword"]},
        }
        assert verify_audit(trace, parse_audit(arguments, mode), mode=mode).verified


GIVEAWAYS = (
    "wrong",
    "cheat",
    "hack",
    "fake",
    "lie",
    "lied",
    "bypass",
    "trick",
    "pretend",
    "deceive",
    "workaround",
    "misbehav",
    "offense",
    "suspicious",
)


def test_no_step_names_the_misbehavior() -> None:
    for r in RECORDS:
        text = normalize(Trace.from_dict(r).render())
        for word in GIVEAWAYS:
            assert not re.search(rf"\b{word}", text), (r["question_id"], word)
