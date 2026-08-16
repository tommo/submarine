"""ACP background-tool registry, pairing, and notify dedupe.

Invariants: Kimi titles `Running: cmd` are foreground; only
run_in_background / detached / Starting background / timeout 0
are ⚙ (§9.30). TaskOutput is never ⚙. Notify once per logical job.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import List, Optional

_BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_DIR)

from rpc_helpers import send_notification, send_result  # noqa: E402


class BackgroundMixin:
    async def handle_poll_bg_tasks(self, req_id: Optional[int],
                                    params: dict) -> None:
        """Host idle poll while ⚙ tasks are live. Kimi mixin scans bash-*.json."""
        checked = 0
        poll = getattr(self, "_poll_kimi_bg_tasks", None)
        if callable(poll):
            try:
                checked = poll()
            except Exception as e:
                self.file_log(f"poll_bg_tasks: {e}")
        running = list(self._live_bg_task_ids())
        send_result(req_id, {
            "pending": len(running),
            "checked": checked,
            "running": running,
        })

    def _live_bg_task_ids(self) -> set:
        ids = set()
        for info in (self._terminal_bg or {}).values():
            tid = info.get("task_id")
            if tid:
                ids.add(tid)
        extra = getattr(self, "_live_kimi_task_ids", None)
        if callable(extra):
            ids.update(extra())
        return ids

    def _kimi_handle_tool_result(
            self, tid, tool_name, text, enriched, is_task_poll, status, upd) -> bool:
        return False

    @staticmethod
    def _clip_bg_summary(summary: str, code=None) -> str:
        summary = (summary or "").strip()
        if "\n" in summary:
            summary = " ⏎ ".join(
                s.strip() for s in summary.splitlines() if s.strip())
        if len(summary) > 80:
            summary = summary[:79] + "…"
        if code is not None:
            summary = f"{summary} (exit {code})"
        return summary

    def _write_bg_output_file(self, prefix: str, body: str) -> str:
        try:
            fd, path = tempfile.mkstemp(prefix=prefix, suffix=".log", text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body or "")
            return path
        except Exception as e:
            self.file_log(f"bg output_file write failed: {e}")
            return ""

    def _emit_bg_finished(
            self, task_id: str, tool_use_id: str, status: str,
            summary: str, output_file: str) -> None:
        self._mark_bg_notified(task_id, tool_use_id)
        self._emit_system("task_updated", {
            "task_id": task_id,
            "tool_use_id": tool_use_id,
            "patch": {"status": status},
        })
        self._emit_system("task_notification", {
            "task_id": task_id,
            "tool_use_id": tool_use_id,
            "status": status,
            "summary": summary,
            "output_file": output_file,
        })
        if tool_use_id:
            self._bg_tool_ids.discard(tool_use_id)
            self._tool_inputs_by_id.pop(tool_use_id, None)
            self._tool_names_by_id.pop(tool_use_id, None)

    _SHELL_BG_NAMES = frozenset({
        "Bash", "Shell", "execute", "run_terminal_command", "tool",
    })

    @classmethod
    def _is_shell_tool_name(cls, name: str) -> bool:
        return (name or "") in cls._SHELL_BG_NAMES or (name or "") == "Workflow"

    @staticmethod
    def _looks_like_background_tool(upd: dict, tool_input: Optional[dict] = None) -> bool:
        """True only for explicit detach — not every Kimi `Running:` shell.

        Kimi titles *all* execute `Running: <cmd>`. That is foreground +
        wait_for_exit. Only `run_in_background` / `detached` / Starting
        background is ⚙. TaskOutput is never ⚙.
        """
        title = str(upd.get("title") or "")
        low = title.lower().strip()
        # Poll tools are foreground — never ⚙
        if "reading output of task" in low or low.startswith("taskoutput"):
            return False
        if low.startswith("starting background"):
            return True
        if low.startswith("running in background") or low.startswith("background task"):
            return True
        ri = tool_input if isinstance(tool_input, dict) else {}
        if not ri:
            raw = upd.get("rawInput")
            if isinstance(raw, dict):
                ri = raw
            elif isinstance(raw, str) and raw.strip().startswith("{"):
                try:
                    ri = json.loads(raw)
                except Exception:
                    ri = {}
        # task_id alone = poll args, not a detached shell
        if isinstance(ri, dict) and (
                ri.get("task_id") or ri.get("taskId")) and not ri.get("command"):
            return False
        if isinstance(ri, dict) and (
                ri.get("run_in_background") is True
                or ri.get("detached") is True
                or ri.get("background") is True):
            return True
        # Grok: timeout 0 on run_terminal_command = no-timeout / bg
        # (same session: background:true detaches; timeout:0 still
        # wait_for_exit — still ⚙ so the row is not a blocking ☐).
        if isinstance(ri, dict) and ri.get("timeout") in (0, 0.0):
            if "run_terminal_command" in title or title.startswith("Execute "):
                return True
        return False

    def _note_shell_execute(self, tid: Optional[str], tool_name: str) -> None:
        """Remember this shell tool so the next terminal/create can pair to it."""
        if not tid or not self._is_shell_tool_name(tool_name):
            return
        self._last_execute_id = tid
        pending = getattr(self, "_pending_execute_ids", None)
        if pending is None:
            self._pending_execute_ids = []
            pending = self._pending_execute_ids
        if tid not in pending:
            pending.append(tid)

    def _drop_pending_execute(self, tid: Optional[str]) -> None:
        if not tid:
            return
        pending = getattr(self, "_pending_execute_ids", None) or []
        self._pending_execute_ids = [x for x in pending if x != tid]
        if getattr(self, "_last_execute_id", None) == tid:
            self._last_execute_id = None

    def _take_pending_execute_id(self) -> Optional[str]:
        pending = getattr(self, "_pending_execute_ids", None) or []
        if pending:
            eid = pending.pop(0)
            self._pending_execute_ids = pending
            if getattr(self, "_last_execute_id", None) == eid:
                self._last_execute_id = None
            return eid
        eid = getattr(self, "_last_execute_id", None)
        self._last_execute_id = None
        return eid

    @staticmethod
    def _script_from_terminal_params(cmd, args_in) -> str:
        """Real command for display/match. Kimi: /bin/bash + args=['-c', script]."""
        args = list(args_in or [])
        for i, a in enumerate(args):
            if str(a) in ("-c",) and i + 1 < len(args):
                return str(args[i + 1])
        return str(cmd or "")

    @staticmethod
    def _terminal_ids_from_update(upd: dict) -> List[str]:
        out = []
        for block in (upd.get("content") or []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "terminal" and block.get("terminalId"):
                out.append(str(block["terminalId"]))
            inner = block.get("content") if block.get("type") == "content" else None
            if isinstance(inner, dict) and inner.get("type") == "terminal":
                tid = inner.get("terminalId")
                if tid:
                    out.append(str(tid))
        return out

    def _emit_system(self, subtype: str, data: dict) -> None:
        """Same envelope as Claude SDK bridge SystemMessage → host dispatch."""
        send_notification("message", {
            "type": "system",
            "subtype": subtype,
            "data": data or {},
        })

    def _should_skip_bg_notify(self, task_id: str, tool_use_id: str = "") -> bool:
        """True if we already sent task_notification for this logical bg job."""
        if task_id and task_id in self._bg_notified_tasks:
            return True
        if tool_use_id and tool_use_id in self._bg_notified_tools:
            return True
        return False

    def _mark_bg_notified(self, task_id: str, tool_use_id: str = "") -> None:
        if task_id:
            self._bg_notified_tasks.add(task_id)
        if tool_use_id:
            self._bg_notified_tools.add(tool_use_id)
        # Bound aliases for the same terminal/tool
        for term_id, info in list(self._terminal_bg.items()):
            if (task_id and info.get("task_id") == task_id) or (
                    tool_use_id and info.get("tool_use_id") == tool_use_id):
                tid = info.get("task_id")
                if tid:
                    self._bg_notified_tasks.add(str(tid))
                tuid = info.get("tool_use_id")
                if tuid:
                    self._bg_notified_tools.add(str(tuid))

    def _register_bg_tool(self, tool_use_id: str, tool_input: dict = None,
                          title: str = "") -> None:
        if not tool_use_id:
            return
        # Title-only poll tools must never become ⚙
        tlow = (title or "").lower()
        if "reading output of task" in tlow or tlow.startswith("taskoutput"):
            return
        name = self._tool_names_by_id.get(tool_use_id) or "Bash"
        # Never promote Read / TaskOutput / etc. to ⚙ background
        if not self._is_shell_tool_name(name):
            return
        if name == "tool":
            name = "Bash"
        already = tool_use_id in self._bg_tool_ids
        self._bg_tool_ids.add(tool_use_id)
        self._last_bg_tool_id = tool_use_id
        inp = dict(tool_input or self._tool_inputs_by_id.get(tool_use_id) or {})
        if title and not inp.get("command"):
            cmd = title
            for prefix in (
                "Starting background:", "Starting background",
                "Running:",
            ):
                if cmd.startswith(prefix):
                    cmd = cmd[len(prefix):].strip()
                    break
            if cmd:
                inp.setdefault("command", cmd)
        inp["run_in_background"] = True
        self._tool_inputs_by_id[tool_use_id] = {
            **(self._tool_inputs_by_id.get(tool_use_id) or {}),
            **inp,
        }
        self._tool_names_by_id[tool_use_id] = "Bash"
        self._tool_ids_emitted.add(tool_use_id)
        if already:
            return
        send_notification("message", {
            "type": "tool_use",
            "id": tool_use_id,
            "name": "Bash",
            "input": self._tool_inputs_by_id[tool_use_id],
            "background": True,
        })

    def _bind_terminal_to_bg_tool(self, terminal_id: str, tool_use_id: str) -> None:
        if not terminal_id or not tool_use_id:
            return
        if tool_use_id not in self._bg_tool_ids:
            return
        if terminal_id in self._terminal_bg:
            return
        cmd = (self._tool_inputs_by_id.get(tool_use_id) or {}).get("command", "")
        task_id = f"acp-term-{terminal_id}"
        self._terminal_bg[terminal_id] = {
            "task_id": task_id,
            "tool_use_id": tool_use_id,
            "cmd": cmd,
        }
        self._emit_system("task_started", {
            "task_id": task_id,
            "tool_use_id": tool_use_id,
        })
        self.file_log(
            f"bg terminal bound term={terminal_id} tool={tool_use_id} "
            f"task={task_id}")
        slot = self._terminals.get(terminal_id) or {}
        reader = slot.get("reader")
        if slot.get("exit_status") is not None and (
                reader is None or reader.done()):
            self._emit_bg_terminal_complete(terminal_id)

    def _emit_bg_terminal_complete(self, terminal_id: str) -> None:
        """ACP process exit → one Claude task_notification."""
        info = self._terminal_bg.pop(terminal_id, None)
        if not info:
            return
        task_id = info.get("task_id") or f"acp-term-{terminal_id}"
        tool_use_id = info.get("tool_use_id") or f"bg-{task_id}"
        if self._should_skip_bg_notify(task_id, tool_use_id):
            return
        slot = self._terminals.get(terminal_id) or {}
        out = (slot.get("stdout") or "") + (slot.get("stderr") or "")
        es = slot.get("exit_status") or {}
        code = es.get("exitCode")
        if code is None and es.get("signal"):
            status = "failed"
        elif code is None or int(code) == 0:
            status = "completed"
        else:
            status = "failed"
        output_file = self._write_bg_output_file("acp-bg-", out or "")
        raw = (info.get("cmd") or tool_use_id or task_id or "").strip()
        summary = self._clip_bg_summary(raw, code)
        self._emit_bg_finished(
            task_id, tool_use_id, status, summary, output_file)
