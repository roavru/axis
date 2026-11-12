"""nanobot tools exposed to Claude Code as an in-process MCP server.

Claude Code never runs a nanobot tool. When Claude calls one, the MCP handler
holds the call until nanobot's own runner has executed it, with nanobot's
permission checks, and sends the result back on the next model request.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import TYPE_CHECKING, Any, cast

import mcp.types as mt

if TYPE_CHECKING:
    from claude_agent_sdk import HookJSONOutput

PREFIX = "mcp__nanobot__"
# Claude Code limits tool names to 64 characters, including the MCP prefix.
_NAME_LIMIT = 64 - len(PREFIX)
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
# Claude Code cuts each MCP tool description at this many characters.
DESCRIPTION_LIMIT = 2048
CANCELLED_TEXT = "Cancelled by user."
_DATA_URL_RE = re.compile(r"^data:(image/[\w.+-]+);base64,(.+)$", re.DOTALL)


def short_name(name: str) -> str:
    if len(name) <= _NAME_LIMIT and _NAME_RE.match(name):
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[: _NAME_LIMIT - 11] + "_" + digest


class ToolSet:
    """The nanobot tools offered to one Claude Code process."""

    def __init__(self, tools: list[dict[str, Any]] | None) -> None:
        self.definitions: list[mt.Tool] = []
        self._original: dict[str, str] = {}
        for tool in tools or []:
            function = cast(dict[str, Any], tool.get("function") or tool)
            name = function.get("name")
            if not isinstance(name, str) or not name:
                continue
            short = short_name(name)
            self._original[PREFIX + short] = name
            parameters = function.get("parameters")
            schema: dict[str, Any] = (
                cast(dict[str, Any], parameters)
                if isinstance(parameters, dict)
                else {"type": "object", "properties": {}}
            )
            self.definitions.append(
                mt.Tool.model_validate({
                    "name": short,
                    "description": str(function.get("description") or "")[:DESCRIPTION_LIMIT],
                    "inputSchema": schema,
                    # Claude serializes calls to non-read-only tools, so each handler can
                    # take its ID from the PreToolUse hook that ran just before it.
                    "annotations": {"readOnlyHint": False},
                    # Keep every tool loaded, and let nanobot's own truncation apply.
                    "_meta": {"anthropic/alwaysLoad": True, "anthropic/maxResultSizeChars": 500_000},
                })
            )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._original.values()))

    def original(self, claude_name: str) -> str | None:
        """Return nanobot's name, or None for a tool nanobot did not offer."""
        return self._original.get(claude_name)


class PendingCalls:
    """Pairs Claude's tool_use IDs with the MCP handler calls that wait for them."""

    def __init__(self) -> None:
        self._results: dict[str, asyncio.Future[mt.CallToolResult]] = {}
        self._next: str | None = None

    def _future(self, tool_use_id: str) -> asyncio.Future[mt.CallToolResult]:
        future = self._results.get(tool_use_id)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self._results[tool_use_id] = future
        return future

    def before(self, tool_use_id: str) -> None:
        self._future(tool_use_id)
        self._next = tool_use_id

    async def wait(self) -> mt.CallToolResult:
        tool_use_id, self._next = self._next, None
        if tool_use_id is None:
            raise RuntimeError("Claude called a nanobot tool without its PreToolUse ID")
        try:
            return await self._future(tool_use_id)
        finally:
            self._results.pop(tool_use_id, None)

    def resolve(self, tool_use_id: str, result: mt.CallToolResult) -> None:
        future = self._future(tool_use_id)
        if not future.done():
            future.set_result(result)

    def cancel_all(self) -> None:
        for future in self._results.values():
            if not future.done():
                future.set_result(cancelled_result())
        self._results.clear()
        self._next = None


def make_server(tools: ToolSet, pending: PendingCalls) -> Any:
    # Loaded on first use, so nanobot starts without building MCP servers.
    from mcp.server.lowlevel import Server

    server: Server[Any, Any] = Server("nanobot")

    @server.list_tools()
    async def _list_tools() -> list[mt.Tool]:
        return tools.definitions

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> mt.CallToolResult:
        _ = arguments
        if tools.original(PREFIX + name) is None:
            raise RuntimeError(f"Claude Code called an unregistered nanobot tool: {name}")
        return await pending.wait()

    return server


def make_hooks(pending: PendingCalls) -> dict[str, Any]:
    from claude_agent_sdk import HookMatcher

    async def before(input_data: Any, tool_use_id: str | None, context: Any) -> HookJSONOutput:
        _ = input_data, context
        if tool_use_id:
            pending.before(tool_use_id)
        return {}

    # No timeout cap: the matched hook only records an ID and returns immediately.
    return {"PreToolUse": [HookMatcher(matcher="^mcp__nanobot__", hooks=[before])]}


def cancelled_result() -> mt.CallToolResult:
    return mt.CallToolResult(content=[mt.TextContent(type="text", text=CANCELLED_TEXT)], isError=True)


def tool_result(message: dict[str, Any]) -> mt.CallToolResult:
    """Convert one nanobot ``role: tool`` message to an MCP result."""
    content = message.get("content")
    blocks: list[mt.TextContent | mt.ImageContent] = []
    if isinstance(content, list):
        for item in cast(list[object], content):
            if not isinstance(item, dict):
                blocks.append(mt.TextContent(type="text", text=str(item)))
                continue
            block = cast(dict[str, Any], item)
            if block.get("type") == "text":
                blocks.append(mt.TextContent(type="text", text=str(block.get("text") or "")))
            elif block.get("type") == "image_url":
                url = str(cast(dict[str, Any], block.get("image_url") or {}).get("url") or "")
                match = _DATA_URL_RE.match(url)
                if match:
                    blocks.append(mt.ImageContent(type="image", mimeType=match.group(1), data=match.group(2)))
                elif url:
                    blocks.append(mt.TextContent(type="text", text=f"[image: {url}]"))
            else:
                blocks.append(mt.TextContent(type="text", text=json.dumps(block, ensure_ascii=False, default=str)))
    elif content is not None:
        blocks.append(mt.TextContent(type="text", text=str(content)))
    if not blocks:
        blocks.append(mt.TextContent(type="text", text="(no output)"))
    return mt.CallToolResult(content=list(blocks), isError=False)


def append_text(result: mt.CallToolResult, text: str) -> mt.CallToolResult:
    """Attach a user message that arrived while tools were running."""
    return mt.CallToolResult(
        content=[*result.content, mt.TextContent(type="text", text=text)],
        isError=result.isError,
    )
