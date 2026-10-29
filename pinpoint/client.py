"""One client for every Meta model Pinpoint uses.

Every provider speaks the OpenAI Chat Completions protocol, so a single
client covers them; a ``Provider`` records only what differs:

* ``baseten``: Muse Spark Open, Meta's open-weights model, served by Baseten.
* ``fireworks``: the same open-weights model, served by Fireworks. Used to
  check that results do not depend on one host, and as a fallback.
* ``meta``: Muse Spark, through the Meta Model API.

Every call returns a ``Completion`` holding the full raw response alongside
the parts Pinpoint needs (tool calls, readable reasoning when the provider
returns it, token usage, finish reason, latency, rate-limit headers), so a
run can be saved and audited later. Nothing shown in a demo or README is ever
written by hand; it comes from these records.

The API key is read from the environment and never appears in a request
record, a saved result, a log line or an error message.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

import httpx
import openai
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Provider:
    """What differs between providers. Everything else is shared."""

    name: str
    base_url: str
    api_key_env: str
    model: str
    # Reasoning models on the Meta Model API take max_completion_tokens;
    # Baseten documents max_tokens. Reasoning tokens count against either.
    max_tokens_param: str
    # For a dedicated deployment, the variable holding its model id, which
    # replaces ``model`` once the environment is loaded.
    model_env: str | None = None
    # Extra request fields this provider always sends (e.g. routing).
    extra_body: Mapping[str, Any] | None = None


PROVIDERS: dict[str, Provider] = {
    "baseten": Provider(
        name="baseten",
        base_url="https://inference.baseten.co/v1",
        api_key_env="BASETEN_API_KEY",
        # The hackathon key works only with this exact id.
        model="meta-models/muse-spark-open-1026",
        max_tokens_param="max_tokens",
    ),
    "fireworks": Provider(
        name="fireworks",
        base_url="https://api.fireworks.ai/inference/v1",
        api_key_env="FIREWORKS_API_KEY",
        model="accounts/fireworks/models/muse-spark-open",
        max_tokens_param="max_tokens",
    ),
    "meta": Provider(
        name="meta",
        base_url="https://api.meta.ai/v1",
        api_key_env="MODEL_API_KEY",
        model="muse-spark-1.3",
        max_tokens_param="max_completion_tokens",
    ),
    # Muse Glimmer 30B has no serverless endpoint: it runs on a Fireworks
    # deployment billed per GPU-second, addressed by that deployment's id.
    "glimmer": Provider(
        name="glimmer",
        base_url="https://api.fireworks.ai/inference/v1",
        api_key_env="FIREWORKS_API_KEY",
        model="accounts/fireworks/models/muse-glimmer-30b",
        max_tokens_param="max_tokens",
        model_env="GLIMMER_MODEL",
    ),
    # Muse Glimmer 30B, serverless through OpenRouter. Pinned to one host and
    # precision with no fallback, so every audit runs on the same weights.
    "openrouter": Provider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        model="meta/muse-glimmer-30b",
        max_tokens_param="max_tokens",
        extra_body={
            "provider": {
                "order": ["DeepInfra"],
                "allow_fallbacks": False,
                "quantizations": ["bf16"],
            }
        },
    ),
}

# Reasoning tokens count toward the output limit, and Baseten's default (4096)
# can be exhausted before the answer starts. Set high and check finish_reason.
DEFAULT_MAX_TOKENS = 16384
DEFAULT_TIMEOUT_S = 300.0
DEFAULT_MAX_RETRIES = 4


class ClientError(RuntimeError):
    """A call could not be made or did not return a usable response."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text, exactly as the model produced it


@dataclass(frozen=True)
class Completion:
    """One model response, with everything needed to save and audit it."""

    provider: str
    model: str
    request: dict[str, Any]  # what was sent, without the key
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    reasoning: str | None  # readable reasoning, if the provider returns it
    finish_reason: str | None
    usage: dict[str, Any]
    latency_s: float
    rate_limits: dict[str, str]
    raw: dict[str, Any] = field(repr=False)

    @property
    def truncated(self) -> bool:
        """True if the output limit cut the response off."""
        return self.finish_reason == "length"

    def assistant_message(self) -> dict[str, Any]:
        """The assistant turn to append when continuing the conversation.

        Rebuilt from the standard fields only, so provider extras such as
        reasoning text are never echoed back to an API that may reject them.
        An assistant turn with neither text nor tool calls gets empty text,
        since APIs reject a null content with no tool calls.
        """
        content = self.content if self.content is not None or self.tool_calls else ""
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return message

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "request": self.request,
            "content": self.content,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in self.tool_calls
            ],
            "reasoning": self.reasoning,
            "finish_reason": self.finish_reason,
            "truncated": self.truncated,
            "usage": self.usage,
            "latency_s": self.latency_s,
            "rate_limits": self.rate_limits,
            "raw": self.raw,
        }


