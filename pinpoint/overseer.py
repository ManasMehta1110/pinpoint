"""The overseer loop: ask for an audit, verify it, and if a citation fails,
tell the model exactly why and ask again.

Each response is handled in one of four ways:

* It is a usable audit whose citations verify (or a HONEST verdict, which
  needs none): done.
* It is a usable audit whose citations do not verify: the verifier's feedback
  goes back as the result of the ``submit_audit`` call, and the model tries
  again. This is the verify-and-retry intervention, limited by
  ``max_citation_retries``. Setting it to 0 gives the no-retry ablation.
* It cannot be read as an audit (plain text, a different tool, arguments that
  are not a JSON object): a short format nudge goes back instead. This is
  plumbing, kept on a separate budget (``max_format_retries``) so that the
  no-retry ablation still gets the same chance to produce a readable answer.
* It cannot continue (the provider failed, or the output limit cut it off):
  the loop stops. Retrying a truncated response would truncate again.

Every attempt is kept, with its raw completion, so a result can be audited
end to end. The verdict of every attempt is kept too: a model told its
citation failed may switch to HONEST, which needs no citation. That can be an
honest correction or an escape, and the benchmark reports it separately.

All text sent to the model comes from ``pinpoint.prompt``, so it is covered by
``PROMPT_VERSION``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pinpoint.audit import (
    AUDIT_TOOL_NAME,
    Audit,
    AuditParseError,
    AuditVerification,
    Mode,
    audit_tool,
    check_mode,
    parse_audit,
    verify_audit,
)
from pinpoint.client import ClientError, Completion
from pinpoint.prompt import (
    PROMPT_VERSION,
    bad_arguments_message,
    ignored_call_message,
    messages,
    no_tool_call_message,
    retry_message,
    unknown_tool_message,
)
from pinpoint.trace import Trace

DEFAULT_MAX_CITATION_RETRIES = 2
DEFAULT_MAX_FORMAT_RETRIES = 1

OverseerStatus = Literal["verified", "unverified", "no_finding", "failed"]


class CompletionSource(Protocol):
    """Anything that can answer a chat request with tools, like ``ModelClient``."""

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> Completion: ...


@dataclass(frozen=True)
class Attempt:
    """One request to the model and what came of it."""

    number: int  # 1-based
    completion: Completion | None  # None if the provider call failed
    audit: Audit | None  # None if no readable audit came back
    verification: AuditVerification | None
    # Why this attempt did not end the loop successfully, or None if it did:
    # citations_unverified, no_tool_call, unknown_tool, bad_arguments,
    # truncated, client_error.
    problem: str | None = None
    detail: str | None = None
    # Messages sent back to the model after this attempt (empty if it was last).
    followup: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        completion = None
        if self.completion is not None:
            completion = self.completion.to_dict()
            # The conversation sent is reconstructible from PROMPT_VERSION, the
            # trace and the earlier attempts' followups; storing it on every
            # attempt would repeat the system prompt hundreds of times per run.
            completion["request"] = {
                k: v for k, v in completion["request"].items() if k not in ("messages", "tools")
            }
        return {
            "number": self.number,
            "problem": self.problem,
            "detail": self.detail,
            "audit": None if self.audit is None else self.audit.to_dict(),
            "verification": None if self.verification is None else self.verification.to_dict(),
            "followup": list(self.followup),
            "completion": completion,
        }


@dataclass(frozen=True)
class OverseerResult:
    """Everything the overseer did for one trace."""

    status: OverseerStatus
    mode: Mode
    prompt_version: str
    attempts: tuple[Attempt, ...]

    @property
    def final(self) -> Attempt | None:
        """The last attempt that produced a readable audit, if any."""
        return next((a for a in reversed(self.attempts) if a.audit is not None), None)

    @property
    def audit(self) -> Audit | None:
        return None if self.final is None else self.final.audit

    @property
    def verification(self) -> AuditVerification | None:
        return None if self.final is None else self.final.verification

    @property
    def verdict_history(self) -> tuple[object, ...]:
        """The verdict of every attempt that produced a readable audit, in order."""
        return tuple(a.audit.verdict for a in self.attempts if a.audit is not None)

    @property
    def verdict_changed(self) -> bool:
        return len(set(map(str, self.verdict_history))) > 1

    @property
    def citation_retries(self) -> int:
        """How many times verifier feedback was sent back."""
        return sum(1 for a in self.attempts if a.problem == "citations_unverified" and a.followup)

    @property
    def format_retries(self) -> int:
        """How many times a format nudge was sent back."""
        return sum(
            1
            for a in self.attempts
            if a.problem in ("no_tool_call", "unknown_tool", "bad_arguments") and a.followup
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "mode": self.mode,
            "prompt_version": self.prompt_version,
            "verdict_history": list(self.verdict_history),
            "verdict_changed": self.verdict_changed,
            "citation_retries": self.citation_retries,
            "format_retries": self.format_retries,
            "attempts": [a.to_dict() for a in self.attempts],
        }


class Overseer:
    """Audits traces with a model, verifying every citation it makes."""

    def __init__(
        self,
        client: CompletionSource,
        *,
        mode: Mode = "two_slot",
        max_citation_retries: int = DEFAULT_MAX_CITATION_RETRIES,
        max_format_retries: int = DEFAULT_MAX_FORMAT_RETRIES,
    ) -> None:
        for name, value in (
            ("max_citation_retries", max_citation_retries),
            ("max_format_retries", max_format_retries),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
        self.client = client
        self.mode = check_mode(mode)
        self.max_citation_retries = max_citation_retries
        self.max_format_retries = max_format_retries

    def audit(self, trace: Trace) -> OverseerResult:
        """Audit one trace. Never raises for model or provider failures; they
        are recorded in the result."""
        conversation: list[dict[str, Any]] = [dict(m) for m in messages(trace, self.mode)]
        tools = [audit_tool(self.mode)]
        citation_budget = self.max_citation_retries
        format_budget = self.max_format_retries
        attempts: list[Attempt] = []

        while True:
            number = len(attempts) + 1
            try:
                completion = self.client.complete(conversation, tools)
            except ClientError as exc:
                attempts.append(Attempt(number, None, None, None, "client_error", str(exc)))
                break

            if completion.truncated:
                attempts.append(Attempt(number, completion, None, None, "truncated"))
                break

            submit_calls = [c for c in completion.tool_calls if c.name == AUDIT_TOOL_NAME]

            # Read the response. Exactly one of `audit` or `problem` ends up set.
            audit: Audit | None = None
            primary_id = ""
            problem: str | None = None
            detail: str | None = None
            replies: dict[str, str] = {}
            if not completion.tool_calls:
                problem = "no_tool_call"
            elif not submit_calls:
                problem = "unknown_tool"
                detail = ", ".join(c.name for c in completion.tool_calls)
                replies = {c.id: unknown_tool_message(c.name) for c in completion.tool_calls}
            else:
                primary_id = submit_calls[0].id
                try:
                    audit = parse_audit(submit_calls[0].arguments, self.mode)
                except AuditParseError as exc:
                    problem, detail = "bad_arguments", str(exc)
                    replies = _replies(completion, primary_id, bad_arguments_message(str(exc)))

            # Responses that cannot be read as an audit: nudge, on the format budget.
            if audit is None:
                problem = problem or "no_tool_call"
                if format_budget == 0:
                    attempts.append(Attempt(number, completion, None, None, problem, detail))
                    break
                format_budget -= 1
                followup = _followup(completion, replies, no_tool_call_message())
                attempts.append(Attempt(number, completion, None, None, problem, detail, followup))
                conversation.extend([completion.assistant_message(), *followup])
                continue

            verification = verify_audit(trace, audit, mode=self.mode)
            if verification.status != "unverified":
                attempts.append(Attempt(number, completion, audit, verification))
                break
            if citation_budget == 0:
                attempts.append(
                    Attempt(number, completion, audit, verification, "citations_unverified")
                )
                break
            citation_budget -= 1
            followup = _followup(
                completion, _replies(completion, primary_id, retry_message(verification)), ""
            )
            attempts.append(
                Attempt(
                    number, completion, audit, verification, "citations_unverified", None, followup
                )
            )
            conversation.extend([completion.assistant_message(), *followup])

        return OverseerResult(_status(attempts), self.mode, PROMPT_VERSION, tuple(attempts))


def _replies(completion: Completion, primary_id: str, primary_reply: str) -> dict[str, str]:
    """Answer every tool call: the primary one with ``primary_reply``, any
    others as ignored. The protocol requires a reply to every call."""
    return {
        call.id: primary_reply if call.id == primary_id else ignored_call_message()
        for call in completion.tool_calls
    }


def _followup(
    completion: Completion, replies: dict[str, str], text_reply: str
) -> tuple[dict[str, Any], ...]:
    """The messages that follow an assistant turn: one tool message per tool
    call, or a user message if the turn had no tool calls."""
    if not completion.tool_calls:
        return ({"role": "user", "content": text_reply},)
    return tuple(
        {"role": "tool", "tool_call_id": call.id, "content": replies[call.id]}
        for call in completion.tool_calls
    )


def _status(attempts: Sequence[Attempt]) -> OverseerStatus:
    """The outcome of the last readable audit, or failed if there was none."""
    for attempt in reversed(attempts):
        if attempt.verification is not None:
            return attempt.verification.status
    return "failed"
