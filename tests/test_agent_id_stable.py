"""A conversation keeps one agent_id across sleep, wake and restore.

Regression: sleep/close wrote bare `{session_id, state, model}` rows, and a
restore of a row without an id minted a fresh random one — so the id an
agent was told to message, or a Copy Agent ID, stopped resolving.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.records import SessionStore, derived_agent_id
from core.registry import agent_id_for
from tests.fakes import make_session


def _store():
    return SessionStore(os.path.join(
        tempfile.mkdtemp(prefix="submarine-test-"), ".sessions.json"))


class StableAgentIdTest(unittest.TestCase):
    def test_derived_id_is_deterministic(self):
        self.assertEqual(agent_id_for("sid-1"), agent_id_for("sid-1"))
        self.assertNotEqual(agent_id_for("sid-1"), agent_id_for("sid-2"))
        self.assertRegex(agent_id_for("sid-1"), r"^submarine::[0-9a-f]{12}$")
        self.assertRegex(agent_id_for(None), r"^submarine::[0-9a-f]{12}$")

    def test_a_bare_row_restores_to_the_same_id_every_time(self):
        store = _store()
        store.persist_state("sid-bare", "sleeping", "m")
        a = make_session(resume_id="sid-bare", store=store)
        b = make_session(resume_id="sid-bare", store=store)
        self.assertEqual(a.agent_id, b.agent_id)
        self.assertEqual(a.agent_id, derived_agent_id("sid-bare"))

    def test_an_unknown_conversation_restores_to_the_same_id(self):
        store = _store()
        a = make_session(resume_id="sid-gone", store=store)
        b = make_session(resume_id="sid-gone", store=store)
        self.assertEqual(a.agent_id, b.agent_id)

    def test_a_saved_id_wins(self):
        store = _store()
        store.upsert({"session_id": "sid-x", "agent_id": "submarine::abcdefabcdef"})
        s = make_session(resume_id="sid-x", store=store)
        self.assertEqual(s.agent_id, "submarine::abcdefabcdef")

    def test_state_rows_carry_the_live_id(self):
        store = _store()
        store.persist_state("sid-new", "sleeping", None, "submarine::111111111111")
        self.assertEqual(store.find("sid-new")["agent_id"], "submarine::111111111111")
        # A row written bare earlier takes the live id, not the derived one.
        store.save([{"session_id": "sid-old", "state": "open"}])
        store.persist_state("sid-old", "sleeping", None, "submarine::222222222222")
        self.assertEqual(store.find("sid-old")["agent_id"], "submarine::222222222222")
        # A caller without a live session leaves the id alone.
        store.persist_state("sid-old", "closed")
        self.assertEqual(store.find("sid-old")["agent_id"], "submarine::222222222222")

    def test_every_loaded_row_answers_with_its_restore_id(self):
        store = _store()
        store.save([{"session_id": "sid-a", "state": "sleeping"}])
        self.assertEqual(store.load()[0]["agent_id"], derived_agent_id("sid-a"))

    def test_a_fork_is_a_new_agent(self):
        store = _store()
        store.upsert({"session_id": "sid-f", "agent_id": "submarine::abcdefabcdef"})
        s = make_session(resume_id="sid-f", fork=True, store=store)
        self.assertNotEqual(s.agent_id, "submarine::abcdefabcdef")



class LegacyAgentIdTest(unittest.TestCase):
    """`agent-<hex>` (minted before `submarine::`) still names the same agent."""

    def test_canon(self):
        from core.agent_ids import canon_agent_id
        self.assertEqual(canon_agent_id("agent-0123456789AB"), "submarine::0123456789ab")
        self.assertEqual(canon_agent_id("submarine-0123456789ab"), "submarine::0123456789ab")
        self.assertEqual(canon_agent_id("submarine::0123456789ab"), "submarine::0123456789ab")
        self.assertEqual(canon_agent_id("agent-parent"), "agent-parent")   # not an id
        self.assertIsNone(canon_agent_id(None))

    def test_a_legacy_row_restores_to_the_same_hex(self):
        store = _store()
        store.save([{"session_id": "sid-l", "agent_id": "agent-abcdefabcdef",
                     "parent_agent_id": "agent-000000000001",
                     "child_agent_ids": ["agent-000000000002"]}])
        row = store.find("sid-l")
        self.assertEqual(row["agent_id"], "submarine::abcdefabcdef")
        self.assertEqual(row["parent_agent_id"], "submarine::000000000001")
        self.assertEqual(row["child_agent_ids"], ["submarine::000000000002"])
        s = make_session(resume_id="sid-l", store=store)
        self.assertEqual(s.agent_id, "submarine::abcdefabcdef")

    def test_a_session_set_with_the_old_form_holds_the_new_one(self):
        s = make_session()
        s.agent_id = "agent-abcdefabcdef"
        s.parent_agent_id = "agent-000000000001"
        self.assertEqual(s.agent_id, "submarine::abcdefabcdef")
        self.assertEqual(s.parent_agent_id, "submarine::000000000001")

    def test_the_registry_resolves_either_form(self):
        from core.registry import SessionRegistry
        reg = SessionRegistry()
        s = make_session(registry=reg)
        s.agent_id = "submarine::abcdefabcdef"
        reg.register(s)
        self.assertIs(reg.by_agent_id("agent-abcdefabcdef"), s)
        self.assertIs(reg.by_agent_id("submarine::abcdefabcdef"), s)

    def test_artifacts_keep_a_legacy_folder_and_use_a_safe_new_one(self):
        from core.artifacts import ArtifactStore
        root = tempfile.mkdtemp(prefix="submarine-art-")
        store = ArtifactStore(root=root)
        self.assertEqual(os.path.basename(store.owner_dir("submarine::abcdefabcdef")),
                         "submarine-abcdefabcdef")
        os.makedirs(os.path.join(root, "agent-abcdefabcdef"))
        self.assertEqual(os.path.basename(store.owner_dir("submarine::abcdefabcdef")),
                         "agent-abcdefabcdef")


if __name__ == "__main__":
    unittest.main()
