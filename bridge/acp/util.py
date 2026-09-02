"""Shared ACP helpers: ANSI strip and monochrome spawn env.

Invariants: tool/terminal text must stay parseable without CSI leftovers
(paid for by garbled ⚙ rows). apply_plain_terminal_env is used by
terminal/create and Grok spawn (§9.32).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class AcpCall:
    """One ACP toolCallId. Per-call state lives here, not sidecar maps."""
    id: str
    name: str = ""
    input: Dict[str, Any] = field(default_factory=dict)
    title: str = ""
    background: bool = False
    emitted: bool = False
    result_sent: bool = False

    def merge_input(self, extra: Optional[dict]) -> dict:
        if extra:
            self.input = {**self.input, **extra}
        return self.input

    def set_name(self, name: Optional[str]) -> str:
        if name and name != "tool":
            self.name = name
        return self.name or name or "tool"

    def close(self) -> None:
        """Result painted; keep the object so late updates don't reopen."""
        self.result_sent = True
        self.emitted = False
        self.background = False


# CSI / OSC sequences leftover when tools ignore NO_COLOR.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))"
)

def strip_ansi(text: str) -> str:
    """Remove ANSI color/style codes from tool/terminal text."""
    if not text or "\x1b" not in text:
        return text or ""
    return _ANSI_ESCAPE_RE.sub("", text)


def retain_terminal_tail(text: str, limit: int) -> str:
    """ACP outputByteLimit: drop the prefix, keep the last `limit` UTF-8 bytes."""
    if limit <= 0:
        return ""
    if not text:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    start = len(raw) - limit
    while start < len(raw) and raw[start] & 0xC0 == 0x80:
        start += 1
    return raw[start:].decode("utf-8")


def _acp_is_compact_text(text: str) -> bool:
    """kimi auto-compact / /compact client-visible phrases."""
    low = (text or "").lower()
    return (
        "compacting conversation context" in low
        or "compaction completed" in low
        or "compaction started" in low
        or "compacting context" in low
        or "context compaction" in low
    )


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
