"""One long-running Claude Code process and its message stream."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from loguru import logger

from nanobot.providers.claude_agent.messages import Step, assistant_error
from nanobot.providers.claude_agent.options import ProcessConfig, build_options
from nanobot.providers.claude_agent.tools import PendingCalls, ToolSet, make_hooks, make_server

# Older binaries silently ignore verbatim prompts and may expand user text as commands.
MIN_VERSION = (2, 1, 248)
_INTERRUPT_DRAIN_S = 30.0


class ClaudeAgentError(RuntimeError):
    """Claude Code could not start or finish a request."""

    def __init__(self, message: str, *, kind: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind


def version_tuple(value: str) -> tuple[int, ...]:
    head = value.split("-", 1)[0].split("+", 1)[0]
    return tuple(int(part) for part in head.split(".") if part.isdigit())


def check_version(version: str | None) -> None:
    if version and version_tuple(version) >= MIN_VERSION:
        return
    found = f"Claude Code {version} is too old" if version else "Claude Code did not report its version"
    required = ".".join(str(part) for part in MIN_VERSION)
    raise ClaudeAgentError(f"{found}; {required} or later is required. Run `claude update`.", kind="version")


@dataclass
class StepResult:
    kind: Literal["tools", "result"]
    is_error: bool = False
    error: str | None = None
    error_kind: str | None = None
    stop_reason: str | None = None


DeltaCallback = Callable[[str], Awaitable[None]] | None
# Builds the SDK client for a set of options; tests pass one backed by a fake Claude Code.
ClientFactory = Callable[[Any], Any]

_ERROR_KINDS = {
    "rate_limit": "rate_limit",
    "authentication_failed": "authentication",
    "billing_error": "billing",
    "invalid_request": "invalid_request",
    "server_error": "server_error",
}


class ClaudeProcess:
    """A Claude Code process that serves one Claude conversation."""

    def __init__(
        self,
        config: ProcessConfig,
        tools: ToolSet,
        *,
        cwd: Path,
        resume: str | None = None,
        resume_at: str | None = None,
        fork: bool = False,
        persist: bool = True,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory
        self.tools = tools
        self.persist = persist
        self.pending = PendingCalls()
        self.lock = asyncio.Lock()
        self.head: str | None = resume_at
        self.awaiting: list[str] = []
        self.last_used = time.monotonic()
        self.session_id: str | None = None if (resume is None or fork) else resume
        self._new_session_id = None if resume is not None else str(uuid.uuid4())
        if self._new_session_id is not None:
            self.session_id = self._new_session_id
        self._cwd = cwd
        self._resume = resume
        self._resume_at = resume_at
        self._fork = fork
        self._client: Any = None
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None
        self.ended = False

    async def start(self) -> None:
        from claude_agent_sdk import ClaudeSDKClient

        options = build_options(
            self.config,
            cwd=self._cwd,
            mcp_server=make_server(self.tools, self.pending) if self.tools.definitions else None,
            hooks=make_hooks(self.pending) if self.tools.definitions else None,
            session_id=self._new_session_id,
            resume=self._resume,
            resume_at=self._resume_at if self._fork or self._resume_at else None,
            fork=self._fork,
            persist=self.persist,
            stderr=_log_stderr,
        )
        self._client = (self._client_factory or ClaudeSDKClient)(options)
        try:
            await self._client.connect()
            info = await self._client.get_server_info()
            version = info.get("claude_code_version") if info else None
            check_version(version if isinstance(version, str) else None)
        except Exception:
            await self.close()
            raise
        self._reader = asyncio.create_task(self._read_all(), name="claude-agent-reader")

    async def _read_all(self) -> None:
        try:
            async for message in self._client.receive_messages():
                session_id = getattr(message, "session_id", None)
                if session_id and self.session_id is None:
                    self.session_id = session_id
                await self._queue.put(message)
        except BaseException as exc:  # noqa: BLE001 - reported to the waiting step
            self._failure = exc
        finally:
            self.ended = True
            await self._queue.put(None)

    async def send(self, blocks: list[dict[str, Any]]) -> None:
        if self.ended:
            raise ClaudeAgentError("Claude Code stopped") from self._failure

        async def one_message():
            yield {
                "type": "user",
                "message": {"role": "user", "content": blocks},
                "parent_tool_use_id": None,
            }

        self.last_used = time.monotonic()
        await self._client.query(one_message())

    def deliver(self, results: dict[str, Any]) -> None:
        """Release held MCP handlers with nanobot's results."""
        for tool_use_id in self.awaiting:
            result = results.get(tool_use_id)
            if result is None:
                from nanobot.providers.claude_agent.tools import cancelled_result

                result = cancelled_result()
            self.pending.resolve(tool_use_id, result)
        self.awaiting = []
        self.last_used = time.monotonic()

    async def read_step(
        self,
        step: Step,
        *,
        on_content_delta: DeltaCallback = None,
        on_thinking_delta: DeltaCallback = None,
    ) -> StepResult:
        """Read until Claude asks for nanobot tools or finishes the turn."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent

        message_id: str | None = None
        error: str | None = None
        while True:
            message = await self._queue.get()
            if message is None:
                raise ClaudeAgentError("Claude Code stopped before finishing its reply") from self._failure
            self.last_used = time.monotonic()
            if isinstance(message, StreamEvent):
                if message.parent_tool_use_id is not None:
                    continue
                event = StreamPart.parse(message.event)
                if event.kind == "message_start":
                    message_id = event.message_id
                    if message_id:
                        step.streamed.add(message_id)
                    step.usage.update(message_id, event.usage)
                elif event.text is not None:
                    await _emit(step.text_parts, event.text, on_content_delta)
                elif event.thinking is not None:
                    await _emit(step.reasoning_parts, event.thinking, on_thinking_delta)
                elif event.kind == "message_delta":
                    step.usage.update(message_id, event.usage)
                elif event.kind == "message_stop" and step.tool_calls and error is None:
                    self.head = step.head
                    self.awaiting = [call.id for call in step.tool_calls]
                    return StepResult("tools")
            elif isinstance(message, AssistantMessage):
                if message.parent_tool_use_id is not None:
                    continue
                failed = assistant_error(message)
                if failed:
                    # Synthetic API errors are diagnostics, not assistant answers.
                    error = failed
                    continue
                step.add_assistant(message, self.tools)
            elif isinstance(message, ResultMessage):
                if step.head:
                    self.head = step.head
                self.awaiting = []
                if message.is_error or error is not None:
                    detail = error or message.result or message.subtype
                    return StepResult(
                        "result",
                        is_error=True,
                        error=f"Claude Code error: {detail}",
                        error_kind=_ERROR_KINDS.get(error or "", None),
                        stop_reason=message.stop_reason,
                    )
                return StepResult("result", stop_reason=message.stop_reason)

    async def interrupt(self) -> None:
        """Stop the current turn and wait until Claude Code has flushed it."""
        from claude_agent_sdk import ResultMessage

        self.pending.cancel_all()
        self.awaiting = []
        if self.ended or self._client is None:
            return
        try:
            await self._client.interrupt()
            async with asyncio.timeout(_INTERRUPT_DRAIN_S):
                while True:
                    message = await self._queue.get()
                    if message is None or isinstance(message, ResultMessage):
                        break
        except (Exception, TimeoutError):
            logger.debug("Claude Code interrupt did not drain cleanly; closing the process")
            await self.close()

    async def close(self) -> None:
        self.pending.cancel_all()
        self.awaiting = []
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                logger.debug("Claude Code process did not disconnect cleanly")
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
            self._reader = None
        self.ended = True


def _mapping(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _nonempty(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class StreamPart:
    """The fields nanobot reads from one raw Claude API stream event."""

    kind: str | None
    message_id: str | None = None
    usage: dict[str, Any] | None = None
    text: str | None = None
    thinking: str | None = None

    @classmethod
    def parse(cls, event: dict[str, Any]) -> StreamPart:
        kind = _nonempty(event.get("type"))
        if kind == "message_start":
            started = _mapping(event.get("message"))
            return cls(kind, message_id=_nonempty(started.get("id")), usage=_mapping(started.get("usage")))
        if kind == "message_delta":
            return cls(kind, usage=_mapping(event.get("usage")))
        if kind == "content_block_start":
            block = _mapping(event.get("content_block"))
            if block.get("type") == "text":
                return cls(kind, text=_nonempty(block.get("text")))
            if block.get("type") == "thinking":
                return cls(kind, thinking=_nonempty(block.get("thinking")))
        if kind == "content_block_delta":
            delta = _mapping(event.get("delta"))
            if delta.get("type") == "text_delta":
                return cls(kind, text=_nonempty(delta.get("text")))
            if delta.get("type") == "thinking_delta":
                return cls(kind, thinking=_nonempty(delta.get("thinking")))
        return cls(kind)


def _log_stderr(line: str) -> None:
    logger.debug("claude stderr: {}", line)


async def _emit(parts: list[str], text: str, callback: DeltaCallback) -> None:
    parts.append(text)
    if callback is not None:
        await callback(text)
