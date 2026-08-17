"""Full-stack pins for the p6 bg/subagent audit (F1–F7)."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

from core.background import (
    SHELL_BG,
    SUBAGENT_BG,
    _POLL_TOOLS,
    BackgroundTaskGate,
    is_child_session_id,
    is_shell_background_tool,
)
from core.turn import TurnController
from tests.fakes import FakeClient, FakeOutput, FakeScheduler, make_session
from tests.test_bg_tool_gates import _FwdStub, _patch_notify, _restore_notify

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)


def _gate(backend="claude", busy=False):
    turn = TurnController()
    if busy:
        turn.begin_query()
    sched = FakeScheduler()
    out = FakeOutput()
    queries = []
    surfaces = []

    def on_query(prompt, display):
        queries.append((prompt, display))

    def on_surface():
        surfaces.append(True)

    g = BackgroundTaskGate(
        turn, sched, out, backend=backend,
        on_query=on_query, on_surface=on_surface)
    return g, turn, sched, queries, surfaces


class TestF1NotifyBufferAndHold(unittest.TestCase):
    def test_busy_notify_is_buffered_not_marked(self):
        g, turn, sched, queries, surfaces = _gate("claude", busy=True)
        g.on_task_notification({
            "task_id": "acp-child-sid",
            "tool_use_id": "spawn-1",
            "status": "completed",
            "summary": "done",
        }, working=True)
        self.assertTrue(g.pending_notifications)
        self.assertNotIn("acp-child-sid", g.notified_task_ids)
        self.assertNotIn("spawn-1", g.notified_tool_ids)
        self.assertEqual(queries, [])
        self.assertEqual(surfaces, [])

    def test_flush_hold_keeps_buffer(self):
        g, turn, sched, queries, surfaces = _gate("claude", busy=True)
        g.on_task_notification({
            "task_id": "t1",
            "tool_use_id": "tool-1",
            "status": "completed",
            "summary": "done",
        }, working=True)
        g.flush()
        self.assertTrue(g.pending_notifications)
        self.assertEqual(g.pending_hold_gen, turn.gen)
        self.assertNotIn("t1", g.notified_task_ids)
        self.assertEqual(queries, [])

    def test_claude_surfaces_via_query_after_end_live(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="claude")
        s.query("hello")
        s.events.tool_use({
            "name": "Subagent",
            "id": "spawn-1",
            "background": True,
            "input": {"description": "check"},
        })
        s.bg.on_task_started({
            "task_id": "acp-child-sid", "tool_use_id": "spawn-1"})
        s.bg.on_task_notification({
            "task_id": "acp-child-sid",
            "tool_use_id": "spawn-1",
            "status": "completed",
            "summary": "done",
        }, working=True)
        self.assertTrue(s.bg.pending_notifications)
        self.assertNotIn("spawn-1", s.bg.notified_tool_ids)
        cb = [t[2] for t in client.sent if t[0] == "query"][-1]
        cb({"status": "complete"})
        queries = [t for t in client.sent if t[0] == "query"]
        self.assertGreaterEqual(len(queries), 2)
        prompt = (queries[-1][1] or {}).get("prompt") or ""
        self.assertIn("task-notification", prompt)
        self.assertIn("spawn-1", s.bg.notified_tool_ids)
        self.assertFalse(s.bg.pending_notifications)

    def test_grok_surfaces_after_end_live(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="grok")
        s.query("hello")
        s.events.tool_use({
            "name": "Subagent",
            "id": "spawn-1",
            "background": True,
            "input": {"description": "check"},
        })
        s.bg.on_task_notification({
            "task_id": "acp-child-sid",
            "tool_use_id": "spawn-1",
            "status": "completed",
            "summary": "done",
        }, working=True)
        cb = [t[2] for t in client.sent if t[0] == "query"][-1]
        cb({"status": "complete"})
        self.assertIn(True, s.chrome.unread)
        self.assertGreater(s.output.hint_refreshes, 0)
        self.assertIn("spawn-1", s.bg.notified_tool_ids)
        self.assertFalse(s.working)
        extra = [t for t in client.sent if t[0] == "query"]
        self.assertEqual(len(extra), 1)


class TestF2EscDoesNotResumeForSubagent(unittest.TestCase):
    def test_esc_with_live_subagent_stays_idle(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="grok")
        s.query("hello")
        s.events.tool_use({
            "name": "Subagent",
            "id": "spawn-1",
            "background": True,
            "input": {"description": "check"},
        })
        self.assertTrue(s.bg.has_background())
        s.interrupt()
        self.assertEqual(s.turn.kind, "interrupting")
        s._on_done({"status": "interrupted"}, _expected_gen=s.turn.gen)
        self.assertEqual(s.turn.kind, "idle")
        self.assertFalse(s.working)
        self.assertTrue(s.bg.has_background())
        self.assertTrue(s._interrupt_stream)


class TestF3BindBeforeComplete(unittest.TestCase):
    def test_complete_before_subagent_id_text_closes_spawn_row(self):
        b = _FwdStub()
        b._bg_tool_ids.add("spawn-1")
        b._tool_names_by_id["spawn-1"] = "Subagent"
        b._tool_inputs_by_id["spawn-1"] = {"description": "working check"}
        sid = "01a00fast-bbbb-cccc-dddd-eeeeffff0000"
        b._ingest_child_session({
            "sessionId": sid,
            "update": {
                "sessionUpdate": "turn_completed",
                "stop_reason": "end_turn",
            },
        })
        slot = b._child_sessions[sid]
        self.assertEqual(slot.get("tool_use_id"), "spawn-1")
        self.assertEqual(slot.get("description"), "working check")
        self.assertTrue(b._finished)
        _task_id, tuid, status, summary, _path = b._finished[-1]
        self.assertEqual(tuid, "spawn-1")
        self.assertEqual(status, "completed")
        self.assertNotIn(sid, summary)
        started = [d for st, d in b._systems if st == "task_started"]
        self.assertTrue(started)
        self.assertEqual(started[-1].get("tool_use_id"), "spawn-1")

    def test_late_bind_after_complete_emits_task_started(self):
        b = _FwdStub()
        sid = "01a00late-bbbb-cccc-dddd-eeeeffff0000"
        b._ingest_child_session({
            "sessionId": sid,
            "update": {
                "sessionUpdate": "turn_completed",
                "stop_reason": "end_turn",
            },
        })
        self.assertFalse(b._child_sessions[sid].get("tool_use_id"))
        b._bg_tool_ids.add("spawn-1")
        b._tool_names_by_id["spawn-1"] = "Subagent"
        b._bind_child_to_tool("subagent_id: %s\n" % sid, "spawn-1")
        self.assertEqual(b._child_sessions[sid].get("tool_use_id"), "spawn-1")
        started = [d for st, d in b._systems if st == "task_started"]
        updated = [d for st, d in b._systems if st == "task_updated"]
        self.assertTrue(started)
        self.assertEqual(started[-1].get("tool_use_id"), "spawn-1")
        self.assertTrue(updated)


class TestF4SpawnGates(unittest.TestCase):
    def test_forward_task_without_detach_is_not_gear(self):
        notes = []
        orig = _patch_notify(notes)
        try:
            b = _FwdStub()
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
        finally:
            _restore_notify(orig)
        uses = [p for _, p in notes if p.get("type") == "tool_use"]
        self.assertEqual(len(uses), 1, uses)
        self.assertEqual(uses[0].get("name"), "Task")
        self.assertFalse(uses[0].get("background"))
        self.assertNotIn("task-fg", b._bg_tool_ids)

    def test_forward_subagent_is_gear(self):
        notes = []
        orig = _patch_notify(notes)
        try:
            b = _FwdStub()
            b._forward_update({
                "sessionId": "session_test",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "sub-1",
                    "title": "spawn_subagent",
                    "kind": "other",
                    "status": "pending",
                    "rawInput": {"prompt": "say hi", "description": "check"},
                },
            })
        finally:
            _restore_notify(orig)
        uses = [p for _, p in notes if p.get("type") == "tool_use"]
        self.assertEqual(len(uses), 1, uses)
        self.assertTrue(uses[0].get("background"))
        self.assertIn("sub-1", b._bg_tool_ids)


class TestF5InterruptCancelsChildren(unittest.TestCase):
    def test_cancel_signals_waiter_and_fails_notify(self):
        b = _FwdStub()
        sid = "01a00wait-bbbb-cccc-dddd-eeeeffff0000"
        b._bg_tool_ids.add("spawn-1")
        b._tool_names_by_id["spawn-1"] = "Subagent"
        b._register_child_session(sid)

        async def _go():
            waiter = asyncio.create_task(
                b._acp_terminal_wait({"terminalId": sid}))
            await asyncio.sleep(0)
            b._cancel_child_sessions("interrupt")
            return await waiter

        result = asyncio.run(_go())
        self.assertEqual(result.get("exitCode"), None)
        self.assertEqual(result.get("signal"), "SIGTERM")
        self.assertTrue(b._finished)
        _tid, tuid, status, _sum, _path = b._finished[-1]
        self.assertEqual(tuid, "spawn-1")
        self.assertEqual(status, "failed")

    def test_child_wait_honors_timeout(self):
        b = _FwdStub()
        sid = "01a00to-bbbb-cccc-dddd-eeeeffff0001"
        b._register_child_session(sid)
        b.terminal_wait_timeout_s = 0.05

        async def _go():
            return await b._acp_terminal_wait({"terminalId": sid})

        result = asyncio.run(_go())
        self.assertEqual(result.get("signal"), "SIGTERM")
        self.assertTrue(b._child_sessions[sid].get("done"))


class TestF6AbortClearsHostMarks(unittest.TestCase):
    def test_abort_clears_notified_sets(self):
        g, turn, sched, queries, surfaces = _gate("claude")
        g.notified_task_ids.add("bash-old")
        g.notified_tool_ids.add("tool-old")
        g.abort()
        self.assertEqual(g.notified_task_ids, set())
        self.assertEqual(g.notified_tool_ids, set())


class TestF7ReconcileVanishedChild(unittest.TestCase):
    def test_reconcile_closes_vanished_child(self):
        s = make_session(initialized=True, client=FakeClient(), backend="grok")
        s.events.tool_use({
            "name": "Subagent",
            "id": "spawn-1",
            "background": True,
            "input": {"description": "check"},
        })
        s.bg.on_task_started({
            "task_id": "acp-child-sid", "tool_use_id": "spawn-1"})
        self.assertIn("spawn-1", s.bg.bg_tools)
        s.bg.reconcile(["acp-child-sid"])
        s.bg.reconcile([])
        tool = s.output.find_tool_by_id("spawn-1")
        status = getattr(tool, "status", None) if tool is not None else None
        self.assertNotEqual(status, "background")
        self.assertNotIn("acp-child-sid", s.bg.task_tool_map)


class TestF8F9AllowlistsAndIds(unittest.TestCase):
    def test_host_is_child_session_id(self):
        self.assertTrue(is_child_session_id(
            "01a00fc4-b4e1-7692-bb0e-e806eba02589"))
        self.assertTrue(is_child_session_id(
            "acp-child-01a00fc4-b4e1-7692-bb0e-e806eba02589"))
        self.assertFalse(is_child_session_id("bash-abc"))
        self.assertFalse(is_child_session_id("term_deadbeef"))

    def test_task_formatter_hides_ulid(self):
        from ui.formatters import _task

        class T:
            status = "background"
            tool_input = {
                "description": "01a00fc4-b4e1-7692-bb0e-e806eba02589",
                "title": "01a00fc4-b4e1-7692-bb0e-e806eba02589",
            }

        out = _task(None, T())
        self.assertNotIn("01a00fc4", out)

    def test_poll_tools_exclude_task(self):
        self.assertNotIn("Task", _POLL_TOOLS)
        self.assertNotIn("Subagent", _POLL_TOOLS)
        self.assertIn("TaskGet", _POLL_TOOLS)

    def test_ui_imports_canonical_allowlist(self):
        from ui.tools import SHELL_BG as UI_SHELL
        self.assertTrue(set(SHELL_BG).issubset(UI_SHELL))
        self.assertTrue(set(SUBAGENT_BG).issubset(UI_SHELL))
        self.assertTrue(is_shell_background_tool("Task"))
        self.assertTrue(is_shell_background_tool("Subagent"))
        self.assertTrue(is_shell_background_tool("Bash"))
        self.assertFalse(is_shell_background_tool("Read"))


class TestF10KimiSpawnCanonical(unittest.TestCase):
    def test_kimi_maps_spawn_subagent_to_subagent(self):
        from kimi_main import KimiBridge
        self.assertEqual(
            KimiBridge.TOOL_TO_CANONICAL.get("spawn_subagent"), "Subagent")
        self.assertEqual(KimiBridge.TOOL_TO_CANONICAL.get("Agent"), "Task")


class TestF11F12Cleanup(unittest.TestCase):
    def test_plugin_loaded_aborts_bg(self):
        path = os.path.join(_ROOT, "main.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("def _abort_session_ui", src)
        self.assertIn("bg.abort()", src)
        self.assertIn("def plugin_loaded", src)
        self.assertNotIn("SIGTERM", src.split("def plugin_loaded")[1][:800])

    def test_soft_fallback_removed(self):
        path = os.path.join(_ROOT, "core", "session.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("def _bg_soft_fallback_query", src)


if __name__ == "__main__":
    unittest.main()