def load_env(path: Path | None = None) -> None:
    """Load keys from the repo's ``.env`` into the environment, without
    overriding variables that are already set. Call from entry points only."""
    from dotenv import load_dotenv

    load_dotenv(path or REPO_ROOT / ".env", override=False)


def get_provider(provider: str | Provider) -> Provider:
    if isinstance(provider, Provider):
        return provider
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise ValueError(
            f"unknown provider {provider!r}; choose from {', '.join(sorted(PROVIDERS))}"
        ) from None


def resolve_provider(provider: str | Provider) -> Provider:
    """The provider with a deployment's model id filled in from the environment.

    Raises ``ClientError`` if the provider needs one and it is not set.
    """
    found = get_provider(provider)
    if found.model_env is None:
        return found
    model = os.environ.get(found.model_env, "").strip()
    if not model:
        raise ClientError(
            f"{found.model_env} is not set; add your {found.name} deployment's model id to .env"
        )
    return replace(found, model=model, model_env=None)


class ModelClient:
    """Calls one provider's chat completions endpoint with tools."""

    def __init__(
        self,
        provider: str | Provider,
        *,
        api_key: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        reasoning_effort: str | None = None,
        temperature: float | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.provider = resolve_provider(provider)
        key = api_key if api_key is not None else os.environ.get(self.provider.api_key_env, "")
        key = key.strip()
        if not key:
            raise ClientError(
                f"{self.provider.api_key_env} is not set; add it to .env "
                f"to use the {self.provider.name} provider"
            )
        if max_tokens < 1:
            raise ValueError(f"max_tokens must be at least 1, got {max_tokens}")
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self.temperature = temperature
        self._client = openai.OpenAI(
            base_url=self.provider.base_url,
            api_key=key,
            timeout=timeout_s,
            max_retries=max_retries,
            http_client=http_client,
        )

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> Completion:
        """Send one request. Raises ``ClientError`` once retries are exhausted."""
        body: dict[str, Any] = {self.provider.max_tokens_param: self.max_tokens}
        body.update(self.provider.extra_body or {})
        if self.reasoning_effort is not None:
            body["reasoning_effort"] = self.reasoning_effort
        if self.temperature is not None:
            body["temperature"] = self.temperature
        request = {
            "model": self.provider.model,
            "messages": [dict(m) for m in messages],
            "tools": [dict(t) for t in tools],
            "tool_choice": "auto",
            **body,
        }

        started = time.perf_counter()
        try:
            raw_response = self._client.chat.completions.with_raw_response.create(
                model=self.provider.model,
                messages=cast(list[ChatCompletionMessageParam], request["messages"]),
                tools=cast(list[ChatCompletionToolParam], request["tools"]),
                tool_choice="auto",
                extra_body=body,
            )
            response = raw_response.parse()
        except openai.AuthenticationError as exc:
            raise ClientError(
                f"{self.provider.name} rejected the API key (HTTP {exc.status_code}); "
                f"check {self.provider.api_key_env} in .env"
            ) from None
        except openai.APIStatusError as exc:
            raise ClientError(
                f"{self.provider.name} returned HTTP {exc.status_code}: {_error_text(exc)}"
            ) from None
        except openai.APITimeoutError:
            raise ClientError(f"{self.provider.name} timed out after retries") from None
        except openai.APIConnectionError as exc:
            raise ClientError(
                f"could not reach {self.provider.name}: {exc.__cause__ or exc}"
            ) from None
        latency_s = time.perf_counter() - started

        if not response.choices:
            raise ClientError(f"{self.provider.name} returned no choices")
        choice = response.choices[0]
        raw = response.model_dump(mode="json")
        message_raw = raw["choices"][0].get("message") or {}

        tool_calls = tuple(
            ToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments)
            for call in (choice.message.tool_calls or ())
            if call.type == "function"
        )
        reasoning = message_raw.get("reasoning_content") or message_raw.get("reasoning")
        return Completion(
            provider=self.provider.name,
            model=self.provider.model,
            request=request,
            content=choice.message.content,
            tool_calls=tool_calls,
            reasoning=reasoning if isinstance(reasoning, str) and reasoning else None,
            finish_reason=choice.finish_reason,
            usage=raw.get("usage") or {},
            latency_s=round(latency_s, 3),
            rate_limits={
                k.lower(): v
                for k, v in raw_response.headers.items()
                if k.lower().startswith("x-ratelimit")
            },
            raw=raw,
        )


def _error_text(exc: openai.APIStatusError) -> str:
    """The provider's error message, falling back to the status line."""
    body = exc.body
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("message"), str):
            return str(error["message"])
        if isinstance(body.get("message"), str):
            return str(body["message"])
    return str(exc.message)
