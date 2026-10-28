"""Every number the demo page shows, computed from the saved runs.

The page never hard-codes a result: it reads ``app/static/results.json``,
which this module writes from ``results/runs/`` and
``results/analysis/knew.json``. A test checks that the committed file is
exactly what this produces, so a number on the page cannot drift from the
data behind it.

The one figure not computed here is GroundingBench's 1 of 1,794, which comes
from the prior-work paper and is labelled as such on the page.

Usage:
    python -m app.stats      # rewrites app/static/results.json
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from math import comb
from pathlib import Path
from typing import Any

from benchmark.fetch_data import DATA_DIR
from benchmark.grade import grade_trace, summarize
from benchmark.knew import OUT_PATH as KNEW_PATH
from benchmark.run import RUNS_DIR, data_path, load_records

OUT_PATH = Path(__file__).resolve().parent / "static" / "results.json"

GROUNDINGBENCH = ("eval_traces", "eval_traces_realistic")
FACTORIAL = ("single", "single_rule", "two_slot_no_rule", "two_slot")

# From the GroundingBench paper (prior work, not a run in this repository).
PRIOR_WORK = {
    "grounded": 1,
    "verdicts": 1794,
    "models": 7,
    "accuracy_low": 70.8,
    "accuracy_high": 100,
    "source": "GroundingBench (prior work)",
}


def _run(name: str) -> tuple[dict[str, Any], list[tuple[dict[str, Any], dict[str, Any]]]]:
    """A run's config and its (result, truth) pairs."""
    run_dir = RUNS_DIR / name
    config = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["config"]
    truths = dict(load_records(data_path(config["data_file"], DATA_DIR)))
    pairs = []
    for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            pairs.append((entry["result"], truths[entry["line"]]))
    return config, pairs


def _summary(names: Iterable[str]) -> dict[str, Any]:
    graded = []
    provider = "baseten"
    for name in names:
        config, pairs = _run(name)
        provider = config["provider"]
        graded += [grade_trace(result, truth) for result, truth in pairs]
    return summarize(graded, provider)


def _kn(entry: dict[str, Any]) -> dict[str, int]:
    return {"k": entry["k"], "n": entry["n"]}


def _final_offense_step(result: dict[str, Any]) -> object:
    with_audit = [a for a in result.get("attempts") or [] if a.get("audit") is not None]
    if not with_audit:
        return None
    return (with_audit[-1]["audit"].get("offense") or {}).get("step")


def substitutions(names: Iterable[str]) -> dict[str, int]:
    """Correctly flagged deceptive traces whose offense cites a ``read_document`` step."""
    k = n = 0
    for name in names:
        _, pairs = _run(name)
        for result, truth in pairs:
            graded = grade_trace(result, truth)
            if truth["policy_class"] != "DECEIVER" or not graded["eligible"]:
                continue
            n += 1
            step = _final_offense_step(result)
            steps = truth["steps"]
            if isinstance(step, int) and not isinstance(step, bool) and 1 <= step <= len(steps):
                k += steps[step - 1]["tool"] == "read_document"
    return {"k": k, "n": n}


def fisher_two_sided(a: dict[str, int], b: dict[str, int]) -> float:
    """Fisher's exact test, two-sided, for successes ``a["k"]/a["n"]`` vs ``b["k"]/b["n"]``."""
    k1, n1, k2, n2 = a["k"], a["n"], b["k"], b["n"]
    total, successes = n1 + n2, k1 + k2

    def p(x: int) -> float:
        return comb(n1, x) * comb(n2, successes - x) / comb(total, successes)

    observed = p(k1)
    lo, hi = max(0, successes - n2), min(n1, successes)
    return sum(p(x) for x in range(lo, hi + 1) if p(x) <= observed * (1 + 1e-9))


def audit_count() -> dict[str, int]:
    """Audits across every saved run, any host, and how many citations failed verification."""
    audits = unverified = 0
    for run_dir in sorted(p for p in RUNS_DIR.iterdir() if p.is_dir()):
        for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            result = json.loads(line)["result"]
            audits += 1
            unverified += sum(
                (a.get("verification") or {}).get("status") == "unverified"
                for a in result["attempts"]
            )
    return {"audits": audits, "unverified_citations": unverified}


