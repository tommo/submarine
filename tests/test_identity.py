"""agent_id is the only session handle — registry, init params, placement."""
from __future__ import annotations

import unittest

from core.placement import last_session_group, remember_active_session
from core.registry import SessionRegistry
from tests.fakes import make_session


class _Settings(dict):
    def get(self, k, d=None):
        return super().get(k, d)

    def set(self, k, v):
        self[k] = v


class _View:
    def __init__(self, vid, agent_stamp=None):
        self._id = vid
        data = {"submarine_output": True}
        if agent_stamp:
            data["submarine_agent_id"] = agent_stamp
        self._settings = _Settings(data)

    def id(self):
        return self._id

    def is_valid(self):
        return True

    def settings(self):
        return self._settings


class _Output:
    def __init__(self, view):
        self.view = view
        self._input_mode = True


class _Window:
    def __init__(self, views=None):
        self._settings = _Settings()
        self._views = list(views or [])
        self._groups = {}

    def settings(self):
        return self._settings

    def views(self):
        return list(self._views)

    def get_view_index(self, view):
        return (0, 0)

    def num_groups(self):
        return 1


class _Live:
    def __init__(self, view, aid):
        self.agent_id = aid
        self.output = _Output(view)
        self.backgrounded = False
        self.client = object()
        self.initialized = True
        self.working = False
        self.quick_mode = False
        self.is_sleeping = False
        self.window = None
        self.session_id = "sess-" + aid

    def reset_phantoms_for_new_view(self):
        pass


class TestRegistryBindUnbind(unittest.TestCase):
    def test_bind_evicts_previous_occupant(self):
        reg = SessionRegistry()
        v = _View(7)
        a = _Live(v, "agent-aaa")
        b = _Live(v, "agent-bbb")
        reg.register(a)
        reg.register(b)
        reg.bind("agent-aaa", 7)
        self.assertIs(reg.for_view_id(7), a)
        self.assertIs(reg.for_view(v), a)
        self.assertFalse(a.backgrounded)
        reg.bind("agent-bbb", 7)
        self.assertIs(reg.for_view(v), b)
        self.assertIs(reg.for_view_id(7), b)
        self.assertTrue(a.backgrounded)
        self.assertFalse(b.backgrounded)
        self.assertIs(reg.by_agent_id("agent-aaa"), a)
        self.assertIn("agent-aaa", reg.background)
        self.assertNotIn("agent-bbb", reg.background)

    def test_unbind_makes_background(self):
        reg = SessionRegistry()
        v = _View(3)
        s = _Live(v, "agent-ccc")
        reg.register_session(s)
        self.assertIs(reg.sessions[3], s)
        self.assertNotIn("agent-ccc", reg.background)
        reg.unbind("agent-ccc")
        self.assertNotIn(3, reg.binding)
        self.assertNotIn(3, reg.sessions)
        self.assertIs(reg.background["agent-ccc"], s)
        self.assertTrue(s.backgrounded)
        self.assertIs(reg.by_agent_id("agent-ccc"), s)

    def test_for_view_follows_rebind(self):
        reg = SessionRegistry()
        v = _View(11)
        first = _Live(v, "agent-one")
        second = _Live(v, "agent-two")
        reg.register(first)
        reg.register(second)
        reg.bind("agent-one", 11)
        self.assertIs(reg.for_view(v), first)
        reg.bind("agent-two", 11)
        self.assertIs(reg.for_view(v), second)
        self.assertIs(reg.get_session_for_view_id(11), second)


class TestBackgroundPredicate(unittest.TestCase):
    def test_background_is_unbound_live(self):
        reg = SessionRegistry()
        bound = _Live(_View(1), "agent-bound")
        loose = _Live(_View(2), "agent-loose")
        reg.register(bound)
        reg.register(loose)
        reg.bind("agent-bound", 1)
        bg = reg.background
        self.assertEqual(set(bg), {"agent-loose"})
        self.assertIs(bg["agent-loose"], loose)
        self.assertEqual(len(reg.iter_sessions()), 2)


