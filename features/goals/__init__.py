"""Host-owned goal harness (backend-agnostic). Sublime-free except loop attach."""
from __future__ import annotations

from .tracker import (
    BLOCKED_STREAK_TO_PAUSE,
    DEFAULT_CONTINUE_MAX,
    DEFAULT_VERIFY_MAX,
    GOAL_RESERVED,
    OPEN_STATUSES,
    PAUSED,
    TERMINAL,
    GoalEvent,
    GoalTracker,
    format_goal_strip_line,
    parse_goal_slash,
    st_is_work,
    ui_phase_body,
    ui_phase_label,
)
from .loop import attach_goal_harness
from .skeptic import (
    GOAL_ROLE_SKEPTIC,
    MODE_TASK,
    is_goal_skeptic,
    parent_view_id_for_skeptic,
    prepare_task_verify_abort,
    preserve_working_after_verify_finish,
    resolve_skeptic_mode,
    resolve_verdict_session,
    skeptic_display_name,
)

__all__ = [
    "BLOCKED_STREAK_TO_PAUSE",
    "DEFAULT_CONTINUE_MAX",
    "DEFAULT_VERIFY_MAX",
    "GOAL_RESERVED",
    "GOAL_ROLE_SKEPTIC",
    "MODE_TASK",
    "OPEN_STATUSES",
    "PAUSED",
    "TERMINAL",
    "GoalEvent",
    "GoalTracker",
    "attach_goal_harness",
    "format_goal_strip_line",
    "is_goal_skeptic",
    "parent_view_id_for_skeptic",
    "parse_goal_slash",
    "prepare_task_verify_abort",
    "preserve_working_after_verify_finish",
    "resolve_skeptic_mode",
    "resolve_verdict_session",
    "skeptic_display_name",
    "st_is_work",
    "ui_phase_body",
    "ui_phase_label",
]