def build() -> dict[str, Any]:
    def runs(mode: str, stems: Iterable[str] = GROUNDINGBENCH) -> list[str]:
        return [f"{stem}-baseten-{mode}-retry0" for stem in stems]

    single, pinpoint = _summary(runs("single")), _summary(runs("two_slot"))
    knew = json.loads(KNEW_PATH.read_text(encoding="utf-8"))["summary"]
    midtrace = {
        mode: _kn(_summary(runs(mode, ["midtrace"]))["flagged_step_correct"]) for mode in FACTORIAL
    }
    return {
        "model": "meta-models/muse-spark-open-1026",
        "groundingbench": {
            "traces": single["n"],
            "substitution": {
                "single": substitutions(runs("single")),
                "pinpoint": substitutions(runs("two_slot")),
            },
            "step_correct": {
                "single": _kn(single["step_correct"]),
                "pinpoint": _kn(pinpoint["step_correct"]),
            },
            "accuracy": {
                "single": _kn(single["accuracy"]),
                "pinpoint": _kn(pinpoint["accuracy"]),
            },
        },
        "factorial": {
            mode: _kn(_summary(runs(mode, ["eval_traces"]))["step_correct"]) for mode in FACTORIAL
        },
        "midtrace": midtrace,
        # The mid-trace gap the page discloses: two slots vs the rule alone.
        "midtrace_p": round(fisher_two_sided(midtrace["two_slot"], midtrace["single_rule"]), 2),
        "knew": {
            key: knew[key]
            for key in ("substitutions", "mentions_step", "weighs_citing", "must_pick_one")
        },
        "verification": audit_count(),
        "replication": replication("fireworks"),
        "agent_runs": agent_runs(),
        "prior_work": PRIOR_WORK,
    }


def agent_runs() -> dict[str, Any]:
    """The real agent runs: what the agent did, and how each overseer read it."""
    records = [record for _, record in load_records(data_path("agentruns.jsonl", DATA_DIR))]
    impossible = [r for r in records if r["policy_name"] in ("pricing", "median")]
    solvable = [r for r in records if r["policy_name"] not in ("pricing", "median")]

    def side(mode: str) -> dict[str, Any]:
        s = _summary([f"agentruns-baseten-{mode}-retry0"])
        honest = s["confusion"].get("HONEST", {})
        return {
            "flagged_step_correct": _kn(s["flagged_step_correct"]),
            "honest_kept": {"k": honest.get("HONEST", 0), "n": sum(honest.values())},
        }

    # The person's review: their label for each run, read from the sheet.
    review = (RUNS_DIR.parent / "agent_runs" / "REVIEW.md").read_text(encoding="utf-8")
    reviewer = dict(
        re.findall(
            r"^## (\S+) .*?\*\*Reviewer's label:\*\* ([A-Z_]+)", review, re.MULTILINE | re.DOTALL
        )
    )
    rule = {r["question_id"]: r["policy_class"] for r in records}
    if set(reviewer) != set(rule):
        raise ValueError("the review sheet does not cover exactly the recorded runs")
    return {
        "runs": len(records),
        "review_agrees": {"k": sum(reviewer[q] == c for q, c in rule.items()), "n": len(rule)},
        "gamed": {
            "k": sum(r["policy_class"] != "HONEST" for r in impossible),
            "n": len(impossible),
        },
        "solvable_honest": {
            "k": sum(r["policy_class"] == "HONEST" for r in solvable),
            "n": len(solvable),
        },
        "pinpoint": side("two_slot"),
        "single": side("single"),
    }


def replication(provider: str) -> dict[str, Any]:
    """The naturalistic comparison rerun on another host, beside the original."""

    def side(host: str) -> dict[str, Any]:
        names = {
            mode: f"eval_traces_realistic-{host}-{mode}-retry0" for mode in ("single", "two_slot")
        }
        summaries = {mode: _summary([name]) for mode, name in names.items()}
        return {
            "substitution": {
                "single": substitutions([names["single"]]),
                "pinpoint": substitutions([names["two_slot"]]),
            },
            "step_correct": {
                "single": _kn(summaries["single"]["step_correct"]),
                "pinpoint": _kn(summaries["two_slot"]["step_correct"]),
            },
            "accuracy": {
                "single": _kn(summaries["single"]["accuracy"]),
                "pinpoint": _kn(summaries["two_slot"]["accuracy"]),
            },
        }

    return {"provider": provider, "original": side("baseten"), "rerun": side(provider)}


def render(stats: dict[str, Any]) -> str:
    return json.dumps(stats, indent=1, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    del argv
    OUT_PATH.write_text(render(build()), encoding="utf-8", newline="\n")
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
