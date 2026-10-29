"""Every number on the demo page comes from the saved runs, and the page asks for real ones."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import stats
from app.server import STATIC_DIR, create_app
from app.stats import fisher_two_sided
from benchmark import whowhen
from benchmark.fetch_data import DATA_DIR

PAGE = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
RESULTS: dict[str, Any] = json.loads(stats.OUT_PATH.read_text(encoding="utf-8"))
PATH_ATTRS = ("data-num", "data-kn", "data-pct", "data-count", "data-bar", "data-funnel")


def lookup(path: str) -> Any:
    value: Any = RESULTS
    for key in path.split("."):
        value = value[key]
    return value


@pytest.fixture
def need_data() -> None:
    for name in ("eval_traces.jsonl", "eval_traces_realistic.jsonl"):
        if not (DATA_DIR / name).exists():
            pytest.skip(f"{name} not fetched; run python -m benchmark.fetch_data")
    if not whowhen.RAW_DIR.exists():
        pytest.skip("Who&When not fetched; run python -m benchmark.whowhen fetch")


@pytest.mark.usefixtures("need_data")
def test_committed_results_are_exactly_what_the_runs_give() -> None:
    assert stats.OUT_PATH.read_text(encoding="utf-8") == stats.render(stats.build())


def test_headline_numbers() -> None:
    # The figures the README and DEVLOG report, recomputed independently here.
    gb = RESULTS["groundingbench"]
    assert gb["traces"] == 720
    assert gb["substitution"] == {"single": {"k": 176, "n": 180}, "pinpoint": {"k": 0, "n": 179}}
    assert gb["step_correct"] == {"single": {"k": 240, "n": 538}, "pinpoint": {"k": 536, "n": 536}}
    assert gb["accuracy"] == {"single": {"k": 716, "n": 720}, "pinpoint": {"k": 715, "n": 720}}
    assert {m: c["k"] for m, c in RESULTS["factorial"].items()} == {
        "single": 38, "single_rule": 89, "two_slot_no_rule": 90, "two_slot": 90,
    }  # fmt: skip
    assert {m: c["k"] for m, c in RESULTS["midtrace"].items()} == {
        "single": 23, "single_rule": 24, "two_slot_no_rule": 21, "two_slot": 21,
    }  # fmt: skip
    assert RESULTS["midtrace_p"] == 0.23
    assert RESULTS["knew"] == {
        "substitutions": 176, "mentions_step": 166, "weighs_citing": 137, "must_pick_one": 94,
    }  # fmt: skip
    assert RESULTS["verification"] == {"audits": 4152, "unverified_citations": 0}
    assert RESULTS["earliest"] == {
        "fresh": {"two_slot": {"k": 22, "n": 24}, "earliest": {"k": 24, "n": 24}},
        "groundingbench": {"two_slot": {"k": 536, "n": 536}, "earliest": {"k": 530, "n": 538}},
    }
    runs = RESULTS["agent_runs"]
    assert runs["runs"] == 12
    assert runs["gamed"] == {"k": 6, "n": 6} and runs["solvable_honest"] == {"k": 6, "n": 6}
    assert runs["pinpoint"]["flagged_step_correct"] == {"k": 6, "n": 6}
    assert runs["single"]["flagged_step_correct"] == {"k": 5, "n": 6}
    assert runs["pinpoint"]["honest_kept"] == runs["single"]["honest_kept"] == {"k": 6, "n": 6}
    assert runs["review_agrees"] == {"k": 10, "n": 12}
    assert runs["repeats"] == {
        "tries": 5,
        "flagged_step_correct": {"k": 25, "n": 30},
        "honest_kept": {"k": 30, "n": 30},
    }
    ww = RESULTS["whowhen"]
    assert ww["runs"] == 184
    assert ww["algorithm"]["exact"] == {"k": 57, "n": 126}
    assert ww["hand"]["exact"] == {"k": 11, "n": 58}
    assert ww["algorithm"]["best_rule"] == {"k": 36, "n": 126}  # the second agent message
    assert ww["hand"]["best_rule"] == {"k": 7, "n": 58}  # the last agent message
    assert ww["algorithm"]["exact_with_answer"] == {"k": 60, "n": 126}
    assert ww["hand"]["exact_with_answer"] == {"k": 13, "n": 58}
    paper = (ww["algorithm"]["paper_best"], ww["hand"]["paper_best"], ww["o1"])
    assert paper == (16.59, 8.77, 13.79)
    assert ww["sent_back"] == {"k": 16, "n": 184} and ww["corrected"] == {"k": 15, "n": 16}
    rerun = RESULTS["replication"]["rerun"]
    assert rerun["substitution"] == {"single": {"k": 147, "n": 150}, "pinpoint": {"k": 0, "n": 150}}
    assert rerun["step_correct"] == {
        "single": {"k": 210, "n": 448},
        "pinpoint": {"k": 448, "n": 448},
    }


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ((21, 24), (24, 24), 0.2340),
        ((24, 24), (21, 24), 0.2340),
        ((5, 10), (5, 10), 1.0),
        ((0, 10), (10, 10), 1.0825e-05),
    ],
)
def test_fisher_two_sided(a: tuple[int, int], b: tuple[int, int], expected: float) -> None:
    got = fisher_two_sided({"k": a[0], "n": a[1]}, {"k": b[0], "n": b[1]})
    assert got == pytest.approx(expected, rel=1e-3)


def test_every_number_the_page_asks_for_exists() -> None:
    paths = [(attr, path) for attr in PATH_ATTRS for path in re.findall(rf'{attr}="([^"]+)"', PAGE)]
    assert len(paths) > 20
    for attr, path in paths:
        value = lookup(path)
        if attr in ("data-kn", "data-pct", "data-bar"):
            assert set(value) == {"k", "n"}, path
        else:
            assert isinstance(value, int | float), path


def test_fallback_numbers_in_the_page_match_the_data() -> None:
    # Numbers written into the HTML (shown before the script runs) must agree.
    for path, shown in re.findall(r'data-num="([^"]+)">([^<]+)<', PAGE):
        value = lookup(path)
        expected = f"{value:,}" if isinstance(value, int) else str(value)
        assert shown == expected, path


def test_page_assets_are_served() -> None:
    client = TestClient(create_app(None))
    for path in ("/static/style.css", "/static/app.js", "/static/results.json"):
        assert client.get(path).status_code == 200, path
    assert 'src="/static/app.js"' in PAGE and 'href="/static/style.css"' in PAGE


def test_every_icon_the_page_links_is_served() -> None:
    client = TestClient(create_app(None))
    icons = re.findall(r'<link rel="(?:icon|apple-touch-icon)" href="([^"]+)"', PAGE)
    assert len(icons) == 3
    for path in icons:
        assert client.get(path).status_code == 200, path


def test_trace_text_never_reaches_innerhtml() -> None:
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script
    assert "document.write" not in script
