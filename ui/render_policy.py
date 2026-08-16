"""Pure helpers: incremental-append vs full rewrite; tasks fold; history cap."""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

from .models import HISTORY_CAP, ToolCall, _open_todos, _todo_is_active


def cap_history(conversations: list) -> Tuple[list, int]:
    """Keep the last HISTORY_CAP turns. Returns (kept, dropped)."""
    if len(conversations) <= HISTORY_CAP:
        return conversations, 0
    dropped = len(conversations) - HISTORY_CAP
    return conversations[-HISTORY_CAP:], dropped


def tasks_fold_rows(open_todos: list, expanded: bool) -> Tuple[list, int]:
    """Return (visible_todos, hidden_count).

    Folded: all in_progress + pending cap 3 (3 - len(active)).
    Expanded: all open todos.
    """
    if not open_todos:
        return [], 0
    active = [t for t in open_todos if _todo_is_active(t)]
    pending = [t for t in open_todos if not _todo_is_active(t)]
    if expanded:
        show = list(open_todos)
    else:
        cap = max(0, 3 - len(active))
        show = active + pending[:cap]
    hidden = len(open_todos) - len(show)
    return show, hidden


def text_events_joined(events: list) -> str:
    parts = []
    for e in events:
        if isinstance(e, str):
            parts.append(e)
    return "".join(parts)


def last_event_is_text(events: list) -> bool:
    return bool(events) and isinstance(events[-1], str)


def should_incremental_append(
        prev_event_count: int,
        prev_joined_text: str,
        events: list,
        structural: bool,
) -> Optional[str]:
    """Return the text delta to append, or None if a full rewrite is required.

    Incremental only when:
      - no structural dirty flag (tool upsert, meta, tasks fold, question)
      - the event list only grew text (same prefix count, last event is str)
      - joined text grew as a suffix of the previous projection
    """
    if structural:
        return None
    if not last_event_is_text(events):
        return None
    joined = text_events_joined(events)
    if not joined.startswith(prev_joined_text):
        return None
    # Event count may stay the same (merge into last str) or grow by str-only.
    if len(events) < prev_event_count:
        return None
    extra_events = events[prev_event_count:]
    if any(not isinstance(e, str) for e in extra_events):
        return None
    delta = joined[len(prev_joined_text):]
    if not delta:
        return None
    return delta
