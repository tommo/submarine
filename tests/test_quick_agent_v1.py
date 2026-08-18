#!/usr/bin/env python3
"""Offline tests for Quick Agent v1 pure helpers + complete_slot path."""
from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.stubs import FakeSettings, install_sublime, reset_settings


def _read(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


def _load_quick():
    install_sublime()
    import features.quick as qa
    return qa


class TestShippedPureHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qa = _load_quick()

    def test_max_slots_is_3(self):
        self.assertEqual(self.qa.MAX_QUICK_SLOTS, 3)

    def test_can_add_slot(self):
        self.assertTrue(self.qa.can_add_slot(0))
        self.assertTrue(self.qa.can_add_slot(2))
        self.assertFalse(self.qa.can_add_slot(3))

    def test_normalize_done_status(self):
        self.assertEqual(self.qa.normalize_done_status("completed"), "completed")
        self.assertEqual(self.qa.normalize_done_status("blocked"), "blocked")
        self.assertEqual(self.qa.normalize_done_status("FAILED"), "blocked")
        self.assertEqual(self.qa.normalize_done_status("closed"), "closed")
        self.assertEqual(self.qa.normalize_done_status("dismiss"), "closed")
        self.assertEqual(self.qa.normalize_done_status("bye"), "closed")

    def test_user_wants_close(self):
        self.assertTrue(self.qa.user_wants_close("close yourself"))
        self.assertTrue(self.qa.user_wants_close("bye"))
        self.assertTrue(self.qa.user_wants_close("dismiss"))
        self.assertTrue(self.qa.user_wants_close("go away"))
        self.assertFalse(self.qa.user_wants_close("hello"))
        self.assertFalse(self.qa.user_wants_close("stop"))
        self.assertFalse(self.qa.user_wants_close("please close the file foo.py"))

    def test_system_prompt_turn_end_not_update_goal(self):
        p = self.qa.default_system_prompt()
        self.assertIn("quick_done", p)
        self.assertIn("closed", p.lower())
        self.assertIn("one-shot", p.lower())
        self.assertIn("update_goal", p)
        self.assertIn("Do NOT use", p)
        self.assertIn("get_window_summary", p)
        self.assertIn("attached context", p.lower())
        merged = self.qa.build_system_prompt({"system_prompt": "Be brief."})
        self.assertIn("Be brief", merged)
        self.assertIn("quick_done", merged)

    def test_default_quick_tools_exclude_window_mcp(self):
        tools = self.qa.DEFAULT_QUICK_ALLOWED_TOOLS
        self.assertIn("Read", tools)
        self.assertIn("mcp__sublime__quick_done", tools)
        joined = " ".join(tools)
        self.assertNotIn("get_window_summary", joined)
        self.assertNotIn("spawn_session", joined)

    def test_stop_session_bridge(self):
        class FC:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        class FS:
            def __init__(self):
                self.client = FC()
                self.initialized = True
                self.working = True

        s = FS()
        self.assertTrue(self.qa.stop_session_bridge(s))
        self.assertIsNone(s.client)
        self.assertFalse(s.working)


class TestCompleteSlotHandler(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qa = _load_quick()
        cls.Session = type("Session", (), {})

        class Session:
            def __init__(self, *a, **k):
                self.quick_mode = True
                self.client = None
                self.output = None
                self.window = None
                self.working = False
                self.initialized = False
                self.name = ""
                self.draft_prompt = ""
                self.backend = "deepseek"
                self.profile = {}
                self._quick_slot_id = None
                self.context = type("C", (), {
                    "items": [],
                    "_add_path_ref": lambda *a, **k: None,
                })()

            def start(self):
                pass

            def _enter_input_with_draft(self):
                pass

        cls.Session = Session

    def _make_session(self, slot_id, working=True):
        class FC:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True
                self.client_dead = True

            def is_alive(self):
                return not self.stopped

        class FakeTool:
            def __init__(self, name, status="pending"):
                self.name = name
                self.status = status
                self.result = None

        class FakeCurrent:
            def __init__(self):
                self.working = True
                self.events = [
                    FakeTool("mcp__sublime__quick_done", status="pending"),
                ]

        class FakeOutput:
            view = None
            _input_mode = False
            current = None
            _render_pending = False
            rendered = False

            def __init__(self):
                self.current = FakeCurrent()

            def is_input_mode(self):
                return False

            def _do_render(self):
                self.rendered = True

        s = self.Session()
        s.client = FC()
        s.working = working
        s.initialized = True
        s.quick_mode = True
        s.output = FakeOutput()
        s._quick_slot_id = slot_id
        return s

    def _make_host(self, slots_working=None):
        if slots_working is None:
            slots_working = [True, False]

        win_settings = {}

        class WSettings:
            def get(self, k, d=None):
                return win_settings.get(k, d)

            def set(self, k, v):
                win_settings[k] = v

            def erase(self, k):
                win_settings.pop(k, None)

            def has(self, k):
                return k in win_settings

        class FakeWindow:
            def settings(self):
                return WSettings()

            def id(self):
                return 99

            def folders(self):
                return []

            def views(self):
                return []

            def active_view(self):
                return None

            def focus_view(self, v):
                pass

            def new_file(self):
                return None

        host = self.qa.QuickHost(FakeWindow())
        host.view = None
        for i, working in enumerate(slots_working, 1):
            sid = "q%s" % i
            sess = self._make_session(sid, working=working)
            sess.window = host.window
            slot = self.qa.QuickSlot(slot_id=sid, session=sess, name="Q%s" % i)
            host.slots[sid] = slot
            sess._quick_slot_id = sid
        host.active_id = "q1"
        self.qa._hosts[host.window.id()] = host
        return host, win_settings

    def test_complete_slot_stops_bridge_ready_for_next_submit(self):
        host, _ = self._make_host([True])
        slot = host.slots["q1"]
        sess = slot.session
        client = sess.client
        result = host.complete_slot(sess, status="completed", message="done ok")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["slot"], "q1")
        self.assertEqual(result["bridge_stopped"], "deferred")
        self.assertTrue(result.get("ready"))
        self.assertEqual(slot.status, "live")
        self.assertEqual(slot.status_message, "done ok")
        self.assertFalse(sess.working)
        self.assertFalse(getattr(sess, "_quick_finished", True))
        self.assertEqual(sess.output.current.events, [])
        self.assertFalse(sess.output.current.working)
        self.assertTrue(sess.output.rendered)
        self.assertIsNotNone(sess.client)
        self.assertFalse(client.stopped)
        self.qa.stop_session_bridge(sess)
        self.assertIsNone(sess.client)
        self.assertTrue(client.stopped)
        self.assertFalse(host.session_is_runnable(sess))

    def test_complete_slot_blocked_still_live(self):
        host, _ = self._make_host([True])
        sess = host.slots["q1"].session
        client = sess.client
        result = host.complete_slot(sess, status="blocked", message="need human")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(host.slots["q1"].status, "live")
        self.assertIsNotNone(sess.client)
        self.assertFalse(client.stopped)

    def test_complete_slot_closed_defers_dismiss(self):
        host, _ = self._make_host([True])
        slot = host.slots["q1"]
        sess = slot.session
        client = sess.client
        result = host.complete_slot(sess, status="closed", message="bye")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "closed")
        self.assertTrue(result.get("closed"))
        self.assertFalse(result.get("ready"))
        self.assertEqual(result["bridge_stopped"], "deferred")
        self.assertEqual(slot.status, "closed")
        self.assertTrue(getattr(sess, "_quick_finished", False))
        self.assertIsNotNone(sess.client)
        self.assertFalse(client.stopped)
        self.qa.stop_session_bridge(sess)
        host.close_slot("q1")
        self.assertNotIn("q1", host.slots)

    def test_complete_quick_from_tool_uses_executing_slot_not_active(self):
        host, win_settings = self._make_host([False, True])
        host.active_id = "q1"
        host.slots["q1"].session.working = False
        host.slots["q2"].session.working = True
        win_settings["submarine_executing_quick_slot"] = "q2"

        class V:
            def id(self):
                return 1001

            def is_valid(self):
                return True

        host.view = V()
        result = self.qa.complete_quick_from_tool(
            status="completed",
            message="bg done",
            view_id=1001,
        )
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("slot"), "q2")
        self.assertEqual(host.slots["q2"].status, "live")
        self.assertEqual(host.slots["q1"].status, "live")
        self.assertIsNotNone(host.slots["q1"].session.client)
        self.assertIsNotNone(host.slots["q2"].session.client)

    def test_resolve_prefers_sole_working_slot(self):
        host, win_settings = self._make_host([False, True])
        host.active_id = "q1"

        class V:
            def id(self):
                return 2002

            def is_valid(self):
                return True

        host.view = V()
        h, sess = self.qa.resolve_quick_session_for_tool(view_id=2002)
        self.assertIs(h, host)
        self.assertIs(sess, host.slots["q2"].session)

    def tearDown(self):
        self.qa._hosts.clear()
        reset_settings()


