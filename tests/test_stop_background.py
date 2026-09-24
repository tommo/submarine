"""Stop one background task (the ⚙ row), not the turn."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BRIDGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge")
if _BRIDGE not in sys.path:
    sys.path.insert(0, _BRIDGE)

from tests.fakes import FakeClient, FakeOutput, make_session


class SessionStopTest(unittest.TestCase):
    def _session(self, backend="claude"):
        client = FakeClient()
        s = make_session(output=FakeOutput(), client=client, initialized=True)
        s.backend = backend
        s.turn.begin_query()
        s.events.dispatch("message", {"type": "tool_use", "id": "toolu_1", "name": "Bash",
                                      "input": {"command": "sleep 300", "run_in_background": True},
                                      "background": True})
        s.events.dispatch("message", {"type": "system", "subtype": "task_started",
                                      "data": {"task_id": "b7x", "tool_use_id": "toolu_1"}})
        return s, client

    def test_a_running_row_is_listed_with_its_task(self):
        s, _c = self._session()
        tasks = s.background_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual((tasks[0]["tool_use_id"], tasks[0]["task_id"], tasks[0]["label"]),
                         ("toolu_1", "b7x", "Bash: sleep 300"))

    def test_stop_asks_the_bridge_and_reports(self):
        s, client = self._session()
        got = []
        self.assertTrue(s.stop_background_task("toolu_1", lambda ok, msg: got.append((ok, msg))))
        method, params, cb = client.sent[-1]
        self.assertEqual((method, params), ("stop_task", {"task_id": "b7x", "tool_use_id": "toolu_1"}))
        cb({"result": {"ok": True}})
        s.scheduler.fire_all()
        self.assertEqual(got, [(True, "stopping Bash: sleep 300")])

    def test_a_bridge_without_it_says_so(self):
        s, client = self._session(backend="codex")
        got = []
        s.stop_background_task("toolu_1", lambda ok, msg: got.append((ok, msg)))
        client.sent[-1][2]({"error": {"message": "Method not found: stop_task"}})
        s.scheduler.fire_all()
        self.assertEqual(got, [(False, "codex cannot stop a single background task")])

    def test_an_unknown_row_is_refused(self):
        s, client = self._session()
        got = []
        self.assertFalse(s.stop_background_task("nope", lambda ok, msg: got.append(ok)))
        s.scheduler.fire_all()
        self.assertEqual(got, [False])
        self.assertNotIn("stop_task", [m for m, _p, _c in client.sent])


class AcpBridgeStopTest(unittest.TestCase):
    def test_the_shell_behind_the_row_is_killed(self):
        import grok_main
        import acp.background as bg
        b = grok_main.GrokBridge.__new__(grok_main.GrokBridge)
        b.file_log = lambda *a, **k: None
        replies = []
        saved = (bg.send_result, bg.send_error)
        bg.send_result = lambda rid, res: replies.append(("ok", res))
        bg.send_error = lambda rid, code, msg: replies.append(("err", msg))
        self.addCleanup(lambda: (setattr(bg, "send_result", saved[0]),
                                 setattr(bg, "send_error", saved[1])))

        async def go():
            proc = await asyncio.create_subprocess_exec(
                "sleep", "30", start_new_session=True)
            b._terminals = {"t9": {"proc": proc}}
            b._terminal_bg = {"t9": {"task_id": "acp-term-t9", "tool_use_id": "call_1"}}
            await b.handle_stop_task(4, {"task_id": "", "tool_use_id": "call_1"})
            return proc.returncode
        rc = asyncio.run(go())
        self.assertIsNotNone(rc, "the process is gone")
        self.assertEqual(replies, [("ok", {"ok": True, "task_id": "acp-term-t9"})])

    def test_nothing_running_is_an_error(self):
        import grok_main
        import acp.background as bg
        b = grok_main.GrokBridge.__new__(grok_main.GrokBridge)
        b.file_log = lambda *a, **k: None
        b._terminals, b._terminal_bg = {}, {}
        replies = []
        saved = bg.send_error
        bg.send_error = lambda rid, code, msg: replies.append(msg)
        self.addCleanup(lambda: setattr(bg, "send_error", saved))
        asyncio.run(b.handle_stop_task(4, {"task_id": "acp-term-zz"}))
        self.assertIn("no running background shell", replies[0])


if __name__ == "__main__":
    unittest.main()
