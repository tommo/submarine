"""Shared ACP helpers: ANSI strip and monochrome spawn env.

Invariants: tool/terminal text must stay parseable without CSI leftovers
(paid for by garbled ⚙ rows). apply_plain_terminal_env is used by
terminal/create and Grok spawn (§9.32).
"""
from __future__ import annotations

import re


# CSI / OSC sequences leftover when tools ignore NO_COLOR.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))"
)

def strip_ansi(text: str) -> str:
    """Remove ANSI color/style codes from tool/terminal text."""
    if not text or "\x1b" not in text:
        return text or ""
    return _ANSI_ESCAPE_RE.sub("", text)

def apply_plain_terminal_env(env: dict) -> dict:
    """Force monochrome non-TTY env for agent-spawned shells/tools."""
    env["TERM"] = "dumb"
    env.pop("COLORTERM", None)
    env["NO_COLOR"] = "1"
    env["FORCE_COLOR"] = "0"
    env["CLICOLOR"] = "0"
    env["CLICOLOR_FORCE"] = "0"
    env["PAGER"] = "cat"
    env["GIT_PAGER"] = "cat"
    env["DEBIAN_FRONTEND"] = "noninteractive"
    # Line-buffer Python / many CLIs when stdout is a pipe (ACP terminal).
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env
