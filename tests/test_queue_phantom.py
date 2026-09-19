"""Queued-prompt chrome above ◎ — port of sublime-claude's queue phantom."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes import FakeClient, FakeChrome, make_session
from ui.view import format_queue_phantom_html


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
