"""An in-process stand-in for Claude Code, speaking the SDK's wire protocol.

The real ``claude_agent_sdk.ClaudeSDKClient`` runs on top of it: the SDK's
control protocol, hook callbacks and in-process MCP server all run for real.
Only the model is scripted. Message shapes follow sessions recorded from
Claude Code 2.1.322.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import ClaudeSDKClient, Transport


@dataclass
class Text:
    text: str
    thinking: str | None = None


@dataclass
class Tools:
    calls: list[tuple[str, dict[str, Any]]]


@dataclass
class ApiError:
    error: str = "rate_limit"


@dataclass
class Hang:
    """Stream part of a reply, then wait until the SDK interrupts the turn."""

    text: str = "partial"


Action = Text | Tools | ApiError | Hang


@dataclass
class Brain:
    """Scripted replies shared by every fake process in a test."""

    turns: list[list[Action]]
    received: list[list[dict[str, Any]]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    listed_tools: list[list[str]] = field(default_factory=list)
    options: list[Any] = field(default_factory=list)
    interrupts: int = 0
    version: str = "2.1.322"

    def factory(self, options: Any) -> ClaudeSDKClient:
        self.options.append(options)
        return ClaudeSDKClient(options, transport=FakeClaude(self, options))


_ids = itertools.count(1)


class FakeClaude(Transport):
    def __init__(self, brain: Brain, options: Any) -> None:
        self._brain = brain
        self._options = options
        self._out: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._responses: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._hooks: list[tuple[re.Pattern[str], list[str]]] = []
        self._interrupted = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._mcp_ready = False
        if options.resume and not options.fork_session:
            self.session_id = options.resume
        else:
            self.session_id = options.session_id or str(uuid.uuid4())

    async def connect(self) -> None:
        return None

    def is_ready(self) -> bool:
        return True

    async def end_input(self) -> None:
        return None

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self._out.put(None)

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            message = await self._out.get()
            if message is None:
                return
            yield message

    async def write(self, data: str) -> None:
        for line in data.splitlines():
            if line.strip():
                await self._handle(json.loads(line))

    async def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "control_request":
            request = message["request"]
            response: dict[str, Any] = {}
            if request["subtype"] == "initialize":
                for matcher in (request.get("hooks") or {}).get("PreToolUse", []):
                    self._hooks.append((re.compile(matcher["matcher"] or ".*"), matcher["hookCallbackIds"]))
                response = {"claude_code_version": self._brain.version, "models": [], "account": {}}
            elif request["subtype"] == "interrupt":
                self._brain.interrupts += 1
                self._interrupted.set()
            await self._out.put({
                "type": "control_response",
                "response": {"subtype": "success", "request_id": message["request_id"], "response": response},
            })
        elif kind == "control_response":
            body = message["response"]
            future = self._responses.pop(body["request_id"], None)
            if future is not None and not future.done():
                future.set_result(body.get("response") or {})
        elif kind == "user":
            content = message["message"]["content"]
            self._brain.received.append(content if isinstance(content, list) else [{"type": "text", "text": content}])
            task = asyncio.create_task(self._turn())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _request(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = f"cli_{next(_ids)}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._responses[request_id] = future
        await self._out.put({"type": "control_request", "request_id": request_id, "request": request})
        return await future

    async def _mcp(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await self._request({
            "subtype": "mcp_message",
            "server_name": "nanobot",
            "message": {"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params},
        })
        return response["mcp_response"].get("result") or {}

    def _envelope(self, kind: str, **fields: Any) -> dict[str, Any]:
        return {"type": kind, "session_id": self.session_id, "uuid": str(uuid.uuid4()), **fields}

    async def _stream(self, event: dict[str, Any]) -> None:
        await self._out.put(self._envelope("stream_event", event=event, parent_tool_use_id=None))

    async def _assistant(self, message_id: str, content: list[dict[str, Any]], usage: dict[str, int]) -> None:
        await self._out.put(self._envelope(
            "assistant",
            parent_tool_use_id=None,
            message={"id": message_id, "model": self._options.model, "content": content, "usage": usage},
        ))

    async def _turn(self) -> None:
        actions = self._brain.turns.pop(0)
        await self._out.put(self._envelope("system", subtype="init"))
        turns = 0
        for action in actions:
            turns += 1
            message_id = f"msg_{next(_ids)}"
            usage = {"input_tokens": 10, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 5, "output_tokens": 1}
            await self._stream({"type": "message_start", "message": {"id": message_id, "usage": usage}})
            if isinstance(action, ApiError):
                await self._out.put(self._envelope(
                    "assistant", parent_tool_use_id=None, error=action.error,
                    message={"id": message_id, "model": self._options.model,
                             "content": [{"type": "text", "text": "API Error"}]},
                ))
                await self._result(turns, is_error=True, subtype="success", text="API Error: rate limited")
                return
            if isinstance(action, Hang):
                await self._stream({"type": "content_block_start", "index": 0,
                                    "content_block": {"type": "text", "text": action.text}})
                await self._interrupted.wait()
                await self._result(turns, is_error=True, subtype="error_during_execution", text=None)
                return
            final = {**usage, "output_tokens": 20}
            if isinstance(action, Text):
                blocks: list[dict[str, Any]] = []
                index = 0
                if action.thinking:
                    await self._stream({"type": "content_block_start", "index": index,
                                        "content_block": {"type": "thinking", "thinking": ""}})
                    await self._stream({"type": "content_block_delta", "index": index,
                                        "delta": {"type": "thinking_delta", "thinking": action.thinking}})
                    blocks.append({"type": "thinking", "thinking": action.thinking, "signature": "sig"})
                    index += 1
                await self._stream({"type": "content_block_start", "index": index,
                                    "content_block": {"type": "text", "text": ""}})
                half = len(action.text) // 2
                for piece in (action.text[:half], action.text[half:]):
                    await self._stream({"type": "content_block_delta", "index": index,
                                        "delta": {"type": "text_delta", "text": piece}})
                blocks.append({"type": "text", "text": action.text})
                await self._stream({"type": "message_delta", "usage": final})
                await self._assistant(message_id, blocks, final)
                await self._stream({"type": "message_stop"})
                continue
            # Tools: the assistant message, then PreToolUse and the MCP call for each tool, one at a time.
            if not self._mcp_ready:
                await self._mcp("initialize", {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "claude-code", "version": self._brain.version},
                })
                await self._request({
                    "subtype": "mcp_message", "server_name": "nanobot",
                    "message": {"jsonrpc": "2.0", "method": "notifications/initialized"},
                })
                self._mcp_ready = True
            listed = await self._mcp("tools/list", {})
            self._brain.listed_tools.append([tool["name"] for tool in listed.get("tools", [])])
            calls = [(f"toolu_{next(_ids)}", f"mcp__nanobot__{name}", args) for name, args in action.calls]
            await self._stream({"type": "message_delta", "usage": final})
            await self._assistant(
                message_id,
                [{"type": "tool_use", "id": cid, "name": name, "input": args} for cid, name, args in calls],
                final,
            )
            results = []
            for position, (cid, name, args) in enumerate(calls):
                for pattern, callback_ids in self._hooks:
                    if pattern.search(name):
                        for callback_id in callback_ids:
                            await self._request({
                                "subtype": "hook_callback",
                                "callback_id": callback_id,
                                "tool_use_id": cid,
                                "input": {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": args,
                                          "session_id": self.session_id, "cwd": "/"},
                            })
                if position == 0:
                    await self._stream({"type": "message_stop"})
                result = await self._mcp("tools/call", {"name": name.removeprefix("mcp__nanobot__"), "arguments": args})
                self._brain.tool_results.append({"id": cid, **result})
                results.append({"type": "tool_result", "tool_use_id": cid, "content": result.get("content", [])})
            await self._out.put(self._envelope(
                "user", parent_tool_use_id=None, message={"role": "user", "content": results},
            ))
        await self._result(turns, is_error=False, subtype="success", text="done")

    async def _result(self, turns: int, *, is_error: bool, subtype: str, text: str | None) -> None:
        await self._out.put(self._envelope(
            "result", subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error,
            num_turns=turns, stop_reason="end_turn", result=text,
        ))
