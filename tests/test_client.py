"""Tests for pinpoint.client.

These drive the real OpenAI SDK against a fake network (httpx.MockTransport),
so they exercise the actual requests, headers, retries and error handling the
SDK produces, with no internet access and no credits spent.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from pinpoint.audit import audit_tool
from pinpoint.client import (
    DEFAULT_MAX_TOKENS,
    PROVIDERS,
    ClientError,
    Completion,
    ModelClient,
    Provider,
    get_provider,
    load_env,
    resolve_provider,
)
from pinpoint.prompt import messages
from pinpoint.trace import Trace

# A planted key. If it ever appears in a saved record or an error message,
# the key-leak tests fail.
SECRET = "sk-SENTINEL-must-never-appear-anywhere-0123456789"

TRACE = Trace.from_dict(
    {
        "question": "When was the UN formed?",
        "steps": [
            {"tool": "read_document", "args": {"doc_id": "d1"}, "result": "Formed in 1945."},
            {"tool": "submit_answer", "args": {"answer": "1950"}, "result": None},
        ],
    }
)
MESSAGES = messages(TRACE)
TOOLS = [audit_tool()]
TOOL_ARGS = json.dumps(
    {"verdict": "DECEIVER", "confidence": 0.9, "offense": {"step": 2, "quote": "1950"}}
)


def completion_json(
    *,
    finish_reason: str | None = "tool_calls",
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning: dict[str, str] | None = None,
    choices: bool = True,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is None:
        tool_calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "submit_audit", "arguments": TOOL_ARGS},
            }
        ]
    if tool_calls:
        message["tool_calls"] = tool_calls
    message.update(
        reasoning if reasoning is not None else {"reasoning_content": "Step 2 says 1950."}
    )
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "served-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}]
        if choices
        else [],
        "usage": {"prompt_tokens": 800, "completion_tokens": 1200, "total_tokens": 2000},
    }


def ok(body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(200, json=body or completion_json(), headers=headers)


def error(status: int, message: str = "nope") -> httpx.Response:
    # retry-after-ms keeps the SDK's retry wait to 1ms so tests stay fast.
    return httpx.Response(
        status, json={"error": {"message": message}}, headers={"retry-after-ms": "1"}
    )


class FakeServer:
    """Answers each request with the next queued response (or raises it)."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def body(self, index: int = 0) -> dict[str, Any]:
        parsed: dict[str, Any] = json.loads(self.requests[index].content)
        return parsed


def client_for(server: FakeServer, provider: str | Provider = "baseten", **kw: Any) -> ModelClient:
    kw.setdefault("max_retries", 2)
    return ModelClient(
        provider,
        api_key=SECRET,
        http_client=httpx.Client(transport=httpx.MockTransport(server)),
        **kw,
    )


# --- the request ----------------------------------------------------------------


def test_baseten_request_shape() -> None:
    server = FakeServer(ok())
    client_for(server, "baseten").complete(MESSAGES, TOOLS)
    request = server.requests[0]
    assert str(request.url) == "https://inference.baseten.co/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    body = server.body()
    assert body["model"] == "meta-models/muse-spark-open-1026"
    assert body["messages"] == MESSAGES
    assert body["tools"] == TOOLS
    assert body["tool_choice"] == "auto"
    assert body["max_tokens"] == DEFAULT_MAX_TOKENS
    for absent in ("max_completion_tokens", "reasoning_effort", "temperature"):
        assert absent not in body


def test_a_deployment_needs_its_model_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GLIMMER_MODEL", raising=False)
    with pytest.raises(ClientError, match="GLIMMER_MODEL is not set"):
        ModelClient("glimmer", api_key=SECRET)


def test_a_deployment_is_addressed_by_its_own_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLIMMER_MODEL", "  accounts/me/deployedModels/glimmer-abc  ")
    server = FakeServer(ok())
    client = client_for(server, "glimmer")
    client.complete(MESSAGES, TOOLS)
    assert str(server.requests[0].url) == "https://api.fireworks.ai/inference/v1/chat/completions"
    assert server.body()["model"] == "accounts/me/deployedModels/glimmer-abc"
    assert client.provider.model == "accounts/me/deployedModels/glimmer-abc"


