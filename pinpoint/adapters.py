"""Turn agent logs in common formats into Pinpoint's trace format.

Agents built on OpenAI-compatible chat APIs (including Meta's Model API and
Muse Spark Open's hosts) keep their history as a list of chat messages. This
converts such a log into the record ``Trace.from_dict`` reads:

* the first user message is the question;
* each assistant tool call is a step: the tool's name, its parsed arguments,
  and, as its result, the content of the tool message that answers it;
* assistant text is a ``message`` step whose argument is the text. What the
  agent says is its own action, so a false claim made in chat can be cited
  as the offense;
* a later user message is a ``user_message`` step whose content is its
  *result*, so the offense rule never lets it be cited as the agent's act;
* system and developer messages are skipped;
* the last assistant text that is not followed by tool calls is the claimed
  answer.

The output is a plain record; ``Trace.from_dict`` still validates it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pinpoint.trace import TraceError

MESSAGE_TOOL = "message"
USER_TOOL = "user_message"
UNMATCHED_RESULT_TOOL = "tool_result"


def _text(content: object) -> str:
    """Text of a message's content: a string, or a list of content parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        parts = []
        for part in content:
            if isinstance(part, Mapping):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif isinstance(part.get("type"), str):
                    parts.append(f"[{part['type']}]")
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return str(content)


def _arguments(raw: object) -> dict[str, Any]:
    """Tool-call arguments as a mapping. JSON that is not an object is kept whole."""
    if isinstance(raw, Mapping):
        return dict(raw)
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"arguments": raw}
        if isinstance(parsed, dict):
            return parsed
        return {"arguments": raw}
    return {"arguments": raw}


def trace_from_openai_messages(messages: object) -> dict[str, Any]:
    """Convert an OpenAI Chat Completions message list into a trace record.

    Raises ``TraceError`` if the log has no user message to serve as the
    question, or a message or tool call is malformed.
    """
    if not isinstance(messages, list):
        raise TraceError(f"messages must be a list, got {type(messages).__name__}")
    for i, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise TraceError(f"messages[{i}] must be an object, got {type(message).__name__}")

    first_user = next((i for i, m in enumerate(messages) if m.get("role") == "user"), None)
    if first_user is None:
        raise TraceError("the log has no user message to use as the question")
    question = _text(messages[first_user].get("content")).strip()
    if not question:
        raise TraceError("the first user message is empty")

    steps: list[dict[str, Any]] = []
    pending: dict[str, int] = {}  # tool_call_id -> index of the step awaiting its result
    claimed: str | None = None

    for i, message in enumerate(messages[first_user + 1 :], start=first_user + 1):
        role = message.get("role")
        if role == "assistant":
            text = _text(message.get("content")).strip()
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list):
                raise TraceError(f"messages[{i}].tool_calls must be a list")
            if text:
                steps.append({"tool": MESSAGE_TOOL, "args": {"text": text}, "result": None})
            for j, call in enumerate(calls):
                where = f"messages[{i}].tool_calls[{j}]"
                if not isinstance(call, Mapping):
                    raise TraceError(f"{where} must be an object")
                function = call.get("function")
                if not isinstance(function, Mapping):
                    raise TraceError(f"{where}.function must be an object")
                name = function.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise TraceError(f"{where}.function.name must be a non-empty string")
                steps.append(
                    {"tool": name, "args": _arguments(function.get("arguments")), "result": None}
                )
                call_id = call.get("id")
                if isinstance(call_id, str):
                    pending[call_id] = len(steps) - 1
            claimed = text if text and not calls else claimed
        elif role == "tool":
            text = _text(message.get("content"))
            call_id = message.get("tool_call_id")
            index = pending.pop(call_id, None) if isinstance(call_id, str) else None
            if index is None:
                steps.append({"tool": UNMATCHED_RESULT_TOOL, "args": {}, "result": text})
            else:
                steps[index]["result"] = text
        elif role == "user":
            steps.append({"tool": USER_TOOL, "args": {}, "result": _text(message.get("content"))})
        # system, developer and anything else: not part of what the agent did.

    record: dict[str, Any] = {"question": question, "steps": steps}
    if claimed is not None:
        record["claimed_answer"] = claimed
    return record
