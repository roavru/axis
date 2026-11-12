"""Check that Claude Code is installed, recent enough and signed in."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Literal, cast

from nanobot.providers.claude_agent.options import find_claude
from nanobot.providers.claude_agent.session import MIN_VERSION, version_tuple

_TIMEOUT_S = 15


@dataclass(frozen=True)
class ClaudeStatus:
    state: Literal["missing", "outdated", "logged_out", "ready", "failed"]
    message: str
    version: str | None = None
    auth_method: str | None = None


def _run(executable: str, *args: str) -> str:
    completed = subprocess.run(
        [executable, *args],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        check=False,
    )
    return completed.stdout.strip()


def probe() -> ClaudeStatus:
    """Inspect the local Claude Code without reading its saved credentials."""
    executable = find_claude()
    if executable is None:
        return ClaudeStatus(
            "missing",
            "Claude Code is not installed. Install it, or set NANOBOT_CLAUDE_PATH.",
        )
    try:
        version = _run(executable, "--version").split(" ", 1)[0] or None
        if version is None or version_tuple(version) < MIN_VERSION:
            required = ".".join(str(part) for part in MIN_VERSION)
            return ClaudeStatus(
                "outdated",
                f"Claude Code {version or 'unknown'} is too old; {required} or later is required. Run `claude update`.",
                version=version,
            )
        raw = _run(executable, "auth", "status")
        status = cast(dict[str, Any], json.loads(raw)) if raw.startswith("{") else {}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return ClaudeStatus("failed", f"Could not check Claude Code: {exc}")
    if not status.get("loggedIn"):
        return ClaudeStatus(
            "logged_out",
            "Claude Code is not signed in. Run `claude auth login` "
            "(or `claude setup-token` on a server without a browser).",
            version=version,
        )
    auth_method = str(status.get("authMethod") or "") or None
    return ClaudeStatus("ready", "Claude Code is ready.", version=version, auth_method=auth_method)


_CACHE_TTL_S = 60.0
_cached: tuple[float, ClaudeStatus] | None = None


def cached_probe() -> ClaudeStatus:
    """Probe at most once a minute, since each probe starts two processes."""
    global _cached
    now = time.monotonic()
    if _cached is None or now - _cached[0] > _CACHE_TTL_S:
        _cached = (now, probe())
    return _cached[1]
