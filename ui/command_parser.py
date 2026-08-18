"""Slash command parsing for /command syntax."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass
class SlashCommand:
    """A parsed slash command."""
    name: str
    args: str
    raw: str


@dataclass
class CommandDef:
    """Definition of a slash command."""
    name: str
    description: str
    handler: Optional[Callable] = None


class CommandParser:
    """Parses input for /command patterns."""

    BUILTIN_COMMANDS = {
        "clear": "Harness /clear — new conversation, same process",
        "restart": "Same as /clear — new conversation in this view",
        "restart-new": "Same as /clear — new conversation in this view",
        "compact": "Summarize conversation to reduce context",
        "context": "Show pending context items",
        "rename": "Rename this session: /rename <title>",
        "goal": "Goal mode: /goal <obj> [--budget N] | status|pause|resume|clear",
    }

    @staticmethod
    def parse(text: str) -> Optional[SlashCommand]:
        text = text.strip()
        if not text.startswith("/"):
            return None
        parts = text[1:].split(None, 1)
        if not parts:
            return None
        name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        return SlashCommand(name=name, args=args, raw=text)

    @staticmethod
    def is_builtin(name: str) -> bool:
        return name in CommandParser.BUILTIN_COMMANDS

    @staticmethod
    def get_completions() -> List[CommandDef]:
        return [
            CommandDef(name=name, description=desc)
            for name, desc in CommandParser.BUILTIN_COMMANDS.items()
        ]