class TestNoViewIdOnSession(unittest.TestCase):
    def test_session_has_no_view_id_attribute(self):
        s = make_session()
        self.assertFalse(hasattr(s, "view_id"))
        self.assertFalse(hasattr(s, "parent_view_id"))
        self.assertTrue(s.agent_id.startswith("agent-"))


class TestResumeReusesSavedAgentId(unittest.TestCase):
    def test_resume_reuses_store_agent_id(self):
        import os
        import tempfile

        from core.records import SessionStore
        from core.session import Session
        from tests.fakes import DictPersist, FakeChrome, FakeOutput, FakeScheduler

        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            store = SessionStore(path)
            store.upsert({
                "session_id": "sid-saved",
                "agent_id": "agent-from-disk",
                "parent_agent_id": "agent-parent-disk",
                "name": "saved",
                "backend": "claude",
                "state": "sleeping",
                "query_count": 1,
            })
            s2 = Session(
                FakeOutput(), FakeChrome(), FakeScheduler(), DictPersist(),
                store=store, resume_id="sid-saved",
            )
            self.assertEqual(s2.agent_id, "agent-from-disk")
            self.assertEqual(s2.parent_agent_id, "agent-parent-disk")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


class TestResolveRefLegacy(unittest.TestCase):
    def setUp(self):
        import core.registry as regmod
        regmod._legacy_view_ref_logged = False
        self._logs = []

        def _capture(msg):
            self._logs.append(msg)

        self._orig = None
        try:
            import plat.log as logmod
            self._orig = logmod.log_plugin
            logmod.log_plugin = _capture
        except Exception:
            pass

    def tearDown(self):
        if self._orig is not None:
            import plat.log as logmod
            logmod.log_plugin = self._orig

    def test_int_path_logs_once(self):
        import core.registry as regmod
        reg = SessionRegistry()
        v = _View(42)
        s = _Live(v, "agent-legacy")
        reg.register_session(s)
        hit = reg.resolve_ref(42)
        self.assertIs(hit, s)
        hit2 = reg.resolve_ref("42")
        self.assertIs(hit2, s)
        self.assertTrue(regmod._legacy_view_ref_logged)
        self.assertEqual(len(self._logs), 1)
        self.assertIn("deprecated", self._logs[0])
        self.assertIs(reg.resolve_ref("agent-legacy"), s)


class TestPlacementActiveAgent(unittest.TestCase):
    def test_round_trip_writes_agent_not_view(self):
        from core.registry import default_registry
        default_registry.clear()
        v = _View(5, agent_stamp="agent-place")
        s = _Live(v, "agent-place")
        s.window = None
        default_registry.register_session(s)
        win = _Window([v])
        remember_active_session(win, v)
        self.assertEqual(win.settings().get("submarine_active_agent"), "agent-place")
        self.assertIsNone(win.settings().get("submarine_active_view"))
        default_registry.clear()

    def test_old_key_fallback_once(self):
        v = _View(8)
        win = _Window([v])
        win.settings().set("submarine_active_view", 8)
        group = last_session_group(win)
        self.assertEqual(group, 0)


class TestMcpCallerByAgentId(unittest.TestCase):
    def test_background_session_resolves_after_unbind(self):
        """Detached busy session is still found by agent_id, not the host view."""
        reg = SessionRegistry()
        host = _View(1)
        a = _Live(host, "agent-busy")
        b = _Live(host, "agent-shown")
        reg.register(a)
        reg.register(b)
        reg.bind("agent-busy", 1)
        reg.unbind("agent-busy")
        reg.bind("agent-shown", 1)
        self.assertIs(reg.for_view_id(1), b)
        self.assertIs(reg.by_agent_id("agent-busy"), a)
        self.assertIs(reg.resolve_ref("agent-busy"), a)


if __name__ == "__main__":
    unittest.main()
