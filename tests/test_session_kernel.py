"""Kernel invariants: busy closer, gen guard, interrupt queue, sleep, compact."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

from core.records import SessionStore, derive_state
from core.registry import SessionRegistry
from core.session import create_session
from core.turn import COMPACT_TIMEOUT_MS
from tests.fakes import DictPersist, FakeClient, FakeScheduler, make_session


class TestBusyOnlyWithCloser(unittest.TestCase):
    def test_construct_is_idle(self):
        s = make_session()
        self.assertFalse(s.working)
        self.assertEqual(s.turn.kind, "idle")

    def test_working_is_turn_property(self):
        s = make_session(initialized=True, client=FakeClient())
        self.assertFalse(s.working)
        s.turn.begin_query()
        self.assertTrue(s.working)
        s.turn.end_live()
        self.assertFalse(s.working)

    def test_query_owns_busy_until_done(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client)
        s.query("hello")
        self.assertTrue(s.working)
        self.assertEqual(s.turn.kind, "live")
        methods = [m for m, _p, _c in client.sent]
        self.assertIn("query", methods)
        _m, _p, cb = [t for t in client.sent if t[0] == "query"][0]
        cb({"status": "complete"})
        self.assertFalse(s.working)

    def test_adopt_is_noop(self):
        s = make_session()
        s._adopt_agent_turn("⚙ task completed")
        self.assertFalse(s.working)
        self.assertEqual(s.turn.kind, "idle")


class TestQueryGenGuard(unittest.TestCase):
    def test_stale_done_does_not_kill_new_turn(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client)
        s.query("first")
        first_cb = [t[2] for t in client.sent if t[0] == "query"][-1]
        g1 = s.turn.gen
        s.query("second")
        g2 = s.turn.gen
        self.assertNotEqual(g1, g2)
        first_cb({"status": "interrupted"})
        self.assertTrue(s.working)
        self.assertEqual(s.turn.kind, "live")
        self.assertEqual(s.turn.gen, g2)


class TestInterruptKeepsQueue(unittest.TestCase):
    def test_interrupt_keeps_user_queue(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client)
        s.query("live")
        s.queue_prompt("follow-up")
        self.assertIn("follow-up", s._queued_prompts)
        s.interrupt()
        self.assertIn("follow-up", s._queued_prompts)
        self.assertEqual(s.turn.kind, "interrupting")
        self.assertTrue(s.working)
        methods = [m for m, _p, _c in client.sent]
        self.assertIn("interrupt", methods)

    def test_no_new_query_until_cancel_settle(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client)
        s.query("live")
        s.queue_prompt("next")
        queries_before = sum(1 for m, _p, _c in client.sent if m == "query")
        s.interrupt()
        queries_after = sum(1 for m, _p, _c in client.sent if m == "query")
        self.assertEqual(queries_before, queries_after)
        # ACK settles, then queue fires
        s._on_done({"status": "interrupted"}, _expected_gen=s.turn.gen)
        queries_final = sum(1 for m, _p, _c in client.sent if m == "query")
        self.assertEqual(queries_final, queries_before + 1)
        self.assertEqual(s._queued_prompts, [])


class TestKimiCompactCloser(unittest.TestCase):
    def test_end_turn_does_not_close_kimi_compact(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="kimi")
        s.query("/compact")
        self.assertTrue(s._compacting)
        cb = [t[2] for t in client.sent if t[0] == "query"][-1]
        cb({"status": "complete"})
        self.assertEqual(s.turn.kind, "compacting")
        self.assertTrue(s.working)

    def test_compact_done_text_closes(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="kimi")
        s.query("/compact")
        cb = [t[2] for t in client.sent if t[0] == "query"][-1]
        cb({"status": "complete"})
        s.events.dispatch("message", {
            "type": "text",
            "text": "Compaction completed. 12 messages compacted.",
        })
        self.assertFalse(s._compacting)
        self.assertFalse(s.working)
        self.assertEqual(s.turn.kind, "idle")

    def test_180s_timeout_closes(self):
        client = FakeClient()
        sched = FakeScheduler()
        s = make_session(
            initialized=True, client=client, backend="kimi", scheduler=sched)
        s.query("/compact")
        cb = [t[2] for t in client.sent if t[0] == "query"][-1]
        cb({"status": "complete"})
        self.assertEqual(s.turn.kind, "compacting")
        armed = [ms for ms, _fn, _t in sched.pending if ms == COMPACT_TIMEOUT_MS]
        self.assertTrue(armed)
        sched.fire_due(COMPACT_TIMEOUT_MS)
        self.assertFalse(s.working)
        self.assertEqual(s.turn.kind, "idle")


class TestDerivedSleep(unittest.TestCase):
    def test_restore_constructs_sleep_triple(self):
        s = make_session(resume_id="sess-abc")
        self.assertEqual(s.session_id, "sess-abc")
        self.assertIsNone(s.client)
        self.assertFalse(s.initialized)
        self.assertTrue(s.is_sleeping)

    def test_fork_does_not_prefill_session_id(self):
        s = make_session(resume_id="sess-abc", fork=True)
        self.assertIsNone(s.session_id)
        self.assertFalse(s.is_sleeping)

    def test_derive_state_rule(self):
        self.assertEqual(derive_state(object(), True, "s"), "open")
        self.assertEqual(derive_state(None, False, "s"), "sleeping")
        self.assertEqual(derive_state(None, False, None), "closed")

    def test_sleep_sets_triple(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client)
        s.session_id = "sess-z"
        self.assertTrue(s.sleep())
        self.assertIsNone(s.client)
        self.assertFalse(s.initialized)
        self.assertTrue(s.is_sleeping)


class TestOneLiveSession(unittest.TestCase):
    def test_create_session_dedupes_live_resume(self):
        from tests.fakes import FakeChrome, FakeOutput, FakeScheduler

        reg = SessionRegistry()
        first = make_session(
            registry=reg, resume_id="sid-1", initialized=True)
        first.session_id = "sid-1"
        first.client = FakeClient()
        first.initialized = True
        reg.register(first)
        again = create_session(
            FakeOutput(), FakeChrome(), FakeScheduler(), DictPersist(),
            registry=reg, resume_id="sid-1")
        self.assertIs(again, first)

    def test_fork_makes_a_new_session(self):
        from tests.fakes import FakeChrome, FakeOutput, FakeScheduler

        reg = SessionRegistry()
        first = make_session(registry=reg, resume_id="sid-1")
        first.session_id = "sid-1"
        first.initialized = True
        first.client = FakeClient()
        reg.register(first)
        forked = create_session(
            FakeOutput(), FakeChrome(), FakeScheduler(), DictPersist(),
            registry=reg, resume_id="sid-1", fork=True)
        self.assertIsNot(forked, first)
        self.assertIsNone(forked.session_id)


class TestLeftoverSynthBash(unittest.TestCase):
    def test_synth_bash_after_done_stays_idle(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="kimi")
        s.query("go")
        cb = [t[2] for t in client.sent if t[0] == "query"][-1]
        cb({"status": "complete"})
        self.assertFalse(s.working)
        s.events.dispatch("message", {
            "type": "tool_use",
            "name": "Bash",
            "id": "t1",
            "input": {"command": "tail log"},
            "background": False,
        })
        self.assertFalse(s.working)
        self.assertEqual(s.turn.kind, "idle")
        self.assertTrue(s.output.tools)


class TestInitializeParams(unittest.TestCase):
    def test_default_permission_mode_empties_allowed_tools(self):
        client = FakeClient()
        s = make_session(
            client=client,
            settings={"permission_mode": "default", "allowed_tools": ["Bash"]},
            cwd="/tmp/proj",
        )
        s.start()
        inits = [t for t in client.sent if t[0] == "initialize"]
        self.assertTrue(inits)
        params = inits[0][1]
        self.assertEqual(params["allowed_tools"], [])
        self.assertEqual(params["permission_mode"], "default")
        self.assertEqual(params["cwd"], "/tmp/proj")
        self.assertIn("mcp_enable_read_image", params)

    def test_resume_and_fork_flags(self):
        client = FakeClient()
        s = make_session(
            client=client,
            resume_id="sid-9",
            fork=True,
            cwd="/tmp/proj",
        )
        s.start()
        params = [t[1] for t in client.sent if t[0] == "initialize"][0]
        self.assertEqual(params["resume"], "sid-9")
        self.assertTrue(params["fork_session"])

    def test_new_session_ignores_view_model_stamp(self):
        client = FakeClient()
        persist = DictPersist({"submarine_model": "deepseek-v4-pro"})
        s = make_session(
            client=client,
            persist=persist,
            settings={"default_model": "grok-4.6"},
            backend="grok",
            cwd="/tmp/proj",
        )
        s.start()
        params = [t[1] for t in client.sent if t[0] == "initialize"][0]
        self.assertEqual(params.get("model"), "grok-4.6")

    def test_init_params_carry_agent_id_not_view_id(self):
        client = FakeClient()
        s = make_session(
            client=client,
            cwd="/tmp/proj",
            initial_context={"parent_agent_id": "agent-parent"},
        )
        s.start()
        params = [t[1] for t in client.sent if t[0] == "initialize"][0]
        self.assertEqual(params["agent_id"], s.agent_id)
        self.assertTrue(str(s.agent_id).startswith("agent-"))
        self.assertEqual(params["parent_agent_id"], "agent-parent")
        self.assertNotIn("view_id", params)
        self.assertNotIn("parent_view_id", params)
        self.assertFalse(hasattr(s, "view_id"))
        self.assertFalse(hasattr(s, "parent_view_id"))


class TestSessionStore(unittest.TestCase):
    def test_mru_cap_and_schema(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            store = SessionStore(path)
            store.upsert({
                "session_id": "a",
                "name": "one",
                "backend": "claude",
                "state": "open",
            })
            store.upsert({
                "session_id": "b",
                "name": "two",
                "backend": "kimi",
                "state": "sleeping",
            })
            store.upsert({
                "session_id": "a",
                "name": "one-renamed",
                "backend": "claude",
                "state": "open",
            })
            rows = store.load()
            self.assertEqual(rows[0]["session_id"], "a")
            self.assertEqual(rows[0]["name"], "one-renamed")
            self.assertEqual(len(rows), 2)
        finally:
            os.remove(path)


class TestNoSublimeImport(unittest.TestCase):
    def test_core_modules_import_without_sublime(self):
        import core.background  # noqa: F401
        import core.events  # noqa: F401
        import core.placement  # noqa: F401
        import core.records  # noqa: F401
        import core.registry  # noqa: F401
        import core.rewind  # noqa: F401
        import core.session  # noqa: F401
        import core.turn  # noqa: F401
        for name in (
            "core.background", "core.events", "core.placement",
            "core.records", "core.registry", "core.rewind",
            "core.session", "core.turn",
        ):
            self.assertIsNone(
                getattr(sys.modules[name], "sublime", None),
                "%s must not bind sublime" % name,
            )


if __name__ == "__main__":
    unittest.main()
