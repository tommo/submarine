"""Bridge notification dispatch. No sublime, no view access.

Taxonomy is frozen (bridge-backends.md §Extra JSON-RPC contract).
Result-before-RPC-completion is assumed (bridges guarantee it).
Handlers call OutputPort / ChromePort / TurnController / BackgroundTaskGate.
"""
from __future__ import annotations

from typing import Any, Callable, Optional, TYPE_CHECKING

from .background import is_shell_background_tool
from .turn import looks_like_compact_done, looks_like_compact_start

if TYPE_CHECKING:
    from .background import BackgroundTaskGate
    from .ports import ChromePort, OutputPort, Scheduler
    from .turn import TurnController

_LOOP_TOOLS = (
    "ScheduleWakeup", "CronCreate", "scheduler_create", "SchedulerCreate",
)

_PERM_ALLOW = frozenset({"allow", "allow_all", "allow_session"})
_PLAN_APPROVE = "approve"


class BridgeEventRouter:
    """Dispatch table for bridge → host notifications."""

    def __init__(
        self,
        output,  # type: OutputPort
        chrome,  # type: ChromePort
        turn,  # type: TurnController
        scheduler,  # type: Scheduler
        bg,  # type: BackgroundTaskGate
        send,  # type: Callable[..., bool]
        backend="claude",  # type: str
        on_query=None,  # type: Optional[Callable]
        on_queue=None,  # type: Optional[Callable[[str], None]]
        on_interrupt=None,  # type: Optional[Callable[[], None]]
        on_session_id=None,  # type: Optional[Callable[[str], None]]
        on_usage=None,  # type: Optional[Callable[[dict], None]]
        on_cost=None,  # type: Optional[Callable[[float], None]]
        on_phase=None,  # type: Optional[Callable[[str], None]]
        on_error=None,  # type: Optional[Callable[[str], None]]
        on_loop=None,  # type: Optional[Callable[[Optional[float]], None]]
        on_plan_mode=None,  # type: Optional[Callable[[bool, Optional[str]], None]]
        on_compact_start=None,  # type: Optional[Callable[[], None]]
        on_compact_done=None,  # type: Optional[Callable[[], None]]
        on_resume_stream=None,  # type: Optional[Callable[[], None]]
        on_result_idle=None,  # type: Optional[Callable[[], None]]
        on_tool_name=None,  # type: Optional[Callable[[Optional[str]], None]]
        elapsed=None,  # type: Optional[Callable[[], float]]
        is_compacting=None,  # type: Optional[Callable[[], bool]]
        interrupt_stream=None,  # type: Optional[Callable[[], bool]]
        drop_asking=None,  # type: Optional[Callable]
        is_asking_tool=None,  # type: Optional[Callable]
        resume_drop_asking=None,  # type: Optional[Callable[[], bool]]
    ):
        self.output = output
        self.chrome = chrome
        self.turn = turn
        self.scheduler = scheduler
        self.bg = bg
        self.send = send
        self.backend = backend
        self.on_query = on_query
        self.on_queue = on_queue
        self.on_interrupt = on_interrupt
        self.on_session_id = on_session_id
        self.on_usage = on_usage
        self.on_cost = on_cost
        self.on_phase = on_phase
        self.on_error = on_error
        self.on_loop = on_loop
        self.on_plan_mode = on_plan_mode
        self.on_compact_start = on_compact_start
        self.on_compact_done = on_compact_done
        self.on_resume_stream = on_resume_stream
        self.on_result_idle = on_result_idle
        self.on_tool_name = on_tool_name
        self.elapsed = elapsed
        self.is_compacting = is_compacting
        self.interrupt_stream = interrupt_stream
        self.drop_asking = drop_asking
        self.is_asking_tool = is_asking_tool
        self.resume_drop_asking = resume_drop_asking
        self.current_tool = None  # type: Optional[str]
        self._api_retry_hint = None  # type: Optional[str]

    def dispatch(self, method, params):
        # type: (str, dict) -> None
        params = params or {}
        method_handler = self._method_handlers().get(method)
        if method_handler is not None:
            method_handler(params)
            return
        if method != "message":
            return
        t = params.get("type")
        msg_handler = self._message_handlers().get(t)
        if msg_handler is not None:
            msg_handler(params)

    def _method_handlers(self):
        return {
            "permission_request": self.permission_request,
            "question_request": self.question_request,
            "plan_mode_enter": self.plan_mode_enter,
            "plan_mode_exit": self.plan_mode_exit,
            "plan_response": lambda _p: None,
            "queued_inject": self.queued_inject,
            "notification_wake": self.notification_wake,
            "loop_scheduled": self.loop_scheduled,
        }

    def _message_handlers(self):
        return {
            "tool_use": self.tool_use,
            "tool_result": self.tool_result,
            "text_delta": self.text,
            "text": self.text,
            "replay_user": self.replay_user,
            "thinking": self.thinking,
            "turn_usage": self.turn_usage,
            "result": self.result,
            "system": self.system,
            "plan_todos": self.plan_todos,
        }

    # ── method-level ──────────────────────────────────────────────────

    def permission_request(self, params):
        # type: (dict) -> None
        pid = params.get("id")
        tool = params.get("tool", "Unknown")
        tool_input = params.get("input", {}) or {}
        if self.drop_asking is not None and self.drop_asking(lambda: self.send(
                "permission_response", {
                    "id": pid,
                    "allow": False,
                    "always": False,
                    "input": None,
                    "message": "User denied permission",
                })):
            return

        def on_response(response, _tool=tool, _pid=pid, _inp=tool_input):
            allow = response in _PERM_ALLOW
            always = response == "allow_all"
            if not allow:
                try:
                    self.output.tool_error(_tool, "", None)
                except Exception:
                    pass
                self.current_tool = None
                if self.on_tool_name is not None:
                    self.on_tool_name(None)
            self.send("permission_response", {
                "id": _pid,
                "allow": allow,
                "always": always,
                "input": _inp if allow else None,
                "message": None if allow else "User denied permission",
            })

        self.output.permission_request(pid, tool, tool_input, on_response)

    def question_request(self, params):
        # type: (dict) -> None
        qid = params.get("id")
        questions = params.get("questions") or []
        if self.drop_asking is not None and self.drop_asking(lambda: self.send(
                "question_response", {"id": qid, "answers": None})):
            return
        if not questions:
            self.send("question_response", {"id": qid, "answers": {}})
            return

        def on_done(answers, _qid=qid):
            self.send("question_response", {"id": _qid, "answers": answers})

        self.output.question_request(qid, questions, on_done)

    def plan_mode_enter(self, params):
        # type: (dict) -> None
        if self.on_plan_mode is not None:
            self.on_plan_mode(True, None)
        self.chrome.set_status("plan mode")

    def plan_mode_exit(self, params):
        # type: (dict) -> None
        plan_id = params.get("id")
        tool_input = params.get("tool_input") or {}
        if self.drop_asking is not None and self.drop_asking(lambda: self.send(
                "plan_response", {
                    "id": plan_id,
                    "approved": False,
                    "plan": "",
                    "planFilePath": "",
                })):
            return
        plan_file = (
            tool_input.get("planFilePath")
            or tool_input.get("plan_file")
            or ""
        )
        allowed_prompts = tool_input.get("allowedPrompts") or []
        if self.on_plan_mode is not None:
            self.on_plan_mode(False, plan_file or None)

        def on_response(response, _pid=plan_id, _file=plan_file, _inp=tool_input):
            approved = response == _PLAN_APPROVE
            plan_text = _inp.get("plan") or ""
            if _file and os_path_isfile(_file):
                try:
                    with open(_file, "r", encoding="utf-8", errors="replace") as f:
                        plan_text = f.read()
                except Exception:
                    pass
            self.send("plan_response", {
                "id": _pid,
                "approved": approved,
                "plan": plan_text,
                "planFilePath": _file or "",
            })
            self.chrome.set_status("implementing..." if approved else "ready")

        self.output.plan_approval_request(
            plan_id, plan_file or "", allowed_prompts, on_response)

    def queued_inject(self, params):
        # type: (dict) -> None
        message = (params.get("message") or "").strip()
        if not message:
            return
        if self.turn.should_queue_prompt():
            if self.on_queue is not None:
                self.on_queue(message)
            return
        if self.on_query is not None:
            self.on_query(message, None)

    def notification_wake(self, params):
        # type: (dict) -> None
        wake_prompt = params.get("wake_prompt") or ""
        display_message = params.get("display_message") or ""
        if display_message:
            user_message = display_message
        else:
            first_line = wake_prompt.split("\n")[0].strip() if wake_prompt else ""
            user_message = first_line if first_line else "🔔 Notification received"

        if params.get("interrupt") and self.turn.busy:
            if self.on_interrupt is not None:
                self.on_interrupt()

            def start_interrupt_wake():
                if self.on_query is not None:
                    self.on_query(wake_prompt, user_message)

            self.scheduler.call_later(600, start_interrupt_wake)
            return

        if self.turn.busy:
            def start_wake_query():
                if not self.turn.busy:
                    if self.on_query is not None:
                        self.on_query(wake_prompt, user_message)
                else:
                    self.scheduler.call_later(500, start_wake_query)

            self.scheduler.call_later(500, start_wake_query)
            return
        if self.on_query is not None:
            self.on_query(wake_prompt, user_message)

    def loop_scheduled(self, params):
        # type: (dict) -> None
        fire_at = params.get("fire_at")
        if self.on_loop is not None:
            self.on_loop(fire_at)
        self.chrome.wakeup_banner(fire_at)
        self.chrome.refresh_tab_title()

    # ── message-level ─────────────────────────────────────────────────

    def replay_user(self, params):
        # type: (dict) -> None
        """session/load user chunk — deliberate no-op (must not kill ◎)."""
        return

    def plan_todos(self, params):
        # type: (dict) -> None
        if not self.turn.busy:
            return
        entries = params.get("entries") or []
        if not isinstance(entries, list) or not entries:
            return
        try:
            self.output.apply_plan_todos(entries)
        except Exception:
            pass

    def thinking(self, params):
        # type: (dict) -> None
        action = self.turn.inbound_action("thinking")
        if action == "drop":
            return
        self._clear_api_retry_hint()
        if self.turn.busy and self.on_phase is not None:
            self.on_phase("responding")

    def turn_usage(self, params):
        # type: (dict) -> None
        usage = params.get("usage") or {}
        if usage and self.on_usage is not None:
            self.on_usage(usage)

    def text(self, params):
        # type: (dict) -> None
        self._clear_api_retry_hint()
        text = params.get("text") or ""
        if params.get("replay"):
            if text:
                self.output.text(text)
            return
        if (
            not self.turn.busy
            and self.interrupt_stream is not None
            and self.interrupt_stream()
            and not params.get("replay")
        ):
            self._maybe_resume_stream()
        compacting = self.is_compacting() if self.is_compacting else (
            self.turn.kind == "compacting")
        if compacting and looks_like_compact_done(text):
            if self.turn.busy and self.on_phase is not None:
                self.on_phase("responding")
            if text:
                self.output.text(text)
            if self.on_compact_done is not None:
                self.on_compact_done()
            return
        if looks_like_compact_start(text):
            if self.on_compact_start is not None:
                self.on_compact_start()
        action = self.turn.inbound_action("text")
        if action == "drop":
            return
        if self.turn.busy and self.on_phase is not None:
            self.on_phase("responding")
        if text:
            self.output.text(text)

    def tool_use(self, params):
        # type: (dict) -> None
        name = params.get("name") or ""
        tool_input = params.get("input") or {}
        background = bool(params.get("background"))
        tool_id = params.get("id")
        if not name or not str(name).strip():
            return
        if (
            self.resume_drop_asking is not None
            and self.resume_drop_asking()
            and self.is_asking_tool is not None
            and self.is_asking_tool(name)
        ):
            return
        if params.get("replay"):
            try:
                self.output.tool(name, tool_input, tool_id=tool_id, background=False)
            except Exception:
                pass
            return

        inbound = "tool_use_bg" if background else (
            "synth_bash" if name in ("Bash", "Shell") else "tool_use"
        )
        action = self.turn.inbound_action(inbound)
        if action == "drop":
            return

        if (
            not self.turn.busy
            and self.interrupt_stream is not None
            and self.interrupt_stream()
        ):
            self._maybe_resume_stream()

        if not self.turn.busy:
            # Leftover after @done: paint, never re-own busy (invariant 10).
            try:
                self.output.tool(
                    name, tool_input, tool_id=tool_id,
                    background=bool(background) or action == "paint_bg")
            except Exception:
                pass
            if (background or action == "paint_bg") and tool_id:
                tid = ""
                if isinstance(tool_input, dict):
                    tid = str(
                        tool_input.get("task_id")
                        or tool_input.get("taskId")
                        or "")
                tool = None
                try:
                    tool = self.output.find_tool_by_id(tool_id)
                except Exception:
                    tool = None
                self.bg.register_tool(tool_id, tool=tool, task_id=tid)
                try:
                    if self.output.is_input_mode():
                        self.output.refresh_background_hints()
                except Exception:
                    pass
            return

        self._clear_api_retry_hint()
        if name in _LOOP_TOOLS and self.on_loop is not None:
            fire_at = None
            if name == "ScheduleWakeup" and isinstance(tool_input, dict):
                try:
                    d = float(tool_input.get("delaySeconds") or 0)
                except (TypeError, ValueError):
                    d = 0
                if d > 0:
                    import time as _time
                    fire_at = _time.time() + max(60.0, min(d, 3600.0))
            self.on_loop(fire_at)

        if background:
            if not is_shell_background_tool(name):
                background = False
                if tool_id:
                    self.bg.drop_tool(tool_id)
            else:
                self.output.tool(name, tool_input, tool_id=tool_id, background=True)
                tool = None
                try:
                    tool = self.output.find_tool_by_id(tool_id) if tool_id else None
                except Exception:
                    tool = None
                if tool_id:
                    tid = ""
                    if isinstance(tool_input, dict):
                        tid = str(
                            tool_input.get("task_id")
                            or tool_input.get("taskId")
                            or "")
                    self.bg.register_tool(tool_id, tool=tool, task_id=tid)
                try:
                    if self.output.is_input_mode():
                        self.output.refresh_background_hints()
                except Exception:
                    pass
                return

        if (not tool_id and self.current_tool and self.current_tool.strip()
                and self.current_tool != name):
            try:
                self.output.tool_done(self.current_tool)
            except Exception:
                pass
        self.current_tool = name
        if self.on_tool_name is not None:
            self.on_tool_name(name)
        if not background and self.on_phase is not None:
            self.on_phase("tool")
        self.output.tool(name, tool_input, tool_id=tool_id, background=False)

    def tool_result(self, params):
        # type: (dict) -> None
        tool_use_id = params.get("tool_use_id")
        content = params.get("content", "")
        if isinstance(content, list):
            content = "\n".join(str(c) for c in content)
        if isinstance(content, str) and len(content) > 10000:
            content = content[:10000]
        is_error = params.get("is_error")
        if params.get("replay"):
            matched = None
            try:
                matched = self.output.find_tool_by_id(tool_use_id) if tool_use_id else None
            except Exception:
                matched = None
            name = getattr(matched, "name", None) or "tool"
            try:
                if is_error:
                    self.output.tool_error(name, content, tool_id=tool_use_id)
                else:
                    self.output.tool_done(name, content, tool_id=tool_use_id)
            except Exception:
                pass
            return

        matched = None
        try:
            matched = self.output.find_tool_by_id(tool_use_id) if tool_use_id else None
        except Exception:
            matched = None
        was_background = (
            matched is not None
            and getattr(matched, "status", None) in ("background", "⚙")
        )
        tool_name = getattr(matched, "name", None) or self.current_tool
        if not tool_name or not str(tool_name).strip():
            self.current_tool = None
            if self.on_tool_name is not None:
                self.on_tool_name(None)
            return
        if was_background:
            return
        if is_error:
            self.output.tool_error(tool_name, content, tool_id=tool_use_id)
        else:
            self.output.tool_done(tool_name, content, tool_id=tool_use_id)
        if not is_error:
            self.bg.note_task_poll_delivery(tool_name, content or "")
        if tool_name == self.current_tool:
            self.current_tool = None
            if self.on_tool_name is not None:
                self.on_tool_name(None)
        if self.turn.busy and self.on_phase is not None:
            self.on_phase("waiting")

    def result(self, params):
        # type: (dict) -> None
        sid = params.get("session_id") or params.get("sessionId")
        if sid and self.on_session_id is not None:
            self.on_session_id(str(sid))
        cost = params.get("total_cost_usd") or 0
        try:
            cost_f = float(cost)
        except (TypeError, ValueError):
            cost_f = 0.0
        if cost_f and self.on_cost is not None:
            self.on_cost(cost_f)
        try:
            dur = float(params.get("duration_ms") or 0) / 1000.0
        except (TypeError, ValueError):
            dur = 0.0
        if dur <= 0 and self.elapsed is not None:
            try:
                dur = max(0.0, float(self.elapsed()))
            except Exception:
                pass
        usage = params.get("usage")
        if usage and self.on_usage is not None:
            self.on_usage(usage)
        stop = params.get("stop_reason") or params.get("stopReason") or ""
        leftover_end = bool(params.get("leftover_end"))
        if (
            not leftover_end
            and (
                params.get("status") == "interrupted"
                or stop in ("interrupted", "cancelled", "canceled")
            )
        ):
            try:
                self.output.reset_active_states(soft=True)
            except Exception:
                pass
            return
        compacting = self.is_compacting() if self.is_compacting else (
            self.turn.kind == "compacting")
        if params.get("is_error"):
            stop = stop or "error"
            try:
                self.output.reset_active_states(soft=True)
            except Exception:
                pass
            msg = "turn failed (%s)" % stop
            try:
                self.output.text("\n\n*⚠ %s.*\n" % msg)
            except Exception:
                pass
            if self.on_error is not None:
                self.on_error(msg)
            return
        if not compacting:
            try:
                self.output.meta(dur, cost_f, usage=usage)
            except Exception:
                pass
        if compacting:
            return
        # Adopted leftover closer (no host query RPC): go idle here.
        if (
            self.turn.busy
            and not self.turn.awaiting_rpc
            and params.get("status") != "interrupted"
            and not params.get("is_error")
        ):
            self.turn.end_live()
            if self.on_result_idle is not None:
                self.on_result_idle()

    def system(self, params):
        # type: (dict) -> None
        subtype = params.get("subtype") or ""
        data = params.get("data") or {}
        if subtype == "task_started":
            self.bg.on_task_started(data)
        elif subtype == "task_updated":
            self.bg.on_task_updated(data)
        elif subtype == "task_notification":
            self.bg.on_task_notification(data, working=self.turn.busy)
        elif subtype == "api_retry":
            self._on_api_retry(data)
        elif subtype == "compact_boundary":
            if self.on_usage is not None:
                self.on_usage({})
        elif subtype in ("error", "init", "compaction"):
            msg = ""
            if isinstance(data, dict):
                msg = data.get("message") or ""
            elif isinstance(data, str):
                msg = data
            if msg:
                try:
                    self.output.text("\n*%s*\n" % msg)
                except Exception:
                    pass
            if subtype == "error" and msg and self.on_error is not None:
                self.on_error(msg)
        # task_progress / notify-* / status: hook only (workflow is features/)

    def _on_api_retry(self, data):
        # type: (dict) -> None
        attempt = data.get("attempt")
        max_retries = data.get("max_retries")
        status = data.get("error_status")
        exhausted = attempt is not None and max_retries and attempt >= max_retries
        tag = str(status) if status else "error"
        hint = "⚠ %s retry %s/%s%s" % (
            tag, attempt, max_retries,
            " · exhausted" if exhausted else "")
        self._api_retry_hint = hint
        try:
            self.output.set_retry_hint(hint)
        except Exception:
            pass
        self.chrome.set_status(hint)

    def _clear_api_retry_hint(self):
        # type: () -> None
        if self._api_retry_hint:
            self._api_retry_hint = None
            try:
                self.output.set_retry_hint("")
            except Exception:
                pass

    def _maybe_resume_stream(self):
        # type: () -> None
        if self.on_resume_stream is not None:
            self.on_resume_stream()
        else:
            self.turn.resume_stream()


def os_path_isfile(path):
    # type: (str) -> bool
    import os
    try:
        return bool(path) and os.path.isfile(path)
    except Exception:
        return False
