from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from claude_agent_fake import ApiError, Brain, Hang, Text, Tools

from nanobot.providers.base import LLMResponse, ProviderCallContext
from nanobot.providers.claude_agent import ClaudeAgentProvider
from nanobot.providers.claude_agent.sessions import ClaudeSessions
from nanobot.providers.conversation_state import ProviderConversationStateController
from nanobot.utils.helpers import build_assistant_message

MODEL = "claude-agent/haiku"
KEY = "websocket:chat"
SYSTEM = {"role": "system", "content": "You are Neo."}
WORD_TOOL = {
    "type": "function",
    "function": {
        "name": "get_word",
        "description": "Return a word.",
        "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}},
    },
}


@pytest.fixture(autouse=True)
def claude_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOBOT_CLAUDE_PATH", "/usr/bin/claude")


def provider(brain: Brain, tmp_path: Path) -> ClaudeAgentProvider:
    return ClaudeAgentProvider(workdir=tmp_path, default_model=MODEL, client_factory=brain.factory)


def texts(blocks: list[dict[str, Any]]) -> str:
    return "".join(block.get("text", "") for block in blocks)


class Chat:
    """Drive a provider the way nanobot's runner does, through the real state controller."""

    def __init__(self, llm: ClaudeAgentProvider, messages: list[dict[str, Any]] | None = None, state: Any = None):
        self.llm = llm
        self.messages = messages or [SYSTEM]
        self.controller = ProviderConversationStateController(
            provider=llm, model=MODEL, messages=self.messages, state=state, session_id=KEY,
        )
        self.deltas: list[str] = []

    async def step(self, tools: list[dict[str, Any]] | None = None) -> LLMResponse:
        context = self.controller.prepare_request(self.messages, context_window_tokens=200_000)

        async def on_delta(text: str) -> None:
            self.deltas.append(text)

        response = await self.llm.chat_stream_with_retry(
            messages=self.messages, tools=tools, model=MODEL, provider_context=context, on_content_delta=on_delta,
        )
        self.controller.observe_response(response, self.messages)
        assistant = build_assistant_message(
            response.content, tool_calls=[call.to_openai_tool_call() for call in response.tool_calls] or None,
        )
        self.messages.append(self.controller.project_response_message(assistant, response))
        return response

    async def turn(self, text: str, tools: list[dict[str, Any]] | None = None) -> LLMResponse:
        self.messages.append({"role": "user", "content": text})
        response = await self.step(tools)
        while response.finish_reason == "tool_calls":
            for call in response.tool_calls:
                self.messages.append({
                    "role": "tool", "tool_call_id": call.id, "name": call.name,
                    "content": f"word-{call.arguments.get('n')}",
                })
            response = await self.step(tools)
        return response


@pytest.mark.asyncio
async def test_text_reply_streams_and_counts_each_response_once(tmp_path: Path) -> None:
    brain = Brain(turns=[[Text("Hello there", thinking="hm")]])
    chat = Chat(provider(brain, tmp_path))

    response = await chat.turn("hi")

    assert response.finish_reason == "stop"
    assert response.content == "Hello there"
    assert response.reasoning_content == "hm"
    assert "".join(chat.deltas) == "Hello there"
    # message_start, message_delta and the assistant message all repeat the same usage.
    assert response.usage is not None
    assert (response.usage.input_tokens, response.usage.output_tokens) == (115, 20)
    assert response.usage.context_tokens == 115
    assert brain.received == [[{"type": "text", "text": "hi"}]]
    await chat.llm.aclose()


@pytest.mark.asyncio
async def test_tool_calls_run_in_nanobot_and_results_reach_claude(tmp_path: Path) -> None:
    brain = Brain(turns=[[Tools([("get_word", {"n": 1}), ("get_word", {"n": 2})]), Text("both words")]])
    chat = Chat(provider(brain, tmp_path))

    chat.messages.append({"role": "user", "content": "get two words"})
    first = await chat.step([WORD_TOOL])

    assert first.finish_reason == "tool_calls"
    assert [(call.name, call.arguments) for call in first.tool_calls] == [
        ("get_word", {"n": 1}), ("get_word", {"n": 2}),
    ]
    assert brain.listed_tools == [["get_word"]]
    assert brain.tool_results == []

    for call in first.tool_calls:
        chat.messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                              "content": f"word-{call.arguments['n']}"})
    final = await chat.step([WORD_TOOL])

    assert final.content == "both words"
    assert [texts(result["content"]) for result in brain.tool_results] == ["word-1", "word-2"]
    assert len(brain.options) == 1
    await chat.llm.aclose()


@pytest.mark.asyncio
async def test_next_user_turn_continues_the_live_process(tmp_path: Path) -> None:
    brain = Brain(turns=[[Text("one")], [Text("two")]])
    chat = Chat(provider(brain, tmp_path))

    await chat.turn("first")
    second = await chat.turn("second")

    assert second.content == "two"
    assert len(brain.options) == 1
    # Only the new turn is sent; Claude already has the rest.
    assert brain.received[1] == [{"type": "text", "text": "second"}]
    await chat.llm.aclose()


