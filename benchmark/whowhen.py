"""Who&When: Pinpoint's failure attribution on an independent, human-labelled benchmark.

Who&When (Zhang et al., ICML 2025, arXiv 2505.00212) holds 184 failed runs of
LLM multi-agent systems (126 algorithm-generated, 58 hand-crafted), each with a
human label for the agent and the step of the decisive mistake. The paper's
best GPT-4o method finds that step 16.59% / 8.77% of the time without being
told the correct answer (Table 1); o1 and DeepSeek R1 reach 13.79% / 6.90% on
the hand-crafted runs (Table 4).

The data comes from the authors' MIT-licensed repository at a pinned commit,
checked against a pinned hash, and stays in the gitignored ``data/``
directory. Each message becomes one step; messages from the environment (code
output, the user) are tool results, so the mistake citation, which must quote
an agent's own message, cannot cite them. Two algorithm-generated labels point
at such a message and are counted as misses.

Steps here count from 1; the benchmark's count from 0.

Usage:
    python -m benchmark.whowhen fetch
    python -m benchmark.whowhen run [--with-answer] [--limit N --smoke]
    python -m benchmark.whowhen grade RUN_DIR
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmark.fetch_data import DATA_DIR
from benchmark.grade import PRICES, wilson
from benchmark.run import REPO_ROOT, RUNS_DIR, git_info
from pinpoint.attribution import ATTRIBUTION_VERSION, AttributionResult, Attributor
from pinpoint.client import PROVIDERS, ClientError, ModelClient, load_env
from pinpoint.overseer import CompletionSource
from pinpoint.trace import Trace

REPO = "mingyin1/Agents_Failure_Attribution"
COMMIT = "b2bae5c5b06d681d04ea5e9b63b7a30525c04925"
SUBSETS = {"Algorithm-Generated": 126, "Hand-Crafted": 58}
SPEAKER_KEY = {"Algorithm-Generated": "name", "Hand-Crafted": "role"}
# Pinned when the data was first fetched for this experiment.
COMBINED_SHA256 = "e2a8400d3ee899238e18bdcdf6a0df7a40d2d479fb43d1048ba0d0310a876a51"
RAW_DIR = DATA_DIR / "whowhen"
SMOKE_DIR = REPO_ROOT / "results" / "smoke"

# Speakers that are the environment, not agents: their messages are results.
ENVIRONMENT = frozenset({"Computer_terminal", "ComputerTerminal", "human"})

# Step-level accuracy (%) from the paper, without / with the correct answer.
# Table 1 (GPT-4o, both subsets) and Table 4 (o1 and DeepSeek R1, hand-crafted).
PAPER: dict[str, dict[str, Any]] = {
    "without_answer": {
        "Algorithm-Generated": {
            "random": 19.06,
            "all-at-once": 13.53,
            "step-by-step": 15.31,
            "binary search": 16.59,
        },
        "Hand-Crafted": {
            "random": 4.16,
            "all-at-once": 3.51,
            "step-by-step": 8.77,
            "binary search": 6.90,
        },
    },
    "with_answer": {
        "Algorithm-Generated": {
            "all-at-once": 12.50,
            "step-by-step": 25.51,
            "binary search": 23.98,
        },
        "Hand-Crafted": {"all-at-once": 5.26, "step-by-step": 7.02, "binary search": 6.90},
    },
    "reasoning_models_hand_crafted": {
        "o1 step-by-step": 13.79,
        "o1 all-at-once": 10.34,
        "R1 step-by-step": 6.90,
        "R1 all-at-once": 3.45,
    },
}


class WhoWhenError(RuntimeError):
    """The data cannot be fetched or does not match its pin."""


# --- data ---------------------------------------------------------------------------------


def _files(raw_dir: Path) -> list[tuple[str, int, Path]]:
    return [(s, i, raw_dir / s / f"{i}.json") for s, n in SUBSETS.items() for i in range(1, n + 1)]


def combined_sha256(raw_dir: Path = RAW_DIR) -> str:
    digest = hashlib.sha256()
    for subset, i, path in _files(raw_dir):
        digest.update(f"{subset}/{i}.json\n".encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "pinpoint"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data: bytes = response.read()
    return data


def fetch(raw_dir: Path = RAW_DIR, download: Callable[[str], bytes] = _download) -> str:
    """Download every file at the pinned commit (atomically) and check the pin."""
    for subset, i, path in _files(raw_dir):
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://raw.githubusercontent.com/{REPO}/{COMMIT}/" + urllib.parse.quote(
            f"Who&When/{subset}/{i}.json"
        )
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(download(url))
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    actual = combined_sha256(raw_dir)
    if actual != COMBINED_SHA256:
        raise WhoWhenError(f"the data does not match its pin (got {actual})")
    return actual


def to_record(raw: dict[str, Any], subset: str, index: int) -> dict[str, Any]:
    """One benchmark record: an observable trace plus the human labels beside it."""
    key = SPEAKER_KEY[subset]
    steps: list[dict[str, Any]] = []
    for message in raw["history"]:
        speaker = str(message.get(key) or "unknown").replace("\n", " ").strip() or "unknown"
        content = str(message.get("content") or "")
        if speaker in ENVIRONMENT:
            steps.append({"tool": speaker, "args": {}, "result": content})
        else:
            steps.append({"tool": speaker, "args": {"message": content}, "result": None})
    return {
        "id": f"{subset}/{index}",
        "subset": subset,
        "question": str(raw["question"]),
        "steps": steps,
        "ground_truth": str(raw.get("ground_truth", "")),
        "mistake_agent": str(raw["mistake_agent"]),
        "mistake_step": int(raw["mistake_step"]),  # 0-based, as in the benchmark
        "mistake_reason": raw.get("mistake_reason"),
    }


def load_records(raw_dir: Path = RAW_DIR) -> list[dict[str, Any]]:
    complete = all(path.is_file() for _, _, path in _files(raw_dir))
    if not complete or combined_sha256(raw_dir) != COMBINED_SHA256:
        raise WhoWhenError(
            "data missing or not the pinned version; run: python -m benchmark.whowhen fetch"
        )
    return [
        to_record(json.loads(p.read_text(encoding="utf-8")), s, i) for s, i, p in _files(raw_dir)
    ]


def trace_of(record: dict[str, Any]) -> Trace:
    """Only the observable fields reach the model."""
    return Trace.from_dict({"question": record["question"], "steps": record["steps"]})


# --- running --------------------------------------------------------------------------------


def run(
    records: Sequence[dict[str, Any]],
    run_dir: Path,
    attributor: Attributor,
    config: dict[str, Any],
    *,
    with_answer: bool,
    workers: int = 4,
    log: Callable[[str], None] = print,
) -> None:
    """Attribute every record, saving each result as it arrives; resumes a partial run."""
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = run_dir / "manifest.json"
    if manifest.exists():
        saved = json.loads(manifest.read_text(encoding="utf-8"))["config"]
        if saved != config:
            raise WhoWhenError(f"{run_dir} was started with a different configuration")
    else:
        manifest.write_text(
            json.dumps(
                {
                    "config": config,
                    "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "code": git_info(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    results = run_dir / "results.jsonl"
    done = set()
    if results.exists():
        for line in results.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["id"])
            except (json.JSONDecodeError, KeyError):
                continue
    todo = [r for r in records if r["id"] not in done]
    log(f"{len(done)} saved, {len(todo)} to run")

    def one(record: dict[str, Any]) -> tuple[str, AttributionResult, float]:
        start = time.monotonic()
        answer = record["ground_truth"] if with_answer else None
        return (
            record["id"],
            attributor.attribute(trace_of(record), answer),
            time.monotonic() - start,
        )

    with (
        results.open("a", encoding="utf-8") as out,
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        futures = [pool.submit(one, r) for r in todo]
        for i, future in enumerate(as_completed(futures), 1):
            record_id, result, seconds = future.result()
            out.write(
                json.dumps(
                    {"id": record_id, "seconds": round(seconds, 2), "result": result.to_dict()},
                    ensure_ascii=False,
                )
                + "\n"
            )
            out.flush()
            if i % 10 == 0 or i == len(futures):
                log(f"{len(done) + i}/{len(records)} saved")


# --- grading ---------------------------------------------------------------------------------


def _final(result: dict[str, Any]) -> dict[str, Any] | None:
    readable = [a for a in result["attempts"] if a.get("attribution") is not None]
    return readable[-1] if readable else None


def grade_one(record: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Scored three ways: exact step (ours), the paper's lenient rule, and agent."""
    final = _final(result)
    label0 = record["mistake_step"]
    out = {
        "id": record["id"],
        "subset": record["subset"],
        "status": result["status"],
        "label_step": label0 + 1,
        "predicted_step": None,
        "exact": False,
        "exact_verified": False,
        "lenient": False,
        "agent": False,
        "unreachable": record["steps"][label0]["result"] is not None,
    }
    if final is None or not final["attribution"].get("mistake"):
        return out
    step = final["attribution"]["mistake"].get("step")
    if not isinstance(step, int) or isinstance(step, bool):
        return out
    out["predicted_step"] = step
    out["exact"] = step == label0 + 1
    out["exact_verified"] = out["exact"] and result["status"] == "verified"
    # The paper's scorer: the true 0-based step as a substring of the predicted one.
    out["lenient"] = str(label0) in str(step - 1)
    speaker = record["steps"][step - 1]["tool"] if 1 <= step <= len(record["steps"]) else ""
    out["agent"] = record["mistake_agent"] in speaker
    return out


