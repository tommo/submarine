"""Parent links must survive host swaps and restarts.

Regression: in single mode every session stamps identity onto the shared
host view, and `stamp_identity` skipped None, so a child's parent_agent_id
stayed on the view after the parent was bound. Restore read it back and
the parent became its own parent while the child, restored viewless, lost
the link — and plugin_unloaded wrote both to .sessions.json.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from core.records import SessionStore, STAMP_PARENT_AGENT_ID, stamp_identity
from core.registry import default_registry
from tests.fakes import DictPersist, make_session
from tests.test_single_view import RecordingWindow, _SingleViewCase, _session
from ui import keys
from ui.host import HostView


class TestStampIdentityErasesNone(unittest.TestCase):
    def test_none_clears_a_previous_value(self):
        p = DictPersist({STAMP_PARENT_AGENT_ID: "agent-child-parent"})
        stamp_identity(p, **{STAMP_PARENT_AGENT_ID: None})
        self.assertIsNone(p.read(STAMP_PARENT_AGENT_ID))

    def test_value_still_written(self):
        p = DictPersist()
        stamp_identity(p, **{STAMP_PARENT_AGENT_ID: "agent-p"})
        self.assertEqual(p.read(STAMP_PARENT_AGENT_ID), "agent-p")


class _ViewPersist(object):
    """PersistPort over whatever view the output is bound to right now."""

    def __init__(self, output):
        self.output = output

    def _st(self):
        v = self.output.view
        return v.settings() if v is not None else None

    def stamp(self, key, value):
        st = self._st()
        if st is not None:
            keys.write_setting(st, key, value)

    def read(self, key):
        st = self._st()
        return keys.read_setting(st, key) if st is not None else None

    def clear(self, key):
        st = self._st()
        if st is not None:
            keys.erase_setting(st, key)


class TestHostSwapDoesNotLeakParentStamp(_SingleViewCase):
    def test_parent_bound_after_child_leaves_no_parent_stamp(self):
        win = RecordingWindow()
        parent = _session(win, "parent")
        child = _session(win, "child")
        for s in (parent, child):
            s.persist = _ViewPersist(s.output)
        child.parent_agent_id = parent.agent_id
        hv = HostView.for_window(win)
        hv.attach(win, child)
        host = hv.host_view(win)
        self.assertEqual(
            keys.read_setting(host.settings(), keys.PARENT_AGENT_ID), parent.agent_id)
        hv.attach(win, parent)
        self.assertIsNone(
            keys.read_setting(host.settings(), keys.PARENT_AGENT_ID),
            "child's parent_agent_id stamp survived onto the parent's turn on the host")


class TestSaveSessionParentLink(unittest.TestCase):
    def _store(self, row):
        d = tempfile.mkdtemp(prefix="submarine-test-")
        path = os.path.join(d, ".sessions.json")
        with open(path, "w") as f:
            json.dump([row], f)
        return SessionStore(path)

    def test_self_link_is_never_saved(self):
        store = self._store({"session_id": "s-root", "agent_id": "agent-root",
                             "query_count": 3})
        path = store.path  # make_session redirects the store; point it back
        s = make_session(resume_id="s-root", store=store)
        s.store.path = path
        s.query_count = 3
        s.parent_agent_id = s.agent_id  # what a stale host stamp produced
        s._save_session()
        self.assertNotIn("parent_agent_id", store.find("s-root"))
        self.assertIsNone(s.parent_agent_id)

    def test_saved_parent_link_survives_a_live_session_without_one(self):
        store = self._store({"session_id": "s-kid", "agent_id": "agent-kid",
                             "parent_agent_id": "agent-root",
                             "parent_session_id": "s-root", "query_count": 1})
        path = store.path
        s = make_session(resume_id="s-kid", store=store)
        s.store.path = path
        s.query_count = 1
        s.parent_agent_id = None  # restored viewless before the link loaded
        s.parent_session_id = None
        s._save_session()
        saved = store.find("s-kid")
        self.assertEqual(saved.get("parent_agent_id"), "agent-root")
        self.assertEqual(saved.get("parent_session_id"), "s-root")

    def test_hydrate_ignores_self_link(self):
        from ui.listeners import _hydrate_session_from_saved
        s = make_session()
        _hydrate_session_from_saved(s, {"session_id": "x", "agent_id": "agent-x",
                                        "parent_agent_id": "agent-x"})
        self.assertIsNone(s.parent_agent_id)


if __name__ == "__main__":
    unittest.main()