class TestToolWiringInRepo(unittest.TestCase):
    def test_quick_done_in_mcp_server_tools_list(self):
        src = _read("mcp/tools.py")
        self.assertIn('"quick_done"', src)
        self.assertIn("quick_done", src)
        catalog = _read("mcp/server.py")
        self.assertIn("list_tool_descriptors", catalog)

    def test_quick_done_in_tool_router(self):
        src = _read("mcp/tools.py")
        self.assertIn("quick_done", src)
        self.assertIn("_quick_done_codegen", src)

    def test_quick_done_handler_in_mcp_server(self):
        src = _read("mcp/socket_server.py")
        self.assertIn("def _quick_done", src)
        self.assertIn("complete_quick_from_tool", src)

    def test_slot_id_assigned_in_source(self):
        src = _read("features/quick.py")
        self.assertIn("s._quick_slot_id = sid", src)
        self.assertIn("submarine_executing_quick_slot", src)
        self.assertIn("resolve_quick_session_for_tool", src)

    def test_submit_creates_session_model_in_source(self):
        src = _read("features/quick.py")
        self.assertIn("def submit_prompt", src)
        self.assertIn("_start_session_with_prompt", src)
        self.assertIn("_quick_pending_prompt", src)
        self.assertIn("_finalize_quick_done_tools", src)
        self.assertIn('status == "closed"', src)
        self.assertIn("user_wants_close", src)
        out = _read("ui/tools.py")
        self.assertIn("is_host_control_tool", out)

    def test_host_max_enforced_in_source(self):
        src = _read("features/quick.py")
        self.assertIn("MAX_QUICK_SLOTS = 3", src)
        self.assertIn("at most", src)

    def test_view_keys_renamed(self):
        src = _read("features/quick.py")
        self.assertIn("submarine_quick", src)
        self.assertIn("submarine_quick_host", src)
        self.assertNotIn('"claude_quick"', src)


