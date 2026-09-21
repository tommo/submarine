"""Queued-prompt chrome above ◎ — port of sublime-claude's queue phantom."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes import FakeClient, FakeChrome, make_session
from ui.view import (
    format_queue_phantom_html,
    format_sleep_banner_html,
    sleep_banner_anchor,
)


class TestQueuePhantomHtml(unittest.TestCase):
    def test_empty_queue_is_still_the_hairline(self):
        html = format_queue_phantom_html([])
        self.assertIn('id="submarine-queue"', html)
        self.assertIn("border-top:1px solid", html)
        self.assertIn("height:0", html)
        self.assertNotIn("⏳", html)
        self.assertNotIn("send_now", html)

    def test_queued_rows_have_send_now_and_drop(self):
        html = format_queue_phantom_html(["hello world", "second"])
        self.assertIn("⏳ hello world", html)
        self.assertIn('href="send_now"', html)
        self.assertIn('href="send:0"', html)
        self.assertIn('href="drop:0"', html)
        self.assertIn('href="send:1"', html)
        self.assertIn('href="drop:1"', html)
        self.assertIn("send now", html)
        self.assertIn("Ctrl+↵ send now", html)

    def test_long_preview_is_clipped(self):
        html = format_queue_phantom_html(["x" * 80])
        self.assertIn("…", html)
        self.assertNotIn("x" * 80, html)


class TestSleepBannerHtml(unittest.TestCase):
    def test_paused_hint_is_strong(self):
        html = format_sleep_banner_html("⏸ Session paused — press Enter to wake")
        self.assertIn("Session paused", html)
        self.assertIn("font-weight:bold", html)
        self.assertIn("border-left:3px solid", html)
        self.assertIn("padding:12px 10px 14px 10px", html)
        self.assertIn("height:10px", html)
        self.assertIn("height:2px", html)
        self.assertNotIn("margin:10px", html)

    def test_banner_anchors_on_the_last_nonempty_line(self):
        self.assertEqual(sleep_banner_anchor("@done(1s)\n◎ "), 10)
        self.assertEqual(sleep_banner_anchor("hello\n@done(1s)\n"), 6)

    def test_html_is_escaped(self):
        html = format_sleep_banner_html("<script>x</script>")
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)


class TestQueuePhantomNavigate(unittest.TestCase):
    def _busy(self):
        s = make_session(initialized=True, client=FakeClient())
        s.turn.begin_query()
        s._queued_prompts = ["one", "two"]
        return s

    def test_drop_removes_that_row(self):
        s = self._busy()
        s._on_queue_phantom_navigate("drop:0")
        self.assertEqual(s._queued_prompts, ["two"])
        self.assertEqual(s.chrome.queues[-1], ["two"])

    def test_send_now_header_cancels_and_sends_top(self):
        s = self._busy()
        s._on_queue_phantom_navigate("send_now")
        self.assertTrue(s._send_now_pending or s.turn.kind == "interrupting"
                        or s._interrupting)

    def test_update_paints_via_chrome(self):
        s = make_session()
        s._queued_prompts = ["later"]
        s._update_queue_phantom()
        self.assertEqual(s.chrome.queues[-1], ["later"])


if __name__ == "__main__":
    unittest.main()


class TestEditQueued(unittest.TestCase):
    def _session(self):
        from tests.fakes import FakeClient, make_session
        from tests.test_single_view import RecordingWindow
        from ui.host import HostView, set_ui_mode_override
        from ui.view import SubmarineOutputView
        from core.registry import default_registry
        default_registry.clear(); HostView.reset(); set_ui_mode_override("single")
        win = RecordingWindow(); out = SubmarineOutputView(win)
        s = make_session(output=out, chrome=out, window=win, registry=default_registry,
                         client=FakeClient(), initialized=True)
        s.session_id = "sess-q"; s._composer_allowed = True
        HostView.for_window(win).attach(win, s)
        return s, out

    def test_chip_has_an_edit_link(self):
        from ui.view import format_queue_phantom_html
        html = format_queue_phantom_html(["do this"], "hint")
        self.assertIn('href="edit:0"', html)
        self.assertIn('href="send:0"', html)
        self.assertIn('href="drop:0"', html)

    def test_edit_moves_the_message_into_the_composer(self):
        s, out = self._session()
        s.query("first")                      # working → the next goes to the queue
        s.queue_prompt("second thing")
        s.queue_prompt("third thing")
        self.assertEqual(s._queued_prompts, ["second thing", "third thing"])
        s.scheduler.fire_due(50)              # the sticky composer opens
        s._on_queue_phantom_navigate("edit:1")
        self.assertEqual(s._queued_prompts, ["second thing"])
        self.assertTrue(out.is_input_mode())
        self.assertEqual(out.get_input_text(), "third thing")

    def test_edit_keeps_a_draft_being_typed(self):
        s, out = self._session()
        s.query("first")
        s.queue_prompt("queued one")
        s.scheduler.fire_due(50)
        out.set_composer_text("typing now")
        s.edit_queued(0)
        self.assertEqual(s._queued_prompts, [])
        self.assertEqual(out.get_input_text(), "queued one\ntyping now")

    def test_out_of_range_is_a_noop(self):
        s, _out = self._session()
        self.assertFalse(s.edit_queued(3))


class TestQueueAfterInterrupt(unittest.TestCase):
    """A message typed while the turn is being cancelled is not injected into
    the dying turn (lost, or delivered a round late); it waits in the queue
    and starts its own turn once the cancel is acknowledged."""

    def _session(self):
        from tests.fakes import FakeClient, make_session
        client = FakeClient()
        s = make_session(client=client, initialized=True)
        s.backend = "claude"
        s.session_id = "sess-i"
        return s, client

    def test_no_inject_while_interrupting_then_fires_after_ack(self):
        s, client = self._session()
        s.query("first")
        self.assertTrue(s.working)
        s.interrupt()
        self.assertEqual(s.turn.kind, "interrupting")
        s.queue_prompt("after the cancel")
        self.assertEqual([m for m, _p, _cb in client.sent if m == "inject_message"], [],
                         "injected into the turn being cancelled")
        self.assertEqual(s._queued_prompts, ["after the cancel"])
        # the bridge acknowledges the cancel
        _m, _p, cb = [c for c in client.sent if c[0] == "query"][-1]
        cb({"status": "interrupted"})
        s.scheduler.fire_all()
        queries = [p.get("prompt") for m, p, _cb in client.sent if m == "query"]
        self.assertEqual(queries[-1], "after the cancel", "queued message not sent after the ACK")
        self.assertEqual(s._queued_prompts, [])

    def test_a_normal_mid_turn_message_is_still_injected(self):
        s, client = self._session()
        s.query("first")
        s.queue_prompt("meanwhile")
        self.assertEqual([m for m, _p, _cb in client.sent if m == "inject_message"], ["inject_message"])

    def test_queued_message_goes_before_a_background_notification_turn(self):
        """Esc kills background jobs; their completions used to start a
        notification turn first and push the user's message one round back.
        (Grok: every open terminal reports on cancel.)"""
        s, client = self._session()
        s.backend = "grok"
        s.query("first")
        s.bg.on_task_started({"task_id": "t1", "tool_use_id": "u1", "description": "long job"})
        s.interrupt()
        s.queue_prompt("what I actually want")
        s.bg.on_task_notification({"task_id": "t1", "status": "failed", "summary": "long job"})
        _m, _p, cb = [c for c in client.sent if c[0] == "query"][-1]
        cb({"status": "interrupted"})
        s.scheduler.fire_all()
        queries = [p.get("prompt") for m, p, _cb in client.sent if m == "query"]
        self.assertEqual(len(queries), 2, "expected exactly one new turn after the cancel")
        self.assertTrue(queries[-1].endswith("what I actually want"), queries[-1])
        self.assertIn("<task-notification>", queries[-1])       # the completion rides along
        self.assertEqual(s.bg.pending_notifications, [])

    def test_after_the_ack_messages_inject_again(self):
        """Regression: gating on flags that outlive the ACK blocked every
        later mid-turn message from being injected."""
        s, client = self._session()
        s.query("first")
        s.interrupt()
        _m, _p, cb = [c for c in client.sent if c[0] == "query"][-1]
        cb({"status": "interrupted"})
        s.scheduler.fire_all()
        self.assertFalse(s.working)
        s.query("second")                       # a fresh turn after the cancel
        self.assertTrue(s.working)
        s.queue_prompt("mid-turn note")
        self.assertEqual([m for m, _p, _cb in client.sent if m == "inject_message"], ["inject_message"])

    def test_resumed_leftover_stream_still_takes_injects(self):
        s, client = self._session()
        s.query("first")
        s.interrupt()
        _m, _p, cb = [c for c in client.sent if c[0] == "query"][-1]
        cb({"status": "interrupted"})
        s.scheduler.fire_all()
        s._resume_interrupt_stream()             # leftover text keeps the turn alive, no query()
        self.assertTrue(s.working)
        s.queue_prompt("while it streams")
        self.assertEqual([m for m, _p, _cb in client.sent if m == "inject_message"], ["inject_message"])

