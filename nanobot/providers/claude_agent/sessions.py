"""Claude Code processes shared by nanobot's model requests.

nanobot's history stays the source of truth. Each response carries a
``ProviderConversationState`` naming the Claude session and the last transcript
entry Claude wrote. The next request either continues the live process, resumes
or forks the saved Claude session at that entry, or starts a new Claude session
from nanobot's full history.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from nanobot.providers.base import LLMResponse, ProviderConversationState
from nanobot.providers.claude_agent.messages import (
    Step,
    system_prompt,
    text_of,
    transcript_blocks,
    user_blocks,
)
from nanobot.providers.claude_agent.options import ProcessConfig, find_claude
from nanobot.providers.claude_agent.session import (
    ClaudeAgentError,
    ClaudeProcess,
    ClientFactory,
    StepResult,
)
from nanobot.providers.claude_agent.tools import ToolSet, append_text, tool_result

STATE_KIND = "claude_agent"
STATE_VERSION = 1
STATE_PROVIDER = "claude_agent"

IDLE_SECONDS = 5 * 60
# A process held open for nanobot's tools is closed after this long without results.
AWAITING_TOOLS_SECONDS = 24 * 60 * 60
MAX_PROCESSES = 8
_REAP_INTERVAL_S = 30.0
_DEFAULT_SYSTEM = "You are a helpful assistant."


@dataclass
class Request:
    key: str | None
    model: str
    effort: str | None
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    state: ProviderConversationState | None
    on_content_delta: Callable[[str], Awaitable[None]] | None = None
    on_thinking_delta: Callable[[str], Awaitable[None]] | None = None
    # The conversation's project folder, when the caller knows it.
    working_directory: Path | None = None


@dataclass(frozen=True)
class SavedState:
    session: str
    head: str
    awaiting: tuple[str, ...]
    # Where the Claude session was created; None for states saved before this was recorded.
    cwd: Path | None

    @classmethod
    def parse(cls, state: ProviderConversationState | None) -> SavedState | None:
        if state is None or not matches(state, state.model):
            return None
        payload = state.payload
        awaiting = cast(object, payload.get("awaiting") or [])
        cwd = payload.get("cwd")
        return cls(
            session=str(payload["session"]),
            head=str(payload["head"]),
            awaiting=tuple(str(item) for item in cast(list[object], awaiting))
            if isinstance(awaiting, list)
            else (),
            cwd=Path(cwd) if isinstance(cwd, str) and cwd else None,
        )


def matches(state: ProviderConversationState, model: str) -> bool:
    payload = state.payload
    return (
        state.kind == STATE_KIND
        and state.version == STATE_VERSION
        and state.provider == STATE_PROVIDER
        and state.model == model
        and isinstance(payload.get("session"), str)
        and isinstance(payload.get("head"), str)
    )


class ClaudeSessions:
    def __init__(
        self,
        *,
        workdir: Path,
        connection_env: dict[str, str] | None = None,
        idle_seconds: float = IDLE_SECONDS,
        max_processes: int = MAX_PROCESSES,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._workdir = workdir
        self._prompt_dir = workdir / "prompts"
        self._connection_env = tuple(sorted((connection_env or {}).items()))
        self._idle_seconds = idle_seconds
        self._max_processes = max_processes
        self._live: dict[str, ClaudeProcess] = {}
        self._prompt_files: set[Path] = set()
        self._reaper: asyncio.Task[None] | None = None

    async def run(self, request: Request) -> LLMResponse:
        executable = find_claude()
        if executable is None:
            raise ClaudeAgentError(
                "Claude Code is not installed. Install it and run `claude auth login`, "
                "or set NANOBOT_CLAUDE_PATH.",
                kind="missing",
            )
        tools = ToolSet(request.tools)
        saved = SavedState.parse(request.state) if request.state is not None else None
        config = ProcessConfig(
            executable=executable,
            model=request.model,
            effort=request.effort,
            system_prompt_file=self._prompt_file(system_prompt(request.messages) or _DEFAULT_SYSTEM),
            tool_names=tools.names,
            cwd=self._cwd_for(request, saved),
            connection_env=self._connection_env,
        )
        if saved is not None and (saved.cwd or self._workdir) != config.cwd:
            # The conversation moved to another folder; Claude's saved session lives in the old one.
            saved = None
        persist = request.key is not None
        try:
            process, payload = await self._choose(request, saved, config, tools, persist=persist)
        except ClaudeAgentError:
            raise
        except Exception as exc:
            raise ClaudeAgentError(f"Claude Code could not start: {exc}") from exc
        async with process.lock:
            try:
                if isinstance(payload, dict):
                    process.deliver(payload)
                else:
                    await process.send(payload)
                step = Step()
                result = await process.read_step(
                    step,
                    on_content_delta=request.on_content_delta,
                    on_thinking_delta=request.on_thinking_delta,
                )
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await asyncio.shield(process.interrupt())
                await self._forget(process)
                raise
            except ClaudeAgentError:
                await self._forget(process)
                raise
            except Exception as exc:
                await self._forget(process)
                raise ClaudeAgentError(f"Claude Code failed: {exc}") from exc
        if result.is_error or not persist:
            await self._forget(process)
        else:
            self._register(process)
        return self._response(process, step, result, model=request.model, keep_state=persist and not result.is_error)

    def _cwd_for(self, request: Request, saved: SavedState | None) -> Path:
        """Run where the conversation works, so Claude Code reports the real folder.

        A request without a folder, such as the memory archiver's side call,
        stays where its saved Claude session was created.
        """
        folder = request.working_directory
        if folder is not None and folder.is_dir():
            return folder.resolve()
        if saved is not None and saved.cwd is not None:
            return saved.cwd
        return self._workdir

    async def _choose(
        self,
        request: Request,
        saved: SavedState | None,
        config: ProcessConfig,
        tools: ToolSet,
        *,
        persist: bool,
    ) -> tuple[ClaudeProcess, dict[str, Any] | list[dict[str, Any]]]:
        """Pick the process for this request and what to give it."""
        pending = request.state.pending_messages if saved is not None and request.state is not None else []
        if saved is not None:
            live = self._live.get(saved.session)
            reusable = (
                live is not None
                and not live.ended
                and not live.lock.locked()
                and live.head == saved.head
                and live.config == config
            )
            if saved.awaiting:
                results = _tool_results(pending, saved.awaiting)
                if reusable and live is not None and tuple(live.awaiting) == saved.awaiting and results is not None:
                    return live, results
                if live is not None and live.awaiting and not live.lock.locked():
                    # nanobot moved on without these results; release the held tool calls.
                    await self._forget(live)
            elif pending and all(message.get("role") == "user" for message in pending):
                blocks = [block for message in pending for block in user_blocks(message.get("content"))]
                if reusable and live is not None and not live.awaiting:
                    return live, blocks
                # Fork when the saved session may have moved past the saved entry, or
                # another process still owns it; otherwise resume it in place.
                fork = live is not None or not await self._ends_at(saved.session, saved.head, config.cwd)
                process = await self._spawn(
                    config, tools, resume=saved.session, resume_at=saved.head, fork=fork, persist=persist,
                )
                return process, blocks
        process = await self._spawn(config, tools, persist=persist)
        return process, transcript_blocks(request.messages)

    async def _spawn(self, config: ProcessConfig, tools: ToolSet, **kwargs: Any) -> ClaudeProcess:
        self._ensure_reaper()
        process = ClaudeProcess(config, tools, client_factory=self._client_factory, **kwargs)
        await process.start()
        return process

    async def _ends_at(self, session: str, head: str, cwd: Path) -> bool:
        """Whether the saved Claude transcript still ends at *head*."""
        try:
            from claude_agent_sdk import get_session_messages

            messages = await asyncio.to_thread(get_session_messages, session, str(cwd))
        except Exception:
            return False
        return bool(messages) and getattr(messages[-1], "uuid", None) == head

    def _register(self, process: ClaudeProcess) -> None:
        if process.session_id is None:
            return
        previous = self._live.get(process.session_id)
        if previous is not None and previous is not process:
            asyncio.create_task(previous.close())
        self._live[process.session_id] = process
        overflow = len(self._live) - self._max_processes
        if overflow <= 0:
            return
        candidates = sorted(
            (item for item in self._live.values() if item is not process and not item.lock.locked()),
            key=lambda item: (bool(item.awaiting), item.last_used),
        )
        for victim in candidates[:overflow]:
            self._drop(victim)
            asyncio.create_task(victim.close())

    def _drop(self, process: ClaudeProcess) -> None:
        if process.session_id is not None and self._live.get(process.session_id) is process:
            del self._live[process.session_id]

    async def _forget(self, process: ClaudeProcess) -> None:
        self._drop(process)
        await process.close()

    def _response(
        self,
        process: ClaudeProcess,
        step: Step,
        result: StepResult,
        *,
        model: str,
        keep_state: bool,
    ) -> LLMResponse:
        usage = step.usage.snapshot()
        if result.is_error:
            return LLMResponse(
                content=result.error,
                finish_reason="error",
                usage=usage,
                error_kind=result.error_kind,
                # Claude Code already retried; repeating the request would only repeat the error.
                error_should_retry=False,
            )
        state: ProviderConversationState | None = None
        if keep_state and process.session_id and process.head:
            payload: dict[str, Any] = {
                "session": process.session_id,
                "head": process.head,
                "awaiting": list(process.awaiting),
                "cwd": str(process.config.cwd),
            }
            # Without a reported size, nanobot falls back to its own estimate.
            if step.usage.context_tokens is not None:
                payload["context_tokens"] = step.usage.context_tokens
            state = ProviderConversationState(
                kind=STATE_KIND,
                provider=STATE_PROVIDER,
                model=model,
                version=STATE_VERSION,
                payload=payload,
            )
        finish = "tool_calls" if result.kind == "tools" else ("length" if result.stop_reason == "max_tokens" else "stop")
        return LLMResponse(
            content=step.text,
            tool_calls=step.tool_calls,
            finish_reason=finish,
            usage=usage,
            reasoning_content=step.reasoning,
            provider_state=state,
        )

    def _prompt_file(self, text: str) -> Path:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
        path = self._prompt_dir / f"{digest}.md"
        if not path.exists():
            self._prompt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = path.with_suffix(f".{os.getpid()}.tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(temporary, path)
        self._prompt_files.add(path)
        return path

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap(), name="claude-agent-reaper")

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(_REAP_INTERVAL_S)
            now = time.monotonic()
            for process in list(self._live.values()):
                if process.lock.locked():
                    continue
                limit = AWAITING_TOOLS_SECONDS if process.awaiting else self._idle_seconds
                if process.ended or now - process.last_used > limit:
                    self._drop(process)
                    await process.close()

    async def aclose(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reaper
            self._reaper = None
        processes, self._live = list(self._live.values()), {}
        await asyncio.gather(*(process.close() for process in processes), return_exceptions=True)
        for path in self._prompt_files:
            with contextlib.suppress(OSError):
                path.unlink()
        self._prompt_files.clear()


def _tool_results(
    pending: list[dict[str, Any]],
    awaiting: tuple[str, ...],
) -> dict[str, Any] | None:
    """Map nanobot's tool messages to the calls Claude is waiting for.

    Messages that arrive after the results, such as a user follow-up injected
    mid-run, are attached to the last result so Claude reads them now.
    """
    results: dict[str, Any] = {}
    last: str | None = None
    for message in pending:
        role = message.get("role")
        if role == "tool":
            tool_use_id = str(message.get("tool_call_id") or "")
            if tool_use_id not in awaiting:
                return None
            results[tool_use_id] = tool_result(message)
            last = tool_use_id
        elif role == "user" and last is not None:
            text = text_of(message.get("content"))
            if text:
                results[last] = append_text(results[last], f"<user_message>\n{text}\n</user_message>")
        else:
            return None
    return results if results else None
