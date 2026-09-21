"""Thin Session façade: identity, transport, turn, persist.

UI effects go through OutputPort / ChromePort. Timers go through Scheduler.
`working` is a read-only property over TurnController — the only busy bit.

is_sleeping is DERIVED: session_id and client is None and not initialized.
Restore constructs exactly that triple.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .background import BackgroundTaskGate
from .events import BridgeEventRouter
from .ports import ChromePort, OutputPort, PersistPort, Scheduler
from .records import (
    STAMP_AGENT_ID,
    STAMP_BACKEND,
    STAMP_EFFORT,
    STAMP_MODEL,
    STAMP_PARENT_AGENT_ID,
    STAMP_PROVIDER_LABEL,
    STAMP_SESSION_ID,
    STAMP_SLEEPING,
    STAMP_SUBSESSION_ID,
    SessionStore,
    derive_state,
    read_stamp,
    remember_bookmark_record,
    stamp_identity,
    starred_ids_for_projects,
)
from .registry import SessionRegistry, default_registry, merge_subsession_queue, new_agent_id, resolve_init_model
from .rewind import RewindService, is_synthetic_turn
from .turn import (
    _SELF_WAKE_BACKENDS,
    TurnController,
    is_compact_prompt,
)

try:
    from backend.rpc import JsonRpcClient
    from backend import specs as backend_specs
    from backend import grok as grok_backend
except ImportError:  # tests that only exercise identity / turn
    JsonRpcClient = None  # type: ignore
    backend_specs = None  # type: ignore
    grok_backend = None  # type: ignore

try:
    from plat.log import log_plugin
except ImportError:
    def log_plugin(message):  # type: ignore
        print("[Submarine] %s" % message)

try:
    from plat.constants import DEFAULT_SESSION_NAME
except ImportError:
    DEFAULT_SESSION_NAME = "Submarine"

try:
    from plat.constants import SPINNER_RESPONDING, SPINNER_WAITING
except ImportError:  # pragma: no cover - package layout
    SPINNER_RESPONDING = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    SPINNER_WAITING = "◇◈◆◈"

# Busy-mark frame intervals, by turn phase (ms). Ported from sublime-claude's
# Session._animate: the diamond while waiting on the model, the braille spinner
# while it is answering or running a tool.
SPINNER_TOOL_MS = 200
SPINNER_RESPOND_MS = 160
SPINNER_WAIT_MS = 240


_CONTEXT_LIMITS = {
    "@400k": 400000,
    "@200k": 200000,
}

CLAUDE_BRIDGE_SCRIPTS = frozenset({"claude_main.py", "main.py"})


def fork_session_title(name):
    # type: (str) -> str
    """Tab / list title for a forked session: '(fork) <name>'."""
    base = (name or "session").strip() or "session"
    if base.lower().startswith("(fork)"):
        return base
    if base.endswith(" (fork)"):
        base = base[:-7].rstrip() or "session"
    if base.lower().startswith("fork:"):
        base = base[5:].strip() or "session"
    return "(fork) %s" % base


def _longer_prompt(*parts):
    # type: (*object) -> str
    """Keep the longest prefix of a truncated 30-char name."""
    best = ""
    for p in parts:
        text = " ".join(str(p or "").split())
        if not text:
            continue
        if not best:
            best = text
            continue
        stem = best.rstrip(".… ").strip()
        if stem and text.startswith(stem) and len(text) > len(best):
            best = text
    return best[:200]


def resolve_model_id(model_id):
    # type: (Optional[str]) -> tuple
    """Strip @400k/@200k → (real_id, token_cap or None)."""
    if not model_id:
        return model_id, None
    for suffix, tokens in _CONTEXT_LIMITS.items():
        if model_id.endswith(suffix):
            return model_id[:-len(suffix)], tokens
    return model_id, None


def auto_sleep_due(session, now, timeout_min):
    # type: (Any, float, float) -> tuple
    """True when an idle live session has been quiet longer than timeout.

    Returns (due, effective_idle_ts). Interrupt/cancel must not look idle
    since the turn *started* — last_activity is the floor.
    """
    if not timeout_min or timeout_min <= 0:
        return False, 0.0
    if getattr(session, "sleep_disabled", False):
        return False, 0.0
    if getattr(session, "quick_mode", False):
        return False, 0.0
    if getattr(session, "_interrupting", False):
        return False, 0.0
    turn = getattr(session, "turn", None)
    if turn is not None and getattr(turn, "kind", None) == "interrupting":
        return False, 0.0
    try:
        gt = getattr(session, "goal_tracker", None)
        if gt is not None and gt.is_open() and gt.status in (
                "active", "infra_paused"):
            return False, 0.0
    except Exception:
        pass
    sleeping = getattr(session, "is_sleeping", False)
    if callable(sleeping):
        try:
            sleeping = bool(sleeping())
        except Exception:
            sleeping = False
    working = bool(getattr(session, "working", False))
    if turn is not None and getattr(turn, "busy", False):
        working = True
    if not (getattr(session, "initialized", False)
            and not working
            and not sleeping):
        return False, 0.0
    idle_at = float(getattr(session, "last_idle_at", 0) or 0)
    last_act = float(getattr(session, "last_activity", 0) or 0)
    # Idle clock cannot predate last work — long ACP turns used to leave
    # last_idle_at at pre-run sticky-◎ open time → instant sleep on done.
    effective_idle = max(idle_at, last_act)
    threshold = now - (timeout_min * 60)
    if effective_idle <= 0 or effective_idle >= threshold:
        return False, effective_idle
    return True, effective_idle


def _is_claude_bridge(spec):
    # type: (Any) -> bool
    script = os.path.basename(getattr(spec, "bridge_script", "") or "")
    return script in CLAUDE_BRIDGE_SCRIPTS


class Session:
    """One agent conversation. Constructed with injected ports; does not spawn
    a bridge until start().
    """

    def __init__(
        self,
        output,  # type: OutputPort
        chrome,  # type: ChromePort
        scheduler,  # type: Scheduler
        persist,  # type: PersistPort
        registry=None,  # type: Optional[SessionRegistry]
        resume_id=None,  # type: Optional[str]
        fork=False,  # type: bool
        profile=None,  # type: Optional[Dict]
        initial_context=None,  # type: Optional[Dict]
        backend="claude",  # type: str
        cwd=None,  # type: Optional[str]
        additional_dirs=None,  # type: Optional[List[str]]
        settings=None,  # type: Optional[dict]
        plugin_dir=None,  # type: Optional[str]
        store=None,  # type: Optional[SessionStore]
        rpc_factory=None,  # type: Optional[Callable]
        window=None,  # type: Any
        model=None,  # type: Optional[str]
    ):
        self.output = output
        self.chrome = chrome
        self.scheduler = scheduler
        self.persist = persist
        self.registry = registry or default_registry
        self.window = window
        self.backend = backend or "claude"
        self.settings = settings if settings is not None else {}
        self.plugin_dir = plugin_dir or os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        self.store = store or SessionStore()
        self.rpc_factory = rpc_factory
        self.cwd = cwd or ""
        self.additional_dirs = list(additional_dirs or [])

        self.client = None  # type: Any
        self.initialized = False
        self.turn = TurnController(
            scheduler,
            on_compact_timeout=self._on_compact_timeout,
            on_interrupt_settle=self._on_interrupt_settle,
        )
        self.turn_phase = "idle"
        self.unread = False
        self.surface = {}  # type: dict
        self.is_looping = False
        self.next_wake_at = None  # type: Optional[float]
        self.quick_mode = False
        self.current_tool = None  # type: Optional[str]
        # Busy-mark chain generation: bumping it orphans the previous chain, so
        # a re-kick never leaves two chains racing the frame counter.
        self._anim_gen = 0
        self.keep_running_on_close = None  # type: Optional[bool]

        # Identity triple. Resume (not fork) pre-fills session_id so the
        # derived sleep triple is true before any UI.
        self.session_id = resume_id if resume_id and not fork else None  # type: Optional[str]
        self.resume_id = resume_id
        self.fork = bool(fork)
        self.profile = profile
        self.initial_context = initial_context
        self.effort = None  # type: Optional[str]
        self.available_models = []  # type: list
        self.model = (str(model).strip() if model else None)  # type: Optional[str]
        self.name = None  # type: Optional[str]
        self.first_prompt = None  # type: Optional[str]
        self._recovered_title = None  # type: Optional[str]
        self.total_cost = 0.0
        self.query_count = 0
        self.context_usage = None  # type: Optional[dict]
        self.permission_mode = None  # type: Optional[str]

        self.agent_id = new_agent_id()
        self.subsession_id = None
        self.parent_agent_id = None
        self.parent_session_id = None  # type: Optional[str]
        self.child_agent_ids = []  # type: List[str]
        self.agent_id_aliases = []  # type: List[str]
        if initial_context:
            self.agent_id = (
                initial_context.get("agent_id")
                or initial_context.get("subsession_id")
                or self.agent_id
            )
            self.subsession_id = (
                initial_context.get("subsession_id") or self.agent_id
            )
            self.parent_agent_id = initial_context.get("parent_agent_id")
            self.parent_session_id = initial_context.get("parent_session_id")
            self.child_agent_ids = list(
                initial_context.get("child_agent_ids") or [])
            self.agent_id_aliases = list(
                initial_context.get("agent_id_aliases") or [])
        elif resume_id and not fork:
            try:
                saved = self.store.find(resume_id)
            except Exception:
                saved = None
            if saved:
                if saved.get("agent_id"):
                    self.agent_id = saved.get("agent_id")
                if saved.get("subsession_id"):
                    self.subsession_id = saved.get("subsession_id")
                if saved.get("parent_agent_id"):
                    self.parent_agent_id = saved.get("parent_agent_id")
                if saved.get("parent_session_id"):
                    self.parent_session_id = saved.get("parent_session_id")
                self.agent_id_aliases = list(
                    saved.get("agent_id_aliases") or [])
                kids = list(saved.get("child_agent_ids") or [])
                # Children spawned by the previous incarnation of this sheet.
                self.child_agent_ids = [c for c in kids if c]

        self.last_activity = time.time()
        self.last_access = self.last_activity
        self.last_idle_at = 0.0
        self.sleep_disabled = False
        self.error_halted = False
        self.error_halt_message = ""
        self.backgrounded = False
        self.torn_off = False
        self.plan_mode = False
        self.plan_file = None  # type: Optional[str]
        self.draft_prompt = ""
        self._composer_allowed = True
        self._input_mode_entered = False
        self._park_composer_after_init = False
        # Resume after interrupt-in-asking: drop leftover question/permission/plan
        # until the user starts a new query.
        self._resume_drop_asking = bool(resume_id) and not fork
        self._resume_asking_interrupt_sent = False
        self._pending_resume_at = None  # type: Optional[str]
        # Directory the resumed session's own transcript lives in, when the
        # backend's resume is cwd-scoped (see `_resume_cwd`).
        self._resume_cwd_resolved = ""
        self._queued_prompts = []  # type: List[str]
        self._queued_ctx = {}  # type: Dict[str, dict]
        self._inject_pending = False
        self._interrupt_stream = False
        self._interrupting = False
        self._send_now_pending = False
        self._firing_queue = False
        self._compacting = False
        self._query_start = None  # type: Optional[float]
        # Grok self-wake: leftover_end can arrive before agent_continue.
        self._pending_leftover_end = False
        self._self_wake_idle_gen = 0
        self._user_cancelled_turn = False
        self._auto_retry_count = 0
        self._auto_retry_pending = False
        self._pending_images = []  # type: list
        self._parent_notified = False
        self._pending_signal_complete = None
        self._pending_retain = None
        self._saved_goal_json = None  # type: Optional[dict]
        self._artifacts_auto_opened = False

        # Features/ui hook points (NOT implemented here).
        self.on_turn_end = []  # type: List[Callable]
        self.on_saved = []  # type: List[Callable]
        self.on_init = []  # type: List[Callable]
        self.on_bg_surface = []  # type: List[Callable]

        self.bg = BackgroundTaskGate(
            self.turn,
            scheduler,
            output,
            backend=self.backend,
            send_poll=self._send_bg_poll,
            on_query=self._bg_query,
            on_surface=self._bg_surface,
            on_compact_done=self._finish_compact,
        )
        self.events = BridgeEventRouter(
            output,
            chrome,
            self.turn,
            scheduler,
            self.bg,
            send=self._send,
            backend=self.backend,
            on_query=self._event_query,
            on_queue=self.queue_prompt,
            on_interrupt=lambda: self.interrupt(break_channel=False),
            on_session_id=self._accept_session_id,
            on_usage=self._accept_usage,
            on_cost=self._accept_cost,
            on_phase=self._set_turn_phase,
            on_error=self._mark_error_halt,
            on_loop=self._accept_loop,
            on_plan_mode=self._accept_plan_mode,
            on_compact_start=self._enter_compact_ui,
            on_compact_done=self._finish_compact,
            on_resume_stream=self._resume_interrupt_stream,
            on_result_idle=self._result_idle,
            on_tool_name=self._accept_tool_name,
            elapsed=self._elapsed,
            is_compacting=lambda: self._compacting,
            interrupt_stream=lambda: self._interrupt_stream,
            drop_asking=self._drop_resume_asking_if_needed,
            is_asking_tool=self._is_asking_tool,
            resume_drop_asking=lambda: bool(self._resume_drop_asking),
            on_artifact_write=self._on_artifact_write,
            user_cancelled=lambda: bool(self._user_cancelled_turn),
            on_leftover_pending=self._mark_leftover_pending,
        )
        self.rewind = RewindService(
            send=self._send,
            on_apply_claude=self._apply_claude_undo,
            on_apply_grok=self._apply_grok_undo,
            on_fail=self._rewind_fail,
            scheduler=scheduler,
            jsonl_finder=self._find_jsonl_path,
        )

    def _mark_leftover_pending(self):
        # type: () -> None
        """A self-wake closer arrived while idle — do not adopt a dead turn."""
        self._pending_leftover_end = True

    def _on_artifact_write(self, path):
        # type: (str) -> None
        """Convention path: ACP fs write under the artifact root."""
        try:
            from .artifacts import handle_convention_write
            handle_convention_write(
                path,
                session=self,
                agent_id=getattr(self, "agent_id", None),
                session_id=getattr(self, "session_id", None),
            )
        except Exception:
            pass

    # ── derived state ─────────────────────────────────────────────────

    @property
    def working(self):
        # type: () -> bool
        """The only busy flag. Never assign this — use TurnController."""
        return self.turn.busy

    @property
    def is_sleeping(self):
        # type: () -> bool
        return bool(self.session_id) and self.client is None and not self.initialized

    @property
    def display_name(self):
        # type: () -> str
        return (self.name or DEFAULT_SESSION_NAME) or DEFAULT_SESSION_NAME

    @property
    def _query_gen(self):
        # type: () -> int
        return self.turn.gen

    def effective_idle_at(self):
        # type: () -> float
        """Idle clock cannot predate last work."""
        return max(float(self.last_idle_at or 0), float(self.last_activity or 0))

    def should_auto_sleep(self, now, timeout_min):
        # type: (float, float) -> Optional[bool]
        """None = stay awake; False = sleep; True = force sleep (2×)."""
        due, idle_at = auto_sleep_due(self, now, timeout_min)
        if not due:
            return None
        force_threshold = now - (timeout_min * 60 * 2)
        return idle_at < force_threshold

    # ── transport ─────────────────────────────────────────────────────

    def start(self, resume_session_at=None):
        # type: (Optional[str]) -> None
        self._composer_allowed = False
        if self.resume_id and not self.fork:
            self._resume_drop_asking = True
            self._resume_asking_interrupt_sent = False
            self._clear_asking_state()
        try:
            self.chrome.connecting_banner(True)
        except Exception:
            pass

        spec = self._spec()
        env = dict(self.settings.get("env") or {})
        default_models = self.settings.get("default_models") or {}
        default_model = (
            default_models.get(self.backend)
            or (getattr(spec, "fallback_model", None) if spec else None)
            or self.settings.get("default_model")
        )
        saved_entry = None
        if self.resume_id:
            saved_entry = self.store.find(self.resume_id)
            if saved_entry and not self.fork:
                try:
                    saved_q = int(saved_entry.get("query_count") or 0)
                except (TypeError, ValueError):
                    saved_q = 0
                if saved_q > int(getattr(self, "query_count", 0) or 0):
                    self.query_count = saved_q
        self._resume_cwd_resolved = self._resume_cwd(saved_entry)
        if self._resume_cwd_resolved:
            # The whole session moves to the directory its transcript lives in:
            # the bridge process, the backend's own resume, and the record this
            # process writes back.
            self.cwd = self._resume_cwd_resolved
        view_model = read_stamp(self.persist, STAMP_MODEL) if self.resume_id else None
        chosen_raw = resolve_init_model(
            requested_model=self.model,
            profile_model=(self.profile.get("model") if self.profile else None),
            session_model=self.model,
            view_model=view_model,
            saved_model=(saved_entry or {}).get("model"),
            default_model=default_model,
            resume=bool(self.resume_id),
        )
        if chosen_raw:
            _, ctx = resolve_model_id(chosen_raw)
            if not ctx:
                try:
                    from backend import providers as provider_mod
                    pcfg = (self.settings.get("custom_providers") or {}).get(
                        self.backend) or {}
                    ctx = provider_mod.context_tokens_for_provider(
                        pcfg, chosen_raw)
                except Exception:
                    ctx = None
            if ctx:
                env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(ctx)
                env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(ctx)

        if spec is not None:
            for k, v in (getattr(spec, "static_env", None) or {}).items():
                env.setdefault(k, v)
            dyn = getattr(spec, "dynamic_env", None)
            if dyn is not None:
                overwrite, defaults = dyn({
                    "custom_providers": self.settings.get("custom_providers") or {},
                    "deepseek_api_key": self.settings.get("deepseek_api_key"),
                })
                env.update(overwrite or {})
                for k, v in (defaults or {}).items():
                    env.setdefault(k, v)

        label = getattr(spec, "label", None) or self.backend
        stamp_identity(
            self.persist,
            **{
                STAMP_BACKEND: self.backend,
                STAMP_PROVIDER_LABEL: label,
                STAMP_MODEL: chosen_raw,
                STAMP_EFFORT: self.effort,
            }
        )
        self._persist_view_identity()

        factory = self.rpc_factory
        if factory is None:
            if JsonRpcClient is None:
                self._on_init({"error": {"message": "JsonRpcClient unavailable"}})
                return
            factory = JsonRpcClient
        self.client = factory(self._on_notification)
        python_path = self.settings.get("python_path") or "python3"
        script_name = getattr(spec, "bridge_script", None) or "claude_main.py"
        bridge_script = os.path.join(self.plugin_dir, "bridge", script_name)
        try:
            self.client.start(
                [python_path, bridge_script], env=env, cwd=self._cwd())
        except Exception as e:
            self._on_init({"error": {"message": str(e)}})
            return
        self.chrome.set_status("connecting...")
        self._notify_session_list()

        init_params = self._build_init_params(
            spec, chosen_raw, saved_entry, resume_session_at)
        sent = self._send("initialize", init_params, self._on_init)
        if not sent:
            self.scheduler.call_later(
                50,
                lambda: self._on_init({
                    "error": {
                        "message": (
                            "Bridge process died before initialization. "
                            "Check that the backend CLI is installed and authenticated."
                        ),
                    },
                }),
            )

    def _resume_cwd(self, saved_entry):
        # type: (Optional[dict]) -> str
        """Directory the backend filed the resumed session under, "" to keep.

        Grok scopes `session/load` to the cwd, so a session resumed from another
        project fails with FS_NOT_FOUND and the CLI silently opens a fresh one
        with no prior turns. `features.resume` supplies the lookup; the saved
        row's project is only a hint for finding the session on disk.
        """
        if not self.resume_id or self.fork:
            return ""
        resolver = getattr(self, "_transcript_cwd", None)
        if not callable(resolver):
            return ""
        hint = ""
        if saved_entry:
            hint = saved_entry.get("project") or ""
        try:
            return resolver(self.backend, self.resume_id, hint or self.cwd or "",
                            getattr(self, "agent_id", None) or "") or ""
        except Exception:
            return ""

    def _build_init_params(self, spec, chosen_raw, saved_entry, resume_session_at):
        # type: (Any, Optional[str], Optional[dict], Optional[str]) -> dict
        permission_mode = self.settings.get("permission_mode") or "acceptEdits"
        self.permission_mode = permission_mode
        if permission_mode == "default":
            allowed_tools = []
        else:
            allowed_tools = list(self.settings.get("allowed_tools") or [])

        cwd = self.cwd or self._default_cwd()
        if saved_entry and saved_entry.get("project"):
            saved_project = saved_entry.get("project") or ""
            if saved_project and saved_project != cwd:
                cwd = saved_project
        if self._resume_cwd_resolved:
            cwd = self._resume_cwd_resolved
        # Fork is a new session: keep cwd, drop parent activity / goal / usage.
        if saved_entry and not self.fork:
            try:
                if saved_entry.get("context_usage"):
                    self.context_usage = saved_entry.get("context_usage")
                if saved_entry.get("plan_file"):
                    self.plan_file = saved_entry.get("plan_file")
                if saved_entry.get("goal"):
                    self._saved_goal_json = saved_entry.get("goal")
                if saved_entry.get("last_activity"):
                    self.last_activity = float(saved_entry.get("last_activity"))
                if saved_entry.get("last_access") or saved_entry.get("last_activity"):
                    self.last_access = float(
                        saved_entry.get("last_access")
                        or saved_entry.get("last_activity"))
                fp = saved_entry.get("first_prompt")
                if fp:
                    self.first_prompt = fp
            except Exception:
                pass

        init_params = {
            "cwd": cwd,
            "additional_dirs": list(self.additional_dirs),
            "allowed_tools": allowed_tools,
            "permission_mode": permission_mode,
            "agent_id": self.agent_id,
            "mcp_enable_read_image": self._mcp_enable_read_image(chosen_raw),
        }  # type: Dict[str, Any]
        if self.resume_id:
            init_params["resume"] = self.resume_id
            if self.fork:
                init_params["fork_session"] = True
            if resume_session_at:
                init_params["resume_session_at"] = resume_session_at
        if self.subsession_id:
            init_params["subsession_id"] = self.subsession_id
        if self.parent_agent_id:
            init_params["parent_agent_id"] = self.parent_agent_id

        effort = self._resolve_effort(spec)
        self.effort = effort
        if self.backend == "grok" or not self.resume_id:
            init_params["effort"] = effort
        elif self.profile and self.profile.get("effort"):
            init_params["effort"] = effort
        elif spec is not None and getattr(spec, "effort", None):
            init_params["effort"] = effort
        if effort:
            stamp_identity(self.persist, **{STAMP_EFFORT: effort})

        if self.profile:
            if self.profile.get("betas"):
                init_params["betas"] = self.profile["betas"]
            system_prompt = self.profile.get("system_prompt") or ""
            append_system = (self.profile.get("append_system_prompt") or "").strip()
            if system_prompt:
                init_params["system_prompt"] = system_prompt
            elif append_system:
                init_params["append_system_prompt"] = append_system
            if self.quick_mode:
                init_params["quick_mode"] = True
        if chosen_raw:
            real_model, _ = resolve_model_id(chosen_raw)
            init_params["model"] = real_model
            self.model = real_model
            stamp_identity(self.persist, **{STAMP_MODEL: real_model})
        if self.backend == "grok" and init_params.get("model") and grok_backend is not None:
            try:
                if not grok_backend.model_supports_reasoning_effort(init_params["model"]):
                    init_params.pop("effort", None)
                    self.effort = None
                    try:
                        self.persist.clear(STAMP_EFFORT)
                    except Exception:
                        pass
            except Exception:
                pass
        return init_params

    def _on_init(self, result):
        # type: (dict) -> None
        result = result or {}
        try:
            self.chrome.connecting_banner(False)
        except Exception:
            pass
        if "error" in result:
            err = result.get("error")
            error_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            log_plugin("init error: %s" % error_msg)
            # The bridge has nothing to serve after a failed handshake. Drop
            # it now: a client left installed makes sleep() refuse cleanup and
            # restart() unable to recover (there is no session id to wake).
            if self.client:
                client = self.client
                self.client = None
                try:
                    if not client.send("shutdown", {}, lambda _: client.stop()):
                        client.stop()   # already dead: reap it now
                except Exception:
                    try:
                        client.stop()
                    except Exception:
                        pass
            self.initialized = False
            self._mark_error_halt(error_msg)
            self.chrome.set_status("error")
            try:
                self.output.text("\n*Failed to connect: %s*\n" % error_msg)
            except Exception:
                pass
            self.turn.end_live()
            self._composer_allowed = True
            try:
                self.persist.clear(STAMP_SLEEPING)
            except Exception:
                pass
            self.scheduler.call_later(200, self._enter_input_if_idle)
            return

        self.initialized = True
        was_rewind = self.turn.kind == "rewinding"
        if was_rewind:
            self.turn.end_live()
        self.current_tool = None
        self.last_activity = time.time()
        sid = result.get("session_id") or result.get("sessionId")
        if sid:
            self.session_id = sid
            self._persist_view_identity()
        mid = result.get("model") or result.get("modelId")
        if mid:
            self.model = str(mid)
            stamp_identity(self.persist, **{STAMP_MODEL: self.model})
        if result.get("effort"):
            self.effort = str(result.get("effort"))
        raw_models = result.get("models")
        if isinstance(raw_models, list) and raw_models:
            self.available_models = raw_models
        if result.get("resume_fallback"):
            try:
                self.output.text(
                    "\n*Session reopened without agent transcript "
                    "(UI history kept; model starts fresh).*\n"
                )
            except Exception:
                pass
        self.chrome.set_status("ready")
        if not self.quick_mode:
            self._save_session()
        self._composer_allowed = True
        try:
            self.persist.clear(STAMP_SLEEPING)
        except Exception:
            pass
        if getattr(self, "_resume_drop_asking", False):
            self._clear_asking_state()
        for cb in list(self.on_init):
            try:
                cb(self, result)
            except Exception:
                pass
        self._enter_input_if_idle()
        if was_rewind or getattr(self, "_park_composer_after_init", False):
            self._park_composer_after_init = False
            self._enter_input_with_draft()
            self._park_undo_caret()

    def _on_notification(self, method, params):
        # type: (str, dict) -> None
        params = params or {}
        if method in ("permission_request", "question_request"):
            self._note_activity()
        if method == "message":
            t = params.get("type")
            if t in (
                "tool_use", "tool_result", "text_delta", "text",
                "thinking", "plan_todos",
            ):
                # Long Kimi agent-side work can run after host @done with
                # working=False. Keep last_activity fresh so auto-sleep does
                # not treat that as idle.
                self._note_activity()
        self.events.dispatch(method, params)

    # ── query / queue / interrupt ─────────────────────────────────────

    def query(self, prompt, display_prompt=None, silent=False, _auto_retry=False,
              context_names=None, context_refs=None, images=None):
        # type: (str, Optional[str], bool, bool, Optional[list], Optional[list], Optional[list]) -> None
        if not self.client or not self.initialized:
            self.chrome.set_status("not initialized")
            return
        self._resume_drop_asking = False
        if not _auto_retry:
            self._auto_retry_pending = False
            self._auto_retry_count = 0

        typed = prompt
        raw = (prompt or "").strip()
        compact = is_compact_prompt(raw)
        # Live session/prompt still owns the agent (Kimi: tool ✔ is not
        # end_turn). A second query RPC is "another turn is already in
        # progress". Queue until _on_done; drain via _fire_next_queued.
        firing_queue = bool(getattr(self, "_firing_queue", False))
        if raw and not firing_queue and not _auto_retry and not compact:
            busy = bool(self.working) or bool(self.turn.awaiting_rpc)
            try:
                busy = busy or self.turn.should_queue_prompt()
            except Exception:
                pass
            if busy:
                self.queue_prompt(prompt)
                return
        if self._compacting and not compact and not silent and not _auto_retry:
            if raw and raw not in self._queued_prompts:
                self._queued_prompts.append(prompt)
                self._update_queue_phantom()
            try:
                self.output.text("\n*Compacting context — message queued.*\n")
            except Exception:
                pass
            return
        if compact:
            self._compacting = True

        self._interrupt_stream = False
        self._interrupting = False
        self._user_cancelled_turn = False
        self.touch_access()
        query_gen = self.turn.begin_query()
        self.query_count += 1
        self._clear_error_halt()
        self._pending_resume_at = None
        # Preserve sticky draft whenever the user is typing mid-stream —
        # including when a queued turn fires. Only clear on a normal user
        # submit (no sticky open, not silent, not queue-driven).
        if self.output and self.output.is_input_mode():
            try:
                live = self.output.get_input_text()
            except Exception:
                live = ""
            shown = (display_prompt if display_prompt is not None else typed) or ""
            # User submit of the current ◎ draft: consume it (promote in place),
            # do not restore it into the next composer.
            if not silent and not firing_queue and live.strip() == shown.strip():
                self.draft_prompt = ""
            else:
                self.draft_prompt = live
        elif silent or firing_queue:
            pass
        else:
            self.draft_prompt = ""
        self._input_mode_entered = False
        if not self.client or not getattr(self.client, "is_alive", lambda: True)():
            self.turn.end_live(query_gen)
            self._mark_error_halt("bridge died")
            return

        # Fold pending context in at *send* time, not at submit: a prompt that
        # lands in the queue keeps the user's text (and its readable chip), and
        # the context rides whichever prompt actually reaches the model. Images
        # come back separately — they are content blocks, never text.
        prompt, ctx_images = self._build_prompt_with_context(prompt)
        if ctx_images:
            images = list(images or []) + ctx_images
        ctx_names = list(context_names or [])
        ctx_refs = list(context_refs or [])
        ctx = getattr(self, "context", None)
        if ctx is not None and not ctx_names and not ctx_refs:
            try:
                _taken, ctx_names, ctx_refs = ctx.take()
            except Exception:
                ctx_names, ctx_refs = [], []

        ui_prompt = display_prompt if display_prompt is not None else typed
        if not silent:
            try:
                self.output.show(focus=False)
            except Exception:
                pass
            try:
                self.registry.register_session(self)
            except Exception:
                pass
            if not self.name:
                one = " ".join((ui_prompt or "").split())
                if one:
                    self._set_name(one[:200])
            self.output.prompt(ui_prompt, ctx_names, context_refs=ctx_refs)

        self._set_turn_phase("waiting")
        self.chrome.refresh_tab_title()
        self._query_start = time.time()
        query_params = {"prompt": prompt}  # type: Dict[str, Any]
        if images:
            query_params["images"] = images

        def _on_done_for_gen(result, _gen=query_gen):
            self._on_done(result, _expected_gen=_gen)

        if not self._send("query", query_params, _on_done_for_gen):
            self.turn.end_live(query_gen)
            self._set_turn_phase("idle")
            self._mark_error_halt("bridge died")
            try:
                self.output.text("\n\n*Failed to send query. Bridge process died.*\n")
            except Exception:
                pass
            return

        # Sticky EOF composer: re-open ◎ so the next message can be typed
        # (queued) while this turn streams. Silent wakes keep any draft.
        try:
            current = getattr(self.output, "current", None)
            if current is not None:
                current.working = True
                if silent:
                    current.duration = 0
                    current.has_meta = False
                    render = getattr(self.output, "_render_current", None)
                    if callable(render):
                        render()
        except Exception:
            pass
        if not silent:
            def _sticky():
                if not self.output or self.output.is_input_mode():
                    return
                has_modal = getattr(self.output, "has_turn_modal_ui", None)
                if callable(has_modal) and has_modal():
                    return
                if getattr(self.output, "_question_input_mode", False):
                    return
                self._input_mode_entered = False
                self._enter_input_with_draft()

            self.scheduler.call_later(40, _sticky)

    def _build_prompt_with_context(self, prompt):
        # type: (str) -> Tuple[str, List[dict]]
        """Pending context → `(prompt_with_text_context, images)`.

        Ported from sublime-claude's `Session._build_prompt_with_context`: the
        📎 chips exist so the next query carries them, and an image has to leave
        as a content block (`{"mime_type", "data", "path"}`) rather than as
        text. No context manager (or nothing pending) → the prompt unchanged.
        """
        build = getattr(getattr(self, "context", None), "build_prompt", None)
        if not callable(build):
            return prompt, []
        try:
            full, images = build(prompt)
        except Exception:
            return prompt, []
        return full, list(images or [])

    def _on_done(self, result, _expected_gen=None):
        # type: (dict, Optional[int]) -> None
        result = result or {}
        if not self.turn.matches_gen(_expected_gen):
            log_plugin(
                "ignore stale turn done gen=%s current=%s" % (
                    _expected_gen, self.turn.gen))
            return

        self.turn.awaiting_rpc = False
        self.current_tool = None

        if "error" in result:
            completion = "error"
        elif result.get("status") == "interrupted":
            completion = "interrupted"
        else:
            completion = "success"

        if completion == "interrupted":
            newer = self.turn.kind == "live" and not self._interrupt_stream
            if newer:
                return
            self.turn.settle_interrupt()
            # A detached ⚙ child is not a closer. Leftover PARENT stream
            # may still resume via _maybe_resume_stream; do not re-own
            # busy just because has_background() is true.
            # Keep _interrupt_stream so leftover parent text can resume;
            # upstream 6bc484f cleared it, but that fights the bg-audit
            # leftover-parent-stream closer.
            self._clear_deferred_state(clear_queue=False)
            # Interrupt ACK used to skip the idle stamp below. A long Kimi
            # turn then looked idle since it *started*, and auto-sleep fired.
            self._note_activity(idle=True)
            self._fire_turn_end("interrupted")
            if self.bg.pending_notifications:
                try:
                    self.bg.flush()
                except Exception:
                    pass
                if self.working:
                    return
            if self._fire_next_queued():
                return
            self._enter_input_if_idle()
            return

        if completion == "error":
            err = result.get("error")
            error_msg = (
                err.get("message", str(err)) if isinstance(err, dict) else str(err)
            )
            log_plugin("query error [backend=%s]: %s" % (self.backend, error_msg))
            self._mark_error_halt(error_msg)
            try:
                self.output.text("\n\n*Error: %s*\n" % error_msg)
            except Exception:
                pass

        try:
            self.output.clear_all_permissions()
        except Exception:
            pass

        if getattr(self, "parent_agent_id", None):
            if not getattr(self, "_pending_signal_complete", None):
                try:
                    self.registry.fire_subsession_waits(self, None)
                except Exception:
                    pass

        if completion != "success":
            self.turn.end_live(_expected_gen)
            self._set_turn_phase("idle")
            self._stamp_idle_clock()
            if completion == "interrupted":
                self._clear_deferred_state(clear_queue=False)
            else:
                self._clear_deferred_state(clear_queue=True)
            self._fire_turn_end(completion)
            if self.bg.pending_notifications:
                try:
                    self.bg.flush()
                except Exception:
                    pass
                if self.working:
                    return
            if completion == "interrupted" and self._fire_next_queued():
                return
            self._enter_input_if_idle()
            return

        if self._fire_next_queued():
            return

        if self._compacting and completion == "success":
            if self.backend == "kimi":
                self.turn.enter_compacting(timeout=True)
                self.turn.awaiting_rpc = False
                self._set_turn_phase("waiting")
                self.current_tool = "compact…"
                self.chrome.set_status("compacting…")
                return
            try:
                self.output.text("\n*Compacted.*\n")
            except Exception:
                pass
            self._finish_compact()
            return
        if self._compacting:
            self._finish_compact()
            return

        self.turn.end_live(_expected_gen)
        self._set_turn_phase("idle")
        self._stamp_idle_clock()
        self.touch_access()
        self._fire_turn_end("success")
        if self.bg.pending_notifications:
            try:
                self.bg.flush()
            except Exception:
                pass
            if self.working:
                return
        self.scheduler.call_later(100, self._enter_input_if_idle)

    def _update_queue_phantom(self):
        # type: () -> None
        try:
            self.chrome.queue_chips(list(self._queued_prompts or []))
        except Exception:
            pass

    def _clear_queue_phantom(self):
        # type: () -> None
        try:
            clear = getattr(self.chrome, "clear_queue_phantom", None)
            if callable(clear):
                clear()
            else:
                self.chrome.queue_chips([])
        except Exception:
            pass

    def _on_queue_phantom_navigate(self, href):
        # type: (str) -> None
        href = href or ""
        if href == "send_now":
            self.send_now("")
            return
        if href.startswith("send:"):
            try:
                idx = int(href.split(":", 1)[1])
            except (TypeError, ValueError):
                return
            q = self._queued_prompts
            if 0 <= idx < len(q):
                msg = q.pop(idx)
                self.send_now(msg)
            return
        if not href.startswith("drop:"):
            return
        try:
            idx = int(href.split(":", 1)[1])
        except (TypeError, ValueError):
            return
        if 0 <= idx < len(self._queued_prompts):
            dropped = self._queued_prompts.pop(idx)
            try:
                (self._queued_ctx or {}).pop(dropped, None)
            except Exception:
                pass
            self._update_queue_phantom()

    def queue_prompt(self, prompt):
        # type: (str) -> None
        prompt = (prompt or "").strip()
        if not prompt:
            return
        self._bind_context_to_queued(prompt)
        self._queued_prompts[:] = merge_subsession_queue(self._queued_prompts, prompt)
        self._update_queue_phantom()
        if self.working and self.client and getattr(self.client, "is_alive", lambda: True)():
            if self.backend == "claude" and prompt in self._queued_prompts:
                def _on_inj(r, p=prompt):
                    if not isinstance(r, dict) or r.get("error"):
                        return
                    res = r.get("result") if isinstance(r.get("result"), dict) else {}
                    if res.get("status") in ("ok", "queued"):
                        try:
                            self._queued_prompts.remove(p)
                        except ValueError:
                            pass
                        if res.get("status") == "queued":
                            self._inject_pending = True
                        self._update_queue_phantom()
                self._send("inject_message", {"message": prompt}, _on_inj)
            return
        if self.client and getattr(self.client, "is_alive", lambda: True)():
            if prompt in self._queued_prompts:
                try:
                    self._queued_prompts.remove(prompt)
                except ValueError:
                    pass
                self._fire_queued_now(prompt)
            return

    def send_now(self, prompt=""):
        # type: (str) -> bool
        prompt = (prompt or "").strip()
        if not self.working:
            if prompt:
                self._fire_queued_now(prompt)
                return True
            if self._queued_prompts:
                return self._fire_next_queued()
            return False
        if prompt:
            self._queued_prompts = [p for p in self._queued_prompts if p != prompt]
            self._queued_prompts.insert(0, prompt)
            self._update_queue_phantom()
        elif not self._queued_prompts:
            return False
        self._send_now_pending = True
        self.interrupt()
        return True

    def interrupt(self, break_channel=True):
        # type: (bool) -> None
        if self.turn.kind == "interrupting":
            # Second Esc: force idle (hung cancel).
            self._interrupt_stream = False
            self._interrupting = False
            self.turn.settle_interrupt()
            self._set_turn_phase("idle")
            self._note_activity(idle=True)
            self._enter_input_if_idle()
            return
        if not self.working and not self._inject_pending:
            # Idle UI: still tell the bridge to reap leftover Grok shells.
            # User is here — do not treat the long agent-side run as idle.
            self._note_activity()
            try:
                if self.output and self.output.has_turn_modal_ui():
                    self.output.interrupted()
            except Exception:
                pass
            if self.client:
                self._send("interrupt", {})
            return
        self._interrupt_stream = True
        self._interrupting = True
        self._user_cancelled_turn = True
        self._note_activity()
        self.turn.begin_interrupt()
        if self._compacting:
            self._compacting = False
            self.current_tool = None
        send_now = bool(self._send_now_pending)
        self._send_now_pending = False
        self._clear_deferred_state(clear_queue=False)
        try:
            self.output.interrupted(show_banner=not send_now)
        except Exception:
            pass
        self.chrome.set_status("send now…" if send_now else "interrupted")
        self._update_queue_phantom()
        if self.client:
            sent = self._send("interrupt", {})
            if not sent:
                self.turn.settle_interrupt()
                self._mark_error_halt("bridge died")

    def _fire_next_queued(self):
        # type: () -> bool
        if not self._queued_prompts:
            return False
        prompt = self._queued_prompts.pop(0)
        self._update_queue_phantom()
        self._fire_queued_now(prompt)
        return True

    def _bind_context_to_queued(self, display):
        # type: (str) -> None
        """Fold pending 📎 into this queued message and drop the chips."""
        ctx = getattr(self, "context", None)
        if ctx is None or not getattr(ctx, "items", None):
            return
        try:
            full, images = ctx.build_prompt(display)
        except Exception:
            full, images = display, []
        try:
            _taken, names, refs = ctx.take()
        except Exception:
            names, refs = [], []
        bag = getattr(self, "_queued_ctx", None)
        if bag is None:
            self._queued_ctx = {}
            bag = self._queued_ctx
        bag[display] = {
            "full": full or display,
            "images": list(images or []),
            "names": names,
            "refs": refs,
        }

    def _fire_queued_now(self, prompt):
        # type: (str) -> None
        self._firing_queue = True
        try:
            meta = (getattr(self, "_queued_ctx", None) or {}).pop(prompt, None)
            if is_synthetic_turn(prompt):
                first = prompt.lstrip().split("\n", 1)[0][:60]
                self.query(prompt, display_prompt="⚙ %s" % first, silent=True)
            elif meta:
                self.query(
                    meta.get("full") or prompt,
                    display_prompt=prompt,
                    images=meta.get("images") or None,
                    context_names=meta.get("names"),
                    context_refs=meta.get("refs"),
                )
            else:
                self.query(prompt, display_prompt=prompt)
        finally:
            self._firing_queue = False

    def _clear_deferred_state(self, clear_queue=True):
        # type: (bool) -> None
        if clear_queue:
            self._queued_prompts = []
        self._inject_pending = False
        self._pending_retain = None
        self._update_queue_phantom()

    # ── sleep / wake / stop ───────────────────────────────────────────

    def sleep(self, force=False):
        # type: (bool) -> bool
        if not self.session_id:
            return False
        if self.working:
            self.interrupt()
            self.scheduler.call_later(500, lambda: self.sleep(force=force))
            return False
        if not force:
            try:
                bg = self.output.active_background_tools()
            except Exception:
                bg = []
            if bg:
                self.chrome.set_status("refusing to sleep: background tools")
                return False
        self.bg.abort()
        if self.client:
            client = self.client
            self.client = None
            try:
                client.send("shutdown", {}, lambda _: client.stop())
            except Exception:
                try:
                    client.stop()
                except Exception:
                    pass
        self.initialized = False
        self._resume_drop_asking = True
        self._resume_asking_interrupt_sent = False
        self._clear_asking_state()
        self._persist_state("sleeping")
        self._apply_sleep_ui()
        self._notify_session_list()
        return True

    def wake(self):
        # type: () -> None
        try:
            self.chrome.clear_phantoms()
            self.chrome.sleep_banner(False)
        except Exception:
            pass
        if self.client or self.initialized:
            try:
                self.persist.clear(STAMP_SLEEPING)
            except Exception:
                pass
            self.touch_access()
            self._composer_allowed = True
            self._enter_input_with_draft()
            self._park_undo_caret()
            return
        if not self.session_id:
            return
        self.touch_access()
        try:
            self.persist.clear(STAMP_SLEEPING)
        except Exception:
            pass
        self._composer_allowed = False
        self._park_composer_after_init = True
        self.resume_id = self.session_id
        self.fork = False
        resume_at = self._pending_resume_at
        self.current_tool = "waking..."
        self.start(resume_session_at=resume_at)
        self._persist_state("open")
        self._notify_session_list()

    def restart(self):
        # type: () -> None
        def do_wake():
            if self.is_sleeping or (self.session_id and not self.client):
                self.wake()

        if self.sleep(force=True):
            self.scheduler.call_later(600, do_wake)

    def stop(self):
        # type: () -> None
        self.bg.abort()
        self._persist_state("closed")
        self._notify_session_list()
        if self.client:
            client = self.client
            self.client = None
            try:
                client.send("shutdown", {}, lambda _: client.stop())
            except Exception:
                try:
                    client.stop()
                except Exception:
                    pass
        self.initialized = False
        try:
            self.output.clear(keep_supportive=False)
        except Exception:
            pass
        # The sheet outlives the session: show the branding page instead of
        # leaving the dead session's chrome in the buffer (ui/idle.py).
        try:
            self.output.render_idle(getattr(self, "window", None))
        except Exception:
            pass
        self._queued_prompts = []
        try:
            self.chrome.set_status("")
        except Exception:
            pass
        # Nothing is running any more: leave the phase honest, so the next
        # turn's phase change is what starts the busy mark again.
        try:
            self._set_turn_phase("idle")
        except Exception:
            pass

    def clear_conversation(self):
        # type: () -> None
        if not self.client or not getattr(self.client, "is_alive", lambda: True)() or not self.initialized:
            self.restart()
            return
        if self.working:
            try:
                self.interrupt()
            except Exception:
                pass
        self.turn.begin_query()  # bump gen so stale done cannot clobber
        self.turn.end_live()

        def _on_clear(result, sess=self):
            if not result or result.get("error"):
                return
            old_sid = sess.session_id
            sid = result.get("session_id") or result.get("sessionId")
            if old_sid and sid and old_sid != sid:
                try:
                    sess._persist_state("closed")
                except Exception:
                    pass
            if sid:
                sess.session_id = str(sid)
                sess.resume_id = None
                sess.fork = False
            mid = result.get("model") or result.get("modelId")
            if mid:
                sess.model = str(mid)
            sess.query_count = 0
            sess.total_cost = 0.0
            sess.name = None
            sess._compacting = False
            sess.turn.state.kind = "idle"
            sess.turn.state.awaiting_rpc = False
            sess._queued_prompts = []
            sess.bg.abort()
            sess._pending_resume_at = None
            sess._clear_error_halt()
            sess._composer_allowed = True
            try:
                sess.output.clear(keep_supportive=False)
            except Exception:
                pass
            sess._persist_view_identity()
            if not sess.quick_mode:
                sess._save_session()
            sess._enter_input_if_idle()

        if not self._send("clear", {}, _on_clear):
            self.restart()

    def change_backend(self, new_backend):
        # type: (str) -> bool
        if new_backend == self.backend:
            return False
        cur_spec = self._spec()
        new_spec = None
        if backend_specs is not None:
            try:
                new_spec = backend_specs.get(new_backend, self.settings)
            except Exception:
                new_spec = None
        if not _is_claude_bridge(cur_spec) or not _is_claude_bridge(new_spec):
            return False
        if self.working:
            return False
        if not self.session_id:
            return False
        self.backend = new_backend
        self.bg.backend = new_backend
        self.events.backend = new_backend
        stamp_identity(
            self.persist,
            **{
                STAMP_BACKEND: new_backend,
                STAMP_PROVIDER_LABEL: getattr(new_spec, "label", None) or new_backend,
            }
        )
        self.restart()
        return True

    def touch_access(self):
        # type: () -> None
        self.last_access = time.time()
        self._notify_session_list()

    # ── undo ──────────────────────────────────────────────────────────

    def undo_message(self):
        # type: () -> None
        if not self.session_id:
            self._rewind_fail("no session to undo")
            return
        if self.working and self.current_tool != "rewinding...":
            self._rewind_fail("busy — interrupt first")
            return
        if (self.backend or "") == "grok":
            client = self.client
            alive = True
            try:
                alive = bool(client and client.is_alive())
            except Exception:
                alive = bool(client)
            if not alive:
                self._rewind_fail("bridge not ready for rewind")
                return
            self.turn.begin_rewind()
            self.current_tool = "rewinding..."
            try:
                self._kick_animation()
            except Exception:
                pass
            self.rewind.grok_undo_async()
            return
        self.rewind.undo_last(
            self.backend, self.session_id, self.cwd or "", self._pending_resume_at)

    def _rewind_fail(self, message):
        # type: (str) -> None
        msg = message or "undo failed"
        if self.turn.kind == "rewinding":
            try:
                self.turn.end_live()
            except Exception:
                pass
            self.current_tool = None
        try:
            self.chrome.set_status(msg)
        except Exception:
            pass
        try:
            import sublime
            sublime.status_message("Submarine: %s" % msg)
        except Exception:
            pass

    def _strip_view_from_last_prompt(self):
        # type: () -> None
        """Claude path: drop the last ◎ … ▶ block (sublime-claude)."""
        self._strip_view_prompt_range(None)

    def _strip_view_from_prompt_index(self, prompt_index):
        # type: (int) -> None
        """Grok path: drop ◎ turns from prompt_index through EOF."""
        self._strip_view_prompt_range(prompt_index)

    def _strip_view_prompt_range(self, prompt_index):
        # type: (Optional[int]) -> None
        out = self.output
        if not out:
            return
        try:
            if out.is_input_mode():
                out.exit_input_mode(keep_text=False)
        except Exception:
            pass
        view = getattr(out, "view", None)
        if view is None:
            return
        try:
            import sublime
            content = view.substr(sublime.Region(0, view.size()))
        except Exception:
            try:
                content = view.substr(None)
            except Exception:
                return
        from .rewind import last_prompt_span, prompt_index_span
        if prompt_index is None:
            span = last_prompt_span(content)
        else:
            span = prompt_index_span(content, prompt_index)
        if not span:
            return
        start, end = span
        try:
            out._replace(start, end, "")
        except Exception:
            return
        try:
            if getattr(out, "renderer", None) is not None:
                out.renderer.current = None
            out.current = None
        except Exception:
            pass
        try:
            import sublime
            view.erase_regions("submarine_conversation")
            view.erase_regions("claude_conversation")
        except Exception:
            pass

    def get_turns_for_undo(self):
        # type: () -> list
        return self.rewind.get_turns_for_undo(
            self.backend, self.session_id, self.cwd or "", self._pending_resume_at)

    def _apply_claude_undo(self, rewind_id, undone_prompt):
        # type: (str, str) -> None
        self._strip_view_from_last_prompt()
        saved_id = self.session_id
        if self.client:
            try:
                self.client.stop()
            except Exception:
                pass
            self.client = None
        self.initialized = False
        self.session_id = saved_id
        self.resume_id = saved_id
        self.fork = False
        self.draft_prompt = undone_prompt or ""
        self._input_mode_entered = True
        self._park_composer_after_init = True
        self._pending_resume_at = rewind_id
        self._save_session()
        self.turn.begin_rewind()
        self.current_tool = "rewinding..."
        try:
            self._kick_animation()
        except Exception:
            pass
        self.start(resume_session_at=rewind_id)

    def _apply_grok_undo(self, idx, draft):
        # type: (int, str) -> None
        self._strip_view_from_prompt_index(idx)
        saved_id = self.session_id
        if self.client:
            try:
                self.client.stop()
            except Exception:
                pass
            self.client = None
        self.initialized = False
        self.session_id = saved_id
        self.resume_id = saved_id
        self.fork = False
        self.draft_prompt = draft or ""
        self._input_mode_entered = True
        self._park_composer_after_init = True
        self._pending_resume_at = None
        self._save_session()
        self.turn.begin_rewind()
        self.current_tool = "rewinding..."
        try:
            self._kick_animation()
        except Exception:
            pass
        self.start()

    def _find_jsonl_path(self):
        # type: () -> Optional[str]
        from .rewind import find_session_jsonl
        cwd = ""
        try:
            cwd = self.cwd or ""
        except Exception:
            cwd = ""
        backend = self.backend or "claude"
        sid = self.session_id or ""
        path = find_session_jsonl(sid, backend, cwd)
        if path:
            return path
        rid = self.resume_id or ""
        if rid and rid != sid:
            return find_session_jsonl(rid, backend, cwd)
        return None

    # ── persist ───────────────────────────────────────────────────────

    def _persist_view_identity(self):
        # type: () -> None
        stamp_identity(
            self.persist,
            **{
                STAMP_AGENT_ID: getattr(self, "agent_id", None),
                STAMP_SUBSESSION_ID: getattr(self, "subsession_id", None),
                STAMP_PARENT_AGENT_ID: getattr(self, "parent_agent_id", None),
                STAMP_SESSION_ID: self.session_id or self.resume_id,
                STAMP_BACKEND: self.backend,
                STAMP_MODEL: getattr(self, "model", None),
            }
        )

    def _save_session(self):
        # type: () -> None
        if not self.session_id or self.quick_mode:
            return
        self._persist_view_identity()
        existing = self.store.find(self.session_id)
        live_q = int(getattr(self, "query_count", 0) or 0)
        saved_q = 0
        if existing:
            try:
                saved_q = int(existing.get("query_count") or 0)
            except (TypeError, ValueError):
                saved_q = 0
        # New unused sheet: do not create a history row. Never delete an
        # existing resume entry just because this process has not queried yet.
        if live_q <= 0 and existing is None:
            return
        existing = existing or {}
        try:
            saved_cost = float(existing.get("total_cost") or 0.0)
        except (TypeError, ValueError):
            saved_cost = 0.0
        entry = {
            "session_id": self.session_id,
            "name": self.name,
            "project": self.cwd or self._default_cwd(),
            # Cost restarts at 0 on resume; never regress the saved figure.
            "total_cost": max(float(self.total_cost or 0.0), saved_cost),
            "query_count": max(live_q, saved_q),
            "backend": self.backend,
            "last_activity": self.last_activity,
            "last_access": float(self.last_access or 0) or float(self.last_activity or 0),
            "state": derive_state(self.client, self.initialized, self.session_id),
        }  # type: Dict[str, Any]
        if self.agent_id:
            entry["agent_id"] = self.agent_id
        if self.subsession_id:
            entry["subsession_id"] = self.subsession_id
        paid = self.parent_agent_id
        if paid and paid == self.agent_id:
            paid = None  # a stale host-view stamp, never a real link
            self.parent_agent_id = None
        if not paid and existing.get("parent_agent_id") not in (None, self.agent_id):
            paid = existing.get("parent_agent_id")
            self.parent_agent_id = paid
        if paid:
            entry["parent_agent_id"] = paid
        psid = self.parent_session_id or existing.get("parent_session_id")
        if psid:
            entry["parent_session_id"] = psid
            self.parent_session_id = psid
        kids = list(getattr(self, "child_agent_ids", None) or [])
        if kids:
            entry["child_agent_ids"] = kids
        aliases = list(getattr(self, "agent_id_aliases", None) or [])
        if aliases:
            entry["agent_id_aliases"] = aliases
        if self.model:
            entry["model"] = self.model
        if self._pending_resume_at:
            entry["resume_session_at"] = self._pending_resume_at
        # Carry-over fields: a restored-but-not-started session has none of
        # these live yet; upsert replaces the row, so keep the saved values.
        if self.context_usage:
            entry["context_usage"] = self.context_usage
        elif existing.get("context_usage"):
            entry["context_usage"] = existing["context_usage"]
        if self.plan_file:
            entry["plan_file"] = self.plan_file
        elif existing.get("plan_file"):
            entry["plan_file"] = existing["plan_file"]
        fp = _longer_prompt(
            getattr(self, "first_prompt", None),
            getattr(self, "_recovered_title", None),
            existing.get("first_prompt"),
            str(self.name).split("\n", 1)[0].strip() if self.name else "",
        )
        if fp:
            entry["first_prompt"] = fp
            self.first_prompt = fp
        gt = getattr(self, "goal_tracker", None)
        if gt is not None and getattr(gt, "goal_id", None):
            try:
                entry["goal"] = gt.to_json()
            except Exception:
                pass
        if "goal" not in entry:
            saved_goal = getattr(self, "_saved_goal_json", None) or existing.get("goal")
            if saved_goal:
                entry["goal"] = saved_goal
        self.store.upsert(entry)
        try:
            if self.session_id and self.session_id in starred_ids_for_projects(
                    self.cwd or self._default_cwd(), entry.get("project")):
                remember_bookmark_record(self.session_id, {
                    "name": entry.get("name"),
                    "backend": entry.get("backend"),
                    "project": entry.get("project"),
                    "model": entry.get("model"),
                    "query_count": entry.get("query_count"),
                    "last_activity": entry.get("last_activity"),
                    "last_access": entry.get("last_access"),
                }, self.cwd or None)
        except Exception:
            pass
        for cb in list(self.on_saved):
            try:
                cb(self)
            except Exception:
                pass

    def _persist_state(self, state):
        # type: (str) -> None
        if not self.session_id:
            return
        mid = self.model or read_stamp(self.persist, STAMP_MODEL)
        existing = self.store.find(self.session_id)
        if existing:
            self.store.persist_state(self.session_id, state, mid)
            return
        self._save_session()
        self.store.persist_state(self.session_id, state, mid)

    def _apply_sleep_ui(self, touch_buffer=False):
        # type: (bool) -> None
        try:
            self.persist.stamp(STAMP_SLEEPING, True)
        except Exception:
            pass
        self._composer_allowed = False
        try:
            self.chrome.sleep_banner(True, "⏸ Session paused — press Enter to wake")
            self._clear_queue_phantom()
            self.chrome.refresh_tab_title()
        except Exception:
            pass

    # ── hooks / internals ─────────────────────────────────────────────

    def reset_phantoms_for_new_view(self):
        # type: () -> None
        try:
            self.chrome.clear_phantoms()
        except Exception:
            pass

    def _adopt_agent_turn(self, label="⚙ task completed"):
        # type: (str) -> None
        """Leftover inbound must not become a host-owned turn.

        Old path set working=True with no session/prompt. After @done, synth
        Bash / notify opened ◎ ⚙ task completed and the session never idled.
        Callers paint; this stays a no-op so busy only comes from query().
        """
        return

    def _flush_bg_notifications(self):
        # type: () -> None
        self.bg.flush()

    def _on_compact_timeout(self):
        # type: () -> None
        log_plugin("compact timeout — forcing idle")
        self._finish_compact(timeout=True)

    def _finish_compact(self, timeout=False):
        # type: (bool) -> None
        if not self._compacting and not timeout:
            return
        self._compacting = False
        self.turn.finish_compact()
        self.current_tool = None
        self.turn.awaiting_rpc = False
        self._set_turn_phase("idle")
        self._stamp_idle_clock()
        self.chrome.refresh_tab_title()
        if timeout:
            try:
                self.output.text("\n*Compaction timed out (host) — check agent.*\n")
            except Exception:
                pass
        self._fire_turn_end("compact")
        if self._fire_next_queued():
            return
        self.scheduler.call_later(80, self._enter_input_if_idle)

    def _enter_compact_ui(self):
        # type: () -> None
        self._compacting = True
        self.current_tool = "compact…"
        self.chrome.set_status("compacting…")
        self._set_turn_phase("waiting")

    def _on_interrupt_settle(self, gen):
        # type: (int) -> None
        if self.turn._interrupt_gen != gen:
            return
        # Before the busy check: a stuck "interrupting" turn is busy, and that
        # is exactly the state this unstick exists for.
        self._unstick_stale_interrupt()
        if self.working:
            return
        if self._fire_next_queued():
            return
        self._enter_input_if_idle()

    def _unstick_stale_interrupt(self) -> bool:
        """If cancel ACK never arrived, stop queueing follow-ups into a hole.

        Esc leaves working=True / turn=interrupting until the bridge ACK.
        Missing ACK meant every later Enter was queue_prompt'd and never
        flushed. Returns True if a queued prompt was started.
        """
        self._interrupting = False
        kind = ""
        try:
            kind = getattr(self.turn, "kind", "") or ""
        except Exception:
            kind = ""
        if kind == "interrupting":
            try:
                self.turn.settle_interrupt()
            except Exception:
                pass
            self._set_turn_phase("idle")
            self._interrupt_stream = False
            try:
                if self.output and self.output.current:
                    self.output.current.working = False
            except Exception:
                pass
        if self.working:
            return False
        before = list(getattr(self, "_queued_prompts", None) or [])
        self._fire_next_queued()
        self._enter_input_if_idle()
        return bool(before) and not getattr(self, "_queued_prompts", None)

    def _resume_interrupt_stream(self):
        # type: () -> None
        """Own busy for leftover stream / Grok bg self-wake (no session/prompt)."""
        if self.working:
            self._arm_self_wake_idle()
            return
        if getattr(self, "_pending_leftover_end", False):
            # Closer already arrived — do not adopt a turn that is done.
            self._pending_leftover_end = False
            log_plugin("skip self-wake; leftover_end already arrived")
            return
        self.turn.resume_stream()
        self._set_turn_phase("responding")
        self._arm_self_wake_idle()
        try:
            if self.output:
                # Must go through prompt() / begin_continued — swapping
                # current in-place left the conversation region on the
                # @done sheet and the next render wiped the session view.
                self.output.begin_continued()
        except Exception:
            pass
        self.chrome.refresh_tab_title()

    def _arm_self_wake_idle(self) -> None:
        """Idle a Grok self-wake if leftover_end never arrives.

        bg terminal exited → Grok self-woke (task-completed-term_*), host
        resume_stream()'d, and turn_completed never closed the sheet.
        Quiet timeout is the backup closer.
        """
        if self.turn.awaiting_rpc:
            return
        if (self.backend or "") not in _SELF_WAKE_BACKENDS:
            return
        if not self.working:
            return
        gen = int(getattr(self, "_self_wake_idle_gen", 0) or 0) + 1
        self._self_wake_idle_gen = gen

        def _fire(g=gen):
            self._maybe_idle_self_wake(g)

        self.scheduler.call_later(6000, _fire)

    def _maybe_idle_self_wake(self, gen: int) -> None:
        if gen != getattr(self, "_self_wake_idle_gen", 0):
            return
        if not self.working or self.turn.awaiting_rpc:
            return
        log_plugin("self-wake idle (no leftover_end)")
        self.events.result({
            "leftover_end": True,
            "stop_reason": "end_turn",
            "is_error": False,
        })

    def _result_idle(self):
        # type: () -> None
        self._interrupt_stream = False
        self._interrupting = False
        self._pending_leftover_end = False
        self._self_wake_idle_gen = int(
            getattr(self, "_self_wake_idle_gen", 0) or 0) + 1
        self._set_turn_phase("idle")
        self._stamp_idle_clock()
        self.touch_access()
        if self.bg.pending_notifications:
            try:
                self.bg.flush()
            except Exception:
                pass
        # Kimi end_turn while wait_for_exit is pending is fine; a closer
        # that skip-dropped left ⚙ under @done. Poll now.
        if self.bg.bg_tools or self.bg.bg_task_ids or self.bg.task_tool_map:
            try:
                self.bg._poll()
            except Exception:
                pass
        if not self.working:
            self.scheduler.call_later(100, self._enter_input_if_idle)

    def _park_undo_caret(self):
        # type: () -> None
        """After undo reconnect: ◎ + caret in the draft (sublime-claude)."""
        out = self.output
        if not out:
            return
        try:
            if not out.is_input_mode():
                out.enter_input_mode()
        except Exception:
            pass
        draft = getattr(self, "draft_prompt", None) or ""
        if str(draft).strip():
            try:
                out.set_composer_text(draft)
            except Exception:
                pass
        try:
            set_owner = getattr(out, "set_caret_owner", None)
            if callable(set_owner):
                set_owner("draft")
        except Exception:
            pass
        parked = False
        try:
            focus = getattr(out, "focus_composer", None)
            if callable(focus):
                focus(force_show=True, steal_focus=True, park_at_end=True)
                parked = True
        except Exception:
            parked = False
        if not parked:
            try:
                park = getattr(out, "park_composer_caret", None)
                if callable(park):
                    park("end")
            except Exception:
                pass

    def _enter_input_if_idle(self):
        # type: () -> None
        if self.working:
            return
        self._enter_input_with_draft()

    def _enter_input_with_draft(self):
        # type: () -> None
        """Enter sticky composer and restore draft.

        Allowed while working — the next message can be typed (and queued)
        mid-stream. `_enter_input_if_idle` is the idle-only wrapper.
        """
        if not self.output:
            return
        if not getattr(self, "_composer_allowed", True):
            return
        if self.is_sleeping:
            return
        view = getattr(self.output, "view", None)
        if view is not None:
            try:
                st = view.settings()
                if st.get("submarine_sleeping") or st.get("claude_sleeping"):
                    return
            except Exception:
                pass
        if getattr(self, "_quick_finished", False):
            return
        has_modal = getattr(self.output, "has_turn_modal_ui", None)
        if callable(has_modal) and has_modal():
            return

        focused = getattr(self.output, "_view_is_focused", None)
        owner_fn = getattr(self.output, "caret_owner", None)

        def _draft_owned():
            try:
                owner = owner_fn() if callable(owner_fn) else "draft"
            except Exception:
                owner = "draft"
            return owner == "draft"

        def _repin_draft():
            try:
                if callable(focused) and focused() and _draft_owned():
                    scroll = getattr(self.output, "scroll_composer_chrome", None)
                    restore = getattr(self.output, "restore_draft_caret", None)
                    if callable(scroll):
                        scroll(force=False)
                    if callable(restore):
                        restore()
            except Exception:
                pass

        if self.output.is_input_mode():
            self._input_mode_entered = True
            if not self.working:
                now = time.time()
                self.last_idle_at = now
                self.last_activity = now
            try:
                self.output.refresh_background_hints()
            except Exception:
                pass
            _repin_draft()
            return

        if self._input_mode_entered and not self.working:
            if self.output.is_input_mode():
                _repin_draft()
                return
            self._input_mode_entered = False

        if not self.working and self._fire_next_queued():
            return

        try:
            current = getattr(self.output, "current", None)
            if current is not None and getattr(current, "working", False) and not self.working:
                current.working = False
        except Exception:
            pass

        try:
            pending_q = getattr(self.output, "pending_question", None)
            if (
                getattr(self.output, "_question_input_mode", False)
                and not (pending_q and getattr(pending_q, "callback", None))
            ):
                self.output._question_input_mode = False
        except Exception:
            pass

        try:
            self.output.enter_input_mode()
        except Exception:
            pass

        if not self.output.is_input_mode():
            def _retry(tries=0):
                if not self.output:
                    return
                if self.output.is_input_mode():
                    self._input_mode_entered = True
                    _repin_draft()
                    return
                if callable(has_modal) and has_modal():
                    if tries < 8:
                        self.scheduler.call_later(
                            100 + tries * 50, lambda t=tries: _retry(t + 1))
                    return
                try:
                    current = getattr(self.output, "current", None)
                    if (
                        current is not None
                        and getattr(current, "working", False)
                        and not self.working
                    ):
                        current.working = False
                except Exception:
                    pass
                self._input_mode_entered = False
                try:
                    self.output.enter_input_mode()
                except Exception:
                    pass
                if self.output.is_input_mode():
                    self._input_mode_entered = True
                    if not self.working:
                        self.last_idle_at = time.time()
                    return
                if tries < 8:
                    self.scheduler.call_later(
                        80 + tries * 40, lambda t=tries: _retry(t + 1))

            self.scheduler.call_later(50, lambda: _retry(0))
            return

        self._input_mode_entered = True
        if not self.working:
            self.last_idle_at = time.time()

        if self.draft_prompt:
            draft = self.draft_prompt
            if not str(draft).strip():
                draft = ""
                self.draft_prompt = ""
            else:
                cur = getattr(self.output, "current", None)
                cur_prompt = getattr(cur, "prompt", None) if cur is not None else None
                if cur_prompt and str(draft).strip() == str(cur_prompt).strip():
                    draft = ""
                    self.draft_prompt = ""
            if not draft:
                collapse = getattr(self.output, "collapse_empty_composer_tail", None)
                if callable(collapse):
                    try:
                        collapse()
                    except Exception:
                        pass
            else:
                try:
                    self.output.set_composer_text(draft)
                except Exception:
                    if draft and view is not None:
                        try:
                            view.run_command("append", {"characters": draft})
                        except Exception:
                            pass
                    ensure = getattr(self.output, "ensure_composer_spare_line", None)
                    if callable(ensure):
                        try:
                            ensure()
                        except Exception:
                            pass
                try:
                    if callable(focused) and focused():
                        set_owner = getattr(self.output, "set_caret_owner", None)
                        if callable(set_owner):
                            set_owner("draft")
                        scroll = getattr(self.output, "scroll_composer_chrome", None)
                        restore = getattr(self.output, "restore_draft_caret", None)
                        if callable(scroll):
                            scroll(force=True)
                        if callable(restore):
                            restore()
                except Exception:
                    pass
        elif self.output.is_input_mode():
            collapse = getattr(self.output, "collapse_empty_composer_tail", None)
            if callable(collapse):
                try:
                    collapse()
                except Exception:
                    pass

    def _is_detached(self):
        # type: () -> bool
        """True when live in by_agent but not bound to a view."""
        if self.quick_mode:
            return False
        aid = getattr(self, "agent_id", None)
        if not aid:
            return bool(getattr(self, "backgrounded", False))
        try:
            if aid not in self.registry.by_agent:
                return bool(getattr(self, "backgrounded", False))
            return self.registry.bound_view_id(self) is None
        except Exception:
            return bool(getattr(self, "backgrounded", False))

    def _notify_session_list(self):
        # type: () -> None
        """Sessions scratch is event-driven; the poll is only the clock column."""
        try:
            from ui.session_list import schedule_session_list_refresh
            schedule_session_list_refresh()
        except Exception:
            pass

    def _set_unread(self, on):
        # type: (bool) -> None
        on = bool(on)
        if bool(getattr(self, "unread", False)) == on:
            return
        self.unread = on
        try:
            self.chrome.set_unread(self.unread)
        except Exception:
            pass
        self._notify_session_list()

    def _set_turn_phase(self, phase):
        # type: (str) -> None
        phase = phase or "idle"
        if self.turn_phase == phase:
            return
        self.turn_phase = phase
        if phase == "idle":
            if self._is_detached():
                self._set_unread(True)
            else:
                self._set_unread(False)
        try:
            self.chrome.refresh_tab_title()
        except Exception:
            pass
        # A phase change is a turn moving: make sure the busy mark is ticking.
        # Only on change — the event port reports "responding" per chunk.
        self._kick_animation()
        self._notify_session_list()

    # ─── busy mark ──────────────────────────────────────────────────────────

    def _kick_animation(self):
        # type: () -> None
        """Start the busy-mark chain for this turn. Bumps the generation, so a
        chain left behind by a cancelled timer can never race this one."""
        if not self.working:
            return
        self._anim_gen = int(getattr(self, "_anim_gen", 0)) + 1
        self._animate(self._anim_gen)

    def _animate(self, gen):
        # type: (int) -> None
        """One spinner frame, then re-arm. Port of sublime-claude's `_animate`.

        Chrome only: the glyph is the sheet's (`ui/renderer.py` draws
        `frames[_spinner_frame]` and no-ops without a view), so a background
        turn costs one timer per frame and no buffer work.
        """
        if gen != self._anim_gen:
            return
        if not self.working or self.client is None:
            if not self.working:
                self._set_turn_phase("idle")
            return
        phase = self.turn_phase or "waiting"
        if phase == "tool":
            frames, interval = SPINNER_RESPONDING, SPINNER_TOOL_MS
        elif phase == "responding":
            frames, interval = SPINNER_RESPONDING, SPINNER_RESPOND_MS
        else:
            frames, interval = SPINNER_WAITING, SPINNER_WAIT_MS
        try:
            self.output.advance_spinner(frames=frames)
        except TypeError:
            try:
                self.output.advance_spinner()
            except Exception:
                pass
        except Exception:
            pass
        try:
            self.scheduler.call_later(interval, lambda: self._animate(gen))
        except Exception:
            pass

    def _note_activity(self, idle=False):
        # type: (bool) -> None
        """Stamp auto-sleep clocks. idle=True also starts the idle timeout."""
        now = time.time()
        self.last_activity = now
        if idle:
            self.last_idle_at = now

    def _stamp_idle_clock(self):
        # type: () -> None
        self._note_activity(idle=True)

    def _clear_asking_state(self):
        # type: () -> None
        try:
            if self.output:
                self.output.clear_asking_state()
        except Exception:
            pass
        try:
            if isinstance(self.surface, dict):
                self.surface["modals"] = []
        except Exception:
            pass

    @staticmethod
    def _is_asking_tool(name):
        # type: (str) -> bool
        n = (name or "").strip()
        if not n:
            return False
        if n in (
            "ask_user", "AskUserQuestion", "ask_user_question", "AskUser",
            "ExitPlanMode", "EnterPlanMode",
        ):
            return True
        return n.lower() in ("ask_user", "askuserquestion", "ask_user_question")

    def _drop_resume_asking_if_needed(self, send_fn):
        # type: (Callable) -> bool
        """True if leftover asking from resume was cancelled (no UI)."""
        if not getattr(self, "_resume_drop_asking", False):
            return False
        try:
            send_fn()
        except Exception as e:
            log_plugin("resume drop asking send: %s" % e)
        if not getattr(self, "_resume_asking_interrupt_sent", False):
            self._resume_asking_interrupt_sent = True
            if self.client:
                try:
                    self.client.send("interrupt", {})
                except Exception:
                    pass
        self._clear_asking_state()
        log_plugin("resume: dropped leftover asking state")
        return True

    def _mark_error_halt(self, message=""):
        # type: (str) -> None
        self.error_halted = True
        self.error_halt_message = message or ""
        self.chrome.set_status("error")

    def _clear_error_halt(self):
        # type: () -> None
        self.error_halted = False
        self.error_halt_message = ""

    def _set_name(self, name):
        # type: (str) -> None
        self.name = name
        self._recovered_title = None
        try:
            self.output.set_name(name)
        except Exception:
            pass
        self._save_session()

    def _fire_turn_end(self, completion):
        # type: (str) -> None
        for cb in list(self.on_turn_end):
            try:
                cb(self, completion)
            except Exception:
                pass

    def _send(self, method, params, callback=None):
        # type: (str, dict, Optional[Callable]) -> bool
        if not self.client:
            return False
        try:
            return bool(self.client.send(method, params, callback))
        except Exception:
            return False

    def _send_bg_poll(self, callback):
        # type: (Callable) -> bool
        return self._send("poll_bg_tasks", {}, callback)

    def _bg_query(self, prompt, display):
        # type: (str, str) -> None
        if self.working or self.turn.awaiting_rpc:
            return
        self.query(prompt, display_prompt=display, silent=False)

    def _bg_surface(self):
        # type: () -> None
        try:
            self.output.refresh_background_hints()
        except Exception:
            pass
        self._set_unread(True)
        for cb in list(self.on_bg_surface):
            try:
                cb(self)
            except Exception:
                pass

    def _event_query(self, prompt, display):
        # type: (str, Optional[str]) -> None
        self.query(prompt, display_prompt=display)

    def _accept_session_id(self, sid):
        # type: (str) -> None
        self.session_id = sid
        self._save_session()

    def _accept_usage(self, usage):
        # type: (dict) -> None
        self.context_usage = usage or None

    def _accept_cost(self, cost):
        # type: (float) -> None
        self.total_cost += float(cost or 0)

    def _accept_loop(self, fire_at):
        # type: (Optional[float]) -> None
        self.next_wake_at = fire_at
        self.is_looping = bool(fire_at)

    def _accept_plan_mode(self, on, plan_file):
        # type: (bool, Optional[str]) -> None
        self.plan_mode = bool(on)
        if plan_file:
            self.plan_file = plan_file
        # ExitPlanMode (approval UI): open the saved plan so it can be read
        # before Y/N. Approving/rejecting must not open it again.
        if not on:
            self._reveal_plan_file(plan_file or self.plan_file)

    def _reveal_plan_file(self, path):
        # type: (Optional[str]) -> None
        import os
        if not path or not os.path.isfile(path):
            return
        window = getattr(self, "window", None)
        try:
            from core.placement import open_plan_file
            open_plan_file(window, path)
        except Exception:
            pass

    def _accept_tool_name(self, name):
        # type: (Optional[str]) -> None
        self.current_tool = name

    def _elapsed(self):
        # type: () -> float
        if self._query_start is None:
            return 0.0
        return max(0.0, time.time() - self._query_start)

    def _spec(self):
        # type: () -> Any
        if backend_specs is None:
            return None
        try:
            return backend_specs.get(self.backend, self.settings)
        except Exception:
            return None

    def _resolve_effort(self, spec):
        # type: (Any) -> str
        if self.profile and self.profile.get("effort"):
            return str(self.profile["effort"]).strip()
        pe = getattr(spec, "effort", None) if spec is not None else None
        if pe:
            return str(pe).strip()
        env = self.settings.get("env") or {}
        return str(
            env.get("CLAUDE_CODE_EFFORT_LEVEL")
            or self.settings.get("effort")
            or "high"
        ).strip()

    def _mcp_enable_read_image(self, model):
        # type: (Optional[str]) -> bool
        flag = self.settings.get("mcp_enable_read_image", "auto")
        mid = model or self.model
        if self.backend == "grok" and mid and grok_backend is not None:
            try:
                if not grok_backend.model_supports_vision(mid):
                    return False
            except Exception:
                pass
        if flag is True or flag == "true":
            return True
        if flag is False or flag == "false":
            return False
        return self.backend == "grok"

    def _cwd(self):
        # type: () -> str
        """Directory this session can stand in (existing folder only)."""
        if self.cwd and os.path.isdir(self.cwd):
            return self.cwd
        window = getattr(self, "window", None)
        if window is not None:
            try:
                for folder in window.folders() or []:
                    if folder and os.path.isdir(folder):
                        return folder
            except Exception:
                pass
            try:
                view = window.active_view()
                if view and view.file_name():
                    parent = os.path.dirname(view.file_name())
                    if parent and os.path.isdir(parent):
                        return parent
            except Exception:
                pass
        return self._default_cwd()

    def _default_cwd(self):
        # type: () -> str
        if self.cwd:
            return self.cwd
        scratch = os.path.expanduser("~/.claude/scratch")
        try:
            os.makedirs(scratch, exist_ok=True)
        except Exception:
            pass
        return scratch


def create_session(
    output,  # type: OutputPort
    chrome,  # type: ChromePort
    scheduler,  # type: Scheduler
    persist,  # type: PersistPort
    registry=None,  # type: Optional[SessionRegistry]
    resume_id=None,  # type: Optional[str]
    fork=False,  # type: bool
    **kwargs
):
    # type: (...) -> Session
    """Construct a Session, or reveal the live one for this session_id.

    One live Session per session_id (invariant 7).
    """
    reg = registry or default_registry
    if resume_id and not fork:
        existing = reg.find_live_by_session_id(resume_id)
        if existing is not None:
            return existing
    s = Session(
        output, chrome, scheduler, persist,
        registry=reg, resume_id=resume_id, fork=fork, **kwargs)
    try:
        s._persist_view_identity()
    except Exception:
        pass
    reg.register(s)
    return s
