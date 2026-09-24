"""Background-task gate: ⚙ pairing, completion display, poll, reconcile.

A background job's completion never starts a turn from here. Every runtime
delivers its own results to the model:
  * Claude Code enqueues a follow-up turn itself (a user message with
    ``origin.kind == "task-notification"``); the bridge forwards it and the
    host adopts it (``BridgeEventRouter.injected_turn``).
  * Grok self-wakes after ``terminal/wait_for_exit`` (synthetic prompt,
    ``turn_completed`` closer) — the host owns that with ``resume_stream``.
  * Kimi runs its own internal completion turn.
A host-built ``<task-notification>`` query on top of that was a second, full
context send answering "already accounted for" — that is what this used to do.

What the host owes the user is the row: ⚙ while the job runs, ✓/✗ with what it
printed when it ends, unread + hint refresh, and a poll/reconcile that never
leaves a dead ⚙ behind. Completion events differ per backend and all count:
  * ``task_notification`` carries status + summary + output_file (acp/kimi,
    and Claude Code — after the turn it follows ``task_updated`` by ~0 ms);
  * a terminal ``task_updated`` has neither tool_use_id nor output_file, so
    ``note_launch_ack`` keeps the task id and log path from the launch ack.
The first completion event flips the row (ACP bridges send the rich
``task_notification`` first); later ones for the same job are no-ops.

Authority split (do not invert):
  * Host ``notified_*`` is the durable "row already flipped" set. ``abort()``
    clears it (sleep/wake /clear must not skip a later reused id).
  * Bridge ``_bg_notified_*`` is ephemeral emit suppression for the process
    lifetime only. The host never treats a wire sighting as "notified."
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from .ports import OutputPort, Scheduler
    from .turn import TurnController

_TASK_TERMINAL = (
    "completed", "failed", "cancelled", "canceled",
    "error", "errored", "aborted", "timeout", "crashed",
    "killed", "stopped",
)

# Canonical host allowlists. UI imports these; bridge cannot (separate
# process) and keeps a commented copy of the split.
SHELL_BG = (
    "Bash", "Shell", "execute", "run_terminal_command", "Workflow",
)
SUBAGENT_BG = (
    "Task", "Subagent",
)

# Claude Code's background-bash launch ack (Bash tool_result). The SDK's later
# `task_updated` carries no output_file, so this text is the only place that
# names the task AND its log — `sandbox/claude_bg/` captures it:
#   "Command running in background with ID: byfbn40bu. Output is being written
#    to: /…/tasks/byfbn40bu.output. You will be notified when it completes."
_CC_BG_ACK_RE = re.compile(r"running in background with ID:\s*([A-Za-z0-9_-]+)",
                           re.I)
_CC_BG_LOG_RE = re.compile(
    r"output is being written to:\s*(\S+)", re.I)

POLL_BUSY_MS = 5000
POLL_IDLE_MS = 8000


def alias_bg_task_ids(task_tool_map, tool_use_id, task_id=""):
    # type: (dict, Optional[str], str) -> set
    """All task_ids bound to this tool (acp-term-* and bash-* for one job)."""
    ids = set()  # type: Set[str]
    if task_id:
        ids.add(str(task_id))
    if tool_use_id:
        for tid, tuid in (task_tool_map or {}).items():
            if tuid == tool_use_id:
                ids.add(str(tid))
    return ids


def bg_notify_already(state, task_id="", tool_use_id=""):
    # type: (dict, str, str) -> bool
    if tool_use_id and tool_use_id in state.get("_bg_notified_tool_ids", ()):
        return True
    mapping = state.get("_task_tool_map") or {}
    for tid in alias_bg_task_ids(mapping, tool_use_id, task_id):
        if tid in state.get("_bg_notified_task_ids", ()):
            return True
    return False


def mark_bg_notify_ids(state, task_id="", tool_use_id=""):
    # type: (dict, str, str) -> None
    if tool_use_id:
        state.setdefault("_bg_notified_tool_ids", set()).add(tool_use_id)
    mapping = state.get("_task_tool_map") or {}
    for tid in alias_bg_task_ids(mapping, tool_use_id, task_id):
        state.setdefault("_bg_notified_task_ids", set()).add(tid)


class BackgroundTaskGate:
    """Owns the ⚙ registry, completion display, poll epochs and reconcile."""

    def __init__(
        self,
        turn,  # type: TurnController
        scheduler,  # type: Scheduler
        output,  # type: OutputPort
        backend="claude",  # type: str
        send_poll=None,  # type: Optional[Callable[[Callable], bool]]
        on_surface=None,  # type: Optional[Callable[[], None]]
        read_output_file=None,  # type: Optional[Callable[[str], str]]
    ):
        self.turn = turn
        self.scheduler = scheduler
        self.output = output
        self.backend = backend
        self.send_poll = send_poll
        self.on_surface = on_surface
        self.read_output_file = read_output_file or _read_file

        self.task_tool_map = {}  # type: dict  # task_id -> tool_use_id
        self.bg_tools = {}  # type: dict
        self.bg_task_ids = set()  # type: Set[str]
        self.seen_running = set()  # type: Set[str]
        self.task_labels = {}  # type: dict  # task_id -> label
        self.task_logs = {}  # type: dict  # task_id -> output file (from the ack)
        self.foreground_tasks = set()  # type: Set[str]
        self.notified_task_ids = set()  # type: Set[str]
        self.notified_tool_ids = set()  # type: Set[str]
        self.poll_epoch = 0
        self._poll_armed = False

    def _state_dict(self):
        # type: () -> dict
        return {
            "_task_tool_map": self.task_tool_map,
            "_bg_notified_task_ids": self.notified_task_ids,
            "_bg_notified_tool_ids": self.notified_tool_ids,
        }

    def alias_ids(self, tool_use_id, task_id=""):
        # type: (Optional[str], str) -> set
        return alias_bg_task_ids(self.task_tool_map, tool_use_id, task_id)

    def already(self, task_id="", tool_use_id=""):
        # type: (str, str) -> bool
        return bg_notify_already(self._state_dict(), task_id, tool_use_id)

    def mark(self, task_id="", tool_use_id=""):
        # type: (str, str) -> None
        mark_bg_notify_ids(self._state_dict(), task_id, tool_use_id)

    def register_tool(self, tool_id, tool=None, task_id=""):
        # type: (str, Any, str) -> None
        if not tool_id:
            return
        self.bg_task_ids.add(tool_id)
        if tool is not None:
            self.bg_tools[tool_id] = tool
        if task_id:
            self.task_tool_map[str(task_id)] = tool_id
        self.schedule_poll()

    def task_id_for(self, tool_use_id):
        # type: (str) -> str
        """The task id behind a ⚙ row, or "". A CLI task id (what
        `stop_task` takes) wins over a bridge's `acp-term-*` alias."""
        if not tool_use_id:
            return ""
        ids = [t for t, tool in self.task_tool_map.items() if tool == tool_use_id]
        for t in reversed(ids):
            if not t.startswith("acp-term-"):
                return t
        return ids[-1] if ids else ""

    def drop_tool(self, tool_id):
        # type: (str) -> None
        if not tool_id:
            return
        self.bg_task_ids.discard(tool_id)
        self.bg_tools.pop(tool_id, None)

    def note_launch_ack(self, tool_use_id, content):
        # type: (str, str) -> bool
        """Claude Code's background launch ack → the job is RUNNING.

        Returns True when `content` is that ack (the caller keeps the ⚙ row).
        Also binds the task id and its log path: the SDK's terminal
        `task_updated` carries neither `tool_use_id` nor `output_file`, so
        without this the completion cannot name what finished or read its log.
        """
        text = content if isinstance(content, str) else str(content or "")
        match = _CC_BG_ACK_RE.search(text)
        if match is None:
            return False
        task_id = match.group(1)
        if tool_use_id:
            self.task_tool_map[task_id] = tool_use_id
            self.bg_task_ids.add(tool_use_id)
        log = _CC_BG_LOG_RE.search(text)
        if log is not None:
            self.task_logs[task_id] = log.group(1).rstrip(".,")
        self.foreground_tasks.discard(task_id)
        self.schedule_poll()
        return True

    def _is_ours(self, tool_use_id):
        # type: (str) -> bool
        return bool(tool_use_id) and (
            tool_use_id in self.bg_task_ids or tool_use_id in self.bg_tools)

    def on_task_started(self, data):
        # type: (dict) -> None
        task_id = data.get("task_id") or ""
        tool_use_id = data.get("tool_use_id") or ""
        label = data.get("description") or data.get("summary") or ""
        if task_id and label:
            self.task_labels[task_id] = str(label).strip()
        if data.get("is_backgrounded") is False:
            # Claude Code opens task_started for FOREGROUND commands too
            # (`is_backgrounded: false`). Their completion was already answered
            # inside the turn: no ⚙ row, and no notification turn.
            if task_id and not self._is_ours(tool_use_id):
                self.foreground_tasks.add(task_id)
            return
        if task_id and tool_use_id:
            self.task_tool_map[task_id] = tool_use_id
            self.schedule_poll()

    def on_task_updated(self, data):
        # type: (dict) -> None
        task_id = data.get("task_id") or ""
        status = (data.get("patch") or {}).get("status", "")
        if not task_id or status not in _TASK_TERMINAL:
            return
        if task_id in self.foreground_tasks:
            return
        tool_use_id = (
            data.get("tool_use_id")
            or self.task_tool_map.get(task_id)
            or ""
        )
        if not tool_use_id:
            return
        # A terminal update IS the completion for jobs that never get a
        # `task_notification` (Claude Code background bash once the turn has
        # ended sends only task_updated + background_tasks_changed).
        self._complete(task_id, tool_use_id, status)

    def on_task_notification(self, data, working=False):
        # type: (dict, bool) -> None
        _ = working
        task_id = data.get("task_id") or ""
        status = data.get("status") or ""
        tool_use_id = (
            data.get("tool_use_id")
            or self.task_tool_map.get(task_id)
            or ("bg-%s" % task_id if task_id else "")
        )
        if not status:
            return
        if task_id in self.foreground_tasks:
            return
        self._complete(
            task_id, tool_use_id, status,
            summary=data.get("summary") or "",
            output_file=data.get("output_file") or "")

    def _notified(self, task_id="", tool_use_id=""):
        # type: (str, str) -> bool
        """True once the row was flipped for this job."""
        if tool_use_id and tool_use_id in self.notified_tool_ids:
            return True
        for tid in self.alias_ids(tool_use_id, task_id):
            if tid in self.notified_task_ids:
                return True
        return False

    def _complete(self, task_id, tool_use_id, status, summary="",
                  output_file=""):
        # type: (str, str, str, str, str) -> bool
        """Flip the job's row once, with what it printed; surface it.

        Never starts a turn. The first event wins: ACP bridges send the rich
        `task_notification` before their `task_updated`; Claude Code's
        `task_updated` has no output but the launch ack named the log
        (`task_logs`). Later events for the same job (aliases, re-sends,
        the poll) are no-ops. Returns False when nothing was shown.
        """
        if self._notified(task_id, tool_use_id):
            return False
        if task_id and task_id in self.foreground_tasks:
            return False
        if not (task_id or tool_use_id):
            return False
        # Mark before touching the row: alias events for the same job must
        # find it already flipped.
        self.mark(task_id, tool_use_id)
        if not output_file:
            output_file = self.task_logs.get(task_id, "")
        text = self._result_text(task_id, status, summary, output_file)
        if tool_use_id:
            self.finalize_tool(
                tool_use_id, keep=True, result=text,
                error=(status != "completed"))
            self.drop_tool(tool_use_id)
        if task_id:
            self.task_tool_map.pop(task_id, None)
            self.task_labels.pop(task_id, None)
            self.task_logs.pop(task_id, None)
            self.seen_running.discard(task_id)
        if self.on_surface is not None:
            try:
                self.on_surface()
            except Exception:
                pass
        else:
            try:
                self.output.refresh_background_hints()
            except Exception:
                pass
        return True

    def _result_text(self, task_id, status, summary="", output_file=""):
        # type: (str, str, str, str) -> str
        """What the finished row shows: a one-line header, then the log."""
        output = ""
        if output_file:
            try:
                output = (self.read_output_file(output_file) or "").strip()
            except Exception:
                output = ""
        max_out = 8000
        if len(output) > max_out:
            output = (
                "…[%s chars omitted; full: %s]\n" % (
                    len(output) - max_out, output_file)
                + output[-max_out:]
            )
        summary = (summary or self.task_labels.get(task_id) or task_id
                   or "background task").strip()
        if is_child_session_id(summary):
            summary = "subagent"
        if "\n" in summary:
            summary = " ⏎ ".join(
                s.strip() for s in summary.splitlines() if s.strip())
        if len(summary) > 100:
            summary = summary[:99] + "…"
        header = summary if status == "completed" else "%s [%s]" % (
            summary, status)
        if output:
            return "%s\n%s" % (header, output)
        if output_file:
            return "%s\nlog: %s" % (header, output_file)
        return header

    def finalize_tool(self, tool_use_id, keep, result=None, error=False):
        # type: (str, bool, Optional[str], bool) -> None
        """Idempotent ⚙ → ✓ (keep; ✗ when error) with `result`, or drop."""
        tool = self.bg_tools.get(tool_use_id)
        if tool is None:
            try:
                tool = self.output.find_tool_by_id(tool_use_id)
            except Exception:
                tool = None
        status = getattr(tool, "status", None) if tool is not None else None
        if tool is None or status not in ("background", "⚙"):
            self.bg_tools.pop(tool_use_id, None)
            return
        if keep and error:
            try:
                self.output.tool_error(
                    getattr(tool, "name", "tool"), result or "", tool_use_id)
            except Exception:
                pass
        elif keep:
            try:
                self.output.tool_done(
                    getattr(tool, "name", "tool"), result or "", tool_use_id)
            except Exception:
                pass
        else:
            try:
                self.output.remove_tool(tool)
            except Exception:
                pass
        self.bg_tools.pop(tool_use_id, None)
        try:
            if self.output.is_input_mode():
                self.output.refresh_background_hints()
        except Exception:
            pass

    def schedule_poll(self):
        # type: () -> None
        if self._poll_armed:
            return
        if not (self.task_tool_map or self.bg_task_ids or self.bg_tools):
            return
        epoch = self.poll_epoch
        self._poll_armed = True

        def _tick(e=epoch):
            self._poll_armed = False
            if e != self.poll_epoch:
                return
            self._poll()

        self.scheduler.call_later(POLL_BUSY_MS, _tick)

    def _poll(self):
        # type: () -> None
        epoch = self.poll_epoch
        if not (self.task_tool_map or self.bg_task_ids or self.bg_tools):
            return
        if self.turn.busy:
            self._arm_poll(POLL_BUSY_MS, epoch)
            return
        if self.send_poll is None:
            self._arm_poll(POLL_IDLE_MS, epoch)
            return

        def _cb(result, e=epoch):
            if e != self.poll_epoch:
                return
            self.on_poll_result(result)

        try:
            self.send_poll(_cb)
        except Exception:
            if self.task_tool_map or self.bg_task_ids or self.bg_tools:
                self._arm_poll(POLL_IDLE_MS, epoch)

    def _arm_poll(self, delay_ms, epoch):
        # type: (int, int) -> None
        if epoch != self.poll_epoch:
            return
        if self._poll_armed:
            return
        self._poll_armed = True

        def _tick(e=epoch):
            self._poll_armed = False
            if e != self.poll_epoch:
                return
            self._poll()

        self.scheduler.call_later(delay_ms, _tick)

    def on_poll_result(self, result):
        # type: (Optional[dict]) -> None
        try:
            running = result.get("running") if result else None
        except Exception:
            running = None
        self.reconcile(running)
        if not (self.task_tool_map or self.bg_task_ids or self.bg_tools):
            return
        self._arm_poll(POLL_IDLE_MS, self.poll_epoch)

    def reconcile(self, running=None):
        # type: (Optional[list]) -> None
        for tid in list(self.bg_tools):
            tool = self.bg_tools.get(tid)
            status = getattr(tool, "status", None) if tool is not None else None
            if tool is None or status not in ("background", "⚙"):
                self.bg_tools.pop(tid, None)
        if running is None:
            return
        live = set(running or [])
        self.seen_running |= live
        live_tools = set()
        for task_id, tool_use_id in self.task_tool_map.items():
            if task_id in live:
                live_tools.add(tool_use_id)
        for tuid in self.bg_task_ids:
            if tuid in live:
                live_tools.add(tuid)

        for task_id, tool_use_id in list(self.task_tool_map.items()):
            if task_id in live:
                continue
            # Missed terminal event: only the row is owed. The runtime already
            # delivered the result to the model (or never will — the job died
            # with it); a host turn here would be a duplicate either way.
            self.finalize_tool(tool_use_id, keep=True)
            self.drop_tool(tool_use_id)
            self.task_tool_map.pop(task_id, None)
            self.task_labels.pop(task_id, None)
            self.task_logs.pop(task_id, None)
            self.seen_running.discard(task_id)

        for tuid in list(self.bg_task_ids):
            if tuid in live_tools or tuid in live:
                continue
            self.finalize_tool(tuid, keep=True)
            self.drop_tool(tuid)

    def abort(self):
        # type: () -> None
        """Drop ⚙ state — bridge is gone. Bump poll epoch so timers die."""
        for tool in list(self.bg_tools.values()):
            try:
                self.output.remove_tool(tool)
            except Exception:
                pass
        self.bg_tools.clear()
        self.bg_task_ids.clear()
        self.task_tool_map.clear()
        self.seen_running.clear()
        self.task_labels.clear()
        self.task_logs.clear()
        self.foreground_tasks.clear()
        self.notified_task_ids.clear()
        self.notified_tool_ids.clear()
        self.poll_epoch += 1
        self._poll_armed = False

    def has_background(self):
        # type: () -> bool
        return bool(self.bg_task_ids or self.bg_tools)


def _read_file(path):
    # type: (str) -> str
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def is_shell_background_tool(name):
    # type: (str) -> bool
    return name in SHELL_BG or name in SUBAGENT_BG


def is_child_session_id(tid):
    # type: (str) -> bool
    """True for a grok/kimi child session ULID (never print as a label)."""
    tid = str(tid or "").strip()
    if not tid:
        return False
    if tid.startswith("acp-child-"):
        tid = tid[len("acp-child-"):]
    if not tid or tid.startswith(("term_", "bash-")):
        return False
    return tid.count("-") >= 4 and len(tid) >= 20