def _rate(k: int, n: int) -> dict[str, Any]:
    lo, hi = wilson(k, n)
    return {
        "k": k,
        "n": n,
        "pct": round(100 * k / n, 2) if n else None,
        "ci95": [round(100 * lo, 1), round(100 * hi, 1)],
    }


def grade(run_dir: Path, records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    by_id = {r["id"]: r for r in records}
    saved = [
        json.loads(line)
        for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    graded = [grade_one(by_id[s["id"]], s["result"]) for s in saved]
    usage = [
        a["completion"]["usage"]
        for s in saved
        for a in s["result"]["attempts"]
        if a.get("completion")
    ]
    tokens_in = sum(u.get("prompt_tokens", 0) for u in usage)
    tokens_out = sum(u.get("completion_tokens", 0) for u in usage)
    price_in, price_out = PRICES["baseten"]
    summary: dict[str, Any] = {}
    for subset in SUBSETS:
        rows = [g for g in graded if g["subset"] == subset]
        n = len(rows)
        summary[subset] = {
            metric: _rate(sum(g[metric] for g in rows), n)
            for metric in ("exact_verified", "exact", "lenient", "agent")
        }
        summary[subset]["unreachable_labels"] = sum(g["unreachable"] for g in rows)
        summary[subset]["statuses"] = {
            s: sum(g["status"] == s for g in rows) for s in ("verified", "unverified", "failed")
        }
    report = {
        "run": run_dir.name,
        "config": json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["config"],
        "complete": len(saved) == len(records),
        "summary": summary,
        "citation_retries": sum(s["result"]["citation_retries"] for s in saved),
        "tokens": {"in": tokens_in, "out": tokens_out},
        "cost_usd": round((tokens_in * price_in + tokens_out * price_out) / 1e6, 2),
        "paper": PAPER,
        "traces": graded,
    }
    (run_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def render(report: dict[str, Any]) -> str:
    setting = "with_answer" if report["config"]["with_answer"] else "without_answer"
    lines = [
        f"== {report['run']} ({'complete' if report['complete'] else 'INCOMPLETE'}), "
        f"${report['cost_usd']}, {report['citation_retries']} citation retries"
    ]
    for subset, s in report["summary"].items():
        paper = PAPER[setting][subset]
        best = max(v for k, v in paper.items() if k != "random")
        exact, ci = s["exact"], s["exact"]["ci95"]
        lines.append(
            f"  {subset}: exact step {exact['pct']}% ({exact['k']}/{exact['n']}, "
            f"CI {ci[0]}-{ci[1]}), verified {s['exact_verified']['pct']}%, "
            f"paper's lenient rule {s['lenient']['pct']}%, agent {s['agent']['pct']}% | "
            f"paper best GPT-4o {best}%, random {paper.get('random', 'n/a')}% | "
            f"unreachable labels {s['unreachable_labels']}, statuses {s['statuses']}"
        )
    return "\n".join(lines)


# --- command line ----------------------------------------------------------------------------


def main(argv: list[str] | None = None, client: CompletionSource | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pinpoint failure attribution on Who&When.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch")
    run_p = sub.add_parser("run")
    run_p.add_argument("--with-answer", action="store_true")
    run_p.add_argument("--citation-retries", type=int, default=2)
    run_p.add_argument("--workers", type=int, default=4)
    run_p.add_argument("--limit", type=int)
    run_p.add_argument(
        "--smoke", action="store_true", help="save under results/smoke; dirty tree allowed"
    )
    grade_p = sub.add_parser("grade")
    grade_p.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)

    try:
        if args.command == "fetch":
            print(
                f"Who&When at {COMMIT[:7]}: sha256 {fetch()[:16]}..., {sum(SUBSETS.values())} runs"
            )
            return 0
        records = load_records()
        if args.command == "grade":
            print(render(grade(args.run_dir, records)))
            return 0
        code = git_info()
        if not args.smoke and (code is None or code["dirty"]):
            print(
                "error: real runs start from a clean, committed tree; commit first", file=sys.stderr
            )
            return 1
        if args.limit is not None:
            # Interleave subsets so a small smoke run sees both.
            by_subset = [[r for r in records if r["subset"] == s] for s in SUBSETS]
            records = [r for pair in zip(*by_subset, strict=False) for r in pair][: args.limit]
        setting = "with-answer" if args.with_answer else "without-answer"
        name = f"whowhen-baseten-{setting}-retry{args.citation_retries}"
        run_dir = (SMOKE_DIR if args.smoke else RUNS_DIR) / name
        if client is None:
            load_env()
            client = ModelClient(PROVIDERS["baseten"], timeout_s=300, max_retries=3)
        config = {
            "dataset": "whowhen",
            "data_commit": COMMIT,
            "data_sha256": COMBINED_SHA256,
            "provider": "baseten",
            "model": PROVIDERS["baseten"].model,
            "with_answer": args.with_answer,
            "max_citation_retries": args.citation_retries,
            "max_format_retries": 1,
            "attribution_version": ATTRIBUTION_VERSION,
            "limit": args.limit,
        }
        attributor = Attributor(client, max_citation_retries=args.citation_retries)
        run(
            records, run_dir, attributor, config, with_answer=args.with_answer, workers=args.workers
        )
        print(render(grade(run_dir, load_records() if args.limit is None else records)))
        return 0
    except (WhoWhenError, ClientError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
