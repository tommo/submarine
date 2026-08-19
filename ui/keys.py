"""View-setting keys: always WRITE submarine_*; READ falls back to claude_*.

One-time migration: a successful read of a legacy key is rewritten to the
new name so later lookups stay on submarine_*.
"""
from __future__ import annotations

from typing import Any, Optional


# New keys (always written).
OUTPUT = "submarine_output"
HOST = "submarine_host"
SESSION_ID = "submarine_session_id"
AGENT_ID = "submarine_agent_id"
BACKEND = "submarine_backend"
MODEL = "submarine_model"
EFFORT = "submarine_effort"
PROVIDER_LABEL = "submarine_provider_label"
SLEEPING = "submarine_sleeping"
INPUT_MODE = "submarine_input_mode"
ERROR_HALTED = "submarine_error_halted"
SOFT_CLOSE = "submarine_soft_close"
RECONNECTING = "submarine_reconnecting"
QUICK = "submarine_quick"
QUICK_PANEL = "submarine_quick_panel"
QUICK_SOFT_CLOSE = "submarine_quick_soft_close"
TASKS_EXPANDED = "submarine_tasks_expanded"
SUBSESSION_ID = "submarine_subsession_id"
PARENT_AGENT_ID = "submarine_parent_agent_id"
QUESTION_INPUT_MODE = "submarine_question_input_mode"

# Window settings
ACTIVE_AGENT = "submarine_active_agent"
ACTIVE_VIEW = "submarine_active_view"
CREATING_SESSION = "submarine_creating_session"
PENDING_CONTEXT_SESSION = "submarine_pending_context_session"
PENDING_CONTEXT_TIME = "submarine_pending_context_time"

# Session list
SESSION_LIST = "submarine_session_list"
SESSION_LIST_ROWS = "submarine_session_list_rows"

# Context keys (keymap) — new names; listeners also answer the old claude_* ones.
CARET_IN_DRAFT = "submarine_caret_in_draft"
SELECTION_IN_HISTORY = "submarine_selection_in_history"
SELECTION_CROSSES_DRAFT = "submarine_selection_crosses_draft"
OUTSIDE_INPUT_AREA = "submarine_outside_input_area"
SUBMIT_WITH_MODIFIER = "submarine_submit_with_modifier"

# Named regions
CONV_REGION = "submarine_conversation"
PERM_BLOCK = "submarine_permission_block"
PERM_BTN_PREFIX = "submarine_btn_"
PLAN_BLOCK = "submarine_plan_block"
PLAN_BTN_PREFIX = "submarine_plan_btn_"
QUESTION_BLOCK = "submarine_question_block"
QUESTION_KEYS = "submarine_question_keys"
QUESTION_INPUT_MARKER = "submarine_question_input_marker"

# Phantom set names
PHANTOM_PAD = "submarine_composer_pad"
PHANTOM_MEDIA = "submarine_media"
PHANTOM_CONTEXT = "submarine_context"
PHANTOM_TURN_CONTEXT = "submarine_turn_context"
PHANTOM_SLEEP = "submarine_sleep"
PHANTOM_QUEUE = "submarine_queue"
PHANTOM_WAKEUP = "submarine_wakeup"
PHANTOM_PERM_BANNER = "submarine_permission_banner"

# Text commands used for buffer edits
CMD_INSERT = "submarine_insert"
CMD_REPLACE = "submarine_replace"
CMD_CLEAR_ALL = "submarine_clear_all"

# Resource paths
PACKAGE = "Packages/Submarine"
SYNTAX_PATH = PACKAGE + "/SubmarineOutput.sublime-syntax"
THEME_DEFAULT = PACKAGE + "/SubmarineOutput.hidden-tmTheme"
THEME_CODEX = PACKAGE + "/SubmarineOutput-codex.hidden-tmTheme"
THEME_QUICK = PACKAGE + "/SubmarineOutput-quick.hidden-tmTheme"
OUTPUT_SETTINGS = "SubmarineOutput.sublime-settings"
PLUGIN_SETTINGS = "Submarine.sublime-settings"
SESSION_LIST_SYNTAX = PACKAGE + "/SessionList.sublime-syntax"
SESSION_LIST_SCHEME = PACKAGE + "/SessionList.hidden-color-scheme"

# Legacy claude_* twins used only as a READ fallback.
_LEGACY = {
    OUTPUT: "claude_output",
    SESSION_ID: "claude_session_id",
    AGENT_ID: "claude_agent_id",
    BACKEND: "claude_backend",
    MODEL: "claude_model",
    EFFORT: "claude_effort",
    PROVIDER_LABEL: "claude_provider_label",
    SLEEPING: "claude_sleeping",
    INPUT_MODE: "claude_input_mode",
    ERROR_HALTED: "claude_error_halted",
    SOFT_CLOSE: "claude_soft_close",
    RECONNECTING: "claude_reconnecting",
    QUICK: "claude_quick",
    QUICK_PANEL: "claude_quick_panel",
    QUICK_SOFT_CLOSE: "claude_quick_soft_close",
    TASKS_EXPANDED: "claude_tasks_expanded",
    SUBSESSION_ID: "claude_subsession_id",
    PARENT_AGENT_ID: "claude_parent_agent_id",
    QUESTION_INPUT_MODE: "claude_question_input_mode",
    ACTIVE_VIEW: "claude_active_view",
    CREATING_SESSION: "claude_creating_session",
    PENDING_CONTEXT_SESSION: "claude_pending_context_session",
    PENDING_CONTEXT_TIME: "claude_pending_context_time",
    SESSION_LIST: "claude_session_list",
    SESSION_LIST_ROWS: "claude_session_list_rows",
}


def read_setting(settings, key: str, default: Any = None) -> Any:
    """Read a submarine_* key; fall back to the old claude_* name and migrate."""
    if settings is None:
        return default
    val = settings.get(key)
    if val is not None:
        return val
    legacy = _LEGACY.get(key)
    if not legacy:
        return default
    val = settings.get(legacy)
    if val is None:
        return default
    try:
        settings.set(key, val)
    except Exception:
        pass
    return val


def write_setting(settings, key: str, value: Any) -> None:
    """Always write the new key (never the legacy twin)."""
    if settings is None:
        return
    settings.set(key, value)


def erase_setting(settings, key: str) -> None:
    if settings is None:
        return
    try:
        settings.erase(key)
    except Exception:
        pass
    legacy = _LEGACY.get(key)
    if legacy:
        try:
            settings.erase(legacy)
        except Exception:
            pass


def is_output_view(view) -> bool:
    if not view:
        return False
    try:
        st = view.settings()
    except Exception:
        return False
    return bool(read_setting(st, OUTPUT, False))
