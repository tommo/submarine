"""Comprehensive bg/subagent safety net (audit matrix A–G).

Drives BackgroundTaskGate + BridgeEventRouter + ACP mixins through
fakes. No wall-clock sleeps — FakeScheduler for host timers, Event
cancellation / wait_for TimeoutError injection for child waiters.

Existing source-pin tests this file supersedes with real stack drives
are noted on the replacement test (see class docstrings).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest

from core.background import (
    SHELL_BG,
    SUBAGENT_BG,
    BackgroundTaskGate,
    is_child_session_id,
    is_shell_background_tool,
)
from core.turn import TurnController
from tests.fakes import FakeClient, FakeOutput, FakeScheduler, make_session

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from acp_base import AcpBridge  # noqa: E402
from kimi_bg import KimiBgMixin  # noqa: E402

# Typical grok child session id (4 hyphens, len 36).
SID_A = "01a00aaa-bbbb-cccc-dddd-111111111111"
SID_B = "01a00bbb-cccc-dddd-eeee-222222222222"
SID_ULID = "01a00fc4-b4e1-7692-bb0e-e806eba02589"

_NOTIFY_MODULES = (
    "acp.transport",
    "acp.updates",
    "acp.background",
    "acp.terminal",
    "kimi_bg",
    "rpc_helpers",
)


def _patch_all_notify(fn):
    """Patch every mixin copy of send_notification (import-bound)."""
    import importlib
    orig = []
    for name in _NOTIFY_MODULES:
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        if hasattr(mod, "send_notification"):
            orig.append((mod, mod.send_notification))
            mod.send_notification = fn
    return orig


def _restore_notify(orig):
    for mod, fn in orig:
        mod.send_notification = fn


class LiveBridge(AcpBridge):
    """AcpBridge mixins, no process. REAL emit path (not _FwdStub).

    Unlike tests.test_bg_tool_gates._FwdStub this does not stub
    _emit_bg_finished / _emit_system, so patched send_notification
    reaches a Session.events router.
    """

    def __init__(self):
        self.session_id = "session_test"
        self._loading_session = False
        self._foreign_session_drops = 0
        self._calls = {}
        self._tool_id_alias = {}
        self._pending_execute_ids = []
        self._last_execute_id = None
        self._terminals = {}
        self._terminal_bg = {}
        self._released_terminals = set()
        self._detached_snaps = {}
        self._detached_procs = {}
        self._detached_slots = {}
        self._child_sessions = {}
        self._bg_notified_tasks = set()
        self._bg_notified_tools = set()
        self._tool_titles_by_id = {}
        self._last_bg_tool_id = None
        self.terminal_wait_timeout_s = 0
        self._prompt_fut = None
        self._prompt_cancelled = False
        self._cancel_in_flight = False
        self._leftover_end_pending = False
        self._resumed = False
        self._resume_fallback = False
        self._in_plan_mode = False
        self._client_schedule_tasks = {}
        self.TOOL_TO_CANONICAL = {
            "spawn_subagent": "Subagent",
            "get_command_or_subagent_output": "TaskGet",
            "Agent": "Task",
            "run_terminal_command": "Bash",
            "Task": "Task",
            "TaskGet": "TaskGet",
            "TaskOutput": "TaskGet",
        }

    def file_log(self, msg):
        pass

    def _cancel_client_schedule(self, key):
        self._client_schedule_tasks.pop(key, None)

    def _handle_mode_update(self, upd):
        pass

    def _handle_commands_update(self, upd):
        pass


class KimiLiveBridge(KimiBgMixin, LiveBridge):
    """Kimi disk-pairing mixin on LiveBridge. No asyncio poller."""

    def __init__(self, tasks_dir=None):
        self._tasks_dir = tasks_dir
        KimiBgMixin.__init__(self)

    def kimi_tasks_dir(self):
        return self._tasks_dir

    def _ensure_bg_poller(self):
        return

    def _schedule_kimi_relink(self, *a, **k):
        return

    def _schedule_kimi_detached_probe(self, *a, **k):
        return


class ConnectedStack:
    """Bridge mixin → send_notification → Session.events (full stack)."""

    def __init__(self, backend="grok", kimi=False, tasks_dir=None):
        self.notes = []
        self.client = FakeClient()
        self.session = make_session(
            initialized=True, client=self.client, backend=backend)
        if kimi:
            self.bridge = KimiLiveBridge(tasks_dir=tasks_dir)
        else:
            self.bridge = LiveBridge()
        self._orig = None

    def __enter__(self):
        self._orig = _patch_all_notify(self._on_note)
        return self

    def __exit__(self, *exc):
        _restore_notify(self._orig)
        return False

    def _on_note(self, method, params):
        self.notes.append((method, params))
        self.session.events.dispatch(method, params or {})


def _complete_last_query(session):
    cbs = [t[2] for t in session.client.sent if t[0] == "query"]
    if not cbs:
        raise AssertionError("no query RPC to complete")
    cbs[-1]({"status": "complete"})


def _gate(backend="claude", busy=False):
    turn = TurnController()
    if busy:
        turn.begin_query()
    sched = FakeScheduler()
    out = FakeOutput()
    queries = []  # nothing ever appends: completions never start a turn
    surfaces = []

    def on_surface():
        surfaces.append(True)

    g = BackgroundTaskGate(
        turn, sched, out, backend=backend, on_surface=on_surface)
    return g, turn, sched, queries, surfaces


def _spawn_update(tid, title="spawn_subagent", prompt="say hi", desc="check"):
    return {
        "sessionId": "session_test",
        "update": {
            "sessionUpdate": "tool_call",
            "toolCallId": tid,
            "title": title,
            "kind": "other",
            "status": "pending",
            "rawInput": {"prompt": prompt, "description": desc},
        },
    }


def _spawn_ack(tid, sid):
    return {
        "sessionId": "session_test",
        "update": {
            "sessionUpdate": "tool_call_update",
            "toolCallId": tid,
            "status": "completed",
            "content": [{"type": "content", "content": {
                "type": "text",
                "text": "Subagent started.\nsubagent_id: %s\n" % sid,
            }}],
        },
    }


def _child_chunk(sid, text):
    return {
        "sessionId": sid,
        "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text},
        },
    }


def _child_done(sid, stop="end_turn"):
    return {
        "sessionId": sid,
        "update": {
            "sessionUpdate": "turn_completed",
            "stop_reason": stop,
        },
    }


def _write_kimi_task(tdir, tid, command, status="running", started=1,
                     detached=True):
    path = os.path.join(tdir, "%s.json" % tid)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "taskId": tid,
            "status": status,
            "command": command,
            "startedAt": started,
            "detached": detached,
            "description": command,
        }, f)
    return path


# ── A. Spawn → ⚙ row → completion ─────────────────────────────────────


class TestA1ShellBgSpawnToDone(unittest.TestCase):
    def test_shell_bg_row_opens_gear_terminal_exit_closes_check(self):
        with ConnectedStack(backend="claude") as st:
            s, b = st.session, st.bridge
            s.query("run sleep")
            b._note_shell_execute("tool-bash", "Bash")
            b._ensure_call("tool-bash").merge_input({
                "command": "sleep 9", "run_in_background": True})
            slot = {
                "cmd": "sleep 9",
                "stdout": "slept\n",
                "stderr": "",
                "exit_status": None,
                "reader": None,
            }
            b._terminals["term_x"] = slot
            b._mark_terminal_bg("term_x", slot)
            tool = s.output.find_tool_by_id("tool-bash")
            self.assertIsNotNone(tool)
            self.assertEqual(tool.status, "background")
            self.assertIn("tool-bash", s.bg.bg_tools)
            self.assertIn("acp-term-term_x", s.bg.task_tool_map)
            slot["exit_status"] = {"exitCode": 0, "signal": None}
            b._emit_bg_terminal_complete("term_x")
            tool = s.output.find_tool_by_id("tool-bash")
            self.assertIsNotNone(tool)
            self.assertEqual(tool.status, "done")
            self.assertIn("slept", str(getattr(tool, "result", "") or ""))
            self.assertIn("tool-bash", s.bg.notified_tool_ids)
            _complete_last_query(s)
            # The exit is the row's business only: no host turn about it.
            prompts = [
                (t[1] or {}).get("prompt") or ""
                for t in s.client.sent if t[0] == "query"]
            self.assertEqual(len(prompts), 1)
            self.assertFalse(any("task-notification" in p for p in prompts))


class TestA1bDetachKeepsStdout(unittest.TestCase):
    def test_emit_after_terminal_popped_uses_detached_slot(self):
        from tests.test_bg_tool_gates import _FwdStub as Stub
        b = Stub()
        b._terminal_bg["term_z"] = {
            "task_id": "acp-term-term_z",
            "tool_use_id": "tool-x",
            "cmd": "sleep 1 && echo HI",
        }
        b._terminals = {}
        b._detached_slots["term_z"] = {
            "stdout": "HI\n",
            "stderr": "",
            "exit_status": {"exitCode": 0, "signal": None},
        }
        bodies = []
        b._write_bg_output_file = lambda prefix, body: (
            bodies.append(body) or "/tmp/bg-out.log")
        b._emit_bg_terminal_complete("term_z")
        self.assertTrue(bodies)
        self.assertIn("HI", bodies[0])
        self.assertEqual(b._finished[-1][0], "acp-term-term_z")
        self.assertEqual(b._finished[-1][2], "completed")
        self.assertNotIn("term_z", b._detached_slots)

    def test_detach_does_not_invent_success_exit(self):
        from tests.test_bg_tool_gates import _FwdStub as Stub

        async def go():
            b = Stub()
            slot = {
                "proc": None, "stdout": "partial", "stderr": "",
                "exit_status": None, "reader": None, "cmd": "sleep 9",
            }
            b._terminals["t1"] = slot
            await b._detach_terminal("t1")
            self.assertTrue(slot.get("detached"))
            self.assertIsNone(slot.get("exit_status"))
            self.assertNotIn("t1", b._terminals)
            self.assertIs(b._detached_slots.get("t1"), slot)

        asyncio.run(go())

    def test_completed_empty_output_keeps_checkmark(self):
        s = make_session(initialized=True, client=FakeClient(), backend="grok")
        s.events.tool_use({
            "name": "Bash", "id": "tool-empty", "background": True,
            "input": {"command": "true", "run_in_background": True},
        })
        self.assertEqual(
            s.output.find_tool_by_id("tool-empty").status, "background")
        s.bg.on_task_notification({
            "task_id": "acp-term-t",
            "tool_use_id": "tool-empty",
            "status": "completed",
            "summary": "true",
            "output_file": "",
        })
        tool = s.output.find_tool_by_id("tool-empty")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.status, "done")


class TestA2GrokSpawnSubagent(unittest.TestCase):
    def test_spawn_completed_update_ack_does_not_close_gear_row(self):
        # Spawn ack (tool_call_update completed) must not demote the ⚙ row:
        # the re-painted tool_use keeps background=True, the ack tool_result
        # is bind-only. ⚙ stays until child turn_completed / task_notification.
        with ConnectedStack(backend="grok") as st:
            s, b = st.session, st.bridge
            s.query("spawn a checker")
            b._forward_update(_spawn_update("spawn-1"))
            self.assertEqual(
                s.output.find_tool_by_id("spawn-1").status, "background")
            b._forward_update(_spawn_ack("spawn-1", SID_A))
            self.assertEqual(
                s.output.find_tool_by_id("spawn-1").status, "background")

    def test_grok_spawn_taskget_hidden_child_buffered_complete_closes(self):
        with ConnectedStack(backend="grok") as st:
            s, b = st.session, st.bridge
            s.query("spawn a checker")
            b._forward_update(_spawn_update("spawn-1"))
            tool = s.output.find_tool_by_id("spawn-1")
            self.assertIsNotNone(tool)
            self.assertEqual(tool.status, "background")
            self.assertEqual(tool.name, "Subagent")

            # Bind without the completed-update re-paint (see expectedFailure).
            b._bind_child_to_tool("subagent_id: %s\n" % SID_A, "spawn-1")
            s.events.tool_result({
                "tool_use_id": "spawn-1",
                "content": "background",
                "is_error": False,
            })
            tool = s.output.find_tool_by_id("spawn-1")
            self.assertEqual(tool.status, "background")
            self.assertIn(SID_A, b._child_sessions)
            self.assertEqual(
                b._child_sessions[SID_A].get("tool_use_id"), "spawn-1")

            n_before = len(st.notes)
            b._forward_update({
                "sessionId": "session_test",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "poll-1",
                    "title": "Get task output: %s" % SID_A,
                    "kind": "other",
                    "status": "pending",
                    "rawInput": {"task_ids": [SID_A], "timeout_ms": 30000},
                },
            })
            uses = [
                p for _, p in st.notes[n_before:]
                if isinstance(p, dict) and p.get("type") == "tool_use"]
            self.assertEqual(uses, [])

            texts_before = list(s.output.texts)
            b._forward_update(_child_chunk(SID_A, "child says hi"))
            self.assertEqual(s.output.texts, texts_before)
            self.assertIn("child says hi", b._child_sessions[SID_A]["text"])

            b._forward_update(_child_done(SID_A))
            _complete_last_query(s)
            tool = s.output.find_tool_by_id("spawn-1")
            status = getattr(tool, "status", None) if tool is not None else None
            self.assertNotEqual(status, "background")
            self.assertFalse(s.working)
            self.assertIn(True, s.chrome.unread)


class TestA3KimiDetachedDiskPairing(unittest.TestCase):
    def test_kimi_native_then_terminal_complete_notifies_once(self):
        with tempfile.TemporaryDirectory() as tdir:
            _write_kimi_task(tdir, "bash-xyz", "sleep 9")
            with ConnectedStack(backend="kimi", kimi=True, tasks_dir=tdir) as st:
                s, b = st.session, st.bridge
                s.query("detach sleep")
                b._register_kimi_native_bg(
                    "tool-k", "bash-xyz",
                    description="sleep 9", command="sleep 9")
                tool = s.output.find_tool_by_id("tool-k")
                self.assertIsNotNone(tool)
                self.assertEqual(tool.status, "background")
                b._terminals["term_k"] = {
                    "cmd": "sleep 9",
                    "stdout": "done\n",
                    "stderr": "",
                    "exit_status": {"exitCode": 0, "signal": None},
                }
                b._link_terminal_to_kimi_task(
                    "term_k", "tool-k",
                    {"taskId": "bash-xyz", "command": "sleep 9",
                     "description": "sleep 9"})
                notifies = [
                    p for _, p in st.notes
                    if isinstance(p, dict) and p.get("type") == "system"
                    and p.get("subtype") == "task_notification"]
                self.assertEqual(notifies, [])
                b._emit_kimi_native_bg_complete(
                    "bash-xyz", {"status": "completed", "description": "sleep 9"})
                b._emit_bg_terminal_complete("term_k")
                notifies = [
                    p for _, p in st.notes
                    if isinstance(p, dict) and p.get("type") == "system"
                    and p.get("subtype") == "task_notification"]
                self.assertEqual(len(notifies), 1, notifies)
                _complete_last_query(s)
                self.assertIn("tool-k", s.bg.notified_tool_ids)

    def test_kimi_terminal_then_native_complete_notifies_once(self):
        with tempfile.TemporaryDirectory() as tdir:
            _write_kimi_task(tdir, "bash-xyz", "sleep 9")
            with ConnectedStack(backend="kimi", kimi=True, tasks_dir=tdir) as st:
                s, b = st.session, st.bridge
                s.query("detach sleep")
                b._register_kimi_native_bg(
                    "tool-k", "bash-xyz",
                    description="sleep 9", command="sleep 9")
                b._terminals["term_k"] = {
                    "cmd": "sleep 9",
                    "stdout": "done\n",
                    "stderr": "",
                    "exit_status": {"exitCode": 0, "signal": None},
                }
                b._link_terminal_to_kimi_task(
                    "term_k", "tool-k",
                    {"taskId": "bash-xyz", "command": "sleep 9"})
                b._emit_bg_terminal_complete("term_k")
                b._emit_kimi_native_bg_complete(
                    "bash-xyz", {"status": "completed"})
                notifies = [
                    p for _, p in st.notes
                    if isinstance(p, dict) and p.get("type") == "system"
                    and p.get("subtype") == "task_notification"]
                self.assertEqual(len(notifies), 1, notifies)
                _complete_last_query(s)
                self.assertIn("tool-k", s.bg.notified_tool_ids)


class TestA4KimiForegroundTaskNotGear(unittest.TestCase):
    def test_kimi_foreground_task_agent_is_not_gear(self):
        """F4: Task/Agent without detach is a blocking row, not ⚙."""
        with ConnectedStack(backend="kimi") as st:
            s, b = st.session, st.bridge
            s.query("use the agent")
            b._forward_update({
                "sessionId": "session_test",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "task-fg",
                    "title": "Task",
                    "kind": "other",
                    "status": "pending",
                    "rawInput": {"prompt": "do the work"},
                },
            })
            tool = s.output.find_tool_by_id("task-fg")
            self.assertIsNotNone(tool)
            self.assertNotEqual(tool.status, "background")
            self.assertNotIn("task-fg", s.bg.bg_tools)
            self.assertFalse(bool(b._call("task-fg") and b._call("task-fg").background))
            b._forward_update({
                "sessionId": "session_test",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "task-fg",
                    "status": "completed",
                    "content": [{"type": "content", "content": {
                        "type": "text", "text": "all done",
                    }}],
                },
            })
            tool = s.output.find_tool_by_id("task-fg")
            self.assertEqual(tool.status, "done")


class TestA5TwoInterleavedSubagents(unittest.TestCase):
    def test_two_interleaved_subagents_independent_buffers_and_rows(self):
        with ConnectedStack(backend="grok") as st:
            s, b = st.session, st.bridge
            s.query("two checkers")
            b._forward_update(_spawn_update("spawn-1", desc="alpha", prompt="a"))
            b._bind_child_to_tool("subagent_id: %s\n" % SID_A, "spawn-1")
            b._forward_update(_spawn_update("spawn-2", desc="beta", prompt="b"))
            b._bind_child_to_tool("subagent_id: %s\n" % SID_B, "spawn-2")
            t1 = s.output.find_tool_by_id("spawn-1")
            t2 = s.output.find_tool_by_id("spawn-2")
            self.assertEqual(t1.status, "background")
            self.assertEqual(t2.status, "background")
            self.assertEqual(b._child_sessions[SID_A]["tool_use_id"], "spawn-1")
            self.assertEqual(b._child_sessions[SID_B]["tool_use_id"], "spawn-2")

            b._forward_update(_child_chunk(SID_A, "A1 "))
            b._forward_update(_child_chunk(SID_B, "B1 "))
            b._forward_update(_child_chunk(SID_A, "A2"))
            b._forward_update(_child_chunk(SID_B, "B2"))
            self.assertEqual(b._child_sessions[SID_A]["text"], "A1 A2")
            self.assertEqual(b._child_sessions[SID_B]["text"], "B1 B2")
            self.assertEqual(s.output.texts, [])

            b._forward_update(_child_done(SID_A))
            t1 = s.output.find_tool_by_id("spawn-1")
            t2 = s.output.find_tool_by_id("spawn-2")
            self.assertNotEqual(getattr(t1, "status", None), "background")
            self.assertEqual(t2.status, "background")

            b._forward_update(_child_done(SID_B))
            t2 = s.output.find_tool_by_id("spawn-2")
            self.assertNotEqual(getattr(t2, "status", None), "background")
            _complete_last_query(s)
            self.assertFalse(s.working)


# ── B. Notify dedupe ──────────────────────────────────────────────────


class TestB6AliasNotifyOnceBothDirections(unittest.TestCase):
    """Replaces helper-only pins in tests/test_bg_notify_dedupe.py."""

    def test_notify_acp_term_then_bash_alias_is_one_notify(self):
        g, turn, sched, queries, surfaces = _gate("claude")
        g.on_task_started({"task_id": "acp-term-abc", "tool_use_id": "tool-1"})
        g.on_task_started({"task_id": "bash-xyz", "tool_use_id": "tool-1"})
        g.on_task_notification({
            "task_id": "acp-term-abc",
            "tool_use_id": "tool-1",
            "status": "completed",
            "summary": "sleep 9",
        })
        self.assertEqual(len(surfaces), 1)
        g.on_task_notification({
            "task_id": "bash-xyz",
            "tool_use_id": "tool-1",
            "status": "completed",
            "summary": "sleep 9 again",
        })
        self.assertEqual(len(surfaces), 1)
        g.on_task_notification({
            "task_id": "bash-xyz",
            "tool_use_id": "tool-1",
            "status": "completed",
            "summary": "late",
        })
        self.assertEqual(len(surfaces), 1)
        self.assertEqual(queries, [])

    def test_notify_bash_then_acp_term_alias_is_one_notify(self):
        g, turn, sched, queries, surfaces = _gate("claude")
        g.on_task_started({"task_id": "acp-term-abc", "tool_use_id": "tool-1"})
        g.on_task_started({"task_id": "bash-xyz", "tool_use_id": "tool-1"})
        g.on_task_notification({
            "task_id": "bash-xyz",
            "tool_use_id": "tool-1",
            "status": "completed",
            "summary": "sleep 9",
        })
        g.on_task_notification({
            "task_id": "acp-term-abc",
            "tool_use_id": "tool-1",
            "status": "completed",
            "summary": "sleep 9",
        })
        self.assertEqual(len(surfaces), 1)
        self.assertEqual(queries, [])


class TestB7CompletionDuringBusyParentIsRowOnly(unittest.TestCase):
    """A completion while the parent turn is live flips the row at once and
    never becomes a host turn — for any backend. The runtime tells the model
    itself (Claude Code injects a turn, Grok/Kimi self-wake)."""

    def _drive(self, backend, tool_use, notification):
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend=backend)
        s.query("hello")
        s.events.tool_use(tool_use)
        s.events.system({"subtype": "task_notification", "data": notification})
        tool = s.output.find_tool_by_id(tool_use["id"])
        self.assertEqual(tool.status, "done")
        self.assertIn(tool_use["id"], s.bg.notified_tool_ids)
        _complete_last_query(s)
        self.assertFalse(s.working)
        self.assertIn(True, s.chrome.unread)
        prompts = [
            (t[1] or {}).get("prompt") or ""
            for t in client.sent if t[0] == "query"]
        self.assertEqual(len(prompts), 1)
        self.assertFalse(any("task-notification" in p for p in prompts))

    def test_claude(self):
        self._drive("claude", {
            "name": "Subagent", "id": "spawn-1", "background": True,
            "input": {"description": "check"},
        }, {
            "task_id": "acp-child-%s" % SID_A, "tool_use_id": "spawn-1",
            "status": "completed", "summary": "done",
        })

    def test_grok(self):
        self._drive("grok", {
            "name": "Subagent", "id": "spawn-1", "background": True,
            "input": {"description": "check"},
        }, {
            "task_id": "acp-child-%s" % SID_A, "tool_use_id": "spawn-1",
            "status": "completed", "summary": "done",
        })

    def test_kimi(self):
        self._drive("kimi", {
            "name": "Bash", "id": "tool-1", "background": True,
            "input": {"command": "sleep 1"},
        }, {
            "task_id": "bash-xyz", "tool_use_id": "tool-1",
            "status": "completed", "summary": "sleep 1",
        })


class TestB9TaskGetPollThenCompletion(unittest.TestCase):
    def test_taskget_poll_then_bridge_notify_flips_the_row_once(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="claude")
        s.query("poll the job")
        s.events.tool_use({
            "name": "Bash", "id": "tool-1", "background": True,
            "input": {"command": "sleep 9", "task_id": "bash-xyz"},
        })
        s.events.system({
            "subtype": "task_started",
            "data": {"task_id": "bash-xyz", "tool_use_id": "tool-1"},
        })
        s.events.tool_use({
            "name": "TaskGet", "id": "poll-1", "background": False,
            "input": {"task_id": "bash-xyz"},
        })
        s.events.tool_result({
            "tool_use_id": "poll-1",
            "content": (
                "task_id: bash-xyz\n"
                "status: completed\n"
                "exit_code: 0\n"
            ),
        })
        # The agent polled its own job; the ⚙ row still waits for the
        # completion event.
        self.assertEqual(s.output.find_tool_by_id("tool-1").status, "background")
        s.events.system({
            "subtype": "task_notification",
            "data": {
                "task_id": "bash-xyz",
                "tool_use_id": "tool-1",
                "status": "completed",
                "summary": "sleep 9",
            },
        })
        self.assertEqual(s.output.find_tool_by_id("tool-1").status, "done")
        _complete_last_query(s)
        extra = [
            (t[1] or {}).get("prompt") or ""
            for t in client.sent if t[0] == "query"]
        self.assertEqual(len(extra), 1)


class TestB10AbortClearsMarksReusedIdNotifiesAgain(unittest.TestCase):
    def test_abort_clears_host_notified_reused_id_notifies_again(self):
        g, turn, sched, queries, surfaces = _gate("claude")
        g.on_task_notification({
            "task_id": "bash-old", "tool_use_id": "tool-old",
            "status": "completed", "summary": "first",
        })
        self.assertIn("bash-old", g.notified_task_ids)
        self.assertEqual(len(surfaces), 1)
        g.abort()
        self.assertEqual(g.notified_task_ids, set())
        self.assertEqual(g.notified_tool_ids, set())
        g.on_task_notification({
            "task_id": "bash-old", "tool_use_id": "tool-old",
            "status": "completed", "summary": "reused",
        })
        self.assertIn("bash-old", g.notified_task_ids)
        self.assertEqual(len(surfaces), 2)


# ── C. Busy-bit invariants ────────────────────────────────────────────


class TestC11LeftoverAfterDoneStaysIdleFullStack(unittest.TestCase):
    """Replaces TurnState-only coverage in tests/test_busy_state.py for leftovers."""

    def test_leftover_tool_use_bg_synth_bash_child_complete_stay_idle(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="grok")
        s.query("go")
        _complete_last_query(s)
        self.assertFalse(s.working)
        s.events.tool_use({
            "name": "Subagent", "id": "spawn-1", "background": True,
            "input": {"description": "late spawn"},
        })
        self.assertFalse(s.working)
        self.assertEqual(s.turn.kind, "idle")
        self.assertIn("spawn-1", s.bg.bg_tools)
        s.events.tool_use({
            "name": "Bash", "id": "synth-1", "background": False,
            "input": {"command": "tail log"},
        })
        self.assertFalse(s.working)
        self.assertEqual(s.turn.kind, "idle")
        s.events.system({
            "subtype": "task_notification",
            "data": {
                "task_id": "acp-child-%s" % SID_A,
                "tool_use_id": "spawn-1",
                "status": "completed",
                "summary": "done",
            },
        })
        s.scheduler.fire_due(1200)
        self.assertFalse(s.working)
        self.assertEqual(s.turn.kind, "idle")
        self.assertIn(True, s.chrome.unread)

    def test_gitapp_after_done_full_stack_stays_idle(self):
        fix = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "fixtures", "gitapp_after_done.json")
        with open(fix, encoding="utf-8") as f:
            data = json.load(f)
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="kimi")
        s.query("gitapp")
        _complete_last_query(s)
        for ev in data["events"]:
            kind = ev.get("t")
            if kind == "terminal_create" and ev.get("synth_bash"):
                s.events.tool_use({
                    "name": "Bash",
                    "id": "synth-%s" % ev.get("cmd", "x"),
                    "input": {"command": ev.get("cmd") or "tail"},
                    "background": False,
                })
            elif kind == "tool_use_bg":
                s.events.tool_use({
                    "name": "Bash", "id": "bg-leftover",
                    "background": True,
                    "input": {"command": "pil test"},
                })
            elif kind == "bg_notify":
                s.events.system({
                    "subtype": "task_notification",
                    "data": {
                        "task_id": "bash-gitapp",
                        "tool_use_id": "bg-leftover",
                        "status": "completed",
                        "summary": "pil test",
                    },
                })
                s.scheduler.fire_due(1200)
        # Kimi ran its own completion turn (perm + fs, silent); the host
        # flips the row and stays idle — a query here doubled that turn.
        self.assertEqual(s.turn.kind, "idle")
        self.assertFalse(s.working)
        self.assertIn("bg-leftover", s.bg.notified_tool_ids)
        prompts = [
            (t[1] or {}).get("prompt") or ""
            for t in client.sent if t[0] == "query"]
        self.assertEqual(len(prompts), 1)
        self.assertFalse(any("task-notification" in p for p in prompts))


class TestC12EscLiveSubagentNoResume(unittest.TestCase):
    def test_esc_with_live_subagent_no_resume_stream_no_closerless_busy(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="grok")
        s.query("hello")
        s.events.tool_use({
            "name": "Subagent", "id": "spawn-1", "background": True,
            "input": {"description": "check"},
        })
        self.assertTrue(s.bg.has_background())
        s.interrupt()
        s._on_done({"status": "interrupted"}, _expected_gen=s.turn.gen)
        self.assertEqual(s.turn.kind, "idle")
        self.assertFalse(s.working)
        self.assertTrue(s.bg.has_background())
        self.assertTrue(s._interrupt_stream)
        s.events.system({
            "subtype": "task_notification",
            "data": {
                "task_id": "acp-child-%s" % SID_A,
                "tool_use_id": "spawn-1",
                "status": "completed",
                "summary": "done",
            },
        })
        s.scheduler.fire_due(1200)
        self.assertFalse(s.working)
        self.assertEqual(s.turn.kind, "idle")


class TestC13EscLiveShellBgKillsTerminal(unittest.TestCase):
    def test_esc_live_shell_bg_bridge_kills_terminal_failed_notify_closes_row(self):
        with ConnectedStack(backend="claude") as st:
            s, b = st.session, st.bridge
            s.query("bg sleep")
            b._note_shell_execute("tool-bash", "Bash")
            b._ensure_call("tool-bash").merge_input({
                "command": "sleep 99", "run_in_background": True})
            slot = {
                "cmd": "sleep 99",
                "stdout": "",
                "stderr": "",
                "exit_status": None,
                "reader": None,
                "proc": None,
            }
            b._terminals["term_x"] = slot
            b._mark_terminal_bg("term_x", slot)
            self.assertEqual(
                s.output.find_tool_by_id("tool-bash").status, "background")
            killed = []
            b._kill_terminal_proc = lambda proc: killed.append(proc)
            s.interrupt()
            self.assertEqual(s.turn.kind, "interrupting")
            s._on_done({"status": "interrupted"}, _expected_gen=s.turn.gen)
            self.assertEqual(s.turn.kind, "idle")
            self.assertFalse(s.working)

            async def _kill():
                for tid in list(b._terminals):
                    await b._terminal_close(tid)
                slot["exit_status"] = {"exitCode": None, "signal": "SIGTERM"}
                b._emit_bg_terminal_complete("term_x")

            asyncio.run(_kill())
            tool = s.output.find_tool_by_id("tool-bash")
            self.assertTrue(
                tool is None or getattr(tool, "status", None) != "background")
            self.assertFalse(s.working)


class TestC14LeftoverParentStreamStillResumes(unittest.TestCase):
    def test_leftover_parent_stream_after_esc_does_not_reown_busy(self):
        """Esc wins: Grok MidTurnAbort leftover paints but never re-busies.

        Upstream 2e6e415: after a user cancel, resume_stream must not adopt
        the leftover turn again (that re-busied an idle sheet).
        """
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="grok")
        s.query("hello")
        s.events.tool_use({
            "name": "Subagent", "id": "spawn-1", "background": True,
            "input": {"description": "check"},
        })
        s.interrupt()
        s._on_done({"status": "interrupted"}, _expected_gen=s.turn.gen)
        self.assertEqual(s.turn.kind, "idle")
        self.assertTrue(s._user_cancelled_turn)
        s.events.dispatch("message", {
            "type": "text_delta",
            "text": "parent kept talking after cancel",
        })
        self.assertEqual(s.turn.kind, "idle")
        self.assertFalse(s.working)
        self.assertIn("parent kept talking after cancel", "".join(
            t if isinstance(t, str) else str(t) for t in s.output.texts))

    def test_self_wake_without_user_cancel_still_resumes(self):
        """No Esc: a Grok self-wake still owns busy until its closer."""
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="grok")
        s.query("hello")
        s._on_done({"status": "complete"}, _expected_gen=s.turn.gen)
        self.assertEqual(s.turn.kind, "idle")
        self.assertFalse(s._user_cancelled_turn)
        s.events.dispatch("message", {
            "type": "system",
            "subtype": "agent_continue",
            "data": {"reason": "tool_call"},
        })
        self.assertEqual(s.turn.kind, "live")
        self.assertTrue(s.working)


# ── D. Lifecycle matrix ───────────────────────────────────────────────


class TestD15InterruptCancelsWaitersAndRows(unittest.TestCase):
    def test_interrupt_sigterms_terminals_cancels_child_waiters_closes_rows(self):
        with ConnectedStack(backend="grok") as st:
            s, b = st.session, st.bridge
            s.query("spawn + shell")
            b._forward_update(_spawn_update("spawn-1"))
            b._bind_child_to_tool("subagent_id: %s\n" % SID_A, "spawn-1")
            b._note_shell_execute("tool-bash", "Bash")
            b._ensure_call("tool-bash").merge_input({
                "command": "sleep 99", "run_in_background": True})
            b._terminals["term_x"] = {
                "cmd": "sleep 99", "stdout": "", "stderr": "",
                "exit_status": None, "reader": None, "proc": None,
            }
            b._mark_terminal_bg("term_x", b._terminals["term_x"])
            self.assertTrue(s.bg.has_background())

            async def _go():
                waiter = asyncio.create_task(
                    b._acp_terminal_wait({"terminalId": SID_A}))
                await asyncio.sleep(0)
                b._cancel_child_sessions("interrupt")
                b._terminals["term_x"]["exit_status"] = {
                    "exitCode": None, "signal": "SIGTERM"}
                b._emit_bg_terminal_complete("term_x")
                return await waiter

            result = asyncio.run(_go())
            self.assertEqual(result.get("exitCode"), None)
            self.assertEqual(result.get("signal"), "SIGTERM")
            t_spawn = s.output.find_tool_by_id("spawn-1")
            t_bash = s.output.find_tool_by_id("tool-bash")
            self.assertTrue(
                t_spawn is None
                or getattr(t_spawn, "status", None) != "background")
            self.assertTrue(
                t_bash is None
                or getattr(t_bash, "status", None) != "background")


class TestD16ClearUnblocksAndRemovesRows(unittest.TestCase):
    def test_clear_marks_child_slots_done_unblocks_waiters_removes_rows(self):
        with ConnectedStack(backend="grok") as st:
            s, b = st.session, st.bridge
            s.query("spawn")
            b._forward_update(_spawn_update("spawn-1"))
            b._bind_child_to_tool("subagent_id: %s\n" % SID_A, "spawn-1")
            self.assertTrue(s.bg.has_background())

            async def _go():
                waiter = asyncio.create_task(
                    b._acp_terminal_wait({"terminalId": SID_A}))
                await asyncio.sleep(0)
                b._reset_conversation_local()
                return await waiter

            result = asyncio.run(_go())
            self.assertEqual(result.get("signal"), "SIGTERM")
            self.assertEqual(b._child_sessions, {})

            s.clear_conversation()
            clears = [t for t in s.client.sent if t[0] == "clear"]
            self.assertTrue(clears)
            clears[-1][2]({"session_id": "sess-new"})
            self.assertFalse(s.bg.has_background())
            self.assertEqual(s.bg.bg_tools, {})
            self.assertTrue(s.output.cleared)


class TestD17SoftSleepRefusedWithLiveGear(unittest.TestCase):
    def test_soft_sleep_with_live_gear_is_refused(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="grok")
        s.session_id = "sess-sleep"
        s.query("hello")
        s.events.tool_use({
            "name": "Subagent", "id": "spawn-1", "background": True,
            "input": {"description": "check"},
        })
        _complete_last_query(s)
        self.assertFalse(s.working)
        self.assertTrue(s.output.active_background_tools())
        self.assertFalse(s.sleep(force=False))
        self.assertIs(s.client, client)
        self.assertTrue(s.initialized)
        self.assertTrue(s.bg.has_background())
        self.assertTrue(any(
            "refusing" in str(x).lower() for x in s.chrome.status))


class TestD18ForceSleepAbortsAndKillsStalePolls(unittest.TestCase):
    def test_force_sleep_clears_rows_marks_bumps_epoch_stale_polls_die(self):
        polls = []

        def send_poll(cb):
            polls.append(cb)
            return True

        g, turn, sched, queries, surfaces = _gate("grok")
        g.send_poll = send_poll
        out_tool = type("T", (), {
            "name": "Subagent", "status": "background", "id": "spawn-1",
        })()
        g.output._tools_by_id["spawn-1"] = out_tool
        g.register_tool("spawn-1", tool=out_tool, task_id="acp-child-x")
        g.notified_task_ids.add("old")
        g.notified_tool_ids.add("old-tool")
        g.schedule_poll()
        sched.fire_due(5000)
        self.assertTrue(polls)
        stale = polls[-1]
        old_epoch = g.poll_epoch
        g.abort()
        self.assertEqual(g.notified_task_ids, set())
        self.assertEqual(g.notified_tool_ids, set())
        self.assertEqual(g.bg_tools, {})
        self.assertEqual(g.poll_epoch, old_epoch + 1)
        stale({"running": []})
        self.assertEqual(g.bg_tools, {})

        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="grok")
        s.session_id = "sess-force"
        s.query("hello")
        s.events.tool_use({
            "name": "Subagent", "id": "spawn-1", "background": True,
            "input": {"description": "check"},
        })
        _complete_last_query(s)
        epoch = s.bg.poll_epoch
        self.assertTrue(s.sleep(force=True))
        self.assertFalse(s.bg.has_background())
        self.assertEqual(s.bg.notified_task_ids, set())
        self.assertGreater(s.bg.poll_epoch, epoch)
        s.scheduler.fire_all()
        self.assertFalse(s.bg.has_background())


class TestD19WakeAfterForceSleepNoZombie(unittest.TestCase):
    def test_wake_after_force_sleep_old_ids_gone_stale_output_no_zombie(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="grok")
        s.session_id = "sess-wake"
        s.query("hello")
        s.events.tool_use({
            "name": "Subagent", "id": "spawn-old", "background": True,
            "input": {"description": "check"},
        })
        s.bg.on_task_started({
            "task_id": "acp-child-%s" % SID_A, "tool_use_id": "spawn-old"})
        _complete_last_query(s)
        self.assertTrue(s.sleep(force=True))
        self.assertNotIn("spawn-old", s.bg.bg_tools)
        self.assertEqual(s.bg.task_tool_map, {})

        b = LiveBridge()

        async def _stale():
            return await b._acp_terminal_output({"terminalId": SID_A})

        result = asyncio.run(_stale())
        self.assertNotIn("exitStatus", result)
        self.assertIn(SID_A, b._child_sessions)
        s.bg.reconcile([])
        self.assertFalse(s.bg.has_background())
        self.assertIsNone(s.output.find_tool_by_id("spawn-old"))


class TestD20ReloadAbortsBgNoSigterm(unittest.TestCase):
    """Replaces the source-pin TestF11F12Cleanup.test_plugin_loaded_aborts_bg
    with a real call of the reload helper."""

    def test_reload_aborts_bg_before_refs_dropped_no_sigterm(self):
        from tests.stubs import install_sublime
        install_sublime()
        from main import _abort_session_ui
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="grok")
        s.events.tool_use({
            "name": "Subagent", "id": "spawn-1", "background": True,
            "input": {"description": "check"},
        })
        self.assertTrue(s.bg.has_background())
        _abort_session_ui(s)
        self.assertFalse(s.bg.has_background())
        self.assertFalse(client.stopped)
        self.assertTrue(s.output.resets)


# ── E. Bind race + reconcile ──────────────────────────────────────────


class TestE21CompleteBeforeBindClosesSpawnRow(unittest.TestCase):
    def test_child_turn_completed_before_subagent_id_text_closes_spawn_row(self):
        with ConnectedStack(backend="grok") as st:
            s, b = st.session, st.bridge
            s.query("fast child")
            b._forward_update(_spawn_update("spawn-1", desc="working check"))
            self.assertEqual(
                s.output.find_tool_by_id("spawn-1").status, "background")
            b._ingest_child_session({
                "sessionId": SID_A,
                "update": {
                    "sessionUpdate": "turn_completed",
                    "stop_reason": "end_turn",
                },
            })
            self.assertEqual(b._child_sessions[SID_A].get("tool_use_id"), "spawn-1")
            zombies = [
                tid for tid in s.bg.bg_tools
                if str(tid).startswith("bg-acp-child-")]
            self.assertEqual(zombies, [])
            tool = s.output.find_tool_by_id("spawn-1")
            status = getattr(tool, "status", None) if tool is not None else None
            self.assertNotEqual(status, "background")
            self.assertNotIn("bg-acp-child-%s" % SID_A, s.bg.bg_tools)
            _complete_last_query(s)
            self.assertFalse(s.working)


class TestE22LiveIdsIncludeChildReconcileClosesVanished(unittest.TestCase):
    def test_live_bg_task_ids_include_acp_child_host_reconcile_closes_vanished(self):
        with ConnectedStack(backend="grok") as st:
            s, b = st.session, st.bridge
            s.query("spawn")
            b._forward_update(_spawn_update("spawn-1"))
            b._bind_child_to_tool("subagent_id: %s\n" % SID_A, "spawn-1")
            live = b._live_bg_task_ids()
            self.assertIn("acp-child-%s" % SID_A, live)
            s.bg.reconcile(list(live))
            self.assertEqual(
                s.output.find_tool_by_id("spawn-1").status, "background")
            s.bg.reconcile([])
            tool = s.output.find_tool_by_id("spawn-1")
            status = getattr(tool, "status", None) if tool is not None else None
            self.assertNotEqual(status, "background")
            self.assertNotIn("acp-child-%s" % SID_A, s.bg.task_tool_map)


class TestE23WaitForExitUnknownIdUnblocks(unittest.TestCase):
    def test_wait_for_exit_unknown_id_interrupt_unblocks_cancelled(self):
        b = LiveBridge()

        async def _go():
            waiter = asyncio.create_task(
                b._acp_terminal_wait({"terminalId": SID_ULID}))
            await asyncio.sleep(0)
            self.assertIn(SID_ULID, b._child_sessions)
            b._cancel_child_sessions("interrupt")
            return await waiter

        result = asyncio.run(_go())
        self.assertEqual(result.get("exitCode"), None)
        self.assertEqual(result.get("signal"), "SIGTERM")

    def test_wait_for_exit_unknown_id_shutdown_unblocks_cancelled(self):
        b = LiveBridge()

        async def _go():
            waiter = asyncio.create_task(
                b._acp_terminal_wait({"terminalId": SID_ULID}))
            await asyncio.sleep(0)
            b._cancel_child_sessions("shutdown")
            return await waiter

        result = asyncio.run(_go())
        self.assertEqual(result.get("signal"), "SIGTERM")

    def test_wait_for_exit_unknown_id_optional_timeout_honored(self):
        b = LiveBridge()
        b.terminal_wait_timeout_s = 1
        b._register_child_session(SID_ULID)

        async def _boom(aw, timeout=None):
            if hasattr(aw, "close"):
                aw.close()
            raise asyncio.TimeoutError()

        orig = asyncio.wait_for
        asyncio.wait_for = _boom
        try:
            result = asyncio.run(b._acp_terminal_wait({"terminalId": SID_ULID}))
        finally:
            asyncio.wait_for = orig
        self.assertEqual(result.get("signal"), "SIGTERM")
        self.assertTrue(b._child_sessions[SID_ULID].get("done"))


# ── F. ULID / formatting ──────────────────────────────────────────────


class TestF24NoRawSessionUlidOnSurfaces(unittest.TestCase):
    def test_no_raw_ulid_in_subagent_label_taskget_notify_or_composer_hint(self):
        from plat.constants import BACKGROUND_PREFIX
        from ui.formatters import _task, _task_get, format_tool_detail

        class SpawnTool:
            name = "Subagent"
            status = "background"
            tool_input = {
                "description": SID_ULID,
                "title": SID_ULID,
                "prompt": SID_ULID,
            }

        label = _task(None, SpawnTool())
        self.assertNotIn(SID_ULID, label)
        self.assertNotIn("01a00fc4", label)
        detail = format_tool_detail(None, SpawnTool())
        self.assertNotIn(SID_ULID, detail)
        hint = "  %s%s%s\n" % (BACKGROUND_PREFIX, SpawnTool.name, detail)
        self.assertNotIn(SID_ULID, hint)

        class PollTool:
            name = "TaskGet"
            status = "pending"
            tool_input = {
                "task_ids": [SID_ULID],
                "task_id": SID_ULID,
                "description": "waiting on child",
            }

        get_label = _task_get(None, PollTool())
        self.assertNotIn(SID_ULID, get_label)
        self.assertNotIn("#01a00", get_label)

        g, turn, sched, queries, surfaces = _gate("claude")
        g.output.tool("Subagent", {"description": "check"}, tool_id="spawn-1",
                      background=True)
        g.register_tool("spawn-1", tool=g.output._tools_by_id["spawn-1"])
        g.on_task_notification({
            "task_id": "acp-child-%s" % SID_ULID,
            "tool_use_id": "spawn-1",
            "status": "completed",
            "summary": SID_ULID,
        })
        self.assertEqual(len(g.output.done), 1)
        block = str(g.output.done[0][1] or "")
        self.assertNotIn(SID_ULID, block)
        self.assertNotIn("01a00fc4", block)

        b = LiveBridge()
        b._ensure_call("spawn-1").background = True
        b._ensure_call("spawn-1").name = "Subagent"
        b._ensure_call("spawn-1").merge_input({"description": "working check"})
        notes = []
        orig = _patch_all_notify(lambda m, p: notes.append((m, p)))
        try:
            b._ingest_child_session({
                "sessionId": SID_ULID,
                "update": {
                    "sessionUpdate": "turn_completed",
                    "stop_reason": "end_turn",
                },
            })
        finally:
            _restore_notify(orig)
        summaries = [
            (p.get("data") or {}).get("summary")
            for _, p in notes
            if isinstance(p, dict) and p.get("subtype") == "task_notification"]
        self.assertTrue(summaries)
        for sm in summaries:
            self.assertNotIn(SID_ULID, str(sm))
            self.assertNotIn("01a00fc4", str(sm))


class TestF25ChildSessionIdHelperShared(unittest.TestCase):
    def test_is_child_session_id_heuristic_host_and_ui_same_helper(self):
        from ui import formatters as ui_fmt
        self.assertIs(ui_fmt.is_child_session_id, is_child_session_id)
        self.assertTrue(is_child_session_id(SID_ULID))
        self.assertTrue(is_child_session_id("acp-child-%s" % SID_ULID))
        self.assertFalse(is_child_session_id("term_deadbeef"))
        self.assertFalse(is_child_session_id("bash-xyz"))
        self.assertFalse(is_child_session_id("bash-wnvdr6k2"))
        self.assertFalse(is_child_session_id(""))
        self.assertFalse(is_child_session_id(None))
        b = LiveBridge()
        self.assertTrue(b._is_child_session_id(SID_ULID))
        self.assertFalse(b._is_child_session_id("term_deadbeef"))
        self.assertFalse(b._is_child_session_id("bash-xyz"))
        self.assertFalse(b._is_child_session_id(""))


# ── G. Allowlist coherence ────────────────────────────────────────────


class TestG26UiShellBgMatchesCore(unittest.TestCase):
    def test_ui_tools_shell_bg_equals_core_shell_union_subagent(self):
        from ui.tools import SHELL_BG as UI_SHELL
        self.assertEqual(set(UI_SHELL), set(SHELL_BG) | set(SUBAGENT_BG))
        self.assertIn("Task", UI_SHELL)
        self.assertIn("Subagent", UI_SHELL)
        self.assertTrue(is_shell_background_tool("Task"))
        self.assertTrue(is_shell_background_tool("Subagent"))
        self.assertTrue(is_shell_background_tool("Bash"))
        self.assertFalse(is_shell_background_tool("Read"))


class TestG28BridgeNameTruthTable(unittest.TestCase):
    def test_bridge_shell_and_spawn_truth_table_full_name_set(self):
        names = [
            "Bash", "Shell", "execute", "run_terminal_command", "Workflow",
            "Task", "Subagent", "spawn_subagent", "tool", "TaskGet", "TaskOutput",
        ]
        shell_yes = {
            "Bash", "Shell", "execute", "run_terminal_command", "Workflow",
            "tool",
        }
        spawn_by_name = {"Subagent", "spawn_subagent"}
        for name in names:
            self.assertEqual(
                AcpBridge._is_shell_tool_name(name),
                name in shell_yes,
                "shell? %s" % name)
            self.assertEqual(
                AcpBridge._is_subagent_spawn(name, {"title": name}, {}),
                name in spawn_by_name,
                "spawn-by-name? %s" % name)
        self.assertFalse(AcpBridge._is_subagent_spawn(
            "Task", {"title": "Task"}, {"prompt": "do work"}))
        self.assertFalse(AcpBridge._is_subagent_spawn(
            "Task", {"title": "Agent"}, {"prompt": "do work"}))
        self.assertTrue(AcpBridge._is_subagent_spawn(
            "Task", {"title": "Agent"},
            {"prompt": "do work", "detached": True}))
        self.assertTrue(AcpBridge._is_subagent_spawn(
            "Task", {"title": "spawn_subagent"}, {"prompt": "x"}))
        self.assertFalse(AcpBridge._is_subagent_spawn(
            "TaskGet",
            {"title": "Get task output: %s" % SID_ULID},
            {"task_ids": [SID_ULID]}))
        self.assertFalse(AcpBridge._is_shell_tool_name("Task"))
        self.assertFalse(AcpBridge._is_shell_tool_name("Subagent"))
        self.assertFalse(AcpBridge._is_shell_tool_name("TaskGet"))
        self.assertFalse(AcpBridge._is_shell_tool_name("Read"))


if __name__ == "__main__":
    unittest.main()