@pytest.mark.asyncio
async def test_restart_resumes_the_saved_claude_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    brain = Brain(turns=[[Text("one")], [Text("two")]])
    first = Chat(provider(brain, tmp_path))
    await first.turn("first")
    saved = first.controller.finish(first.messages)
    session = saved.payload["session"]
    await first.llm.aclose()

    async def transcript_ends_at_head(self: ClaudeSessions, session_id: str, head: str) -> bool:
        return True

    monkeypatch.setattr(ClaudeSessions, "_ends_at", transcript_ends_at_head)
    second = Chat(provider(brain, tmp_path), messages=first.messages, state=saved)
    response = await second.turn("again")

    assert response.content == "two"
    resumed = brain.options[-1]
    assert (resumed.resume, resumed.fork_session) == (session, False)
    assert brain.received[-1] == [{"type": "text", "text": "again"}]
    assert response.provider_state is not None
    assert response.provider_state.payload["session"] == session
    await second.llm.aclose()


@pytest.mark.asyncio
async def test_side_call_with_other_tools_forks_without_touching_the_chat(tmp_path: Path) -> None:
    brain = Brain(turns=[[Text("one")], [Text("summary")], [Text("two")]])
    chat = Chat(provider(brain, tmp_path))
    await chat.turn("first", tools=[WORD_TOOL])
    saved = chat.controller.finish(chat.messages)

    side_prompt = {"role": "user", "content": "summarize"}
    side = await chat.llm.chat_stream_with_retry(
        messages=[*chat.messages, side_prompt], tools=None, model=MODEL,
        provider_context=ProviderCallContext(
            conversation_state=saved.with_pending_messages([side_prompt]), session_id=KEY,
        ),
    )
    forked = brain.options[-1]
    assert side.content == "summary"
    assert (forked.resume, forked.fork_session, forked.resume_session_at) == (
        saved.payload["session"], True, saved.payload["head"],
    )

    after = await chat.turn("second", tools=[WORD_TOOL])
    assert after.content == "two"
    assert len(brain.options) == 2
    await chat.llm.aclose()


@pytest.mark.asyncio
async def test_history_from_other_models_is_sent_as_one_checkpoint(tmp_path: Path) -> None:
    brain = Brain(turns=[[Text("continuing")]])
    history = [
        SYSTEM,
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "get_word", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "name": "get_word", "content": "word-0"},
        {"role": "assistant", "content": "earlier answer"},
    ]
    chat = Chat(provider(brain, tmp_path), messages=history)

    await chat.turn("new question")

    sent = texts(brain.received[0])
    assert sent.startswith("<conversation-checkpoint>")
    for expected in ("earlier question", "word-0", "earlier answer", "new question"):
        assert expected in sent
    await chat.llm.aclose()


@pytest.mark.asyncio
async def test_cancelling_a_step_interrupts_claude_and_next_request_starts_fresh(tmp_path: Path) -> None:
    brain = Brain(turns=[[Text("one")], [Hang()], [Text("fresh")]])
    chat = Chat(provider(brain, tmp_path))
    await chat.turn("first")

    chat.messages.append({"role": "user", "content": "slow"})
    task = asyncio.create_task(chat.step())
    while len(brain.received) < 2:
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert brain.interrupts == 1
    chat.messages.pop()
    response = await chat.turn("after")
    assert response.content == "fresh"
    assert len(brain.options) == 2
    await chat.llm.aclose()


@pytest.mark.asyncio
async def test_plan_limit_error_is_reported_once_without_state(tmp_path: Path) -> None:
    brain = Brain(turns=[[ApiError("rate_limit")]])
    chat = Chat(provider(brain, tmp_path))

    response = await chat.turn("hi")

    assert response.finish_reason == "error"
    assert response.error_kind == "rate_limit"
    assert response.error_should_retry is False
    assert response.provider_state is None
    assert len(brain.options) == 1
    await chat.llm.aclose()


@pytest.mark.asyncio
async def test_request_without_session_is_not_persisted(tmp_path: Path) -> None:
    brain = Brain(turns=[[Text('{"ok": true}')]])
    llm = provider(brain, tmp_path)

    response = await llm.chat_stream_with_retry(
        messages=[SYSTEM, {"role": "user", "content": "json"}], model=MODEL,
    )

    assert response.content == '{"ok": true}'
    assert response.provider_state is None
    assert "no-session-persistence" in brain.options[0].extra_args
    assert list((tmp_path / "prompts").glob("*")) != []
    await llm.aclose()
    assert list((tmp_path / "prompts").glob("*")) == []


@pytest.mark.asyncio
async def test_old_claude_code_is_rejected_with_update_hint(tmp_path: Path) -> None:
    brain = Brain(turns=[[Text("unused")]], version="2.1.200")
    llm = provider(brain, tmp_path)

    response = await llm.chat_stream_with_retry(messages=[SYSTEM, {"role": "user", "content": "hi"}], model=MODEL)

    assert response.finish_reason == "error"
    assert response.content is not None and "claude update" in response.content
    await llm.aclose()
