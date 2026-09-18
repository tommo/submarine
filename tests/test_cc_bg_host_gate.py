"""Claude Code background tasks: the host gate must survive the real wire.

Captured from real sessions in `sandbox/claude_bg/` (`check_e2e.py` drives the
real bridge with a model that backgrounds a `sleep`). Two shapes, both replayed
here through the shipped `BridgeEventRouter` + `BackgroundTaskGate`:

  cc_bg_wire.jsonl   — the turn ends while the job runs; the completion arrives
                       as `task_updated {status: completed}` with no
                       `task_notification` at all.
  cc_bg_during.jsonl — a foreground command keeps the turn live; the CLI then
                       sends `task_updated` AND `task_notification` for the
                       background job, plus a `task_notification` for the
                       FOREGROUND command.

Three things used to be wrong, all of them visible to the user: the launch ack
("Command running in background with ID: …") closed the ⚙ row the instant the
job started; the turn's own result closed it again; and a completed job said
nothing — no notification turn, so the output was never shown and the agent was
never woken. A foreground command's completion could also start a spurious turn.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.background import BackgroundTaskGate
from sandbox.claude_bg import check_host_gate as hg

FIXTURES = hg.FIXTURES


class _Gate(unittest.TestCase):
    def gate(self):
        from core.turn import TurnController
        from tests.fakes import FakeOutput, FakeScheduler

        sched = FakeScheduler()
        out = FakeOutput()
        queries = []
        gate = BackgroundTaskGate(
            TurnController(sched), sched, out, backend="claude",
            read_output_file=hg.read_sandbox_output,
            on_query=lambda body, display=None: queries.append(body),
        )
        return gate, sched, queries


class LaunchAckTest(_Gate):
    def test_the_ack_names_the_task_and_its_log(self):
        gate, _sched, _q = self.gate()
        ack = ("Command running in background with ID: byfbn40bu. Output is "
               "being written to: /tmp/x/tasks/byfbn40bu.output. You will be "
               "notified when it completes.")
        self.assertTrue(gate.note_launch_ack("tool-1", ack),
                        "the ack must be recognized: the row is still running")
        self.assertEqual(gate.task_tool_map.get("byfbn40bu"), "tool-1")
        self.assertEqual(gate.task_logs.get("byfbn40bu"),
                         "/tmp/x/tasks/byfbn40bu.output")
        self.assertIn("tool-1", gate.bg_task_ids)

    def test_the_task_id_does_not_swallow_the_sentence_dot(self):
        """`byfbn40bu.` as a key silently loses the output: the completion's
        task_id (no dot) never matches it."""
        gate, _sched, _q = self.gate()
        gate.note_launch_ack(
            "tool-1",
            "Command running in background with ID: abc123. Output is being "
            "written to: /tmp/abc123.output. You will be notified when it "
            "completes.")
        self.assertEqual(list(gate.task_logs), ["abc123"])

    def test_plain_output_is_not_an_ack(self):
        gate, _sched, _q = self.gate()
        self.assertFalse(gate.note_launch_ack("tool-1", "(Bash completed with no output)"))
        self.assertFalse(gate.note_launch_ack("tool-1", "status: running"))


class ForegroundTaskTest(_Gate):
    def test_a_foreground_command_completion_starts_no_turn(self):
        """Claude Code opens task_started for foreground commands too
        (`is_backgrounded: false`); their output was already answered inside
        the turn."""
        gate, sched, queries = self.gate()
        gate.on_task_started({
            "task_id": "fg-1", "tool_use_id": "tool-fg",
            "description": "foreground sleep", "is_backgrounded": False,
        })
        gate.on_task_notification({
            "task_id": "fg-1", "tool_use_id": "tool-fg",
            "status": "completed", "summary": "foreground sleep",
        })
        gate.on_task_updated({"task_id": "fg-1", "patch": {"status": "completed"}})
        sched.fire_due()
        self.assertEqual(queries, [])
        self.assertEqual(gate.pending_notifications, [])

    def test_a_backend_without_the_flag_is_untouched(self):
        """kimi/acp task_started has no `is_backgrounded` — must not be read as
        foreground, or every kimi completion goes silent."""
        gate, sched, queries = self.gate()
        gate.on_task_started({"task_id": "bash-1", "tool_use_id": "tool-1"})
        gate.on_task_notification({
            "task_id": "bash-1", "tool_use_id": "tool-1",
            "status": "completed", "summary": "sleep 9",
        })
        sched.fire_due()
        self.assertEqual(len(queries), 1)


class TerminalUpdateSurfacesTest(_Gate):
    def test_a_terminal_update_notifies_with_the_jobs_output(self):
        gate, sched, queries = self.gate()
        gate.on_task_started({
            "task_id": "t1", "tool_use_id": "tool-1",
            "description": "sandbox bg sleep", "is_backgrounded": True,
        })
        gate.note_launch_ack(
            "tool-1", "running in background with ID: t1. Output is being written to: /tmp/t1.output.")
        gate.on_task_updated({"task_id": "t1", "patch": {"status": "completed"}})
        sched.fire_due()
        self.assertEqual(len(queries), 1, "the completion must wake the session")
        self.assertTrue(queries[0].startswith("<task-notification>"))
        self.assertIn("CC_BG_DONE", queries[0])
        self.assertIn("sandbox bg sleep", queries[0])

    def test_a_richer_notification_replaces_the_pending_block(self):
        """task_updated has no summary/output_file; the CLI's own notification
        for the same job follows immediately and must upgrade it, not add a
        second turn."""
        gate, sched, queries = self.gate()
        gate.on_task_started({"task_id": "t1", "tool_use_id": "tool-1",
                              "description": "sandbox bg sleep"})
        gate.on_task_updated({"task_id": "t1", "patch": {"status": "completed"}})
        gate.on_task_notification({
            "task_id": "t1", "tool_use_id": "tool-1", "status": "completed",
            "summary": "Background command completed (exit code 0)",
            "output_file": "/tmp/gone.output",
        })
        sched.fire_due()
        self.assertEqual(len(queries), 1)
        self.assertIn("exit code 0", queries[0])

    def test_a_second_terminal_event_is_not_a_second_turn(self):
        gate, sched, queries = self.gate()
        gate.on_task_started({"task_id": "t1", "tool_use_id": "tool-1"})
        gate.on_task_updated({"task_id": "t1", "patch": {"status": "completed"}})
        sched.fire_due()
        gate.on_task_updated({"task_id": "t1", "patch": {"status": "completed"}})
        gate.on_task_notification({"task_id": "t1", "tool_use_id": "tool-1",
                                   "status": "completed", "summary": "again"})
        sched.fire_due()
        self.assertEqual(len(queries), 1)

    def test_reconcile_still_clears_a_stale_row_without_waking(self):
        gate, sched, queries = self.gate()
        gate.on_task_started({"task_id": "t1", "tool_use_id": "tool-1"})
        gate.reconcile(running=[])
        sched.fire_due()
        self.assertEqual(queries, [])


class CapturedWireTest(unittest.TestCase):
    """The whole captured stream, in capture order, through the real host."""

    def _replay(self, key):
        path = FIXTURES[key]
        if not os.path.isfile(path):
            self.skipTest("capture %s missing" % path)
        import json

        with open(path, encoding="utf-8") as f:
            fixture = [json.loads(line) for line in f if line.strip()]
        fails = hg.check(fixture, read_output=hg.read_sandbox_output)
        self.assertEqual(fails, [])

    def test_the_turn_ended_while_the_job_ran(self):
        self._replay("after")

    def test_a_foreground_command_kept_the_turn_live(self):
        self._replay("during")


if __name__ == "__main__":
    unittest.main()
