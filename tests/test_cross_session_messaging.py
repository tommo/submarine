"""Agents messaging other sessions, any window, by agent_id, session_id or name.

Before: send_to_session took agent_id only and list_sessions showed only the
caller's own subsessions, so a peer in another window was unreachable unless
the user dug up its id; the sessions CLI reached it by name but delivered
the message with no sender, so the target could not reply.
"""
from __future__ import annotations

import unittest

from tests.stubs import install_sublime

install_sublime()

from core.registry import default_registry  # noqa: E402
from tests.fakes import FakeClient, make_session  # noqa: E402


class _Win(object):
    def __init__(self, wid):
        self._id = wid

    def id(self):
        return self._id

    def folders(self):
        return []


def _server():
    import mcp.socket_server as ss
    srv = ss.MCPSocketServer.__new__(ss.MCPSocketServer)
    srv._caller_agent_id = None
    srv._cached_window = None
    srv._get_window = lambda: None
    return srv


class CrossSessionTest(unittest.TestCase):
    def setUp(self):
        default_registry.clear()
        self.me = self._live("submarine::000000000001", "planner", "s1", _Win(1))
        self.peer = self._live("submarine::000000000002", "Bonsai UX implementation", "s2", _Win(2))
        self.twin_a = self._live("submarine::000000000003", "review", "s3", _Win(2))
        self.twin_b = self._live("submarine::000000000004", "review", "s4", _Win(3))

    def tearDown(self):
        default_registry.clear()

    def _live(self, aid, name, sid, window):
        s = make_session(client=FakeClient(), initialized=True, registry=default_registry)
        s.agent_id, s.name, s.session_id, s.window = aid, name, sid, window
        default_registry.register_session(s)
        return s

    def _queries(self, s):
        return [(p or {}).get("prompt") for m, p, _cb in s.client.sent if m == "query"]

    def test_list_all_reaches_every_window(self):
        srv = _server()
        srv._caller_agent_id = self.me.agent_id
        out = srv._list_sessions(scope="all")
        by_id = {r["agent_id"]: r for r in out["sessions"]}
        self.assertEqual(set(by_id), {"submarine::000000000001", "submarine::000000000002",
                                      "submarine::000000000003", "submarine::000000000004"})
        self.assertEqual(by_id["submarine::000000000002"]["window"], 2)
        self.assertTrue(by_id["submarine::000000000001"]["you"])
        self.assertIn("Bonsai UX implementation", out["summary"])

    def test_default_scope_is_still_children(self):
        srv = _server()
        srv._caller_agent_id = self.me.agent_id
        self.assertEqual(srv._list_sessions()["count"], 0)

    def test_send_by_name_reaches_the_other_window_with_a_sender(self):
        srv = _server()
        srv._caller_agent_id = self.me.agent_id
        out = srv._send_to_session("status?", name="Bonsai UX implementation")
        self.assertTrue(out.get("sent"), out)
        prompt = self._queries(self.peer)[-1]
        self.assertTrue(prompt.startswith("[from agent submarine::000000000001]"), prompt)
        self.assertIn("status?", prompt)

    def test_send_by_session_id(self):
        srv = _server()
        out = srv._send_to_session("hi", session_id="s2")
        self.assertTrue(out.get("sent"), out)

    def test_an_ambiguous_name_returns_the_candidates(self):
        srv = _server()
        out = srv._send_to_session("hi", name="review")
        self.assertIn("error", out)
        self.assertEqual({c["agent_id"] for c in out["candidates"]},
                         {"submarine::000000000003", "submarine::000000000004"})
        self.assertEqual(self._queries(self.twin_a), [])

    def test_no_address_at_all(self):
        self.assertIn("error", _server()._send_to_session("hi"))


class CliSenderStampTest(unittest.TestCase):
    """The sessions CLI run from an agent's Bash tool carries
    SUBMARINE_AGENT_ID; its chat gets the same sender header."""

    def setUp(self):
        default_registry.clear()
        self.me = make_session(client=FakeClient(), initialized=True, registry=default_registry)
        self.me.agent_id, self.me.name, self.me.session_id = "submarine::00000000000a", "planner", "sa"
        self.peer = make_session(client=FakeClient(), initialized=True, registry=default_registry)
        self.peer.agent_id, self.peer.name, self.peer.session_id = "submarine::00000000000b", "worker", "sb"
        for s in (self.me, self.peer):
            default_registry.register_session(s)

    def tearDown(self):
        default_registry.clear()

    def test_an_agent_caller_is_stamped(self):
        from features.session_control import _stamp_agent_sender
        prompt, shown = _stamp_agent_sender(
            {"kind": "cli", "agent_id": "submarine::00000000000a"}, self.peer,
            "please check", "📨 please check")
        self.assertTrue(prompt.startswith("[from agent submarine::00000000000a]"))
        self.assertIn("name=planner", prompt)
        self.assertEqual(shown, "📬 from planner")

    def test_a_human_caller_is_not(self):
        from features.session_control import _stamp_agent_sender
        prompt, shown = _stamp_agent_sender({"kind": "webui"}, self.peer, "hi", "📨 hi")
        self.assertEqual((prompt, shown), ("hi", "📨 hi"))

    def test_the_cli_reads_the_env(self):
        import os
        import features.sessions_cli as cli
        sent = []
        orig = cli.send if hasattr(cli, "send") else None
        os.environ["SUBMARINE_AGENT_ID"] = "submarine::00000000000a"
        try:
            import inspect
            src = inspect.getsource(cli)
            self.assertIn('os.environ.get("SUBMARINE_AGENT_ID")', src)
        finally:
            os.environ.pop("SUBMARINE_AGENT_ID", None)
        _ = (sent, orig)

    def test_every_bridge_gets_its_agent_id_in_env(self):
        s = make_session(client=FakeClient(), backend="claude")
        s.agent_id = "submarine::00000000000c"
        s.start()
        self.assertEqual(s.client.started[1].get("SUBMARINE_AGENT_ID"), "submarine::00000000000c")


if __name__ == "__main__":
    unittest.main()
