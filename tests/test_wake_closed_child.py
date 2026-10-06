"""send_to_session reaches a closed subsession still in the session index:
the caller's own child is reopened under its agent_id; anyone else's is not."""
import os
import sys
import types
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PARENT = "submarine::aaaaaaaaaaaa"
CHILD = "submarine::bbbbbbbbbbbb"
ROWS = [
    {"agent_id": CHILD, "session_id": "sid-child", "backend": "grok", "model": "grok-4.6",
     "name": "rain-impl", "parent_agent_id": PARENT},
    {"agent_id": "submarine::cccccccccccc", "session_id": "sid-other", "backend": "claude",
     "name": "someone else", "parent_agent_id": "submarine::dddddddddddd"},
]


class WakeClosedChildTest(unittest.TestCase):
    def _server(self, created):
        from tests.stubs import install_sublime
        install_sublime()
        import mcp.socket_server as ss
        parent = types.SimpleNamespace(agent_id=PARENT, child_agent_ids=[CHILD])

        def create_session(window, resume_id=None, backend=None, **kw):
            created.append((resume_id, backend, kw.get("model")))
            return types.SimpleNamespace(name=None, output=types.SimpleNamespace(set_name=lambda n: None))

        imports = {
            "core.records.load_saved_sessions": lambda: ROWS,
            "core.agent_ids.canon_agent_id": lambda v: v,
            "commands.session_cmds.create_session": create_session,
        }
        saved = (ss._try_import, ss._get_session_by_agent_id, ss._parent_in_focus)
        ss._try_import = lambda name: imports.get(name)
        ss._get_session_by_agent_id = lambda aid: parent if aid == PARENT else None
        ss._parent_in_focus = lambda w, p: False
        self.addCleanup(lambda: (setattr(ss, "_try_import", saved[0]),
                                 setattr(ss, "_get_session_by_agent_id", saved[1]),
                                 setattr(ss, "_parent_in_focus", saved[2])))
        srv = ss.MCPSocketServer.__new__(ss.MCPSocketServer)
        srv._get_window = lambda: object()
        srv._caller_agent_id = PARENT
        return srv

    def test_own_closed_child_is_reopened_under_its_session(self):
        created = []
        session, err = self._server(created)._wake_saved_child(CHILD)
        self.assertIsNone(err)
        self.assertEqual(created, [("sid-child", "grok", "grok-4.6")])
        self.assertEqual(session.name, "rain-impl")

    def test_someone_elses_closed_session_is_refused(self):
        created = []
        session, err = self._server(created)._wake_saved_child("submarine::cccccccccccc")
        self.assertIsNone(session)
        self.assertIn("not your subsession", err["error"])
        self.assertEqual(created, [])

    def test_unknown_id_is_not_found(self):
        created = []
        _s, err = self._server(created)._wake_saved_child("submarine::eeeeeeeeeeee")
        self.assertIn("not found", err["error"])


if __name__ == "__main__":
    unittest.main()
