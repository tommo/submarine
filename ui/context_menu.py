"""@ trigger detect + context-menu builder.

Port of old context_parser.py minus extract_context_marker and remove_trigger
(both dead).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple


@dataclass
class ContextMenuItem:
    """A single item in the context menu."""
    action: str  # "browse", "clear", "file"
    label: str
    description: str
    data: Any = None


@dataclass
class ContextTrigger:
    """Represents a detected @ trigger."""
    position: int
    triggered: bool = True


class ContextParser:
    """Parses input for @ context triggers and builds context menus."""

    TRIGGER_CHAR = "@"

    @staticmethod
    def check_trigger(text: str, cursor_pos: int) -> Optional[ContextTrigger]:
        if cursor_pos <= 0:
            return None
        char_before = text[cursor_pos - 1] if cursor_pos <= len(text) else ""
        if char_before == ContextParser.TRIGGER_CHAR:
            return ContextTrigger(position=cursor_pos)
        return None

    @staticmethod
    def build_menu(
        open_files: List[Tuple[str, str]],
        has_pending_context: bool = False,
        pending_count: int = 0,
    ) -> List[ContextMenuItem]:
        items = []
        items.append(ContextMenuItem(
            action="browse",
            label="Browse...",
            description="Choose file from project",
        ))
        if has_pending_context:
            plural = "s" if pending_count != 1 else ""
            items.append(ContextMenuItem(
                action="clear",
                label="Clear context",
                description="%d pending item%s" % (pending_count, plural),
            ))
        for name, path in open_files:
            items.append(ContextMenuItem(
                action="file",
                label=name,
                description=path,
                data=path,
            ))
        return items

    @staticmethod
    def format_menu_items(items: List[ContextMenuItem]) -> List[List[str]]:
        return [[item.label, item.description] for item in items]


class ContextMenuHandler:
    """Handles context menu selection and actions."""

    def __init__(
        self,
        on_browse: Callable[[], None],
        on_clear: Callable[[], None],
        on_add_file: Callable[[str, str], None],
    ):
        self.on_browse = on_browse
        self.on_clear = on_clear
        self.on_add_file = on_add_file

    def handle_selection(self, items: List[ContextMenuItem], index: int) -> None:
        if index < 0:
            return
        selected = items[index]
        if selected.action == "browse":
            self.on_browse()
        elif selected.action == "clear":
            self.on_clear()
        elif selected.action == "file":
            if selected.data:
                self.on_add_file(selected.data, "")
