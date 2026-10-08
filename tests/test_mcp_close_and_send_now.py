"""close_session closes the caller's own subsessions; send_to_session(now=True)
is the user's Send Now.

Before: an agent could not close a sidecar it was done with (no tool), and a
mid-turn send_to_session always waited for the target's turn to end.
"""
from __future__ import annotations

import unittest
from unittest import mock

from tests.stubs import install_sublime

install_sublime()

from core.registry import default_registry  # noqa: E402
from mcp.tools import TOOL_TABLE  # noqa: E402
from tests.fakes import FakeClient, make_session  # noqa: E402


def _server(caller):
    import mcp.socket_server as ss
    srv = ss.MCPSocketServer.__new__(ss.MCPSocketServer)
    srv._caller_agent_id = caller
    srv._cached_window = None
    srv._get_window = lambda: None
    return srv


class _Base(unittest.TestCase):
    def setUp(self):
        default_registry.clear()
        self.parent = self._live("submarine::00000000000a", "planner", "s1")
        self.kid = self._live("submarine::00000000000b", "worker", "s2",
                              parent=self.parent.agent_id)
        self.stranger = self._live("submarine::00000000000c", "other", "s3")

    def tearDown(self):
        default_registry.clear()

    def _live(self, aid, name, sid, parent=None):
        s = make_session(client=FakeClient(), initialized=True, registry=default_registry)
        s.agent_id, s.name, s.session_id = aid, name, sid
        s.parent_agent_id = parent
        default_registry.register_session(s)
        return s


class CloseSessionTest(_Base):
    def _close(self, *ids, force=False):
        closed = []

        def fake_close_row(window, row, remove=None):
            closed.append((row["agent_id"], remove))
            return True
        with mock.patch("ui.session_list.close_row", fake_close_row):
            out = _server(self.parent.agent_id)._close_session(list(ids), force=force)
        return out, closed

    def test_closes_own_idle_child_and_keeps_its_save(self):
        out, closed = self._close(self.kid.agent_id)
        self.assertEqual(closed, [(self.kid.agent_id, False)])
        self.assertEqual(out["closed"], [self.kid.agent_id])
        self.assertNotIn("error", out)

    def test_refuses_a_session_that_is_not_a_child(self):
        out, closed = self._close(self.stranger.agent_id)
        self.assertEqual(closed, [])
        self.assertIn("not your subsession", out["error"])

    def test_refuses_itself(self):
        out, closed = self._close(self.parent.agent_id)
        self.assertEqual(closed, [])
        self.assertIn("that is you", out["error"])

    def test_working_child_needs_force(self):
        self.kid.turn.begin_query()
        out, closed = self._close(self.kid.agent_id)
        self.assertEqual(closed, [])
        self.assertIn("force=true", out["error"])
        out, closed = self._close(self.kid.agent_id, force=True)
        self.assertEqual(closed, [(self.kid.agent_id, False)])

    def test_already_closed_is_not_an_error(self):
        out, closed = self._close("submarine::0000000000ff")
        self.assertEqual(closed, [])
        self.assertNotIn("error", out)
        self.assertTrue(out["results"][0]["closed"])

    def test_codegen_takes_one_or_many(self):
        gen = TOOL_TABLE["close_session"]["codegen"]
        code = gen({"agent_id": "a", "agent_ids": ["b", "a"], "_caller_agent_id": "p"})
        self.assertIn("agent_ids=['a', 'b']", code)
        self.assertIn("force=False", code)
        with self.assertRaises(ValueError):
            gen({})


class SendNowTest(_Base):
    def test_now_steers_a_working_claude_target(self):
        self.kid.turn.begin_query()
        with mock.patch.object(type(self.kid), "steer_now", autospec=True) as steer, \
                mock.patch.object(type(self.kid), "can_steer", return_value=True):
            out = _server(self.parent.agent_id)._send_to_session(
                "stop that", agent_id=self.kid.agent_id, now=True)
        self.assertTrue(out["now"])
        self.assertIn("running turn", out["message"])
        self.assertEqual(steer.call_count, 1)
        sent = steer.call_args[0][1]
        self.assertIn("stop that", sent)          # stamped with the sender
        self.assertNotIn(sent, self.kid._queued_prompts)

    def test_without_now_a_working_target_queues(self):
        self.kid.turn.begin_query()
        with mock.patch.object(type(self.kid), "steer_now", autospec=True) as steer:
            out = _server(self.parent.agent_id)._send_to_session(
                "later", agent_id=self.kid.agent_id)
        self.assertTrue(out["queued"])
        self.assertEqual(steer.call_count, 0)

    def test_now_on_an_idle_target_is_a_plain_send(self):
        out = _server(self.parent.agent_id)._send_to_session(
            "go", agent_id=self.kid.agent_id, now=True)
        self.assertTrue(out["sent"])
        self.assertNotIn("now", out)
        self.assertTrue(any(m == "query" for m, _p, _cb in self.kid.client.sent))

    def test_codegen_passes_now(self):
        gen = TOOL_TABLE["send_to_session"]["codegen"]
        self.assertIn("now=True", gen({"prompt": "x", "agent_id": "a", "now": True}))
        self.assertNotIn("now=", gen({"prompt": "x", "agent_id": "a"}))


if __name__ == "__main__":
    unittest.main()


class SendNowWireTest(_Base):
    def test_claude_target_gets_inject_message_not_a_new_turn(self):
        self.kid.backend = "claude"
        self.kid.turn.begin_query()
        out = _server(self.parent.agent_id)._send_to_session(
            "redirect", agent_id=self.kid.agent_id, now=True)
        self.assertIn("running turn", out["message"])
        methods = [m for m, _p, _cb in self.kid.client.sent]
        self.assertIn("inject_message", methods)
        self.assertNotIn("query", methods)
        self.assertNotIn("interrupt", methods)
        self.assertEqual(self.kid._queued_prompts, [])
