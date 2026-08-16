"""L4 conversation surface. sublime imports live in sheet/composer/view/listeners.

Sublime-free public surface (safe under plain python3):
  models, geometry, command_parser, formatters, tools, render_policy, context_menu.
"""
from __future__ import annotations

from .command_parser import CommandDef, CommandParser, SlashCommand
from .context_menu import ContextMenuHandler, ContextMenuItem, ContextParser, ContextTrigger
from .geometry import (
    OWNER_DRAFT,
    OWNER_HISTORY,
    classify_regions,
    clamp_region_to_draft,
    crosses_draft_boundary,
    draft_select_range,
    history_select_range,
    mutation_allowed_in_draft,
    owner_from_geometry,
    stream_may_force_bottom,
    stream_may_move_caret,
    stream_tick_actions,
    stream_treat_as_composing,
    wholly_in_draft,
    wholly_in_history,
)
from .models import (
    BACKGROUND,
    DONE,
    ERROR,
    HISTORY_CAP,
    PENDING,
    PERM_ALLOW,
    PERM_ALLOW_ALL,
    PERM_ALLOW_SESSION,
    PERM_DENY,
    PLAN_APPROVE,
    PLAN_REJECT,
    PLAN_VIEW,
    Conversation,
    GoalState,
    PermissionRequest,
    PlanApproval,
    QuestionRequest,
    TodoItem,
    ToolCall,
    _goal_is_open,
    _open_todos,
    _todo_is_active,
    _todo_status_norm,
    format_goal_strip_line,
    goal_strip_body,
    goal_strip_label,
    strip_title_decoration,
)
from .tools import is_host_control_tool, may_background

__all__ = [
    "CommandDef", "CommandParser", "SlashCommand",
    "ContextParser", "ContextMenuHandler", "ContextMenuItem", "ContextTrigger",
    "OWNER_DRAFT", "OWNER_HISTORY",
    "classify_regions", "clamp_region_to_draft", "crosses_draft_boundary",
    "draft_select_range", "history_select_range", "mutation_allowed_in_draft",
    "owner_from_geometry", "stream_may_force_bottom", "stream_may_move_caret",
    "stream_tick_actions", "stream_treat_as_composing",
    "wholly_in_draft", "wholly_in_history",
    "BACKGROUND", "DONE", "ERROR", "HISTORY_CAP", "PENDING",
    "PERM_ALLOW", "PERM_ALLOW_ALL", "PERM_ALLOW_SESSION", "PERM_DENY",
    "PLAN_APPROVE", "PLAN_REJECT", "PLAN_VIEW",
    "Conversation", "GoalState", "PermissionRequest", "PlanApproval",
    "QuestionRequest", "TodoItem", "ToolCall",
    "_goal_is_open", "_open_todos", "_todo_is_active", "_todo_status_norm",
    "format_goal_strip_line", "goal_strip_body", "goal_strip_label",
    "strip_title_decoration",
    "is_host_control_tool", "may_background",
]
