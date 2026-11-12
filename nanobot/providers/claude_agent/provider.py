"""Claude models through the user's own Claude Code login."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from nanobot.providers.base import (
    LLMProvider,
    LLMResponse,
    ProviderCallContext,
    ProviderConversationState,
)
from nanobot.providers.claude_agent.options import resolve_effort
from nanobot.providers.claude_agent.session import ClaudeAgentError, ClientFactory
from nanobot.providers.claude_agent.sessions import ClaudeSessions, Request, matches

MODEL_PREFIXES = ("claude-agent/", "claude_agent/")
DEFAULT_MODEL = "claude-agent/opus"


def strip_model_prefix(model: str) -> str:
    for prefix in MODEL_PREFIXES:
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


class ClaudeAgentProvider(LLMProvider):
    """Run the user's installed Claude Code as the model caller.

    Claude Code makes the API request with whatever login it has (a Claude plan,
    an API key or a cloud account). nanobot never reads those credentials. Its
    tools reach Claude as MCP tools and still run in nanobot's own runner.
    """

    def __init__(
        self,
        *,
        workdir: Path,
        default_model: str = DEFAULT_MODEL,
        connection_env: dict[str, str] | None = None,
        provider_name: str = "claude_agent",
        client_factory: ClientFactory | None = None,
    ) -> None:
        super().__init__(api_key=None, api_base=None, provider_name=provider_name)
        self.default_model = default_model
        self._sessions = ClaudeSessions(
            workdir=workdir, connection_env=connection_env, client_factory=client_factory,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        return await self.chat_stream(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        provider_context: ProviderCallContext | None = None,
    ) -> LLMResponse:
        # Claude Code picks its own sampling settings and output limit.
        _ = max_tokens, temperature, on_tool_call_delta
        request = Request(
            key=provider_context.session_id if provider_context is not None else None,
            model=strip_model_prefix(model or self.default_model),
            effort=resolve_effort(reasoning_effort),
            messages=self._sanitize_empty_content(messages),
            tools=None if tool_choice == "none" else tools,
            state=provider_context.current_conversation_state if provider_context is not None else None,
            on_content_delta=on_content_delta,
            on_thinking_delta=on_thinking_delta,
            working_directory=provider_context.working_directory if provider_context is not None else None,
        )
        try:
            return await self._sessions.run(request)
        except ClaudeAgentError as exc:
            return LLMResponse(
                content=str(exc),
                finish_reason="error",
                error_kind=exc.kind,
                error_should_retry=False,
            )

    async def chat_with_context(
        self,
        *,
        provider_context: ProviderCallContext,
        **kwargs: Any,
    ) -> LLMResponse:
        return await self.chat_stream(**kwargs, provider_context=provider_context)

    async def chat_stream_with_context(
        self,
        *,
        provider_context: ProviderCallContext,
        **kwargs: Any,
    ) -> LLMResponse:
        return await self.chat_stream(**kwargs, provider_context=provider_context)

    def can_resume_conversation_state(
        self,
        state: ProviderConversationState,
        model: str | None = None,
    ) -> bool:
        return matches(state, strip_model_prefix(model or self.default_model))

    def get_default_model(self) -> str:
        return self.default_model

    async def aclose(self) -> None:
        await self._sessions.aclose()
