"""Plugin identity, UI tokens, and path constants. Sublime-free."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# ─── Application ──────────────────────────────────────────────────────────────
PLUGIN_NAME = "Submarine"
APP_NAME = "Submarine"
DEFAULT_SESSION_NAME = "Submarine"
SETTINGS_FILE = "Submarine.sublime-settings"

# ─── User directories ─────────────────────────────────────────────────────────
USER_HOME = Path.home()
# Claude CLI cascade lives under ~/.claude — that is the CLI's directory, not
# plugin identity. Plugin-owned files go under ~/.submarine.
CLAUDE_USER_SETTINGS_FILE = USER_HOME / ".claude.json"
USER_SETTINGS_FILE = CLAUDE_USER_SETTINGS_FILE
USER_PROFILES_DIR = USER_HOME / ".submarine"
ARTIFACTS_DIR = USER_PROFILES_DIR / "artifacts"

# ─── Project directories (Claude CLI layout, still read by the cascade) ───────
PROJECT_SETTINGS_DIR = ".claude"
PROJECT_SETTINGS_FILE = "settings.json"
PROJECT_LOCAL_SETTINGS_FILE = "settings.local.json"
PROJECT_SUBLIME_TOOLS_DIR = ".claude/sublime_tools"

# ─── File names ───────────────────────────────────────────────────────────────
PROFILES_FILE = "profiles.json"
SESSIONS_FILE = ".sessions.json"
MCP_CONFIG_FILE = ".mcp.json"

# ─── Socket & IPC ─────────────────────────────────────────────────────────────
MCP_SOCKET_PATH = os.path.join(tempfile.gettempdir(), "submarine_mcp.sock")

# ─── Logging ──────────────────────────────────────────────────────────────────
BRIDGE_LOG_PATH = os.path.join(tempfile.gettempdir(), "submarine_bridge.log")
LOG_PREFIX_INFO = "  "
LOG_PREFIX_ERROR = "ERROR: "

# ─── View settings ────────────────────────────────────────────────────────────
OUTPUT_VIEW_SETTING = "submarine_output"
FONT_SIZE = 12

# ─── Status glyphs (◉ • ◐ ○ ◇ ⏸ ❓ ⚠ ↻) ──────────────────────────────────────
STATUS_ACTIVE_WORKING = "◉"      # Active + responding (streaming)
STATUS_INACTIVE_WORKING = "•"    # Inactive + responding
STATUS_ACTIVE_WAITING = "◐"      # Active + waiting (model/tool gap)
STATUS_INACTIVE_WAITING = "○"    # Inactive + waiting
STATUS_IDLE = "◇"                # Active or inactive, idle
STATUS_ACTIVE_IDLE = STATUS_IDLE
STATUS_INACTIVE_IDLE = STATUS_IDLE
STATUS_SLEEPING = "⏸"            # Session sleeping (bridge down)
STATUS_QUESTION = "❓"           # Permission / question / plan pending
STATUS_ERROR_HALT = "⚠"          # Turn/bridge error — idle but failed
STATUS_WAKE = "↻"                # Pending self-wake (/loop)

# Turn activity (session.turn_phase)
TURN_PHASE_IDLE = "idle"
TURN_PHASE_WAITING = "waiting"
TURN_PHASE_RESPONDING = "responding"
TURN_PHASE_TOOL = "tool"

# Busy animations (glyph only — never "waiting"/"responding" labels).
SPINNER_WAITING = "◇◈◆◈"
SPINNER_RESPONDING = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_FRAMES = SPINNER_RESPONDING

# ─── Input / UI markers ───────────────────────────────────────────────────────
INPUT_MARKER = "◎ "
CONTEXT_PREFIX = "📎 "
BACKGROUND_PREFIX = "⚙ "
CONTEXT_TRIGGER_CHAR = "@"

# ─── Backend display (built-in fallbacks; custom providers carry their own) ───
BACKEND_ABBREV = {
    "codex": "CX",
    "pi": "Pi",
    "grok": "GR",
    "kimi": "KM",
}
BACKEND_LABELS = {
    "claude": "Claude",
    "codex": "Codex",
    "pi": "Pi",
    "grok": "Grok",
    "kimi": "Kimi Code",
}

# ─── Timing ───────────────────────────────────────────────────────────────────
CONTEXT_DEBOUNCE_MS = 300
INPUT_RETRY_DELAY_MS = 500
RECONNECT_DELAY_MS = 100

# ─── Limits ───────────────────────────────────────────────────────────────────
DEFAULT_FIND_FILE_LIMIT = 20
DEFAULT_GET_SYMBOLS_LIMIT = 10
MAX_LINE_LENGTH = 2000

# ─── Buffer sizes ─────────────────────────────────────────────────────────────
BRIDGE_BUFFER_SIZE = 1073741824  # 1GB for StreamReader

# ─── Permission modes (bridge initialize.permission_mode) ─────────────────────
PERMISSION_MODE_DEFAULT = "default"
PERMISSION_MODE_ACCEPT_EDITS = "acceptEdits"
PERMISSION_MODE_BYPASS = "bypassPermissions"
PERMISSION_MODE_PLAN = "plan"
PERMISSION_MODE_AUTO = "auto"

PERMISSION_MODES = [
    PERMISSION_MODE_DEFAULT,
    PERMISSION_MODE_ACCEPT_EDITS,
    PERMISSION_MODE_PLAN,
    PERMISSION_MODE_BYPASS,
    PERMISSION_MODE_AUTO,
]

PERMISSION_MODE_LABELS = {
    PERMISSION_MODE_DEFAULT: "Default (prompt for all)",
    PERMISSION_MODE_ACCEPT_EDITS: "Accept Edits (auto-allow Read/Edit/Write)",
    PERMISSION_MODE_PLAN: "Plan (read-only until approved)",
    PERMISSION_MODE_BYPASS: "Bypass All (auto-allow everything)",
    PERMISSION_MODE_AUTO: "Auto",
}

# ─── Permission button values (output modal → session) ────────────────────────
PERM_ALLOW = "allow"
PERM_DENY = "deny"
PERM_ALLOW_ALL = "allow_all"
PERM_ALLOW_SESSION = "allow_session"

# ─── Plan-approval button values ──────────────────────────────────────────────
PLAN_APPROVE = "approve"
PLAN_REJECT = "reject"
PLAN_VIEW = "view"

# ─── Tool status ──────────────────────────────────────────────────────────────
PENDING = "pending"
DONE = "done"
ERROR = "error"
BACKGROUND = "background"

TOOL_STATUS_PENDING = PENDING
TOOL_STATUS_DONE = DONE
TOOL_STATUS_ERROR = ERROR
TOOL_STATUS_BACKGROUND = BACKGROUND

TOOL_STATUS_SYMBOLS = {
    PENDING: "☐",
    DONE: "✔",
    ERROR: "✘",
    BACKGROUND: "⚙",
}

# ─── Session process states ───────────────────────────────────────────────────
SESSION_STATE_UNINITIALIZED = "uninitialized"
SESSION_STATE_INITIALIZING = "initializing"
SESSION_STATE_READY = "ready"
SESSION_STATE_WORKING = "working"
SESSION_STATE_ERROR = "error"
