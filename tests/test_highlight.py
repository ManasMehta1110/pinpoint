"""Tests for app.highlight: spans agree with the verifier, on real model citations too."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import highlight
from app.highlight import locate, locate_in_step
from benchmark.fetch_data import DATA_DIR
from benchmark.run import RUNS_DIR, data_path, load_records
from pinpoint import verifier
from pinpoint.audit import has_offense_rule
from pinpoint.trace import Step, Trace
from pinpoint.verifier import Scope, clean_quote, contains_phrase, normalize


def test_equivalences_match_the_verifier() -> None:
    assert highlight._EQUIVALENTS == verifier._EQUIVALENTS


@pytest.mark.parametrize(
    ("text", "quote", "found"),
    [
        ("It was formed in 1950.", "1950", "1950"),
        ("It was formed in 1950.", '"formed in 1950."', "formed in 1950"),
        ("It   was\n formed", "was formed", "was\n formed"),
        ("The LOUVRE is in Rome", "louvre is in rome", "LOUVRE is in Rome"),
        ("the agent\u2019s answer", "agent's answer", "agent\u2019s answer"),
        ("1914\u20131918", "1914-1918", "1914\u20131918"),
        ("submit_answer", "answer", "answer"),
        ("  leading space", "leading", "leading"),
    ],
)
def test_locate_finds_the_original_characters(text: str, quote: str, found: str) -> None:
    span = locate(text, quote)
    assert span is not None
    assert text[span[0] : span[1]] == found


@pytest.mark.parametrize(
    ("text", "quote"),
    [("in 1016", "016"), ("the union", "nion"), ("abc", ""), ("abc", '"."'), ("abc", "xyz")],
)
def test_locate_refuses_what_the_verifier_refuses(text: str, quote: str) -> None:
    assert locate(text, quote) is None


def test_ligatures_and_expansions_map_back() -> None:
    # NFKC turns one character into several; the span still covers it.
    text = "the \ufb01nal answer"  # fi ligature
    span = locate(text, "final answer")
    assert span is not None and text[span[0] : span[1]] == "\ufb01nal answer"


def test_unmappable_text_gives_no_span() -> None:
    # A combining sequence that NFKC composes across characters: folding one
    # character at a time disagrees with normalize(), so no span is claimed.
    text = "cafe\u0301 au lait"
    assert locate(text, "au lait") is None


def test_first_occurrence_is_used() -> None:
    assert locate("1950 and 1950", "1950") == (0, 4)


STEP = Step(2, "read_document", {"doc_id": "d3"}, "It was formed in 1945.")


def test_action_scope_never_reaches_the_result() -> None:
    assert locate_in_step(STEP, "1945", "action") is None
    span = locate_in_step(STEP, "1945", "step")
    assert span is not None and STEP.text()[span[0] : span[1]] == "1945"


def test_action_spans_index_the_whole_step_text() -> None:
    span = locate_in_step(STEP, "d3", "action")
    assert span is not None and STEP.text()[span[0] : span[1]] == "d3"


@pytest.mark.parametrize("quote", [None, 3, ["d3"]])
def test_non_string_quotes_give_no_span(quote: object) -> None:
    assert locate_in_step(STEP, quote) is None


@pytest.mark.parametrize(
    ("text", "quote"),
    [("a b", "a  b"), ("x-ray", "x\u2014ray"), ("Step 6", "step 6"), ("e.g. this", "e.g")],
)
def test_found_exactly_when_the_verifier_finds_it(text: str, quote: str) -> None:
    expected = contains_phrase(normalize(text), clean_quote(quote))
    assert (locate(text, quote) is not None) == expected


def test_every_verified_citation_in_every_saved_run_has_a_span() -> None:
    # Every citation the verifier accepted, from every real run, must map to
    # characters whose folded form is exactly the cleaned quote.
    checked = 0
    for run_dir in sorted(p for p in RUNS_DIR.iterdir() if p.is_dir()):
        config = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["config"]
        path = data_path(config["data_file"], DATA_DIR)
        if not path.exists():
            pytest.skip(f"{path.name} not fetched; run python -m benchmark.fetch_data")
        traces = {line: Trace.from_dict(r) for line, r in load_records(path)}
        # The offense is held to the agent's action only in modes with the rule.
        offense_scope: Scope = "action" if has_offense_rule(config["mode"]) else "step"
        for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            trace = traces[entry["line"]]
            for attempt in entry["result"]["attempts"]:
                audit, check = attempt["audit"], attempt["verification"]
                if audit is None or check is None:
                    continue
                for slot, scope in (("offense", offense_scope), ("contradicted_by", "step")):
                    if (check.get(slot) or {}).get("status") != "verified":
                        continue
                    cited = audit[slot]
                    step = trace.get_step(cited["step"])
                    assert step is not None
                    span = locate_in_step(step, cited["quote"], scope)
                    assert span is not None, (run_dir.name, entry["line"], slot)
                    found = step.text()[span[0] : span[1]]
                    assert normalize(found) == clean_quote(cited["quote"])
                    checked += 1
    assert checked >= 2175  # all runs as of the mid-trace experiment; grows with new runs


def test_runs_dir_exists() -> None:
    assert isinstance(RUNS_DIR, Path) and RUNS_DIR.is_dir()
