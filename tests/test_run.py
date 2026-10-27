"""Tests for benchmark.run. No network: models are scripted."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import LABEL_KEYS, OracleModel

from benchmark import run as run_mod
from benchmark.fetch_data import TRACE_FILES, TraceFile, sha256_of
from benchmark.run import RunConfig, RunError, git_info, load_records, read_done, run

GOOD: dict[str, Any] = {
    "What is the capital of France?": [{"verdict": "HONEST", "confidence": 0.9}],
    "Who wrote Hamlet?": [
        {"verdict": "REWARD_HACKER", "confidence": 0.9, "offense": {"step": 1, "quote": "Marlowe"}}
    ],
    "How tall is Everest?": [
        {"verdict": "LAZY", "confidence": 0.9, "offense": {"step": 2, "quote": "not sure"}}
    ],
    "When was the UN formed?": [
        {"verdict": "DECEIVER", "confidence": 0.9, "offense": {"step": 3, "quote": "1950"}}
    ],
}


def config_for(path: Path, **changes: Any) -> RunConfig:
    base = RunConfig(
        data_file=path.name,
        data_sha256=sha256_of(path),
        provider="fake",
        model="fake-model",
        mode="two_slot",
        max_citation_retries=2,
        max_format_retries=1,
        max_tokens=1000,
        reasoning_effort=None,
        temperature=None,
        prompt_version="test",
    )
    return replace(base, **changes)


def saved(run_dir: Path) -> list[dict[str, Any]]:
    lines = (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def quiet(_: str) -> None:
    pass


def all_keys(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in all_keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in all_keys(v)}
    return set()


# --- a full run ----------------------------------------------------------------


def test_run_saves_every_trace_and_a_manifest(
    dataset: tuple[Path, Path], tmp_path: Path, oracle: Callable[..., OracleModel]
) -> None:
    _, path = dataset
    run_dir = tmp_path / "runs" / "r1"
    config = config_for(path)
    count = run(config, load_records(path), run_dir, oracle(GOOD), workers=4, log=quiet)
    assert count == 4
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["config"] == config.to_dict()
    assert "created_at" in manifest
    entries = saved(run_dir)
    assert sorted(e["line"] for e in entries) == [0, 1, 2, 3]
    for entry in entries:
        assert set(entry) == {"line", "wall_s", "result"}
        assert entry["result"]["status"] in {"verified", "no_finding"}


def test_results_contain_no_labels(
    dataset: tuple[Path, Path], tmp_path: Path, oracle: Callable[..., OracleModel]
) -> None:
    _, path = dataset
    run(config_for(path), load_records(path), tmp_path / "r", oracle(GOOD), log=quiet)
    assert not all_keys(saved(tmp_path / "r")) & LABEL_KEYS


def test_the_model_never_sees_labels(
    dataset: tuple[Path, Path], tmp_path: Path, oracle: Callable[..., OracleModel]
) -> None:
    _, path = dataset
    model = oracle(GOOD)
    run(config_for(path), load_records(path), tmp_path / "r", model, log=quiet)
    sent = " ".join(model.sent)
    for leak in ("smoking_gun", "ground_truth", "policy_", "Shakespeare", "8849"):
        assert leak not in sent, leak


@pytest.mark.parametrize("workers", [1, 3, 8])
def test_worker_count_does_not_change_what_is_saved(
    workers: int,
    dataset: tuple[Path, Path],
    tmp_path: Path,
    oracle: Callable[..., OracleModel],
) -> None:
    _, path = dataset
    run(
        config_for(path),
        load_records(path),
        tmp_path / "r",
        oracle(GOOD),
        workers=workers,
        log=quiet,
    )
    assert sorted(e["line"] for e in saved(tmp_path / "r")) == [0, 1, 2, 3]


def test_workers_must_be_positive(dataset: tuple[Path, Path], tmp_path: Path) -> None:
    _, path = dataset
    with pytest.raises(ValueError, match="workers"):
        run(config_for(path), [], tmp_path / "r", OracleModel({}), workers=0, log=quiet)


# --- resuming ------------------------------------------------------------------------


def test_rerun_skips_everything_already_saved(
    dataset: tuple[Path, Path], tmp_path: Path, oracle: Callable[..., OracleModel]
) -> None:
    _, path = dataset
    records = load_records(path)
    run(config_for(path), records, tmp_path / "r", oracle(GOOD), log=quiet)
    untouched = oracle({})  # would fail on any call
    assert run(config_for(path), records, tmp_path / "r", untouched, log=quiet) == 0
    assert untouched.sent == []


def test_partial_run_resumes_where_it_stopped(
    dataset: tuple[Path, Path], tmp_path: Path, oracle: Callable[..., OracleModel]
) -> None:
    _, path = dataset
    records = load_records(path)
    run(config_for(path), records[:2], tmp_path / "r", oracle(GOOD), log=quiet)
    model = oracle(GOOD)
    assert run(config_for(path), records, tmp_path / "r", model, log=quiet) == 2
    assert len(model.sent) == 2
    assert sorted(e["line"] for e in saved(tmp_path / "r")) == [0, 1, 2, 3]


def test_line_cut_off_by_a_crash_is_redone(
    dataset: tuple[Path, Path], tmp_path: Path, oracle: Callable[..., OracleModel]
) -> None:
    _, path = dataset
    records = load_records(path)
    run_dir = tmp_path / "r"
    run(config_for(path), records[:2], run_dir, oracle(GOOD), log=quiet)
    with (run_dir / "results.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"line": 2, "wall_s": 1.0, "res')  # killed mid-write
    assert run(config_for(path), records, run_dir, oracle(GOOD), log=quiet) == 2
    assert sorted(e["line"] for e in saved(run_dir)) == [0, 1, 2, 3]


def test_different_configuration_is_refused(
    dataset: tuple[Path, Path], tmp_path: Path, oracle: Callable[..., OracleModel]
) -> None:
    _, path = dataset
    records = load_records(path)
    run(config_for(path), records[:1], tmp_path / "r", oracle(GOOD), log=quiet)
    changed = config_for(path, mode="single", max_citation_retries=0)
    with pytest.raises(RunError, match=r"different configuration \(max_citation_retries, mode\)"):
        run(changed, records, tmp_path / "r", oracle(GOOD), log=quiet)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('{"line": 0}\nnot json\n{"line": 1}\n', "line 2 is corrupt"),
        ('{"line": 0}\n{"line": 0}\n', "saved twice"),
        ('{"wall_s": 1}\n', "no valid trace line number"),
        ('{"line": true}\n', "no valid trace line number"),
        ("[1, 2]\n", "no valid trace line number"),
    ],
)
def test_damaged_results_file_stops_the_run(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "results.jsonl"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(RunError, match=message):
        read_done(path)


def test_read_done_on_a_missing_file(tmp_path: Path) -> None:
    assert read_done(tmp_path / "absent.jsonl") == set()


# --- failures ---------------------------------------------------------------------------


def test_a_bug_stops_the_run_and_keeps_saved_results(
    dataset: tuple[Path, Path], tmp_path: Path, oracle: Callable[..., OracleModel]
) -> None:
    _, path = dataset
    records = load_records(path)
    script = dict(GOOD)
    script["How tall is Everest?"] = [RuntimeError("bug in the stack")]
    with pytest.raises(RuntimeError, match="bug in the stack"):
        run(config_for(path), records, tmp_path / "r", oracle(script), workers=1, log=quiet)
    assert sorted(e["line"] for e in saved(tmp_path / "r")) == [0, 1]
    # Fixed, the same command finishes the job.
    assert run(config_for(path), records, tmp_path / "r", oracle(GOOD), log=quiet) == 2


# --- helpers ---------------------------------------------------------------------------


def test_load_records_numbers_physical_lines(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    path.write_text('{"a": 1}\n\n{"a": 2}\n', encoding="utf-8")
    assert load_records(path) == [(0, {"a": 1}), (2, {"a": 2})]


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_git_info_dirty_flag(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(
        repo,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "code.py")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "code")

    def dirty() -> bool:
        info = git_info(repo)
        assert info is not None
        return bool(info["dirty"])

    assert dirty() is False
    # Output of an earlier run must not mark the next run dirty (the bug that
    # flagged runs 2-4 of the first ablation).
    (repo / "results" / "runs" / "r1").mkdir(parents=True)
    (repo / "results" / "runs" / "r1" / "results.jsonl").write_text("{}\n", encoding="utf-8")
    assert dirty() is False
    # An edited tracked file is dirty.
    (repo / "code.py").write_text("x = 2\n", encoding="utf-8")
    assert dirty() is True
    _git(repo, "checkout", "--", "code.py")
    assert dirty() is False
    # A new untracked module outside results/ is dirty: it could change behaviour.
    (repo / "new_module.py").write_text("y = 1\n", encoding="utf-8")
    assert dirty() is True


def test_git_info(tmp_path: Path) -> None:
    info = git_info()
    assert info is not None
    assert len(info["commit"]) == 40
    assert isinstance(info["dirty"], bool)
    assert git_info(tmp_path) is None  # not a repository


# --- the command line ---------------------------------------------------------------------


@pytest.fixture
def cli(
    dataset: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """Point the CLI at the synthetic data, and make sure it can never build a
    real client or read the real .env (which would spend credits)."""
    data_dir, path = dataset
    monkeypatch.setattr(
        run_mod, "TRACE_FILES", (TraceFile("eval_traces.jsonl", sha256_of(path), 4),)
    )
    monkeypatch.setattr(run_mod, "load_env", lambda: None)
    monkeypatch.setattr(
        run_mod, "ModelClient", lambda *a, **k: OracleModel({k: list(v) for k, v in GOOD.items()})
    )
    return data_dir, tmp_path / "runs"


def test_cli_runs_with_a_default_name(cli: tuple[Path, Path]) -> None:
    data_dir, runs_dir = cli
    argv = ["--data", "eval_traces.jsonl", "--data-dir", str(data_dir), "--runs-dir", str(runs_dir)]
    assert run_mod.main(argv) == 0
    run_dir = runs_dir / "eval_traces-baseten-two_slot-retry2"
    assert len(saved(run_dir)) == 4
    config = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["config"]
    assert config["model"] == "meta-models/muse-spark-open-1026"
    assert config["mode"] == "two_slot"


def test_cli_limit_takes_the_first_traces(cli: tuple[Path, Path]) -> None:
    data_dir, runs_dir = cli
    argv = ["--data", "eval_traces.jsonl", "--data-dir", str(data_dir),
            "--runs-dir", str(runs_dir), "--limit", "2", "--name", "smoke"]  # fmt: skip
    assert run_mod.main(argv) == 0
    assert sorted(e["line"] for e in saved(runs_dir / "smoke")) == [0, 1]


def test_cli_refuses_missing_data(cli: tuple[Path, Path], tmp_path: Path) -> None:
    argv = ["--data", "eval_traces.jsonl", "--data-dir", str(tmp_path / "nowhere")]
    assert run_mod.main(argv) == 1


def test_cli_refuses_data_that_does_not_match_its_pin(
    cli: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir, runs_dir = cli
    monkeypatch.setattr(run_mod, "TRACE_FILES", (TraceFile("eval_traces.jsonl", "0" * 64, 4),))
    argv = ["--data", "eval_traces.jsonl", "--data-dir", str(data_dir), "--runs-dir", str(runs_dir)]
    assert run_mod.main(argv) == 1
    assert not runs_dir.exists()


def test_cli_reports_a_missing_key(
    cli: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The real client, with .env loading still disabled by the fixture and the
    # key unset: construction fails before any request could be made.
    from pinpoint.client import ModelClient

    data_dir, runs_dir = cli
    monkeypatch.setattr(run_mod, "ModelClient", ModelClient)
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    argv = ["--data", "eval_traces.jsonl", "--data-dir", str(data_dir), "--runs-dir", str(runs_dir)]
    assert run_mod.main(argv) == 1
    assert "BASETEN_API_KEY is not set" in capsys.readouterr().err


# --- trace files kept in the repository ------------------------------------------------


@pytest.fixture
def local(
    cli: tuple[Path, Path], dataset: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Register the synthetic data as a repo-local file named local.jsonl."""
    _, path = dataset
    pinned = run_mod.LocalFile("local.jsonl", path, sha256_of(path))
    monkeypatch.setattr(run_mod, "LOCAL_FILES", (pinned,))
    return path


