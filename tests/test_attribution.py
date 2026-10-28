"""Tests for pinpoint.attribution: prompt, parsing, the strict check, and the loop."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from pinpoint import attribution as attr
from pinpoint.attribution import (
    Attributor,
    check_attribution,
    parse_attribution,
    system_prompt,
    user_message,
)
from pinpoint.audit import AuditParseError, Evidence
from pinpoint.client import ClientError, Completion, ToolCall
from pinpoint.trace import Trace
from pinpoint.verifier import IssueCode

# Pinned: saved attribution runs depend on exactly what the model was shown.
PINNED_FINGERPRINT = "8a74216b96fe"

TRACE = Trace.from_dict(
    {
        "question": "How many even addresses are there?",
        "steps": [
            {"tool": "Excel_Expert", "args": {"message": "Count rows where the number is odd."}},
            {"tool": "Computer_terminal", "args": {}, "result": "exitcode: 0\nCode output: 4"},
            {"tool": "Verification_Expert", "args": {"message": "The answer is 4. TERMINATE"}},
        ],
    }
)

GOOD = {
    "mistake": {"step": 1, "quote": "where the number is odd"},
    "evidence": {"step": 2, "quote": "Code output: 4"},
    "reason": "Counted odd, not even.",
}


def completion(
    *calls: tuple[str, Any], text: str | None = None, truncated: bool = False
) -> Completion:
    return Completion(
        provider="fake",
        model="fake",
        request={},
        content=text,
        tool_calls=tuple(
            ToolCall(f"c{i}", name, a if isinstance(a, str) else json.dumps(a))
            for i, (name, a) in enumerate(calls)
        ),
        reasoning=None,
        finish_reason="length" if truncated else ("tool_calls" if calls else "stop"),
        usage={"prompt_tokens": 100, "completion_tokens": 20},
        latency_s=0.0,
        rate_limits={},
        raw={},
    )


class Script:
    def __init__(self, *replies: Completion | Exception) -> None:
        self.replies = list(replies)
        self.sent: list[list[dict[str, Any]]] = []

    def complete(
        self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]
    ) -> Completion:
        self.sent.append([dict(m) for m in messages])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def submit(args: Any) -> Completion:
    return completion(("submit_attribution", args))


# --- the prompt ------------------------------------------------------------------------


def test_the_prompt_is_pinned() -> None:
    assert attr.fingerprint() == PINNED_FINGERPRINT, (
        "attribution wording changed: bump ATTRIBUTION_VERSION"
    )


def test_the_prompt_never_names_an_answer_key() -> None:
    text = system_prompt().lower()
    for leak in ("last step", "final step", "first step", "mistake_step", "ground truth"):
        assert leak not in text


def test_the_answer_line_appears_only_when_asked() -> None:
    assert "correct answer" not in user_message(TRACE)
    assert "The correct answer to the task is: 2" in user_message(TRACE, "2")


# --- parsing and checking -----------------------------------------------------------------


def test_parsing_is_lenient() -> None:
    a = parse_attribution(
        {"mistake": {"step": "Step 1", "quote": "odd"}, "evidence": {}, "reason": "  x "}
    )
    assert a.mistake == Evidence(1, "odd") and a.evidence is None and a.reason == "x"


@pytest.mark.parametrize("bad", ["not json", "[1]"])
def test_unreadable_arguments_raise(bad: str) -> None:
    with pytest.raises(AuditParseError):
        parse_attribution(bad)


def test_a_real_mistake_and_evidence_verify() -> None:
    assert check_attribution(TRACE, parse_attribution(GOOD)).status == "verified"


def test_the_mistake_cannot_be_environment_output() -> None:
    check = check_attribution(
        TRACE, parse_attribution({"mistake": {"step": 2, "quote": "Code output: 4"}})
    )
    assert check.status == "unverified"
    assert check.mistake is not None and IssueCode.QUOTE_FROM_TOOL_RESULT in check.mistake.codes


def test_a_missing_mistake_is_unverified_with_a_reason() -> None:
    check = check_attribution(TRACE, parse_attribution({"reason": "x"}))
    assert check.status == "unverified" and "mistake citation is required" in check.feedback()


def test_bad_evidence_fails_the_whole_answer() -> None:
    answer = {**GOOD, "evidence": {"step": 2, "quote": "the agent lied"}}
    assert check_attribution(TRACE, parse_attribution(answer)).status == "unverified"


# --- the loop ---------------------------------------------------------------------------------


def test_verified_on_the_first_try() -> None:
    result = Attributor(Script(submit(GOOD))).attribute(TRACE)
    assert result.status == "verified" and len(result.attempts) == 1
    assert result.to_dict()["citation_retries"] == 0


def test_an_unverified_citation_is_sent_back_and_corrected() -> None:
    wrong = {**GOOD, "mistake": {"step": 2, "quote": "Code output: 4"}}
    script = Script(submit(wrong), submit(GOOD))
    result = Attributor(script).attribute(TRACE)
    assert result.status == "verified" and result.to_dict()["citation_retries"] == 1
    feedback = script.sent[1][-1]
    assert (
        feedback["role"] == "tool"
        and "returned by the Computer_terminal tool" in feedback["content"]
    )


def test_the_citation_budget_is_respected() -> None:
    wrong = {**GOOD, "mistake": {"step": 3, "quote": "not in the log"}}
    result = Attributor(Script(*[submit(wrong)] * 3), max_citation_retries=2).attribute(TRACE)
    assert result.status == "unverified" and len(result.attempts) == 3


def test_format_problems_are_nudged_on_their_own_budget() -> None:
    script = Script(completion(text="I think step 1."), submit(GOOD))
    result = Attributor(script).attribute(TRACE)
    assert result.status == "verified" and result.attempts[0].problem == "no_tool_call"
    assert script.sent[1][-1] == {"role": "user", "content": attr._NO_TOOL_CALL}


def test_unknown_tools_and_extra_calls_each_get_a_reply() -> None:
    script = Script(
        completion(("submit_audit", {})),
        completion(("submit_attribution", GOOD), ("submit_attribution", GOOD)),
    )
    result = Attributor(script).attribute(TRACE)
    assert result.status == "verified" and result.attempts[0].problem == "unknown_tool"


def test_provider_errors_and_truncation_are_recorded_not_raised() -> None:
    assert Attributor(Script(ClientError("down"))).attribute(TRACE).status == "failed"
    assert Attributor(Script(completion(truncated=True))).attribute(TRACE).status == "failed"


def test_the_answer_is_passed_through() -> None:
    script = Script(submit(GOOD))
    Attributor(script).attribute(TRACE, answer="2")
    assert "The correct answer to the task is: 2" in script.sent[0][1]["content"]


def test_budgets_must_be_non_negative_integers() -> None:
    for bad in (-1, True, 1.5):
        with pytest.raises(ValueError):
            Attributor(Script(), max_citation_retries=bad)  # type: ignore[arg-type]
