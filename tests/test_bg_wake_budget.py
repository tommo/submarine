"""Background completions must not wake the agent once per expected job.

After a notification turn ends with no tool call, the tasks still running are
acknowledged: their `completed` results surface (✓ + unread) and are deferred
to the next real prompt instead of starting a turn. Failures still wake.
"""
from __future__ import annotations

import unittest

from core.background import BackgroundTaskGate
from core.turn import TurnController
from tests.fakes import FakeClient, FakeOutput, FakeScheduler, make_session


def _gate(policy="auto"):   # most cases exercise auto; the default is defer
    turn = TurnController()
    sched = FakeScheduler()
    out = FakeOutput()
    queries, surfaces = [], []
    g = BackgroundTaskGate(
        turn, sched, out, backend="claude",
        on_query=lambda p, d: queries.append((p, d)),
        on_surface=lambda: surfaces.append(True),
        policy=lambda: policy)
    return g, sched, queries, surfaces


def _start(g, tid, tuid):
    g.on_task_started({"task_id": tid, "tool_use_id": tuid, "description": "publish " + tid})


def _done(g, tid, status="completed", summary=""):
    g.on_task_notification({"task_id": tid, "status": status, "summary": summary})


class TestAcknowledgedBatch(unittest.TestCase):
    def test_each_completion_wakes_by_default(self):
        g, sched, queries, _ = _gate()
        _start(g, "t1", "u1"); _start(g, "t2", "u2")
        _done(g, "t1"); sched.fire_all()
        _done(g, "t2"); sched.fire_all()
        self.assertEqual(len(queries), 2)

    def test_after_a_quiet_notification_turn_the_rest_of_the_batch_is_deferred(self):
        g, sched, queries, surfaces = _gate()
        for i in range(1, 5):
            _start(g, "t%d" % i, "u%d" % i)
        _done(g, "t1"); sched.fire_all()
        self.assertEqual(len(queries), 1)               # the first wake
        g.acknowledge_running()                         # agent answered: nothing to do
        _done(g, "t2"); _done(g, "t3"); sched.fire_all()
        self.assertEqual(len(queries), 1, "acknowledged completions must not wake")
        self.assertEqual(len(surfaces), 2)              # but they are shown
        held = g.take_deferred()
        self.assertIn("publish t2", held)
        self.assertIn("publish t3", held)
        self.assertEqual(g.take_deferred(), "")         # delivered once
        # A job that started after the judgement is new information again.
        _start(g, "t9", "u9")
        _done(g, "t9"); sched.fire_all()
        self.assertEqual(len(queries), 2)

    def test_a_failure_in_an_acknowledged_batch_still_wakes(self):
        g, sched, queries, _ = _gate()
        _start(g, "t1", "u1"); _start(g, "t2", "u2")
        g.acknowledge_running()
        _done(g, "t1", status="failed", summary="publish t1"); sched.fire_all()
        self.assertEqual(len(queries), 1)
        self.assertIn("[failed]", queries[0][0])

    def test_judgement_reroutes_a_completion_already_buffered(self):
        g, sched, queries, surfaces = _gate()
        _start(g, "t1", "u1"); _start(g, "t2", "u2")
        _done(g, "t2")                                   # completed during the turn, not flushed
        self.assertEqual(len(g.pending_notifications), 1)
        g.acknowledge_running()
        self.assertEqual(g.pending_notifications, [])
        sched.fire_all()
        self.assertEqual(queries, [])
        self.assertIn("publish t2", g.take_deferred())

    def test_policies(self):
        g, sched, queries, _ = _gate(policy="defer")
        _start(g, "t1", "u1"); _done(g, "t1"); sched.fire_all()
        self.assertEqual(queries, [])
        self.assertIn("publish t1", g.take_deferred())
        # defer never wakes, not even for a failure
        _start(g, "t2", "u2"); _done(g, "t2", status="failed"); sched.fire_all()
        self.assertEqual(queries, [])
        self.assertIn("[failed]", g.take_deferred())


    def test_defer_is_the_shipped_default(self):
        from tests.fakes import FakeClient, make_session
        client = FakeClient()
        s = make_session(client=client, initialized=True, settings={"background_notify": None})
        s.query("go")
        s._accept_tool_name("Bash")
        _start(s.bg, "t1", "u1")
        s._on_done({"status": "ok"}, _expected_gen=s.turn.gen)
        _done(s.bg, "t1"); s.scheduler.fire_all()
        self.assertFalse(s.working, "the default policy woke the agent")
        self.assertTrue(s.bg.deferred)
        g, sched, queries, _ = _gate(policy="always")
        _start(g, "t1", "u1"); g.acknowledge_running(); _done(g, "t1"); sched.fire_all()
        self.assertEqual(len(queries), 1)


