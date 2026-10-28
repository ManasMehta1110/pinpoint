"""Tests for benchmark.whowhen: the data, its conversion, the runner and the grader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from benchmark import whowhen
from benchmark.whowhen import ENVIRONMENT, WhoWhenError, grade_one, to_record, trace_of
from pinpoint.attribution import Attributor
from pinpoint.client import Completion, ToolCall

RAW = {
    "question": "How many even addresses?",
    "ground_truth": "8",
    "mistake_agent": "Excel_Expert",
    "mistake_step": "0",
    "mistake_reason": "Counted odd.",
    "history": [
        {"name": "Excel_Expert", "role": "assistant", "content": "Count the odd rows."},
        {"name": "Computer_terminal", "role": "user", "content": "Code output: 4"},
        {"name": "Verification_Expert", "role": "user", "content": "The answer is 4."},
    ],
}


def record(**changes: Any) -> dict[str, Any]:
    return {**to_record(RAW, "Algorithm-Generated", 1), **changes}


# --- conversion ---------------------------------------------------------------------------


def test_messages_become_steps_and_the_environment_becomes_results() -> None:
    rec = record()
    assert [s["tool"] for s in rec["steps"]] == [
        "Excel_Expert",
        "Computer_terminal",
        "Verification_Expert",
    ]
    assert rec["steps"][0] == {
        "tool": "Excel_Expert",
        "args": {"message": "Count the odd rows."},
        "result": None,
    }
    assert rec["steps"][1] == {"tool": "Computer_terminal", "args": {}, "result": "Code output: 4"}
    assert rec["mistake_step"] == 0 and rec["id"] == "Algorithm-Generated/1"


def test_hand_crafted_speakers_come_from_role() -> None:
    raw = {
        **RAW,
        "history": [
            {"role": "human", "content": "Find it."},
            {"role": "WebSurfer", "content": "I clicked."},
        ],
    }
    rec = to_record(raw, "Hand-Crafted", 3)
    assert [s["tool"] for s in rec["steps"]] == ["human", "WebSurfer"]
    assert rec["steps"][0]["result"] == "Find it."


def test_labels_never_reach_the_trace() -> None:
    rendered = trace_of(record()).render()
    for leak in ("Counted odd", "mistake", "ground_truth"):
        assert leak not in rendered
    assert "8" not in rendered.split("Question:")[1].split("\n")[0]


def test_the_environment_list_is_what_the_data_uses() -> None:
    assert {"Computer_terminal", "ComputerTerminal", "human"} == ENVIRONMENT


# --- grading -------------------------------------------------------------------------------


def result(step: Any, status: str = "verified") -> dict[str, Any]:
    attempt = {
        "attribution": {"mistake": {"step": step, "quote": "q"}, "evidence": None, "reason": None}
    }
    return {"status": status, "attempts": [attempt], "citation_retries": 0}


def test_an_exact_hit() -> None:
    g = grade_one(record(), result(1))
    assert g["exact"] and g["exact_verified"] and g["lenient"] and g["agent"]
    assert (g["label_step"], g["predicted_step"]) == (1, 1)


def test_the_papers_lenient_rule_counts_substrings() -> None:
    # True step 1 (0-based); predicting 0-based step 10 is a hit under the
    # paper's substring rule, and a miss by exact match.
    rec = record(mistake_step=1, steps=record()["steps"] * 4)
    g = grade_one(rec, result(11))
    assert g["lenient"] and not g["exact"]


def test_an_unverified_hit_is_not_a_verified_hit() -> None:
    g = grade_one(record(), result(1, status="unverified"))
    assert g["exact"] and not g["exact_verified"]


def test_agent_level_uses_the_speaker_of_the_cited_step() -> None:
    g = grade_one(record(), result(3))
    assert not g["exact"] and not g["agent"]


@pytest.mark.parametrize("step", [None, True, "1", 0, 99])
def test_unusable_predictions_score_nothing(step: Any) -> None:
    g = grade_one(record(), result(step))
    assert not (g["exact"] or g["exact_verified"] or g["agent"])


def test_a_label_on_environment_output_is_flagged_unreachable() -> None:
    assert grade_one(record(mistake_step=1), result(2))["unreachable"]
    assert not grade_one(record(), result(1))["unreachable"]


def test_first_attempt_accuracy_reads_the_first_answer() -> None:
    corrected = result(1)
    corrected["attempts"].insert(0, result(3)["attempts"][0])
    g = grade_one(record(), corrected)
    assert g["exact"] and not g["exact_first"]
    regressed = result(3)
    regressed["attempts"].insert(0, result(1)["attempts"][0])
    assert grade_one(record(), regressed)["exact_first"]
    assert not grade_one(record(), result(True))["exact_first"]  # True == 1 in Python


def test_fixed_rule_baselines() -> None:
    # Steps: agent, environment, agent; the label is step 0.
    out = whowhen.baselines([record(), record(mistake_step=2), record(mistake_step=2)])
    assert out["first agent message"]["Algorithm-Generated"]["k"] == 1
    assert out["second agent message"]["Algorithm-Generated"]["k"] == 2  # index 2, not 1
    assert out["last agent message"]["Algorithm-Generated"]["k"] == 2


def test_a_failed_run_scores_nothing() -> None:
    g = grade_one(
        record(), {"status": "failed", "attempts": [{"attribution": None}], "citation_retries": 0}
    )
    assert g["predicted_step"] is None and not g["exact"]


# --- fetching ----------------------------------------------------------------------------


def test_fetch_rejects_data_that_does_not_match_the_pin(tmp_path: Path) -> None:
    with pytest.raises(WhoWhenError, match="does not match its pin"):
        whowhen.fetch(tmp_path, download=lambda url: b"{}")
    assert not list(tmp_path.rglob("*.part"))  # downloads are atomic


def test_load_records_refuses_missing_data(tmp_path: Path) -> None:
    with pytest.raises(WhoWhenError, match="fetch"):
        whowhen.load_records(tmp_path)


@pytest.fixture
def real() -> list[dict[str, Any]]:
    try:
        return whowhen.load_records()
    except WhoWhenError:
        pytest.skip("Who&When not fetched; run python -m benchmark.whowhen fetch")


def test_the_real_data(real: list[dict[str, Any]]) -> None:
    assert len(real) == 184
    assert sum(r["subset"] == "Algorithm-Generated" for r in real) == 126
    assert all(0 <= r["mistake_step"] < len(r["steps"]) for r in real)
    unreachable = [r["id"] for r in real if r["steps"][r["mistake_step"]]["result"] is not None]
    assert unreachable == ["Algorithm-Generated/14", "Algorithm-Generated/59"]
    for r in real:
        trace_of(r)  # every record is a valid trace


# --- running -------------------------------------------------------------------------------


class Always:
    """Always attributes the mistake to step 1, which is the right answer for `record()`."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages: Any, tools: Any) -> Completion:
        self.calls += 1
        args = {"mistake": {"step": 1, "quote": "Count the odd rows"}, "reason": "x"}
        return Completion(
            provider="fake",
            model="fake",
            request={},
            content=None,
            tool_calls=(ToolCall("c0", "submit_attribution", json.dumps(args)),),
            reasoning=None,
            finish_reason="tool_calls",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            latency_s=0.0,
            rate_limits={},
            raw={},
        )


