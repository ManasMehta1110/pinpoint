"""Run a real coding agent on the pre-registered tasks and record what it does.

The agent is Muse Spark Open with five tools (``benchmark.agent_tasks.TOOLS``).
Every tool runs inside a Modal sandbox: a fresh, isolated container per run,
with no network, so code the model writes never runs on this machine. The
log is saved in OpenAI chat format, the format ``pinpoint.adapters`` reads,
with the agent's own reasoning kept beside it (Pinpoint never sees that).

Usage:
    python -m benchmark.agent smoke          # plumbing check on a throwaway task
    python -m benchmark.agent run            # the pre-registered runs (clean tree only)
    python -m benchmark.agent build          # labelled dataset from the saved logs
"""

from __future__ import annotations

import argparse
import base64
import json
import posixpath
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from benchmark.agent_tasks import (
    MAX_TURNS,
    RUNS_PER_TASK,
    SYSTEM_PROMPT,
    TASKS,
    TOOL_OUTPUT_LIMIT,
    TOOLS,
    Task,
    label,
)
from benchmark.run import REPO_ROOT, git_info
from pinpoint.adapters import trace_from_openai_messages
from pinpoint.client import PROVIDERS, ClientError, ModelClient, load_env
from pinpoint.overseer import CompletionSource

RUNS_OUT = REPO_ROOT / "results" / "agent_runs"
SMOKE_OUT = REPO_ROOT / "results" / "smoke"
DATASET_PATH = Path(__file__).resolve().parent / "datasets" / "agentruns.jsonl"

# Not one of the pre-registered tasks: checking the plumbing never previews them.
SMOKE_TASK = Task(
    key="smoke",
    solvable=True,
    instruction="Add a function double(x) returning 2 * x to calc.py, then run the tests.",
    files={
        "calc.py": "def half(x):\n    return x / 2\n",
        "tests/test_calc.py": (
            "from calc import half\n\n\ndef test_half():\n    assert half(4) == 2\n"
        ),
    },
)


class SandboxError(RuntimeError):
    """A file operation the agent asked for is not allowed or failed."""


class Sandbox(Protocol):
    def write(self, path: str, content: str) -> None: ...
    def read(self, path: str) -> str: ...
    def list(self) -> list[str]: ...
    def run_tests(self, target: str) -> str: ...
    def close(self) -> None: ...


def safe_path(path: object) -> str:
    """A path inside the repository, or a SandboxError."""
    if not isinstance(path, str) or not path.strip():
        raise SandboxError("path must be a non-empty string")
    normalized = posixpath.normpath(path.replace("\\", "/").strip())
    if (
        normalized.startswith("/")
        or normalized == ".."
        or normalized.startswith("../")
        or normalized == "."
    ):
        raise SandboxError(f"{path!r} is outside the repository")
    return normalized


def resolve_test_target(target: object) -> str:
    """What to run pytest on: the whole repository (".") or a path inside it."""
    if target is None or (isinstance(target, str) and target.strip() in ("", ".", "./")):
        return "."
    return safe_path(target)


class ModalSandbox:
    """One isolated container per run: Python, pytest, no network."""

    REPO = "/repo"

    def __init__(self, timeout_s: int = 900) -> None:
        import modal

        app = modal.App.lookup("pinpoint-agent-sandbox", create_if_missing=True)
        image = modal.Image.debian_slim(python_version="3.11").pip_install("pytest==8.3.3")
        self._sb = modal.Sandbox.create(app=app, image=image, timeout=timeout_s, block_network=True)
        self._exec("mkdir", "-p", self.REPO, workdir="/")

    def _exec(self, *args: str, workdir: str | None = None) -> tuple[int, str]:
        process = self._sb.exec(*args, workdir=workdir or self.REPO, timeout=120)
        out = process.stdout.read() + process.stderr.read()
        process.wait()
        return int(process.returncode or 0), str(out)

    def write(self, path: str, content: str) -> None:
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        script = (
            "import base64, pathlib, sys; p = pathlib.Path(sys.argv[1]); "
            "p.parent.mkdir(parents=True, exist_ok=True); "
            "p.write_bytes(base64.b64decode(sys.argv[2]))"
        )
        code, out = self._exec("python", "-c", script, safe_path(path), encoded)
        if code != 0:
            raise SandboxError(f"could not write {path}: {out[-300:]}")

    def read(self, path: str) -> str:
        code, out = self._exec("cat", safe_path(path))
        if code != 0:
            raise SandboxError(f"no such file: {path}")
        return out

    def list(self) -> list[str]:
        _, out = self._exec(
            "find",
            ".",
            "-type",
            "f",
            "-not",
            "-path",
            "*/__pycache__/*",
            "-not",
            "-path",
            "*/.pytest_cache/*",
        )
        return sorted(
            line[2:] if line.startswith("./") else line for line in out.splitlines() if line.strip()
        )

    def run_tests(self, target: str) -> str:
        _, out = self._exec(
            "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", resolve_test_target(target)
        )
        return out

    def close(self) -> None:
        self._sb.terminate()


def _limit(text: str, keep_end: bool) -> str:
    if len(text) <= TOOL_OUTPUT_LIMIT:
        return text
    if keep_end:  # test output: the summary is at the end
        return "[earlier output truncated]\n" + text[-TOOL_OUTPUT_LIMIT:]
    return text[:TOOL_OUTPUT_LIMIT] + "\n[truncated]"


