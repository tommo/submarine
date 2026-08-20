"""Background-task gate: ⚙ pairing, poll epochs, notify dedupe, flush.

Authority split (do not invert):
  * Host ``notified_*`` is the durable "already shown / already queried"
    set. Mark only when surface/query actually happens. ``abort()``
    clears it (sleep/wake /clear must not skip a later reused id).
  * Bridge ``_bg_notified_*`` is ephemeral emit suppression for the
    process lifetime only. The host never treats a wire sighting as
    "notified."

Notify policy goes through TurnController.notify_action:
  claude → query (host starts a turn with the notification body)
  kimi/grok → surface (⚙ strip + unread; agent auto-continues)
  busy → hold (KEEP the generation-stamped buffer; retry on end_live)

Dedupe is source-contains (TaskGet/TaskOutput already delivered bash-*)
plus mirrored aliases (acp-term-* and bash-* sharing one tool_use_id).
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
)

# Poll-of-output names only. "Task" / "Subagent" are spawn names, not polls.
_POLL_TOOLS = (
    "TaskGet", "TaskOutput", "get_command_or_subagent_output",
)

# Canonical host allowlists. UI imports these; bridge cannot (separate
# process) and keeps a commented copy of the split.
SHELL_BG = (
    "Bash", "Shell", "execute", "run_terminal_command", "Workflow",
)
SUBAGENT_BG = (
    "Task", "Subagent",
)

_BASH_ID_RE = re.compile(r"\b(bash-[\w-]+)\b", flags=re.I)

FLUSH_DEBOUNCE_MS = 1200
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
    if tool_use_id and tool_use_id in state.get("_pending_bg_tool_ids", ()):
        return True
    mapping = state.get("_task_tool_map") or {}
    for tid in alias_bg_task_ids(mapping, tool_use_id, task_id):
        if tid in state.get("_bg_notified_task_ids", ()) or tid in state.get(
                "_pending_bg_task_ids", ()):
            return True
    return False


def mark_bg_notify_ids(state, task_id="", tool_use_id=""):
    # type: (dict, str, str) -> None
    if tool_use_id:
        state.setdefault("_bg_notified_tool_ids", set()).add(tool_use_id)
        state.setdefault("_pending_bg_tool_ids", set()).discard(tool_use_id)
    mapping = state.get("_task_tool_map") or {}
    for tid in alias_bg_task_ids(mapping, tool_use_id, task_id):
        state.setdefault("_bg_notified_task_ids", set()).add(tid)
        state.setdefault("_pending_bg_task_ids", set()).discard(tid)


class BackgroundTaskGate:
    """Owns ⚙ registry, poll epochs, notify buffer, and flush."""

    def __init__(
        self,
        turn,  # type: TurnController
        scheduler,  # type: Scheduler
        output,  # type: OutputPort
        backend="claude",  # type: str
        send_poll=None,  # type: Optional[Callable[[Callable], bool]]
        on_query=None,  # type: Optional[Callable[[str, str], None]]
        on_surface=None,  # type: Optional[Callable[[], None]]
        on_compact_done=None,  # type: Optional[Callable[[], None]]
        read_output_file=None,  # type: Optional[Callable[[str], str]]
    ):
        self.turn = turn
        self.scheduler = scheduler
        self.output = output
        self.backend = backend
        self.send_poll = send_poll
        self.on_query = on_query
        self.on_surface = on_surface
        self.on_compact_done = on_compact_done
        self.read_output_file = read_output_file or _read_file

        self.task_tool_map = {}  # type: dict
        self.bg_tools = {}  # type: dict
        self.bg_task_ids = set()  # type: Set[str]
        self.seen_running = set()  # type: Set[str]
        self.pending_notifications = []  # type: list
        self.pending_task_ids = set()  # type: Set[str]
        self.pending_tool_ids = set()  # type: Set[str]
        self.notified_task_ids = set()  # type: Set[str]
        self.notified_tool_ids = set()  # type: Set[str]
        self.flush_scheduled = False
        self.pending_hold_gen = None  # type: Optional[int]
        self.poll_epoch = 0
        self._poll_armed = False

    def _state_dict(self):
        # type: () -> dict
        return {
            "_task_tool_map": self.task_tool_map,
            "_bg_notified_task_ids": self.notified_task_ids,
            "_bg_notified_tool_ids": self.notified_tool_ids,
            "_pending_bg_task_ids": self.pending_task_ids,
            "_pending_bg_tool_ids": self.pending_tool_ids,
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

    def drop_tool(self, tool_id):
        # type: (str) -> None
        if not tool_id:
            return
        self.bg_task_ids.discard(tool_id)
        self.bg_tools.pop(tool_id, None)

    def on_task_started(self, data):
        # type: (dict) -> None
        task_id = data.get("task_id") or ""
        tool_use_id = data.get("tool_use_id") or ""
        if task_id and tool_use_id:
            self.task_tool_map[task_id] = tool_use_id
            self.schedule_poll()

    def on_task_updated(self, data):
        # type: (dict) -> None
        task_id = data.get("task_id") or ""
        status = (data.get("patch") or {}).get("status", "")
        if not task_id or status not in _TASK_TERMINAL:
            return
        tool_use_id = self.task_tool_map.get(task_id)
        if not tool_use_id:
            return
        self.finalize_tool(tool_use_id, keep=(status == "completed"))

    def on_task_notification(self, data, working=False):
        # type: (dict, bool) -> None
        task_id = data.get("task_id") or ""
        status = data.get("status") or ""
        tool_use_id = (
            data.get("tool_use_id")
            or self.task_tool_map.get(task_id)
            or ("bg-%s" % task_id if task_id else "")
        )
        if task_id:
            self.task_tool_map.pop(task_id, None)
        if not status:
            return
        if self.already(task_id, tool_use_id):
            if tool_use_id:
                self.finalize_tool(tool_use_id, keep=False)
                self.drop_tool(tool_use_id)
            return
        if tool_use_id:
            self.bg_task_ids.discard(tool_use_id)

        output = ""
        output_file = data.get("output_file") or ""
        if output_file:
            try:
                output = (self.read_output_file(output_file) or "").strip()
            except Exception:
                output = ""
        max_out = 8000
        if len(output) > max_out:
            output = (
                output[:max_out]
                + "\n…[truncated %s chars; full: %s]" % (
                    len(output) - max_out, output_file)
            )
        if tool_use_id:
            self.finalize_tool(
                tool_use_id, keep=(status == "completed"))
            self.bg_tools.pop(tool_use_id, None)

        # Always buffer. "notified" means shown/queried, not seen on the
        # wire — mark() happens in flush after surface/query, not here.
        # `working` is unused for drop: hold at flush-time keeps the buffer.
        _ = working
        summary = (data.get("summary") or task_id or "background task").strip()
        if is_child_session_id(summary):
            summary = "subagent"
        if "\n" in summary:
            summary = " ⏎ ".join(
                s.strip() for s in summary.splitlines() if s.strip())
        if len(summary) > 100:
            summary = summary[:99] + "…"
        header = "%s [%s]" % (summary, status) if status != "completed" else summary
        if not output:
            tip = "task_id=%s" % task_id if task_id else "background task"
            if is_child_session_id(task_id) or (
                    isinstance(task_id, str) and task_id.startswith("acp-child-")):
                tip = "background task"
            if output_file:
                tip += "\nlog: %s" % output_file
            block = "<task-notification>%s\n%s</task-notification>" % (header, tip)
        else:
            block = "<task-notification>%s\n%s</task-notification>" % (header, output)
        if task_id:
            self.pending_task_ids.add(task_id)
        if tool_use_id:
            self.pending_tool_ids.add(tool_use_id)
        self.pending_notifications.append(block)
        if not self.flush_scheduled:
            self.flush_scheduled = True
            self.scheduler.call_later(FLUSH_DEBOUNCE_MS, self.flush)

    def note_task_poll_delivery(self, tool_name, content):
        # type: (str, str) -> None
        """Mark bash-* tasks the agent already saw via TaskGet/TaskOutput."""
        if not content or tool_name not in _POLL_TOOLS:
            return
        text = content if isinstance(content, str) else str(content)
        low = text.lower()
        terminal = (
            "status: completed" in low
            or "status: failed" in low
            or "status: cancelled" in low
            or "status: canceled" in low
            or "retrieval_status: ready" in low
            or "exitcode:" in low.replace(" ", "")
            or "exit_code" in low
        )
        if not terminal and "status: running" in low:
            return
        for m in _BASH_ID_RE.finditer(text):
            tid = m.group(1)
            tuid = self.task_tool_map.get(tid) or ""
            self.mark(tid, tuid)
        if self.pending_notifications:
            kept = []
            for block in self.pending_notifications:
                drop = False
                for m in _BASH_ID_RE.finditer(block):
                    if m.group(1) in self.notified_task_ids:
                        drop = True
                        break
                if not drop:
                    kept.append(block)
            self.pending_notifications = kept

    def flush(self):
        # type: () -> None
        """Apply TurnState.notify_action — never fake-adopt a turn."""
        self.flush_scheduled = False
        if not self.pending_notifications:
            return
        action = self.turn.notify_action(self.backend)
        if action == "hold":
            # Keep the generation-stamped buffer; retry on end_live.
            self.pending_hold_gen = getattr(self.turn, "gen", None)
            return
        blocks = self.pending_notifications
        pending_tasks = set(self.pending_task_ids)
        pending_tools = set(self.pending_tool_ids)
        self.pending_notifications = []
        self.pending_task_ids.clear()
        self.pending_tool_ids.clear()
        self.pending_hold_gen = None
        seen = set()
        uniq = []
        for b in blocks:
            if b in seen:
                continue
            seen.add(b)
            uniq.append(b)
        if not uniq:
            return
        # Surface/query is about to happen — now the ids are "notified".
        for tid in pending_tasks:
            self.mark(tid, "")
        for tuid in pending_tools:
            self.mark("", tuid)
        joined = "\n".join(uniq)
        from .turn import looks_like_compact_done
        if looks_like_compact_done(joined) or "compaction" in joined.lower():
            if self.on_compact_done is not None:
                self.on_compact_done()
            return
        if action == "surface":
            if self.on_surface is not None:
                self.on_surface()
            else:
                try:
                    self.output.refresh_background_hints()
                except Exception:
                    pass
            return
        if action == "query" and self.on_query is not None:
            n = len(uniq)
            display = "⚙ %s task notification%s" % (n, "s" if n != 1 else "")
            self.on_query(joined, display)

    def finalize_tool(self, tool_use_id, keep):
        # type: (str, bool) -> None
        """Idempotent ⚙ → ✓ (keep) or drop. Visual only."""
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
        if keep:
            try:
                self.output.tool_done(getattr(tool, "name", "tool"), "", tool_use_id)
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
        if not self.task_tool_map and not self.bg_task_ids:
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
        if not self.task_tool_map and not self.bg_task_ids:
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
            if self.task_tool_map or self.bg_task_ids:
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
        if not (self.task_tool_map or self.bg_task_ids):
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
        for task_id, tool_use_id in list(self.task_tool_map.items()):
            if not (
                tool_use_id in self.bg_task_ids
                and task_id in self.seen_running
                and task_id not in live
            ):
                continue
            if self.already(task_id, tool_use_id):
                self.finalize_tool(tool_use_id, keep=False)
                self.drop_tool(tool_use_id)
            else:
                self.on_task_notification({
                    "task_id": task_id,
                    "tool_use_id": tool_use_id,
                    "status": "completed",
                    "summary": "%s (completed)" % task_id,
                    "output_file": "",
                }, working=self.turn.busy)
            self.task_tool_map.pop(task_id, None)
            self.seen_running.discard(task_id)

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
        self.pending_notifications = []
        self.pending_task_ids.clear()
        self.pending_tool_ids.clear()
        self.notified_task_ids.clear()
        self.notified_tool_ids.clear()
        self.pending_hold_gen = None
        self.flush_scheduled = False
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