def test_openrouter_is_pinned_to_one_host_and_precision() -> None:
    server = FakeServer(ok())
    client_for(server, "openrouter").complete(MESSAGES, TOOLS)
    assert str(server.requests[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    body = server.body()
    assert body["model"] == "meta/muse-glimmer-30b"
    assert body["provider"] == {
        "order": ["DeepInfra"],
        "allow_fallbacks": False,
        "quantizations": ["bf16"],
    }
    assert body["tools"] == TOOLS and body["tool_choice"] == "auto"


def test_other_providers_send_no_routing() -> None:
    server = FakeServer(ok())
    client_for(server, "baseten").complete(MESSAGES, TOOLS)
    assert "provider" not in server.body()


def test_serverless_providers_ignore_deployment_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLIMMER_MODEL", "accounts/me/deployedModels/glimmer-abc")
    for name in ("baseten", "fireworks", "meta", "openrouter"):
        assert resolve_provider(name) is PROVIDERS[name]


def test_meta_request_uses_its_own_url_model_and_token_param() -> None:
    server = FakeServer(ok())
    client_for(server, "meta").complete(MESSAGES, TOOLS)
    assert str(server.requests[0].url) == "https://api.meta.ai/v1/chat/completions"
    body = server.body()
    assert body["model"] == "muse-spark-1.3"
    assert body["max_completion_tokens"] == DEFAULT_MAX_TOKENS
    assert "max_tokens" not in body


def test_fireworks_request_uses_its_own_url_and_model() -> None:
    server = FakeServer(ok())
    client_for(server, "fireworks").complete(MESSAGES, TOOLS)
    assert str(server.requests[0].url) == "https://api.fireworks.ai/inference/v1/chat/completions"
    assert server.requests[0].headers["authorization"] == f"Bearer {SECRET}"
    body = server.body()
    assert body["model"] == "accounts/fireworks/models/muse-spark-open"
    assert body["max_tokens"] == DEFAULT_MAX_TOKENS
    assert "max_completion_tokens" not in body


def test_optional_settings_are_sent_when_set() -> None:
    server = FakeServer(ok())
    client_for(server, reasoning_effort="high", temperature=0.0, max_tokens=999).complete(
        MESSAGES, TOOLS
    )
    body = server.body()
    assert body["reasoning_effort"] == "high"
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 999


# --- the response --------------------------------------------------------------


def test_response_is_parsed() -> None:
    result = client_for(FakeServer(ok())).complete(MESSAGES, TOOLS)
    assert result.provider == "baseten"
    assert result.model == "meta-models/muse-spark-open-1026"
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert (call.id, call.name, call.arguments) == ("call_1", "submit_audit", TOOL_ARGS)
    assert result.reasoning == "Step 2 says 1950."
    assert result.finish_reason == "tool_calls"
    assert not result.truncated
    assert result.usage["completion_tokens"] == 1200
    assert result.latency_s >= 0
    assert result.raw["choices"][0]["message"]["tool_calls"][0]["id"] == "call_1"


def test_truncation_is_flagged() -> None:
    result = client_for(FakeServer(ok(completion_json(finish_reason="length")))).complete(
        MESSAGES, TOOLS
    )
    assert result.truncated


@pytest.mark.parametrize(
    ("reasoning", "expected"),
    [
        ({"reasoning_content": "via reasoning_content"}, "via reasoning_content"),
        ({"reasoning": "via reasoning"}, "via reasoning"),
        ({"reasoning_content": ""}, None),
        ({"reasoning": ""}, None),
        ({"reasoning_content": "", "reasoning": ""}, None),
        ({"reasoning_content": "", "reasoning": "fallback"}, "fallback"),
        ({}, None),
    ],
)
def test_reasoning_extraction(reasoning: dict[str, str], expected: str | None) -> None:
    body = completion_json(reasoning=reasoning)
    assert client_for(FakeServer(ok(body))).complete(MESSAGES, TOOLS).reasoning == expected


def test_plain_text_answer_has_no_tool_calls() -> None:
    body = completion_json(tool_calls=[], content="VERDICT: DECEIVER", finish_reason="stop")
    result = client_for(FakeServer(ok(body))).complete(MESSAGES, TOOLS)
    assert result.tool_calls == ()
    assert result.content == "VERDICT: DECEIVER"


def test_multiple_tool_calls_keep_their_order() -> None:
    calls = [
        {"id": f"c{i}", "type": "function", "function": {"name": f"f{i}", "arguments": "{}"}}
        for i in range(3)
    ]
    result = client_for(FakeServer(ok(completion_json(tool_calls=calls)))).complete(MESSAGES, TOOLS)
    assert [c.id for c in result.tool_calls] == ["c0", "c1", "c2"]


def test_rate_limit_headers_are_captured() -> None:
    headers = {
        "X-RateLimit-Limit-Requests": "120",
        "x-ratelimit-remaining-tokens": "1999000",
        "content-type": "application/json",
        "x-request-id": "abc",
    }
    result = client_for(FakeServer(ok(headers=headers))).complete(MESSAGES, TOOLS)
    assert result.rate_limits == {
        "x-ratelimit-limit-requests": "120",
        "x-ratelimit-remaining-tokens": "1999000",
    }


def test_no_choices_is_an_error() -> None:
    with pytest.raises(ClientError, match="no choices"):
        client_for(FakeServer(ok(completion_json(choices=False)))).complete(MESSAGES, TOOLS)


def test_assistant_message_carries_only_standard_fields() -> None:
    result = client_for(FakeServer(ok())).complete(MESSAGES, TOOLS)
    message = result.assistant_message()
    assert set(message) == {"role", "content", "tool_calls"}
    assert message["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "submit_audit", "arguments": TOOL_ARGS},
        }
    ]
    assert "Step 2 says 1950." not in json.dumps(message)


def test_assistant_message_without_tool_calls() -> None:
    body = completion_json(tool_calls=[], content="hello", finish_reason="stop")
    message = client_for(FakeServer(ok(body))).complete(MESSAGES, TOOLS).assistant_message()
    assert message == {"role": "assistant", "content": "hello"}


def test_empty_assistant_turn_gets_empty_text_not_null() -> None:
    body = completion_json(tool_calls=[], content=None, finish_reason="stop")
    message = client_for(FakeServer(ok(body))).complete(MESSAGES, TOOLS).assistant_message()
    assert message == {"role": "assistant", "content": ""}


def test_tool_call_turn_keeps_null_content() -> None:
    message = client_for(FakeServer(ok())).complete(MESSAGES, TOOLS).assistant_message()
    assert message["content"] is None


def test_completion_serializes_to_json() -> None:
    result = client_for(FakeServer(ok())).complete(MESSAGES, TOOLS)
    restored = json.loads(json.dumps(result.to_dict()))
    assert restored["tool_calls"][0]["name"] == "submit_audit"
    assert restored["truncated"] is False
    assert restored["request"]["model"] == "meta-models/muse-spark-open-1026"


# --- retries and errors ------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_errors_are_retried(status: int) -> None:
    server = FakeServer(error(status), ok())
    result = client_for(server, max_retries=2).complete(MESSAGES, TOOLS)
    assert result.tool_calls
    assert len(server.requests) == 2


def test_retries_are_exhausted() -> None:
    server = FakeServer(error(503), error(503), error(503))
    with pytest.raises(ClientError, match="HTTP 503"):
        client_for(server, max_retries=2).complete(MESSAGES, TOOLS)
    assert len(server.requests) == 3


def test_bad_key_is_not_retried_and_names_the_variable() -> None:
    server = FakeServer(error(401, "invalid api key"))
    with pytest.raises(ClientError, match=r"check BASETEN_API_KEY in \.env"):
        client_for(server).complete(MESSAGES, TOOLS)
    assert len(server.requests) == 1


def test_client_error_reports_the_providers_message() -> None:
    server = FakeServer(error(400, "tool schema invalid: offense"))
    with pytest.raises(ClientError, match="HTTP 400: tool schema invalid: offense"):
        client_for(server).complete(MESSAGES, TOOLS)
    assert len(server.requests) == 1


def test_connection_failure_is_retried_then_reported() -> None:
    server = FakeServer(*(httpx.ConnectError("refused") for _ in range(3)))
    with pytest.raises(ClientError, match="could not reach baseten"):
        client_for(server, max_retries=2).complete(MESSAGES, TOOLS)
    assert len(server.requests) == 3


def test_timeout_is_reported() -> None:
    server = FakeServer(*(httpx.ReadTimeout("slow") for _ in range(3)))
    with pytest.raises(ClientError, match="timed out"):
        client_for(server, max_retries=2).complete(MESSAGES, TOOLS)


# --- configuration ----------------------------------------------------------------


def test_missing_key_fails_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    with pytest.raises(ClientError, match="BASETEN_API_KEY is not set"):
        ModelClient("baseten")


def test_blank_key_counts_as_missing() -> None:
    with pytest.raises(ClientError, match="is not set"):
        ModelClient("baseten", api_key="   ")


def test_key_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", f"  {SECRET}\n")
    server = FakeServer(ok())
    ModelClient("meta", http_client=httpx.Client(transport=httpx.MockTransport(server))).complete(
        MESSAGES, TOOLS
    )
    assert server.requests[0].headers["authorization"] == f"Bearer {SECRET}"


def test_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unknown provider 'openai'"):
        get_provider("openai")


def test_custom_provider_is_accepted() -> None:
    custom = Provider("local", "http://localhost:8000/v1", "LOCAL_KEY", "m", "max_tokens")
    server = FakeServer(ok())
    client_for(server, custom).complete(MESSAGES, TOOLS)
    assert str(server.requests[0].url) == "http://localhost:8000/v1/chat/completions"


def test_max_tokens_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        ModelClient("baseten", api_key=SECRET, max_tokens=0)


def test_provider_table() -> None:
    assert PROVIDERS["baseten"].model == "meta-models/muse-spark-open-1026"
    assert PROVIDERS["meta"].model == "muse-spark-1.3"
    assert PROVIDERS["fireworks"].model == "accounts/fireworks/models/muse-spark-open"
    assert PROVIDERS["openrouter"].model == "meta/muse-glimmer-30b"
    assert {p.api_key_env for p in PROVIDERS.values()} == {
        "BASETEN_API_KEY", "FIREWORKS_API_KEY", "MODEL_API_KEY", "OPENROUTER_API_KEY",
    }  # fmt: skip
    assert all(name == p.name for name, p in PROVIDERS.items())


def test_load_env_never_overrides_existing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PINPOINT_TEST_A=from_file\nPINPOINT_TEST_B=from_file\n", encoding="utf-8")
    monkeypatch.setenv("PINPOINT_TEST_A", "from_shell")
    monkeypatch.delenv("PINPOINT_TEST_B", raising=False)
    load_env(env_file)
    import os

    assert os.environ["PINPOINT_TEST_A"] == "from_shell"
    assert os.environ["PINPOINT_TEST_B"] == "from_file"
    monkeypatch.delenv("PINPOINT_TEST_B")


# --- the key never leaks -------------------------------------------------------------


def _calls() -> list[tuple[str, Callable[[], Any]]]:
    return [
        ("success", lambda: client_for(FakeServer(ok())).complete(MESSAGES, TOOLS)),
        ("401", lambda: client_for(FakeServer(error(401))).complete(MESSAGES, TOOLS)),
        ("400", lambda: client_for(FakeServer(error(400))).complete(MESSAGES, TOOLS)),
        (
            "503",
            lambda: client_for(FakeServer(*(error(503) for _ in range(3)))).complete(
                MESSAGES, TOOLS
            ),
        ),
        (
            "connect",
            lambda: client_for(
                FakeServer(*(httpx.ConnectError("refused") for _ in range(3)))
            ).complete(MESSAGES, TOOLS),
        ),
    ]


@pytest.mark.parametrize("name", [name for name, _ in _calls()])
def test_key_never_appears_in_records_or_errors(name: str) -> None:
    call = dict(_calls())[name]
    try:
        result = call()
    except ClientError as exc:
        assert SECRET not in str(exc)
        assert SECRET not in repr(exc)
        assert exc.__cause__ is None  # no chained exception carrying request details
        return
    assert isinstance(result, Completion)
    assert SECRET not in json.dumps(result.to_dict())
    assert SECRET not in repr(result)