class TestGoalPersistenceSchema(unittest.TestCase):
    def test_session_record_goal_key_roundtrip(self):
        from core.records import SessionRecord
        rec = SessionRecord(session_id="s1", name="n", goal={"goal_id": "g1", "status": "active"})
        d = rec.to_dict()
        self.assertEqual(d["goal"]["goal_id"], "g1")
        back = SessionRecord.from_dict(d)
        self.assertEqual(back.goal["goal_id"], "g1")

    def test_old_record_without_goal_loads(self):
        from core.records import SessionRecord
        back = SessionRecord.from_dict({"session_id": "s2", "name": "old"})
        self.assertIsNone(back.goal)

    def test_save_session_writes_goal(self):
        from tests.fakes import make_session
        from features.goals.tracker import GoalTracker
        from features.goals.plan import sample_concrete_plan
        import tempfile
        td = tempfile.mkdtemp()
        s = make_session(initialized=True)
        s.session_id = "persist-1"
        s.query_count = 1
        s.store.path = os.path.join(td, ".sessions.json")
        s.goal_tracker = GoalTracker()
        s.goal_tracker.create("ship persist")
        s.goal_tracker.accept_plan(
            sample_concrete_plan("ship persist", s.goal_tracker.goal_id, checklist_done=True)
        )
        s._save_session()
        entry = s.store.find("persist-1")
        self.assertIsNotNone(entry)
        self.assertIn("goal", entry)
        self.assertEqual(entry["goal"]["objective"], "ship persist")
        from features.goals.tracker import GoalTracker as GT
        restored = GT.from_json(entry["goal"])
        self.assertEqual(restored.status, "user_paused")


class TestHostScheduler(unittest.TestCase):
    def test_min_60s_and_one_per_session(self):
        from features import scheduler as sch
        from tests.fakes import make_session
        sch._TABLE["wakes"].clear()
        sch._TABLE["by_session"].clear()
        s = make_session(initialized=True, client=type("C", (), {"is_alive": lambda self: True})())
        s.session_id = "sched-1"
        tid1 = sch.set_timer(10, "wake up", s)
        self.assertGreaterEqual(sch.pending_for_session(s)["fire_at"] - __import__("time").time(), 59)
        tid2 = sch.set_timer(90, "second", s)
        self.assertNotEqual(tid1, tid2)
        self.assertIsNone(sch._TABLE["wakes"].get(tid1))
        self.assertEqual(sch.pending_for_session(s)["timer_id"], tid2)
        n = sch.cancel_timer(s)
        self.assertGreaterEqual(n, 1)
        self.assertIsNone(sch.pending_for_session(s))

    def test_defers_while_working(self):
        from features import scheduler as sch
        from tests.fakes import FakeClient, make_session
        sch._TABLE["wakes"].clear()
        sch._TABLE["by_session"].clear()
        client = FakeClient()
        s = make_session(initialized=True, client=client)
        s.session_id = "sched-2"
        s.turn.begin_query()
        self.assertTrue(s.working)
        tid = sch.set_timer(60, "later", s)
        rec = sch._TABLE["wakes"][tid]
        sch._on_due(s, tid, rec["gen"])
        # still pending — deferred
        self.assertIn(tid, sch._TABLE["wakes"])
        s.turn.end_live()
        sch._on_due(s, tid, rec["gen"])
        self.assertNotIn(tid, sch._TABLE["wakes"])
        methods = [m for m, _p, _c in client.sent]
        self.assertIn("query", methods)


if __name__ == "__main__":
    unittest.main()
