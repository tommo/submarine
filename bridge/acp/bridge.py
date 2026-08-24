"""AcpBridge composition: __init__, subclass hooks, run_bridge.

Mixins hold protocol slices; this module owns process-lifetime
state and the hooks Kimi/Grok override (agent_argv, spawn_env,
normalize_model, apply_model via SessionMixin, …).
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List, Optional

_BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_DIR)

from base import BaseBridge  # noqa: E402
from rpc_helpers import process_cwd  # noqa: E402
from acp.ask_user import AskUserMixin  # noqa: E402
from acp.background import BackgroundMixin  # noqa: E402
from acp.fs import FsMixin  # noqa: E402
from acp.permissions import PermissionsMixin  # noqa: E402
from acp.plan_mode import PlanModeMixin  # noqa: E402
from acp.query import QueryMixin  # noqa: E402
from acp.rewind import RewindMixin  # noqa: E402
from acp.scheduler import SchedulerMixin  # noqa: E402
from acp.session import SessionMixin  # noqa: E402
from acp.terminal import TerminalMixin  # noqa: E402
from acp.tools import ToolsMixin  # noqa: E402
from acp.transport import TransportMixin  # noqa: E402
from acp.updates import UpdatesMixin  # noqa: E402

class AcpBridge(TransportMixin, SessionMixin, UpdatesMixin,
               ToolsMixin, QueryMixin, PermissionsMixin, AskUserMixin,
               PlanModeMixin, SchedulerMixin, BackgroundMixin,
               TerminalMixin, FsMixin, RewindMixin,
               BaseBridge):
    """Protocol-level ACP client; agent-specific details live in subclasses."""

    BACKEND_NAME: str = "acp"

    # Opt-in vision MCP tool (mcp__submarine__read_image). Default off for most
    # ACP agents; GrokBridge sets True. Overridable via initialize param
    # mcp_enable_read_image / settings mcp_enable_read_image.
    MCP_ENABLE_READ_IMAGE: bool = False

    DEFAULT_MODEL: str = ""

    CLIENT_NAME: str = "submarine"

    CLIENT_VERSION: str = "0.2"

    LOG_PATH: str = os.path.join(
        os.environ.get("TMPDIR")
        or os.environ.get("TEMP")
        or os.environ.get("TMP")
        or "/tmp",
        "submarine_acp_bridge.log",
    )

    # Agent tool name → Claude canonical formatter name.
    TOOL_TO_CANONICAL: Dict[str, str] = {}

    # Claude permission_mode → agent modeId for session/set_mode.
    PERM_TO_MODE: Dict[str, str] = {}

    MODE_TO_PERM: Dict[str, str] = {}

    MODEL_ALIASES: Dict[str, str] = {}

    def __init__(self) -> None:
        super().__init__()
        # Agent process + ACP request waiters
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.session_id: Optional[str] = None
        self.next_acp_id: int = 0
        self.pending: Dict[int, asyncio.Future] = {}
        self.reader_task: Optional[asyncio.Task] = None
        # Model / cwd / mode (DEFAULT_MODEL is a spawn placeholder)
        self.model: str = self.DEFAULT_MODEL
        # True only when the host sent initialize.model. DEFAULT_MODEL is a
        # spawn placeholder — do not force it onto a resumed session.
        self._host_model: bool = False
        self.effort: str = ""  # reasoning effort (low/medium/high/…); empty = agent default
        self.cwd: str = process_cwd()
        self.agent_mode: str = ""
        self._agent_id: Optional[Any] = None
        # Vision MCP + negotiated ACP caps
        self._mcp_enable_read_image: bool = bool(
            getattr(self, "MCP_ENABLE_READ_IMAGE", False))
        self.agent_capabilities: Dict[str, Any] = {}
        self.negotiated_protocol_version: int = 1
        # Live ACP terminals (real subprocesses)
        self._terminals: Dict[str, Dict[str, Any]] = {}
        # Generic ACP bg: tool_use ids marked ⚙ + terminalId → job.
        # Kimi bash-*.json tracking lives on KimiBgMixin, not here.
        self._bg_tool_ids: set = set()
        self._terminal_bg: Dict[str, Dict[str, Any]] = {}
        self._bg_notified_tasks: set = set()
        self._bg_notified_tools: set = set()
        # toolCallIds already shown as tool_use (avoid duplicate ☐ rows).
        self._tool_ids_emitted: set = set()
        # toolCallIds already closed with tool_result (avoid a second ✔ row).
        self._tool_results_sent: set = set()
        # Secondary ExitPlanMode/… toolCallId → primary open id (one UI row).
        self._tool_id_alias: Dict[str, str] = {}
        # Session/load + plan/mode advertisement
        self._loading_session: bool = False
        self._in_plan_mode: bool = False
        self._available_modes: List[dict] = []
        self._available_models: List[dict] = []
        self._resumed: bool = False
        self._resume_fallback: bool = False
        self._additional_dirs: List[str] = []
        self._system_prompt: str = ""
        self._auth_methods: List[dict] = []
        self._init_meta: Dict[str, Any] = {}
        # Plugin permission surface (mirrors Claude can_use_tool / settings).
        self.permission_mode: str = "default"
        self.allowed_tools: List[str] = []
        self._auto_allow_patterns: List[str] = []
        self._prompt_cancelled: bool = False
        self._prompt_fut: Optional[asyncio.Future] = None
        self._prompt_acp_id: Optional[int] = None
        # True from first cancel notify until query fully settles — blocks
        # spam session/cancel (Grok ChatStateActor dies on cancel-after-done).
        self._cancel_in_flight: bool = False
        # AskUser Q1+/Other: inject after the elicitation/permission RPC
        # reply is on the wire (kimi 0.37.2 drops non-enum answers).
        self._pending_ask_followup: Optional[str] = None
        # Grok scheduler: track next fire for loop banner / wakes.
        self._schedule_next_fire: Optional[float] = None
        # toolCallId → last known input (completed updates often omit rawInput).
        self._tool_inputs_by_id: Dict[str, dict] = {}
        # toolCallId → normalized name (completed updates often omit title/_meta).
        self._tool_names_by_id: Dict[str, str] = {}
        self._last_execute_id: Optional[str] = None
        self._pending_execute_ids: List[str] = []
        self._last_bg_tool_id: Optional[str] = None
        self._agent_exited: bool = False
        # Client-side backup timers when host does not inject scheduled prompts.
        # task_id (or toolCallId) → asyncio.Task
        self._client_schedule_tasks: Dict[str, Any] = {}
        # Grok multiplexes subagent session/update on the parent ACP pipe with
        # a different sessionId. Count drops so we can log without spam.
        self._foreign_session_drops: int = 0
        # child sessionId → {text, done, exit, event}. Grok polls these via
        # terminal/output (same RPC as shells).
        self._child_sessions: Dict[str, Dict[str, Any]] = {}
        self._released_terminals: set = set()
        # timeout:0 / run_in_background: Grok release()s while the process
        # must keep running. Snap last output; do not SIGTERM.
        self._detached_snaps: Dict[str, dict] = {}
        self._detached_procs: Dict[str, Any] = {}
        # After Esc the prompt RPC is done; Grok may still fire turn_completed.
        # leftover_end closes interrupt leftover busy — not a normal @done.
        self._leftover_end_pending: bool = False
        # Serialize writes to agent stdin — concurrent create_task handlers
        # (permission + terminal + fs) would otherwise interleave JSON lines.
        self._acp_write_lock: Optional[asyncio.Lock] = None
        # terminal/wait_for_exit: 0 = wait until process exits (ACP default).
        # Positive = optional client-side cap (seconds). Long agent tools
        # (codex exec, builds) need unlimited wait; use terminal/kill to abort.
        self.terminal_wait_timeout_s: float = float(
            os.environ.get("SUBMARINE_TERM_TIMEOUT", "0") or 0)
        # Hard gates (fail / drop), not soft truncation of useful content.
        # StreamReader limit must be >> largest NDJSON we will parse.
        self.acp_stream_limit = int(
            os.environ.get("SUBMARINE_ACP_STREAM_LIMIT", str(16 * 1024 * 1024)))
        # Inbound agent line: refuse to parse above this (leave headroom under stream).
        self.acp_max_inbound_line = int(
            os.environ.get("SUBMARINE_ACP_MAX_INBOUND", str(8 * 1024 * 1024)))
        # fs/read whole-file response content (chars). Agent must page with line/limit.
        self.fs_read_max_chars = int(
            os.environ.get("SUBMARINE_FS_READ_MAX", str(2 * 1024 * 1024)))
        self.fs_write_max_chars = int(
            os.environ.get("SUBMARINE_FS_WRITE_MAX", str(2 * 1024 * 1024)))
        # terminal/create outputByteLimit clamp (bytes retained in client).
        self.terminal_output_max_bytes = int(
            os.environ.get("SUBMARINE_TERM_OUTPUT_MAX", str(1 * 1024 * 1024)))

    # ── Subclass hooks ─────────────────────────────────────────────────

    def agent_argv(self) -> List[str]:
        """Return the argv used to spawn the ACP agent process."""
        raise NotImplementedError

    def spawn_env(self) -> Optional[Dict[str, str]]:
        """Optional env overrides for the agent process (None → inherit)."""
        return None

    def normalize_model(self, model: Optional[str]) -> str:
        if not model:
            return self.DEFAULT_MODEL
        key = model.strip()
        return self.MODEL_ALIASES.get(
            key, self.MODEL_ALIASES.get(key.lower(), key))

    # Canonical effort levels used by Claude + Grok (Grok also has none/minimal/xhigh).
    EFFORT_ALIASES: Dict[str, str] = {
        "max": "xhigh",  # Claude "max" → Grok xhigh; harmless for Claude
        "x-high": "xhigh",
        "extra_high": "xhigh",
        "extra-high": "xhigh",
    }

    def normalize_effort(self, effort: Optional[str]) -> str:
        """Return a normalized effort string, or '' if unset/invalid."""
        if not effort:
            return ""
        key = str(effort).strip().lower().replace(" ", "_")
        key = self.EFFORT_ALIASES.get(key, key)
        # Accept common levels; agent rejects unknowns at apply time.
        if key in (
            "none", "minimal", "low", "medium", "high", "xhigh", "max",
            "deep",  # per-model menu id some agents accept
        ):
            return key
        return key  # pass through; agent validates

    async def after_agent_initialize(self, init_result: dict) -> None:
        """Hook after ACP `initialize` (e.g. authenticate)."""
        return None

    def set_model_params(self) -> dict:
        """Params for session/set_model. Subclasses may add effort/_meta."""
        return {
            "sessionId": self.session_id,
            "modelId": self.model,
        }

    def resolve_applied_model(self, requested: Optional[str],
                              current: Optional[str]) -> str:
        """Prefer the id the agent actually bound."""
        cur = str(current).strip() if current else ""
        if cur:
            return cur
        return (requested or "").strip()

    def log_path(self) -> str:
        """Per-process file. Shared submarine_acp_bridge.log is truncated on
        every spawn and two sessions interleave — diagnoses hit the wrong tab.
        """
        base = self.LOG_PATH
        root, ext = os.path.splitext(base)
        return f"{root}.{os.getpid()}{ext or '.log'}"

    def file_log(self, msg: str) -> None:
        try:
            with open(self.log_path(), "a") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    # ── BaseBridge overrides ───────────────────────────────────────────

    def extra_dispatch(self):
        return {
            "set_model": self.handle_set_model,
            "set_permission_mode": self.handle_set_permission_mode,
            # plan_response: BaseBridge.handle_plan_response (+ mode switch override)
            "rewind_points": self.handle_rewind_points,
            "rewind_execute": self.handle_rewind_execute,
            "cancel_loop": self.handle_cancel_loop,
            # Same RPC as Claude SDK bridge — host polls when idle with ⚙ tasks.
            "poll_bg_tasks": self.handle_poll_bg_tasks,
            # Harness /clear: new conversation, same agent process.
            "clear": self.handle_clear,
        }


def run_bridge(bridge: AcpBridge) -> None:
    """Entry point helper for agent-specific main modules."""
    try:
        asyncio.run(bridge.run_stdin_loop())
    except KeyboardInterrupt:
        pass