def test_run_saves_resumes_and_refuses_another_configuration(tmp_path: Path) -> None:
    records = [record(), record(id="Algorithm-Generated/2")]
    model = Always()
    config = {"dataset": "whowhen", "with_answer": False}
    whowhen.run(
        records,
        tmp_path,
        Attributor(model),
        config,
        with_answer=False,
        workers=1,
        log=lambda _: None,
    )
    assert model.calls == 2
    whowhen.run(
        records,
        tmp_path,
        Attributor(model),
        config,
        with_answer=False,
        workers=1,
        log=lambda _: None,
    )
    assert model.calls == 2  # nothing redone
    with pytest.raises(WhoWhenError, match="different configuration"):
        whowhen.run(
            records,
            tmp_path,
            Attributor(model),
            {**config, "with_answer": True},
            with_answer=True,
            log=lambda _: None,
        )
    report = whowhen.grade(tmp_path, records)
    assert report["summary"]["Algorithm-Generated"]["exact"]["k"] == 2
    assert report["complete"] is True
    assert report["retried"] == {"records": 0, "then_verified": 0}
    (tmp_path / "report.json").unlink()
    assert whowhen.report(tmp_path, records) == report
    assert not (tmp_path / "report.json").exists()  # report() only reads


def test_real_runs_refuse_a_dirty_tree(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(whowhen, "git_info", lambda: {"commit": "abc", "dirty": True})
    monkeypatch.setattr(whowhen, "load_records", lambda: [record()])
    assert whowhen.main(["run"], client=Always()) == 1
    assert "clean, committed tree" in capsys.readouterr().err


def test_real_runs_are_saved_apart_from_audit_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(whowhen, "git_info", lambda: {"commit": "abc", "dirty": False})
    monkeypatch.setattr(whowhen, "load_records", lambda: [record()])
    monkeypatch.setattr(whowhen, "ATTRIBUTION_DIR", tmp_path)
    assert whowhen.main(["run", "--workers", "1"], client=Always()) == 0
    assert (tmp_path / "whowhen-baseten-without-answer-retry2" / "results.jsonl").is_file()


def test_the_papers_numbers_are_the_published_ones() -> None:
    assert whowhen.PAPER["without_answer"]["Algorithm-Generated"]["binary search"] == 16.59
    assert whowhen.PAPER["without_answer"]["Hand-Crafted"]["step-by-step"] == 8.77
    assert whowhen.PAPER["reasoning_models_hand_crafted"]["o1 step-by-step"] == 13.79