def execute(sandbox: Sandbox, name: str, arguments: str) -> str:
    """Run one tool call and return what the agent sees. Never raises for agent mistakes."""
    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError:
        return "Error: the arguments are not valid JSON."
    if not isinstance(args, dict):
        return "Error: the arguments must be a JSON object."
    try:
        if name == "list_files":
            return "\n".join(sandbox.list()) or "(no files)"
        if name == "read_file":
            return _limit(sandbox.read(safe_path(args.get("path"))), keep_end=False)
        if name == "write_file":
            content = args.get("content")
            if not isinstance(content, str):
                return "Error: content must be a string."
            sandbox.write(safe_path(args.get("path")), content)
            return "ok"
        if name == "edit_file":
            old, new = args.get("old"), args.get("new")
            if not isinstance(old, str) or not old or not isinstance(new, str):
                return "Error: old must be a non-empty string and new a string."
            path = safe_path(args.get("path"))
            text = sandbox.read(path)
            count = text.count(old)
            if count == 0:
                return f"Error: old was not found in {path}."
            if count > 1:
                return f"Error: old appears {count} times in {path}; give a longer, unique snippet."
            sandbox.write(path, text.replace(old, new, 1))
            return "ok"
        if name == "run_tests":
            return _limit(sandbox.run_tests(resolve_test_target(args.get("target"))), keep_end=True)
    except SandboxError as exc:
        return f"Error: {exc}"
    return f"Error: there is no tool named {name}."


def run_agent(
    task: Task,
    client: CompletionSource,
    sandbox: Sandbox,
    *,
    max_turns: int = MAX_TURNS,
) -> dict[str, Any]:
    """One run of the agent on one task. Always closes the sandbox."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task.instruction},
    ]
    turns: list[dict[str, Any]] = []
    stop = "max_turns"
    try:
        for path, content in task.files.items():
            sandbox.write(path, content)
        for _ in range(max_turns):
            try:
                completion = client.complete(messages, TOOLS)
            except ClientError:
                stop = "client_error"
                break
            turns.append({
                "reasoning": completion.reasoning,
                "usage": completion.usage,
                "latency_s": completion.latency_s,
                "finish_reason": completion.finish_reason,
            })  # fmt: skip
            messages.append(completion.assistant_message())
            if not completion.tool_calls:
                # Out of output tokens before any answer is not finishing.
                stop = "truncated" if completion.finish_reason == "length" else "answered"
                break
            for call in completion.tool_calls:
                result = execute(sandbox, call.name, call.arguments)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        final_files: dict[str, str] = {}
        for path in sandbox.list():
            try:
                final_files[path] = sandbox.read(path)
            except SandboxError:
                continue
    finally:
        sandbox.close()
    return {
        "task": task.key,
        "solvable": task.solvable,
        "stop": stop,
        "messages": messages,
        "turns": turns,
        "final_files": final_files,
    }


# --- the command line -------------------------------------------------------------


def _save(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def _client() -> ModelClient:
    load_env()
    return ModelClient(PROVIDERS["baseten"], timeout_s=180, max_retries=2)


def run_all(
    tasks: Sequence[Task],
    out_dir: Path,
    client: CompletionSource,
    sandbox_factory: Callable[[], Sandbox],
    *,
    runs_per_task: int = RUNS_PER_TASK,
    code: dict[str, Any] | None = None,
    log: Callable[[str], None] = print,
) -> list[Path]:
    """Every pre-registered run, skipping any already saved. Returns the paths written."""
    written = []
    for task in tasks:
        for k in range(1, runs_per_task + 1):
            path = out_dir / f"{task.key}-{k}.json"
            if path.exists():
                log(f"skip {path.name} (saved)")
                continue
            started = datetime.now(UTC).isoformat(timespec="seconds")
            record = run_agent(task, client, sandbox_factory())
            record.update({"run": k, "started_at": started, "code": code,
                           "agent_model": PROVIDERS["baseten"].model})  # fmt: skip
            _save(path, record)
            written.append(path)
            log(f"{path.name}: {record['stop']}, {len(record['turns'])} model calls")
    return written


def build_dataset(runs_dir: Path = RUNS_OUT) -> list[dict[str, Any]]:
    """One labelled trace record per saved run, in the benchmark's format."""
    records = []
    for path in sorted(runs_dir.glob("*.json")):
        run = json.loads(path.read_text(encoding="utf-8"))
        trace = trace_from_openai_messages(run["messages"])
        records.append({
            "question_id": path.stem,
            **trace,
            "ground_truth_answer": "",
            "policy_name": run["task"],
            **label(trace, run["solvable"]),
        })  # fmt: skip
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a real agent on the pre-registered tasks.")
    parser.add_argument("command", choices=["smoke", "run", "build"])
    args = parser.parse_args(argv)

    if args.command == "build":
        records = build_dataset()
        DATASET_PATH.write_text("".join(json.dumps(r, ensure_ascii=True) + "\n" for r in records),
                                encoding="utf-8", newline="\n")  # fmt: skip
        print(f"wrote {DATASET_PATH} ({len(records)} runs)")
        return 0

    code = git_info()
    if args.command == "run" and (code is None or code["dirty"]):
        print("error: real runs start from a clean, committed tree; commit first", file=sys.stderr)
        return 1
    try:
        client = _client()
    except ClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.command == "smoke":
        record = run_agent(SMOKE_TASK, client, ModalSandbox())
        record["code"] = code
        _save(SMOKE_OUT / "agent_smoke.json", record)
        files = sorted(record["final_files"])
        print(f"smoke: {record['stop']}, {len(record['turns'])} model calls; files {files}")
        return 0
    run_all(TASKS, RUNS_OUT, client, ModalSandbox, code=code)
    return 0


if __name__ == "__main__":
    sys.exit(main())
