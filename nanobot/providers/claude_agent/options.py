"""Claude Agent SDK options that run Claude Code as a bare model caller.

Claude Code makes the model request; nanobot keeps its own tools, permissions,
memory, compaction and subagents. Everything Claude Code would otherwise load
from the user's own setup is switched off here.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions

CLAUDE_PATH_ENV = "NANOBOT_CLAUDE_PATH"

# Claude Code features that duplicate nanobot's own. nanobot's version wins.
ISOLATION_ENV: dict[str, str] = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "DISABLE_AUTO_COMPACT": "1",
    "CLAUDE_CODE_ENABLE_AWAY_SUMMARY": "0",
    # All nanobot tools reach Claude as MCP tools; tool search would hide them.
    "ENABLE_TOOL_SEARCH": "false",
    "CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "1",
    # A non-streaming retry would leave nanobot waiting for a step that never ends.
    "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
    # A refusal fallback would silently switch away from the selected model.
    "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK": "1",
    # nanobot truncates tool output itself.
    "MAX_MCP_OUTPUT_TOKENS": "200000",
    # The SDK can only override inherited variables, not remove them.
    # Simple mode skips the subscription login.
    "CLAUDE_CODE_SIMPLE": "0",
    # nanobot tool calls include subagents and permission prompts that can run for a long time.
    "MCP_TOOL_TIMEOUT": str(24 * 60 * 60 * 1000),
    # nanobot's own prompt decides what Claude knows about git.
    "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
}

# A named session skips Claude Code's extra model call that writes a session title.
SESSION_NAME = "nanobot"

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_EFFORT_ALIASES = {"minimal": "low"}


def find_claude() -> str | None:
    """Return the Claude Code executable nanobot should run."""
    configured = os.environ.get(CLAUDE_PATH_ENV, "").strip()
    if configured:
        return configured
    return shutil.which("claude")


def resolve_effort(value: str | None) -> str | None:
    if not value:
        return None
    effort = _EFFORT_ALIASES.get(value.lower(), value.lower())
    return effort if effort in EFFORT_LEVELS else None


@dataclass(frozen=True)
class ProcessConfig:
    """Everything that requires a new Claude Code process when it changes."""

    executable: str
    model: str
    effort: str | None
    system_prompt_file: Path
    tool_names: tuple[str, ...]
    # Claude Code reports this folder to the model and keeps its transcripts under it.
    cwd: Path
    connection_env: tuple[tuple[str, str], ...] = ()


def build_options(
    config: ProcessConfig,
    *,
    mcp_server: Any | None,
    hooks: dict[str, Any] | None,
    session_id: str | None = None,
    resume: str | None = None,
    resume_at: str | None = None,
    fork: bool = False,
    persist: bool = True,
    stderr: Any = None,
) -> ClaudeAgentOptions:
    from claude_agent_sdk import ClaudeAgentOptions

    env = {**dict(config.connection_env), **ISOLATION_ENV}
    if config.effort is not None:
        env["CLAUDE_CODE_EFFORT_LEVEL"] = config.effort
    extra_args: dict[str, str | None] = {"name": SESSION_NAME}
    if not persist:
        extra_args["no-session-persistence"] = None
    options = ClaudeAgentOptions(
        cli_path=config.executable,
        cwd=str(config.cwd),
        model=config.model,
        effort=config.effort,  # type: ignore[arg-type]
        system_prompt={"type": "file", "path": str(config.system_prompt_file)},
        tools=[],
        allowed_tools=["mcp__nanobot__*"],
        permission_mode="dontAsk",
        setting_sources=[],
        strict_mcp_config=True,
        skills=[],
        settings=json.dumps({"disableAllHooks": True}),
        verbatim_prompts=True,
        include_partial_messages=True,
        thinking={"type": "adaptive", "display": "summarized"},
        env=env,
        extra_args=extra_args,
        mcp_servers=(
            {"nanobot": {"type": "sdk", "name": "nanobot", "instance": mcp_server}}
            if mcp_server is not None
            else {}
        ),
        hooks=hooks,  # type: ignore[arg-type]
        stderr=stderr,
    )
    if resume is not None:
        options.resume = resume
        options.fork_session = fork
        if resume_at is not None:
            options.resume_session_at = resume_at
    elif session_id is not None:
        options.session_id = session_id
    return options
