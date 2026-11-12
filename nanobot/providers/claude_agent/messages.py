"""Convert between nanobot's Chat-style messages and Claude Code's stream."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, cast

from nanobot.providers.base import LLMUsage, ToolCallRequest
from nanobot.providers.claude_agent.tools import ToolSet

_DATA_URL_RE = re.compile(r"^data:(image/[\w.+-]+);base64,(.+)$", re.DOTALL)


def system_prompt(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        if message.get("role") != "system":
            continue
        text = text_of(message.get("content"))
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            str(cast(dict[str, Any], item).get("text") or "")
            for item in cast(list[object], content)
            if isinstance(item, dict) and cast(dict[str, Any], item).get("type") == "text"
        ]
        return "\n".join(text for text in texts if text)
    return "" if content is None else str(content)


def _image_block(block: dict[str, Any]) -> dict[str, Any] | None:
    url = str(cast(dict[str, Any], block.get("image_url") or {}).get("url") or "")
    if not url:
        return None
    match = _DATA_URL_RE.match(url)
    if match:
        return {"type": "image", "source": {"type": "base64", "media_type": match.group(1), "data": match.group(2)}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def user_blocks(content: Any) -> list[dict[str, Any]]:
    """Convert one user message's content to Claude content blocks."""
    if isinstance(content, str) or content is None:
        return [{"type": "text", "text": content or "(empty)"}]
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]
    blocks: list[dict[str, Any]] = []
    for item in cast(list[object], content):
        if not isinstance(item, dict):
            blocks.append({"type": "text", "text": str(item)})
            continue
        block = cast(dict[str, Any], item)
        kind = block.get("type")
        if kind == "text":
            blocks.append({"type": "text", "text": str(block.get("text") or "")})
        elif kind == "image_url":
            image = _image_block(block)
            if image is not None:
                blocks.append(image)
        else:
            blocks.append({"type": "text", "text": json.dumps(block, ensure_ascii=False, default=str)})
    return blocks or [{"type": "text", "text": "(empty)"}]


def transcript_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten history Claude did not write into one user message.

    Claude Code cannot load earlier turns it did not produce, so roles become
    text tags. Attachments stay real image blocks. A conversation with only a
    user turn is sent as plain content.
    """
    turns = [message for message in messages if message.get("role") != "system"]
    has_history = any(message.get("role") in {"assistant", "tool"} for message in turns)
    if not has_history:
        blocks: list[dict[str, Any]] = []
        for message in turns:
            blocks.extend(user_blocks(message.get("content")))
        return blocks or [{"type": "text", "text": "(empty)"}]

    blocks = [{
        "type": "text",
        "text": (
            "<conversation-checkpoint>\nThe conversation so far, written by the harness. "
            "Continue it as the assistant.\n"
        ),
    }]
    for message in turns:
        role = str(message.get("role") or "user")
        blocks.append({"type": "text", "text": f"<{role}>"})
        if role == "assistant":
            text = text_of(message.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for call in cast(list[object], message.get("tool_calls") or []):
                if not isinstance(call, dict):
                    continue
                function = cast(dict[str, Any], cast(dict[str, Any], call).get("function") or {})
                payload = {
                    "id": cast(dict[str, Any], call).get("id"),
                    "name": function.get("name"),
                    "input": function.get("arguments"),
                }
                blocks.append({
                    "type": "text",
                    "text": f"<tool-call>\n{json.dumps(payload, ensure_ascii=False, default=str)}\n</tool-call>",
                })
        elif role == "tool":
            payload = {
                "id": message.get("tool_call_id"),
                "name": message.get("name"),
                "output": text_of(message.get("content")),
            }
            blocks.append({
                "type": "text",
                "text": f"<tool-result>\n{json.dumps(payload, ensure_ascii=False, default=str)}\n</tool-result>",
            })
            blocks.extend(block for block in user_blocks(message.get("content")) if block["type"] == "image")
        else:
            blocks.extend(user_blocks(message.get("content")))
        blocks.append({"type": "text", "text": f"</{role}>"})
    blocks.append({"type": "text", "text": "</conversation-checkpoint>"})
    return blocks


@dataclass
class _CallUsage:
    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0


class UsageTracker:
    """Claude Code reports one response's usage several times while it streams.

    Keep the highest count seen for each API response instead of adding them, and
    use the last response's input as the context size.
    """

    def __init__(self) -> None:
        self._calls: dict[str, _CallUsage] = {}
        self._last: str | None = None

    def update(self, message_id: str | None, usage: dict[str, Any] | None) -> None:
        if not message_id or not usage:
            return
        call = self._calls.get(message_id)
        if call is None:
            call = _CallUsage()
            self._calls[message_id] = call
            self._last = message_id
        call.input = max(call.input, int(usage.get("input_tokens") or 0))
        call.cache_read = max(call.cache_read, int(usage.get("cache_read_input_tokens") or 0))
        call.cache_write = max(call.cache_write, int(usage.get("cache_creation_input_tokens") or 0))
        call.output = max(call.output, int(usage.get("output_tokens") or 0))

    @property
    def context_tokens(self) -> int | None:
        if self._last is None:
            return None
        call = self._calls[self._last]
        return call.input + call.cache_read + call.cache_write

    def snapshot(self) -> LLMUsage | None:
        total: LLMUsage | None = None
        for call in self._calls.values():
            usage = LLMUsage.reported(
                input_tokens=call.input + call.cache_read + call.cache_write,
                output_tokens=call.output,
                cache_read_tokens=call.cache_read,
                cache_write_tokens=call.cache_write,
            )
            total = usage if total is None else total + usage
        return total


@dataclass
class Step:
    """One model step as nanobot sees it: text, reasoning and tool calls."""

    text_parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    head: str | None = None
    streamed: set[str] = field(default_factory=set)
    usage: UsageTracker = field(default_factory=UsageTracker)

    @property
    def text(self) -> str | None:
        return "".join(self.text_parts) or None

    @property
    def reasoning(self) -> str | None:
        return "".join(self.reasoning_parts) or None

    def add_assistant(self, message: Any, tools: ToolSet) -> None:
        """Record a complete assistant message from the main conversation."""
        from claude_agent_sdk import TextBlock, ThinkingBlock, ToolUseBlock

        if getattr(message, "uuid", None):
            self.head = message.uuid
        self.usage.update(getattr(message, "message_id", None), getattr(message, "usage", None))
        streamed = getattr(message, "message_id", None) in self.streamed
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                name = tools.original(block.name)
                # Calls to tools nanobot did not offer get Claude Code's own error.
                if name is not None:
                    self.tool_calls.append(ToolCallRequest(id=block.id, name=name, arguments=block.input))
            elif streamed:
                continue
            elif isinstance(block, TextBlock):
                self.text_parts.append(block.text)
            elif isinstance(block, ThinkingBlock):
                self.reasoning_parts.append(block.thinking)


def assistant_error(message: Any) -> str | None:
    error = getattr(message, "error", None)
    return str(error) if error else None
