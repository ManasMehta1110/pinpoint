"""What the page renders: a trace and an audit, as plain JSON with highlight spans.

The gallery (built offline from saved runs) and the live audit endpoint both
go through these functions, so a saved result and a live one are shown the
same way. Audits are taken as dicts, the form both saved results and
``OverseerResult.to_dict()`` use.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.highlight import locate_in_step
from pinpoint.audit import Mode, has_offense_rule, has_two_slots
from pinpoint.trace import Trace
from pinpoint.verifier import Scope


def trace_view(trace: Trace) -> dict[str, Any]:
    """The trace, with each step's text exactly as citations are checked against it.

    ``action_len`` marks where the agent's action ends and the tool's result
    begins in ``text``, so the page can style the two differently.
    """
    return {
        "question": trace.question,
        "claimed_answer": trace.claimed_answer,
        "steps": [
            {
                "index": step.index,
                "tool": step.tool,
                "text": step.text(),
                "action_len": len(step.action_text()),
            }
            for step in trace.steps
        ],
    }


def _slot_view(
    trace: Trace,
    cited: Mapping[str, Any] | None,
    check: Mapping[str, Any] | None,
    scope: Scope,
) -> dict[str, Any] | None:
    if cited is None:
        return None
    step, quote = cited.get("step"), cited.get("quote")
    status = (check or {}).get("status")
    span = None
    found = trace.get_step(step) if isinstance(step, int) else None
    # Only a verified citation is highlighted: an unverified one is shown as
    # text, never as if it pointed at something real.
    if found is not None and status == "verified":
        located = locate_in_step(found, quote, scope)
        if located is not None:
            span = {"start": located[0], "end": located[1]}
    return {
        "step": step,
        "quote": quote,
        "status": status,
        "issues": [i["message"] for i in (check or {}).get("issues", [])],
        "span": span,
    }


def audit_view(
    trace: Trace,
    audit: Mapping[str, Any] | None,
    verification: Mapping[str, Any] | None,
    mode: Mode,
) -> dict[str, Any] | None:
    """One audit for display. In single-slot modes the one citation is ``offense``."""
    if audit is None:
        return None
    check = verification or {}
    offense_scope: Scope = "action" if has_offense_rule(mode) else "step"
    return {
        "mode": mode,
        "verdict": audit.get("verdict"),
        "confidence": audit.get("confidence"),
        "rationale": audit.get("rationale"),
        "status": check.get("status"),
        "offense": _slot_view(trace, audit.get("offense"), check.get("offense"), offense_scope),
        "contradicted_by": _slot_view(
            trace, audit.get("contradicted_by"), check.get("contradicted_by"), "step"
        )
        if has_two_slots(mode)
        else None,
    }
