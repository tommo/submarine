"""Upstream 77f2dec + 2e6e415 theme D: parent/child continuity, starred cap.

Identity law: the link is agent_id (plus aliases) and session_id. view_id is a
runtime display cache and never the owner.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

from core import registry as reg
from core.records import (
    SESSIONS_CAP,
    SessionStore,
    cap_saved_sessions,
    load_bookmark_records,
    load_bookmarks,
    remember_bookmark_record,
    starred_ids_for_projects,
    toggle_bookmark,
)
from core.registry import SessionRegistry
from tests.fakes import make_session


class _Isolated(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="submarine-star-")
        self.home = self._td.name
        self._old = os.environ.get("HOME")
        os.environ["HOME"] = self.home

    def tearDown(self):
        if self._old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old
        self._td.cleanup()


# ── registry continuity ───────────────────────────────────────────────────────

class TestIdentityHelpers(unittest.TestCase):
    def test_remember_agent_alias_keeps_old_id(self):
        s = make_session()
        s.agent_id = "agent-new"
        reg.remember_agent_alias(s, "agent-old")
        self.assertIn("agent-old", s.agent_id_aliases)
        reg.remember_agent_alias(s, "agent-old")
        self.assertEqual(s.agent_id_aliases.count("agent-old"), 1)
        # The live id is not an alias of itself.
        reg.remember_agent_alias(s, "agent-new")
        self.assertNotIn("agent-new", s.agent_id_aliases)

    def test_note_child_dedupes(self):
        s = make_session()
        reg.note_child(s, "agent-kid")
        reg.note_child(s, "agent-kid")
        reg.note_child(s, None)
        self.assertEqual(s.child_agent_ids, ["agent-kid"])

    def test_identity_from_saved_entry(self):
        out = reg.identity_from_saved_entry({
            "agent_id": "agent-a",
            "subsession_id": "agent-a",
            "parent_agent_id": "ignored",
            "agent_id_aliases": ["agent-old"],
            "child_agent_ids": ["agent-kid"],
        })
        self.assertEqual(out["agent_id"], "agent-a")
        self.assertEqual(out["agent_id_aliases"], ["agent-old"])
        self.assertEqual(out["child_agent_ids"], ["agent-kid"])
        self.assertNotIn("parent_agent_id", out)

    def test_parent_match_keys_unions_aliases_and_children(self):
        p = make_session()
        p.agent_id = "agent-p"
        p.session_id = "sess-p"
        p.agent_id_aliases = ["agent-old"]
        reg.note_child(p, "agent-kid")
        aids, _vids, sids, kids = reg.parent_match_keys(p)
        self.assertEqual(aids, {"agent-p", "agent-old"})
        self.assertEqual(sids, {"sess-p"})
        self.assertEqual(kids, ["agent-kid"])


class TestListChildrenOf(unittest.TestCase):
    def setUp(self):
        # make_session() uses default_registry; isolate it per test.
        from core.registry import default_registry
        default_registry.clear()
        self.r = default_registry

    def tearDown(self):
        self.r.clear()

    def _pair(self):
        parent = make_session()
        parent.agent_id = "agent-parent"
        parent.session_id = "sess-parent"
        child = make_session()
        child.agent_id = "agent-child"
        child.parent_agent_id = "agent-parent"
        self.r.register(parent)
        self.r.register(child)
        return parent, child

    def test_finds_child_by_parent_agent_id(self):
        parent, child = self._pair()
        self.assertEqual(self.r.list_children_of(
            parent_agent_id="agent-parent"), [child])

    def test_finds_child_through_a_rotated_parent_id(self):
        """Parent sheet recreated → new agent_id; child still stamps the old."""
        parent, child = self._pair()
        old = parent.agent_id
        parent.agent_id = "agent-parent-v2"
        reg.remember_agent_alias(parent, old)
        self.r.register(parent)
        found = self.r.list_children_of(
            parent_agent_id="agent-parent-v2", parent=parent)
        self.assertEqual(found, [child])
        # And relink rewrote the child's stamp.
        self.assertEqual(child.parent_agent_id, "agent-parent-v2")

    def test_finds_child_by_parent_session_id(self):
        parent, child = self._pair()
        child.parent_agent_id = None
        child.parent_session_id = "sess-parent"
        self.assertEqual(self.r.list_children_of(parent=parent), [child])

    def test_noted_children_win_even_without_a_link(self):
        parent = make_session()
        parent.agent_id = "agent-parent"
        orphan = make_session()
        orphan.agent_id = "agent-noted"
        self.r.register(parent)
        self.r.register(orphan)
        reg.note_child(parent, "agent-noted")
        self.assertIn(orphan, self.r.list_children_of(parent=parent))

    def test_parent_is_never_its_own_child(self):
        parent, _child = self._pair()
        found = self.r.list_children_of(parent_agent_id="agent-parent")
        self.assertNotIn(parent, found)

    def test_relink_records_the_child_and_the_alias(self):
        parent = make_session()
        parent.agent_id = "agent-p2"
        child = make_session()
        child.agent_id = "agent-c"
        child.parent_agent_id = "agent-p1"
        reg.relink_child_to_parent(child, parent)
        self.assertEqual(child.parent_agent_id, "agent-p2")
        self.assertIn("agent-p1", parent.agent_id_aliases)
        self.assertIn("agent-c", parent.child_agent_ids)

    def test_rotation_keeps_the_old_id_as_an_alias(self):
        s = make_session()
        s.agent_id = "agent-v1"
        self.r.register(s)
        s.agent_id = "agent-v2"
        self.r.register(s)
        self.assertIn("agent-v1", s.agent_id_aliases)
        self.assertIs(self.r.by_agent_id("agent-v2"), s)


class TestHarvestMentionedOrphans(unittest.TestCase):
    def setUp(self):
        from core.registry import default_registry
        default_registry.clear()
        self.r = default_registry

    def tearDown(self):
        self.r.clear()

    def test_recovers_a_child_of_a_dead_parent(self):
        parent = make_session()
        parent.agent_id = "agent-parent"
        orphan = make_session()
        orphan.agent_id = "agent-orphan123456"
        orphan.parent_agent_id = "agent-gone"
        self.r.register(parent)
        self.r.register(orphan)
        text = "spawned agent-orphan123456 to explore the repo"
        found = reg.harvest_mentioned_orphans(parent, text)
        self.assertEqual(found, [orphan])

    def test_ignores_self_and_parentless_sessions(self):
        parent = make_session()
        parent.agent_id = "agent-parent"
        parentless = make_session()
        parentless.agent_id = "agent-parentless999"
        ours = make_session()
        ours.agent_id = "agent-ours777"
        ours.parent_agent_id = "agent-parent"
        self.r.register(parent)
        self.r.register(parentless)
        self.r.register(ours)
        text = "agent-parent agent-parentless999 agent-ours777"
        found = reg.harvest_mentioned_orphans(parent, text)
        self.assertNotIn(parent, found)
        self.assertNotIn(parentless, found)
        # A child that is already linked to us is still one of ours.
        self.assertIn(ours, found)

    def test_only_ids_in_the_text_are_harvested(self):
        parent = make_session()
        parent.agent_id = "agent-parent"
        dead_parent_child = make_session()
        dead_parent_child.agent_id = "agent-unmentioned555"
        dead_parent_child.parent_agent_id = "agent-gone"
        self.r.register(parent)
        self.r.register(dead_parent_child)
        self.assertEqual(reg.harvest_mentioned_orphans(parent, "nothing"), [])

    def test_empty_text_is_empty(self):
        parent = make_session()
        self.assertEqual(reg.harvest_mentioned_orphans(parent, ""), [])


class TestSessionPersistence(unittest.TestCase):
    def test_new_identity_fields_round_trip(self):
        s = make_session(initialized=True)
        s.session_id = "sess-1"
        s.agent_id = "agent-a"
        s.parent_session_id = "sess-p"
        s.child_agent_ids = ["agent-kid"]
        s.agent_id_aliases = ["agent-old"]
        s.query_count = 3
        s._save_session()
        entry = s.store.find("sess-1")
        self.assertEqual(entry["parent_session_id"], "sess-p")
        self.assertEqual(entry["child_agent_ids"], ["agent-kid"])
        self.assertEqual(entry["agent_id_aliases"], ["agent-old"])

    def test_resume_restores_aliases_and_children(self):
        from core.session import Session
        from tests.fakes import DictPersist, FakeChrome, FakeScheduler
        import tempfile
        path = os.path.join(tempfile.mkdtemp(prefix="submarine-res-"),
                            ".sessions.json")
        store = SessionStore(path)
        store.save([{
            "session_id": "sess-r",
            "agent_id": "agent-before",
            "agent_id_aliases": ["agent-older"],
            "child_agent_ids": ["agent-kid"],
            "parent_session_id": "sess-p",
            "last_activity": 5.0,
        }])
        s = Session(
            None, FakeChrome(), FakeScheduler(), DictPersist(),
            registry=SessionRegistry(), store=store,
            resume_id="sess-r", backend="grok",
        )
        self.assertEqual(s.agent_id, "agent-before")
        self.assertEqual(s.agent_id_aliases, ["agent-older"])
        self.assertEqual(s.child_agent_ids, ["agent-kid"])
        self.assertEqual(s.parent_session_id, "sess-p")


# ── starred rows survive the cap ──────────────────────────────────────────────

class TestCapSavedSessions(unittest.TestCase):
    def _rows(self, n):
        return [{"session_id": "s%d" % i, "last_activity": float(n - i)}
                for i in range(n)]

    def test_keeps_newest_cap(self):
        rows = self._rows(10)
        kept = cap_saved_sessions(rows, set(), cap=3)
        self.assertEqual([r["session_id"] for r in kept], ["s0", "s1", "s2"])

    def test_keeps_starred_past_the_cap(self):
        rows = self._rows(10)
        kept = cap_saved_sessions(rows, {"s9"}, cap=3)
        ids = [r["session_id"] for r in kept]
        self.assertEqual(ids[:3], ["s0", "s1", "s2"])
        self.assertIn("s9", ids)

    def test_dedupes_and_skips_junk(self):
        rows = [{"session_id": "a"}, {"session_id": "a"}, {}, None, "x"]
        kept = cap_saved_sessions(rows, set(), cap=5)
        self.assertEqual([r["session_id"] for r in kept], ["a"])

    def test_bad_cap_falls_back(self):
        rows = self._rows(3)
        self.assertEqual(len(cap_saved_sessions(rows, set(), cap="x")), 3)
        self.assertEqual(len(cap_saved_sessions(rows, set(), cap=0)), 3)


class TestBookmarkRecords(_Isolated):
    def test_toggle_stores_and_drops_the_record(self):
        now = toggle_bookmark("s1", None, record={
            "name": "auth work", "backend": "grok", "model": "grok-4.6"})
        self.assertTrue(now)
        recs = load_bookmark_records(None)
        self.assertEqual(recs["s1"]["name"], "auth work")
        self.assertIn("s1", load_bookmarks(None))
        toggle_bookmark("s1", None)
        self.assertNotIn("s1", load_bookmark_records(None))

    def test_remember_only_for_starred(self):
        remember_bookmark_record("s-unstarred", {"name": "n"}, None)
        self.assertEqual(load_bookmark_records(None), {})
        toggle_bookmark("s2", None)
        remember_bookmark_record("s2", {"name": "kept"}, None)
        self.assertEqual(load_bookmark_records(None)["s2"]["name"], "kept")

    def test_starred_ids_union_global_and_project(self):
        with tempfile.TemporaryDirectory() as proj:
            toggle_bookmark("global-1", None)
            toggle_bookmark("local-1", proj)
            ids = starred_ids_for_projects(proj)
            self.assertIn("global-1", ids)
            self.assertIn("local-1", ids)


class TestStoreSaveKeepsStarred(_Isolated):
    def test_upsert_past_cap_keeps_starred_rows(self):
        with tempfile.TemporaryDirectory() as proj:
            store = SessionStore(os.path.join(proj, ".sessions.json"))
            rows = [{"session_id": "s%d" % i, "project": proj,
                     "last_activity": float(100 - i)}
                    for i in range(SESSIONS_CAP + 5)]
            store.save(rows)
            self.assertEqual(len(store.load()), SESSIONS_CAP)
            # Star the oldest row, then save again: it must survive the cap.
            oldest = "s%d" % (SESSIONS_CAP + 4)
            toggle_bookmark(oldest, proj)
            store.save(rows)
            ids = [r["session_id"] for r in store.load()]
            self.assertIn(oldest, ids)
            self.assertEqual(len(ids), SESSIONS_CAP + 1)


class TestMcpChildResolution(unittest.TestCase):
    def setUp(self):
        from tests.stubs import install_sublime
        install_sublime()
        from core.registry import default_registry
        default_registry.clear()
        self.r = default_registry

    def tearDown(self):
        self.r.clear()

    def _server(self):
        import mcp.socket_server as ss
        return ss

    def test_list_sessions_finds_a_child_of_a_rotated_parent(self):
        ss = self._server()
        parent = make_session()
        parent.agent_id = "agent-parent"
        child = make_session()
        child.agent_id = "agent-child"
        child.parent_agent_id = "agent-parent"
        self.r.register(parent)
        self.r.register(child)
        orig = ss._get_session_by_agent_id
        ss._get_session_by_agent_id = (
            lambda aid: parent if str(aid) == "agent-parent" else orig(aid))
        try:
            srv = ss.MCPSocketServer()
            srv._caller_agent_id = "agent-parent"
            # Rotate the parent id the way a sheet recreate would.
            parent.agent_id = "agent-parent-v2"
            reg.remember_agent_alias(parent, "agent-parent")
            self.r.register(parent)
            out = srv._list_sessions()
            self.assertEqual(out["count"], 1)
            self.assertEqual(out["sessions"][0]["agent_id"], "agent-child")
            self.assertEqual(child.parent_agent_id, "agent-parent-v2")
        finally:
            ss._get_session_by_agent_id = orig

    def test_no_caller_says_so(self):
        ss = self._server()
        srv = ss.MCPSocketServer()
        srv._caller_agent_id = None
        out = srv._list_sessions()
        self.assertEqual(out["count"], 0)
        self.assertIn("No caller session", out["summary"])

    def test_spawn_notes_the_child_on_the_parent(self):
        with open(os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "mcp", "socket_server.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('_try_import("core.registry.note_child")', src)
        self.assertIn("parent_session_id", src)


if __name__ == "__main__":
    unittest.main()