def test_cli_runs_a_local_file_whatever_the_data_dir(
    cli: tuple[Path, Path], local: Path, tmp_path: Path
) -> None:
    from benchmark.grade import grade_run

    _, runs_dir = cli
    nowhere = tmp_path / "nowhere"
    argv = ["--data", "local.jsonl", "--data-dir", str(nowhere), "--runs-dir", str(runs_dir)]
    assert run_mod.main(argv) == 0
    run_dir = runs_dir / "local-baseten-two_slot-retry2"
    assert len(saved(run_dir)) == 4
    config = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["config"]
    assert config["data_file"] == "local.jsonl"
    assert config["data_sha256"] == sha256_of(local)
    # The grader finds it the same way.
    assert grade_run(run_dir, nowhere)["complete"] is True


def test_cli_refuses_a_local_file_that_does_not_match_its_pin(
    cli: tuple[Path, Path],
    local: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, runs_dir = cli
    monkeypatch.setattr(
        run_mod, "LOCAL_FILES", (run_mod.LocalFile("local.jsonl", local, "0" * 64),)
    )
    assert run_mod.main(["--data", "local.jsonl", "--runs-dir", str(runs_dir)]) == 1
    assert "restore it from git" in capsys.readouterr().err
    assert not runs_dir.exists()


def test_cli_refuses_a_missing_local_file(
    cli: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, runs_dir = cli
    gone = run_mod.LocalFile("local.jsonl", tmp_path / "gone.jsonl", "0" * 64)
    monkeypatch.setattr(run_mod, "LOCAL_FILES", (gone,))
    assert run_mod.main(["--data", "local.jsonl", "--runs-dir", str(runs_dir)]) == 1
    assert "missing; restore it from git" in capsys.readouterr().err


def test_every_trace_file_has_exactly_one_pin() -> None:
    names = [spec.name for spec in TRACE_FILES] + [f.name for f in run_mod.LOCAL_FILES]
    assert len(names) == len(set(names))
    for name in names:
        assert len(run_mod.pinned_sha256(name)) == 64
    with pytest.raises(KeyError):
        run_mod.pinned_sha256("unknown.jsonl")
