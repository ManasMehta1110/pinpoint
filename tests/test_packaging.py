"""Packaging invariants that the rest of the project relies on."""

import ast
import importlib.util
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "pinpoint"

# The pinpoint package audits traces it has never seen labels for. Anything
# that would let it read ground truth is forbidden here: GroundingBench's
# modules, its grader, and the label fields of a benchmark record. Only
# benchmark/ may touch these.
FORBIDDEN_MODULES = {"env", "doers"}
FORBIDDEN_NAMES = {
    "compute_reward",
    "smoking_gun_step",
    "smoking_gun_keyword",
    "policy_class",
    "ground_truth_answer",
}


def _package_sources() -> list[Path]:
    sources = sorted(PACKAGE_DIR.rglob("*.py"))
    assert sources, f"no Python sources found under {PACKAGE_DIR}"
    return sources


def test_pinpoint_is_importable() -> None:
    assert importlib.util.find_spec("pinpoint") is not None


def test_pinpoint_never_imports_grader_modules() -> None:
    for path in _package_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                # Relative imports (level > 0) stay inside pinpoint.
                roots = {node.module.split(".")[0]} if node.module and node.level == 0 else set()
            else:
                continue
            leaked = roots & FORBIDDEN_MODULES
            assert not leaked, f"{path.name}:{node.lineno} imports {sorted(leaked)}"


def test_pinpoint_never_references_label_fields() -> None:
    for path in _package_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                found = node.id
            elif isinstance(node, ast.Attribute):
                found = node.attr
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                found = node.value
            else:
                continue
            assert found not in FORBIDDEN_NAMES, (
                f"{path.name}:{getattr(node, 'lineno', '?')} references label field {found!r}"
            )
