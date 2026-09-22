"""Turns the runtime starts by itself, adopted by the host.

Claude Code answers a finished background task with a follow-up turn of its
own (a user message with `origin.kind == "task-notification"`, SDK
streaming-input mode). The bridge forwards it as `injected_turn` … stream …
`result{origin}`; the host owns it like a query minus the RPC. Before this the
bridge dropped that turn and the host queried the model about the same job —
two full context sends, the second answered "already accounted for".
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes import FakeClient, make_session


def _queries(client):
    return [(p or {}).get("prompt") for m, p, _cb in client.sent if m == "query"]


def _result(origin="task-notification", **extra):
    params = {"origin": origin, "status": "complete", "stop_reason": "end_turn",
              "duration_ms": 1200}
    params.update(extra)
    return params


class AdoptedTurnTest(unittest.TestCase):
    def _idle_session(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="claude")
        ended = []
        s.on_turn_end.append(lambda _s, completion: ended.append(completion))
        return s, client, ended

    def test_the_runtime_turn_is_owned_and_closed_by_its_tagged_result(self):
        s, client, ended = self._idle_session()
        s.events.dispatch("injected_turn", {
            "origin": "task-notification",
            "summaries": ['Background command "sleep" completed (exit code 0)'],
        })
        self.assertTrue(s.working)
        self.assertEqual(s.turn.kind, "live")
        self.assertFalse(s.turn.awaiting_rpc)
        self.assertEqual(
            s.output.prompts[-1][0],
            '⚙ Background command "sleep" completed (exit code 0)')
        s.events.text({"text": "Sleep finished, nothing pending."})
        self.assertEqual(s.output.texts[-1], "Sleep finished, nothing pending.")
        s.events.result(_result())
        self.assertFalse(s.working)
        self.assertEqual(s.turn.kind, "idle")
        self.assertEqual(ended, ["success"])
        self.assertIn(True, s.chrome.unread)
        self.assertEqual(_queries(client), [], "the host never queried")
        self.assertTrue(s.output.metas, "the adopted turn gets its meta line")

    def test_without_summaries_the_row_still_says_what_it_is(self):
        s, _client, _ended = self._idle_session()
        s.events.dispatch("injected_turn", {"origin": "task-notification"})
        self.assertEqual(s.output.prompts[-1][0], "⚙ background task")

    def test_typing_during_the_runtime_turn_queues_and_fires_after_it(self):
        s, client, _ended = self._idle_session()
        s.events.dispatch("injected_turn", {"origin": "task-notification"})
        s.query("and now do this")
        self.assertEqual(_queries(client), [], "queued behind the runtime's turn")
        self.assertEqual(s._queued_prompts, ["and now do this"])
        s.events.result(_result())
        self.assertEqual(_queries(client), ["and now do this"])
        self.assertTrue(s.working)
        self.assertTrue(s.turn.awaiting_rpc)

    def test_an_interrupted_runtime_turn_still_idles(self):
        s, client, ended = self._idle_session()
        s.events.dispatch("injected_turn", {"origin": "task-notification"})
        s.interrupt()
        self.assertEqual(s.turn.kind, "interrupting")
        s.events.result(_result(status="interrupted", stop_reason="interrupted"))
        self.assertFalse(s.working)
        self.assertEqual(s.turn.kind, "idle")
        self.assertEqual(ended, ["interrupted"])

    def test_a_failed_runtime_turn_still_idles(self):
        s, client, _ended = self._idle_session()
        s.events.dispatch("injected_turn", {"origin": "task-notification"})
        s.events.result(_result(is_error=True, stop_reason="error"))
        self.assertFalse(s.working)
        self.assertTrue(any("turn failed" in t for t in s.output.texts))

    def test_a_permission_request_during_the_runtime_turn_is_shown(self):
        """After a resume the session drops leftover asking state until the
        first query; a runtime turn is a real turn and its prompts count."""
        s, _client, _ended = self._idle_session()
        s._resume_drop_asking = True
        s.events.dispatch("injected_turn", {"origin": "task-notification"})
        s.events.permission_request({"id": 7, "tool": "Bash", "input": {}})
        self.assertEqual(len(s.output.permissions), 1)

    def test_a_query_gen_beats_a_runtime_turn_that_arrived_first(self):
        """Race: the CLI's turn ran ahead of ours before our echo, and the
        bridge attributed its content to our open query. Its tagged result
        must not close our turn; ours ends on the RPC callback as usual."""
        s, client, ended = self._idle_session()
        s.query("real prompt")
        self.assertTrue(s.turn.awaiting_rpc)
        s.events.dispatch("injected_turn", {"origin": "task-notification"})
        self.assertEqual(len(s.output.prompts), 1, "no second prompt row")
        s.events.result(_result())
        self.assertTrue(s.working, "our query is still open")
        self.assertEqual(ended, [])
        _m, _p, cb = [c for c in client.sent if c[0] == "query"][-1]
        s.events.result({"status": "complete", "stop_reason": "end_turn"})
        cb({"status": "complete"})
        self.assertFalse(s.working)
        self.assertEqual(ended, ["success"])


class NoHostNotificationTurnTest(unittest.TestCase):
    def test_a_completion_while_idle_flips_the_row_and_nothing_else(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="claude")
        s.query("run it")
        s.events.tool_use({
            "name": "Bash", "id": "tool-1", "background": True,
            "input": {"command": "sleep 5", "run_in_background": True},
        })
        s.events.system({"subtype": "task_started", "data": {
            "task_id": "t1", "tool_use_id": "tool-1", "is_backgrounded": True,
            "description": "sleep 5 in the back"}})
        s.events.tool_result({
            "tool_use_id": "tool-1",
            "content": "Command running in background with ID: t1. Output is "
                       "being written to: /nonexistent/t1.output.",
        })
        _m, _p, cb = [c for c in client.sent if c[0] == "query"][-1]
        cb({"status": "complete"})
        self.assertEqual(s.output.find_tool_by_id("tool-1").status, "background")
        s.events.system({"subtype": "task_updated",
                         "data": {"task_id": "t1", "patch": {"status": "completed"}}})
        s.events.system({"subtype": "task_notification", "data": {
            "task_id": "t1", "tool_use_id": "tool-1", "status": "completed",
            "summary": 'Background command "sleep 5" completed (exit code 0)',
            "output_file": "/nonexistent/t1.output"}})
        s.scheduler.fire_all()
        tool = s.output.find_tool_by_id("tool-1")
        self.assertEqual(tool.status, "done")
        self.assertIn("sleep 5 in the back", tool.result)
        self.assertIn("log: /nonexistent/t1.output", tool.result)
        self.assertEqual(_queries(client), ["run it"])
        self.assertFalse(s.working)
        self.assertIn(True, s.chrome.unread)

    def test_the_session_has_no_notification_query_path(self):
        s = make_session(initialized=True, client=FakeClient(), backend="claude")
        for name in ("_bg_query", "_judge_notification_turn", "_fire_queued_first"):
            self.assertFalse(hasattr(s, name), name)
        for name in ("flush", "take_deferred", "defer_pending",
                     "acknowledge_running", "pending_notifications", "policy"):
            self.assertFalse(hasattr(s.bg, name), name)


if __name__ == "__main__":
    unittest.main()
