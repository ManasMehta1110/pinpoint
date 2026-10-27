"""Run the overseer over a held-out trace file and save every result.

Each run lives in its own directory::

    results/runs/<name>/
        manifest.json   configuration, data hash, code version
        results.jsonl   one line per trace: its line number in the data file
                        and the full OverseerResult (every attempt, every raw
                        completion)

Results hold model output only. Labels stay in the data file and are joined
at grading time by line number, which is the key because question ids repeat
across classes.

A run is resumable: repeating the command skips traces already saved, and
refuses to continue a run that was started with a different configuration.
Each result is written and flushed as it arrives, so a crash loses at most
the traces in flight, and a final line cut off mid-write is discarded on
resume.

Usage:
    python -m benchmark.run --data eval_traces.jsonl --mode two_slot
    python -m benchmark.run --data eval_traces.jsonl --mode single --citation-retries 0
    python -m benchmark.run --data eval_traces.jsonl --limit 8          # smoke run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmark.fetch_data import DATA_DIR, TRACE_FILES, sha256_of
from benchmark.midtrace import DATASET_PATH as MIDTRACE_PATH
from pinpoint.audit import MODES, check_mode
from pinpoint.client import DEFAULT_MAX_TOKENS, PROVIDERS, ClientError, ModelClient, load_env
from pinpoint.overseer import (
    DEFAULT_MAX_CITATION_RETRIES,
    DEFAULT_MAX_FORMAT_RETRIES,
    CompletionSource,
    Overseer,
    OverseerResult,
)
from pinpoint.prompt import PROMPT_VERSION
from pinpoint.trace import Trace

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "results" / "runs"

# Measured on the first real calls; used only for the printed estimate.
ESTIMATED_USD_PER_TRACE = 0.0065


class RunError(RuntimeError):
    """A run cannot start or continue safely."""


@dataclass(frozen=True)
class LocalFile:
    """A trace file kept in this repository, rather than fetched into ``data/``."""

    name: str
    path: Path
    sha256: str


# Pinned when the dataset was pre-registered: committed before any model saw
# it. Regenerating it must reproduce this hash (tests/test_midtrace.py).
LOCAL_FILES: tuple[LocalFile, ...] = (
    LocalFile(
        name="midtrace.jsonl",
        path=MIDTRACE_PATH,
        sha256="2251257216dd10425e26d5fa707c2b5310863cf06af5d3977ed2fc4209cdca8a",
    ),
)


def data_path(name: str, data_dir: Path) -> Path:
    """Where the trace file ``name`` lives: in the repo if local, else in ``data_dir``."""
    local = next((f for f in LOCAL_FILES if f.name == name), None)
    return local.path if local else data_dir / name


def pinned_sha256(name: str) -> str:
    """The hash a trace file must have before a run may use it."""
    pins = {spec.name: spec.sha256 for spec in TRACE_FILES}
    pins.update({f.name: f.sha256 for f in LOCAL_FILES})
    return pins[name]


@dataclass(frozen=True)
class RunConfig:
    """Everything that determines what a run measures. A run directory may
    only ever hold results produced under one configuration."""

    data_file: str
    data_sha256: str
    provider: str
    model: str
    mode: str
    max_citation_retries: int
    max_format_retries: int
    max_tokens: int
    reasoning_effort: str | None
    temperature: float | None
    prompt_version: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_records(path: Path) -> list[tuple[int, dict[str, Any]]]:
    """Return ``(line number, record)`` for every non-blank line. Lines count from 0."""
    records: list[tuple[int, dict[str, Any]]] = []
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh):
            if line.strip():
                records.append((line_no, json.loads(line)))
    return records


def git_info(repo: Path = REPO_ROOT) -> dict[str, Any] | None:
    """The code version a run was made with, or None if git is unavailable.

    ``dirty`` is true if anything outside ``results/`` differs from the
    commit, untracked files included (a new module could change behaviour).
    ``results/`` is excluded because earlier runs write there: counting their
    output made every run after the first look dirty.
    """
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", ".", ":(exclude)results"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return {"commit": commit, "dirty": bool(status.strip())}


def prepare_run_dir(run_dir: Path, config: RunConfig) -> None:
    """Create the run directory and manifest, or check an existing one matches."""
    manifest_path = run_dir / "manifest.json"
    wanted = config.to_dict()
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8")).get("config", {})
        if existing != wanted:
            differing = sorted(
                k for k in set(existing) | set(wanted) if existing.get(k) != wanted.get(k)
            )
            raise RunError(
                f"{run_dir} was started with a different configuration "
                f"({', '.join(differing)}); choose a new --name"
            )
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "config": wanted,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "code": git_info(),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def read_done(results_path: Path) -> set[int]:
    """Line numbers already saved.

    A final line cut off mid-write (a crash) is removed so it can be redone.
    Any other unreadable or duplicated line stops the run: it would mean the
    file was damaged, and continuing could mix good and bad results.
    """
    if not results_path.exists():
        return set()
    lines = results_path.read_text(encoding="utf-8").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    done: set[int] = set()
    for i, line in enumerate(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                results_path.write_text(
                    "".join(f"{kept}\n" for kept in lines[:-1]), encoding="utf-8"
                )
                break
            raise RunError(f"{results_path}: line {i + 1} is corrupt") from None
        line_no = entry.get("line") if isinstance(entry, dict) else None
        if isinstance(line_no, bool) or not isinstance(line_no, int):
            raise RunError(f"{results_path}: line {i + 1} has no valid trace line number")
        if line_no in done:
            raise RunError(f"{results_path}: trace line {line_no} is saved twice")
        done.add(line_no)
    return done


def run(
    config: RunConfig,
    records: Sequence[tuple[int, dict[str, Any]]],
    run_dir: Path,
    client: CompletionSource,
    *,
    workers: int = 8,
    log: Callable[[str], None] = print,
) -> int:
    """Audit every record not yet saved in ``run_dir``. Returns how many were run.

    Model and provider failures are recorded per trace by the overseer. Any
    other exception is a bug: the run stops and re-raises, keeping every
    result already written.
    """
    if workers < 1:
        raise ValueError(f"workers must be at least 1, got {workers}")
    prepare_run_dir(run_dir, config)
    results_path = run_dir / "results.jsonl"
    done = read_done(results_path)
    todo = [(line_no, record) for line_no, record in records if line_no not in done]
    log(f"{len(done)} already saved, {len(todo)} to run")
    if not todo:
        return 0

    overseer = Overseer(
        client,
        mode=check_mode(config.mode),
        max_citation_retries=config.max_citation_retries,
        max_format_retries=config.max_format_retries,
    )

    def audit_one(item: tuple[int, dict[str, Any]]) -> tuple[int, OverseerResult, float]:
        line_no, record = item
        started = time.perf_counter()
        # Trace.from_dict keeps only observable fields: the labels in
        # `record` never reach the overseer.
        result = overseer.audit(Trace.from_dict(record))
        return line_no, result, time.perf_counter() - started

    completed = 0
    # Only this thread writes, so lines are never interleaved.
    with (
        results_path.open("a", encoding="utf-8", newline="\n") as out,
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        futures = [pool.submit(audit_one, item) for item in todo]
        try:
            for future in as_completed(futures):
                line_no, result, wall_s = future.result()
                entry = {"line": line_no, "wall_s": round(wall_s, 3), "result": result.to_dict()}
                out.write(json.dumps(entry, ensure_ascii=False) + "\n")
                out.flush()
                completed += 1
                if completed % 10 == 0 or completed == len(todo):
                    log(f"{completed}/{len(todo)} saved")
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return completed


def default_name(args: argparse.Namespace) -> str:
    name = f"{Path(args.data).stem}-{args.provider}-{args.mode}-retry{args.citation_retries}"
    if args.reasoning_effort:
        name += f"-effort_{args.reasoning_effort}"
    return name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Pinpoint overseer over a trace file.")
    names = [spec.name for spec in TRACE_FILES] + [f.name for f in LOCAL_FILES]
    parser.add_argument("--data", required=True, choices=names)
    parser.add_argument("--mode", default="two_slot", choices=MODES)
    parser.add_argument("--provider", default="baseten", choices=sorted(PROVIDERS))
    parser.add_argument("--citation-retries", type=int, default=DEFAULT_MAX_CITATION_RETRIES)
    parser.add_argument("--format-retries", type=int, default=DEFAULT_MAX_FORMAT_RETRIES)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--reasoning-effort", default=None)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None, help="only the first N traces")
    parser.add_argument("--name", default=None, help="run directory name")
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args(argv)

    path = data_path(args.data, args.data_dir)
    sha256 = pinned_sha256(args.data)
    local = any(f.name == args.data for f in LOCAL_FILES)
    if not path.exists():
        fix = "restore it from git" if local else "run python -m benchmark.fetch_data"
        print(f"error: {path} missing; {fix}", file=sys.stderr)
        return 1
    if sha256_of(path) != sha256:
        fix = "restore it from git" if local else "run python -m benchmark.fetch_data --force"
        print(f"error: {path} does not match the pinned hash; {fix}", file=sys.stderr)
        return 1

    provider = PROVIDERS[args.provider]
    config = RunConfig(
        data_file=args.data,
        data_sha256=sha256,
        provider=provider.name,
        model=provider.model,
        mode=args.mode,
        max_citation_retries=args.citation_retries,
        max_format_retries=args.format_retries,
        max_tokens=args.max_tokens,
        reasoning_effort=args.reasoning_effort,
        temperature=args.temperature,
        prompt_version=PROMPT_VERSION,
    )
    records = load_records(path)
    if args.limit is not None:
        records = records[: args.limit]
    run_dir = args.runs_dir / (args.name or default_name(args))

    try:
        load_env()
        client = ModelClient(
            provider,
            max_tokens=args.max_tokens,
            reasoning_effort=args.reasoning_effort,
            temperature=args.temperature,
        )
        print(
            f"run {run_dir.name}: {len(records)} traces, {provider.model}, mode {args.mode}, "
            f"citation retries {args.citation_retries}; "
            f"about ${len(records) * ESTIMATED_USD_PER_TRACE:.2f} if none are saved yet"
        )
        run(config, records, run_dir, client, workers=args.workers)
    except (RunError, ClientError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"done: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
