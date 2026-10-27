"""Agent traces as Pinpoint sees them: what the agent did, never the answers.

A trace is the observable record of an agent's work: the question it was
given, each tool call it made with its arguments and result, and the answer it
claimed. This module holds only that. Any other key in an input record, such
as a benchmark's ground-truth labels, is ignored on load, so there is nowhere
for the overseer to read it from.

Steps are numbered from 1. That numbering is used in the rendered prompt the
overseer reads and in every citation it makes, so "step 3" means the same
step everywhere.

The text the overseer reads for a step and the text a citation is checked
against both come from ``Step.text``. Keeping one source means a quote the
model genuinely copied can never fail verification because of formatting.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

NO_RESULT = "(no result)"
NO_STEPS = "(the agent made no tool calls)"
_INDENT = "    "


class TraceError(ValueError):
    """An input record is not a valid trace."""


def _format_value(value: Any) -> str:
    """Render an argument or result value as readable, deterministic text."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


@dataclass(frozen=True)
class Step:
    """One tool call made by the agent. ``index`` is 1-based."""

    index: int
    tool: str
    args: Mapping[str, Any]
    result: Any = None

    def text(self) -> str:
        """Return the step's content, without its "Step N" header.

        This is what citations are checked against. ``render`` shows the same
        lines with a header and indentation, so the two agree up to whitespace.
        """
        lines = [self.tool]
        lines.extend(f"{name}: {_format_value(value)}" for name, value in self.args.items())
        lines.append("-> " + (NO_RESULT if self.result is None else _format_value(self.result)))
        return "\n".join(lines)

    def render(self) -> str:
        """Return the step as shown to the overseer, headed "Step N: <tool>"."""
        first, *rest = self.text().split("\n")
        return "\n".join([f"Step {self.index}: {first}", *(_INDENT + line for line in rest)])


@dataclass(frozen=True)
class Trace:
    """The observable record of one agent run."""

    question: str
    steps: tuple[Step, ...]
    claimed_answer: str | None = None
    trace_id: str | None = None

    def get_step(self, index: int) -> Step | None:
        """Return the step with this 1-based index, or ``None`` if there is none.

        Only a true ``int`` is accepted. ``True`` is rejected rather than read
        as 1, and indices below 1 are rejected rather than counting from the
        end the way Python sequences do.
        """
        if isinstance(index, bool) or not isinstance(index, int):
            return None
        if 1 <= index <= len(self.steps):
            return self.steps[index - 1]
        return None

    def render(self) -> str:
        """Return the full trace as the overseer reads it."""
        body = "\n\n".join(step.render() for step in self.steps) if self.steps else NO_STEPS
        parts = [f"Question: {self.question}", body]
        if self.claimed_answer is not None:
            parts.append(f"Claimed answer: {self.claimed_answer}")
        return "\n\n".join(parts)

    @classmethod
    def from_dict(cls, record: Mapping[str, Any]) -> Trace:
        """Build a trace from a JSON-style record, keeping only observable fields.

        Expected shape::

            {"question": str,
             "steps": [{"tool": str, "args": {str: any}, "result": any}, ...],
             "claimed_answer": str | None,     # optional
             "trace_id" or "question_id": str} # optional

        Every other key is ignored. Raises ``TraceError`` naming the offending
        field if the record does not fit this shape.
        """
        if not isinstance(record, Mapping):
            raise TraceError(f"trace must be a mapping, got {type(record).__name__}")

        question = record.get("question")
        if not isinstance(question, str) or not question.strip():
            raise TraceError("question must be a non-empty string")

        raw_steps = record.get("steps")
        if not isinstance(raw_steps, list):
            raise TraceError(f"steps must be a list, got {type(raw_steps).__name__}")
        steps = tuple(_parse_step(raw, position) for position, raw in enumerate(raw_steps, 1))

        claimed_answer = record.get("claimed_answer")
        if claimed_answer is not None and not isinstance(claimed_answer, str):
            raise TraceError("claimed_answer must be a string if present")

        trace_id = record.get("trace_id", record.get("question_id"))
        if trace_id is not None and not isinstance(trace_id, str):
            raise TraceError("trace_id must be a string if present")

        return cls(
            question=question,
            steps=steps,
            claimed_answer=claimed_answer,
            trace_id=trace_id,
        )


def _parse_step(raw: Any, index: int) -> Step:
    where = f"steps[{index - 1}]"
    if not isinstance(raw, Mapping):
        raise TraceError(f"{where} must be a mapping, got {type(raw).__name__}")

    tool = raw.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        raise TraceError(f"{where}.tool must be a non-empty string")
    if "\n" in tool or "\r" in tool:
        raise TraceError(f"{where}.tool must be a single line")

    args = raw.get("args", {})
    if args is None:
        args = {}
    if not isinstance(args, Mapping):
        raise TraceError(f"{where}.args must be a mapping, got {type(args).__name__}")
    for name in args:
        if not isinstance(name, str) or not name or "\n" in name or "\r" in name:
            raise TraceError(f"{where}.args has an invalid argument name: {name!r}")

    # Deep copies: arguments and results are often nested lists or dicts, and
    # a later change to the caller's record must not alter what was audited.
    return Step(
        index=index,
        tool=tool,
        args=copy.deepcopy(dict(args)),
        result=copy.deepcopy(raw.get("result")),
    )