class TestSessionWiring(unittest.TestCase):
    """The session judges its own notification turn and rides deferred text
    along with the next real prompt."""

    def _session(self, policy="auto"):
        client = FakeClient()
        s = make_session(client=client, initialized=True, settings={"background_notify": policy})
        return s, client

    def test_quiet_notification_turn_acknowledges_and_next_prompt_carries_the_rest(self):
        s, client = self._session()
        g = s.bg
        _start(g, "t1", "u1"); _start(g, "t2", "u2")
        _done(g, "t1"); s.scheduler.fire_all()
        self.assertTrue(s.working)                      # the notification turn
        self.assertEqual(s._notify_turn_gen, s.turn.gen)
        # The agent replies with text only.
        s.output.text("nothing to act on")
        s._on_done({"status": "ok"}, _expected_gen=s.turn.gen)
        self.assertIn("t2", g.acknowledged)
        _done(g, "t2"); s.scheduler.fire_all()
        self.assertFalse(s.working, "acknowledged completion must not start a turn")
        self.assertTrue(g.deferred)
        s.query("next real prompt")
        sent = [c for c in client.sent if c[0] == "query"][-1]
        wire = sent[1].get("prompt")
        self.assertIn("<task-notification>", wire)
        self.assertIn("publish t2", wire)
        self.assertTrue(wire.rstrip().endswith("next real prompt"))
        self.assertEqual(g.deferred, [])

    def test_a_notification_turn_with_tool_calls_keeps_waking(self):
        s, client = self._session()
        g = s.bg
        _start(g, "t1", "u1"); _start(g, "t2", "u2")
        _done(g, "t1"); s.scheduler.fire_all()
        s._accept_tool_name("Bash")                     # the router saw a tool call
        s._on_done({"status": "ok"}, _expected_gen=s.turn.gen)
        self.assertEqual(g.acknowledged, set())
        _done(g, "t2"); s.scheduler.fire_all()
        self.assertTrue(s.working)


if __name__ == "__main__":
    unittest.main()

    def test_a_turn_that_asks_the_user_does_not_get_woken_by_an_expiring_job(self):
        """The model launched a job with tools, then in its next turn only
        asked the user something. The job's expiry must not wake it."""
        s, client = self._session()
        g = s.bg
        s.query("do the thing")
        s._accept_tool_name("Bash")
        _start(g, "t1", "u1")                             # launched in this turn
        s._on_done({"status": "ok"}, _expected_gen=s.turn.gen)
        s.query("which output size?")
        s.output.text("Which output size do you mean?")     # no tool call
        s._on_done({"status": "ok"}, _expected_gen=s.turn.gen)
        self.assertIn("t1", g.acknowledged)
        _done(g, "t1"); s.scheduler.fire_all()
        self.assertFalse(s.working, "a job expiring after a question woke the model")
        self.assertTrue(g.deferred)
        s.query("the 1080p one")
        wire = [c for c in client.sent if c[0] == "query"][-1][1]["prompt"]
        self.assertIn("<task-notification>", wire)
        self.assertTrue(wire.endswith("the 1080p one"))

    def test_a_turn_that_used_tools_still_gets_woken(self):
        s, _client = self._session()
        g = s.bg
        s.query("build it")
        s._accept_tool_name("Bash")
        _start(g, "t1", "u1")
        s._on_done({"status": "ok"}, _expected_gen=s.turn.gen)
        self.assertEqual(g.acknowledged, set())
        _done(g, "t1"); s.scheduler.fire_all()
        self.assertTrue(s.working)

