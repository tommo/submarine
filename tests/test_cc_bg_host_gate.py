"""Claude Code background tasks: the host gate must survive the real wire.

Captured from real sessions in `sandbox/claude_bg/` (`check_e2e.py` drives the
real bridge with a model that backgrounds a `sleep`). Two shapes, both replayed
here through the shipped `BridgeEventRouter` + `BackgroundTaskGate`:

  cc_bg_wire.jsonl   — the turn ends while the job runs; the completion arrives
                       as `task_updated` + `task_notification`, and then the
                       CLI runs its own follow-up turn, which the bridge
                       forwards as `injected_turn` … `result{origin}`.
  cc_bg_during.jsonl — a foreground command keeps the turn live; the CLI then
                       sends the completion mid-turn (absorbed by the model as
                       an attachment) plus a `task_notification` for the
                       FOREGROUND command.

The host's part is the row: the launch ack must not close it, the turn's own
result must not close it, the completion flips it with the job's output, and
the CLI's follow-up turn is adopted as a turn of the sheet. The host never
queries the model about a completion — Claude Code already does that itself,
so every host `<task-notification>` turn was a duplicate.
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
        surfaced = []
        gate = BackgroundTaskGate(
            TurnController(sched), sched, out, backend="claude",
            read_output_file=hg.read_sandbox_output,
            on_surface=lambda: surfaced.append(1),
        )
        gate.out = out
        return gate, sched, surfaced

    def row(self, gate, tool_id, name="Bash"):
        gate.out.tool(name, {"command": "sleep"}, tool_id=tool_id, background=True)
        tool = gate.out._tools_by_id[tool_id]
        gate.register_tool(tool_id, tool=tool)
        return tool


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
    def test_a_foreground_command_completion_shows_nothing(self):
        """Claude Code opens task_started for foreground commands too
        (`is_backgrounded: false`); their output was already answered inside
        the turn."""
        gate, sched, surfaced = self.gate()
        gate.on_task_started({
            "task_id": "fg-1", "tool_use_id": "tool-fg",
            "description": "foreground sleep", "is_backgrounded": False,
        })
        gate.on_task_notification({
            "task_id": "fg-1", "tool_use_id": "tool-fg",
            "status": "completed", "summary": "foreground sleep",
        })
        gate.on_task_updated({"task_id": "fg-1", "patch": {"status": "completed"}})
        self.assertEqual(surfaced, [])
        self.assertEqual(gate.out.done, [])

    def test_a_backend_without_the_flag_is_untouched(self):
        """kimi/acp task_started has no `is_backgrounded` — must not be read as
        foreground, or every kimi completion goes silent."""
        gate, sched, surfaced = self.gate()
        self.row(gate, "tool-1")
        gate.on_task_started({"task_id": "bash-1", "tool_use_id": "tool-1"})
        gate.on_task_notification({
            "task_id": "bash-1", "tool_use_id": "tool-1",
            "status": "completed", "summary": "sleep 9",
        })
        self.assertEqual(len(surfaced), 1)
        self.assertEqual(gate.out._tools_by_id["tool-1"].status, "done")


class TerminalUpdateFlipsTheRowTest(_Gate):
    def test_a_terminal_update_flips_the_row_with_the_jobs_output(self):
        gate, sched, surfaced = self.gate()
        tool = self.row(gate, "tool-1")
        gate.on_task_started({
            "task_id": "t1", "tool_use_id": "tool-1",
            "description": "sandbox bg sleep", "is_backgrounded": True,
        })
        gate.note_launch_ack(
            "tool-1", "running in background with ID: t1. Output is being written to: /tmp/t1.output.")
        gate.on_task_updated({"task_id": "t1", "patch": {"status": "completed"}})
        self.assertEqual(tool.status, "done")
        self.assertIn("CC_BG_DONE", tool.result)
        self.assertIn("sandbox bg sleep", tool.result)
        self.assertEqual(len(surfaced), 1)

    def test_the_richer_notification_after_the_update_is_a_no_op(self):
        """task_updated flips the row (the ack named the log); the CLI's own
        notification for the same job follows within a tick and must not
        flip or surface it again."""
        gate, sched, surfaced = self.gate()
        tool = self.row(gate, "tool-1")
        gate.on_task_started({"task_id": "t1", "tool_use_id": "tool-1",
                              "description": "sandbox bg sleep"})
        gate.on_task_updated({"task_id": "t1", "patch": {"status": "completed"}})
        gate.on_task_notification({
            "task_id": "t1", "tool_use_id": "tool-1", "status": "completed",
            "summary": "Background command completed (exit code 0)",
            "output_file": "/tmp/gone.output",
        })
        self.assertEqual(len(surfaced), 1)
        self.assertEqual(len(gate.out.done), 1)

    def test_a_failed_job_is_an_error_row_with_its_output(self):
        gate, sched, surfaced = self.gate()
        tool = self.row(gate, "tool-1")
        gate.on_task_started({"task_id": "t1", "tool_use_id": "tool-1",
                              "description": "flaky build"})
        gate.on_task_notification({
            "task_id": "t1", "tool_use_id": "tool-1", "status": "failed",
            "summary": "flaky build (exit code 2)", "output_file": "/tmp/x",
        })
        self.assertEqual(tool.status, "error")
        self.assertIn("[failed]", tool.result)
        self.assertIn("CC_BG_DONE", tool.result)

    def test_a_killed_task_is_terminal(self):
        gate, sched, surfaced = self.gate()
        tool = self.row(gate, "tool-1")
        gate.on_task_started({"task_id": "t1", "tool_use_id": "tool-1"})
        gate.on_task_updated({"task_id": "t1", "patch": {"status": "killed"}})
        self.assertEqual(tool.status, "error")
        self.assertNotIn("t1", gate.task_tool_map)

    def test_reconcile_still_clears_a_stale_row_without_surfacing(self):
        gate, sched, surfaced = self.gate()
        tool = self.row(gate, "tool-1")
        gate.on_task_started({"task_id": "t1", "tool_use_id": "tool-1"})
        gate.reconcile(running=[])
        self.assertEqual(tool.status, "done")
        self.assertEqual(surfaced, [])


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

    def test_the_turn_ended_while_the_job_ran_and_the_cli_followed_up(self):
        self._replay("after")

    def test_a_foreground_command_kept_the_turn_live(self):
        self._replay("during")


if __name__ == "__main__":
    unittest.main()
