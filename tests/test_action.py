"""Static checks on the GitHub Action's metadata (action.yml)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

ACTION = Path(__file__).resolve().parents[1] / "action.yml"


@pytest.fixture
def action() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml")
    loaded: dict[str, Any] = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    return loaded


def steps(action: dict[str, Any]) -> list[dict[str, Any]]:
    return list(action["runs"]["steps"])


def test_inputs_reach_the_shell_only_through_env(action: dict[str, Any]) -> None:
    # A crafted input pasted into a script could inject shell commands.
    for step in steps(action):
        assert "${{" not in step.get("run", ""), step.get("name")


def test_by_default_the_action_installs_its_own_copy(action: dict[str, Any]) -> None:
    # So @v1 runs v1's code, never whatever main holds today.
    assert action["inputs"]["pinpoint-ref"]["default"] == ""
    install = next(s for s in steps(action) if s.get("name") == "Install Pinpoint")["run"]
    assert '"$GITHUB_ACTION_PATH"' in install
    assert 'if [ -n "$PINPOINT_REF" ]' in install


def test_actions_used_run_on_node_24(action: dict[str, Any]) -> None:
    uses = [s["uses"] for s in steps(action) if "uses" in s]
    assert uses == ["actions/setup-python@v6"]
