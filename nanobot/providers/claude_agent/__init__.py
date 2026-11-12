"""Claude Agent provider: Claude models through the user's own Claude Code.

The SDK is an optional dependency (``nanobot-ai[claude-agent]``) and is only
imported when a Claude Agent request starts.
"""

from nanobot.providers.claude_agent.provider import ClaudeAgentProvider

__all__ = ["ClaudeAgentProvider"]
