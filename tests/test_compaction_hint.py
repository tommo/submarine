"""A compacting agent shows it in the sheet instead of looking hung.

Grok compacts a large context before the turn. When it announces it
(`auto_compact_started` / `auto_compact_completed` on `_x.ai/session/update`)
the bridge dropped both; when it does not, the turn was silent for minutes.
Now: a live hint under the busy mark, a note when done, and a silence hint
after 20s that names compaction when the context is near its window.
"""
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


class BridgeCompactionTest(unittest.TestCase):
    def _bridge(self):
        import grok_main
        import acp.query as q
        import acp.updates as u
        b = grok_main.GrokBridge.__new__(grok_main.GrokBridge)
        b.model = "grok-4.7"
        b._available_models = [{"modelId": "grok-4.7", "_meta": {"totalContextTokens": 500000}}]
        b.file_log = lambda *a, **k: None
        self.sent = []
        saved = (q.send_notification, u.send_notification)
        q.send_notification = u.send_notification = lambda m, p: self.sent.append(p)
        self.addCleanup(lambda: (setattr(q, "send_notification", saved[0]),
                                 setattr(u, "send_notification", saved[1])))
        return b

    def test_announced_compaction_is_forwarded(self):
        b = self._bridge()
        b._handle_compaction("auto_compact_started", {
            "tokens_used": 403518, "context_window": 500000, "percentage": 81})
        b._handle_compaction("auto_compact_completed", {
            "tokens_before": 403518, "tokens_after": 10634, "elapsed_ms": 43911})
        self.assertEqual(self.sent[0]["subtype"], "compaction_started")
        self.assertEqual(self.sent[0]["data"]["percentage"], 81)
        self.assertEqual(self.sent[1]["subtype"], "compaction")
        self.assertEqual(self.sent[1]["data"]["message"],
                         "Context compacted: 404k → 11k tokens in 44s")

    def test_a_silent_prompt_is_reported_once(self):
        b = self._bridge()
        b.SILENCE_HINT_S = 0.01
        b._last_session_tool_ts = 0
        b._last_total_tokens = 411333
        asyncio.run(b._silence_watch(1.0))
        self.assertEqual(len(self.sent), 1)
        d = self.sent[0]["data"]
        self.assertEqual((self.sent[0]["subtype"], d["tokens_used"], d["context_window"]),
                         ("agent_silent", 411333, 500000))

    def test_a_prompt_that_answers_is_not_reported(self):
        b = self._bridge()
        b.SILENCE_HINT_S = 0.01
        b._last_session_tool_ts = 10.0          # an update after the send
        asyncio.run(b._silence_watch(1.0))
        self.assertEqual(self.sent, [])


class HostHintTest(unittest.TestCase):
    def _session(self):
        out = FakeOutput()
        s = make_session(output=out, client=FakeClient(), initialized=True)
        s.backend = "grok"
        s.events.backend = "grok"
        s.turn.begin_query()
        return s, out

    def _system(self, s, subtype, data):
        s.events.dispatch("message", {"type": "system", "subtype": subtype, "data": data})

    def test_announced_compaction_shows_then_notes(self):
        s, out = self._session()
        self._system(s, "compaction_started",
                     {"tokens_used": 403518, "context_window": 500000, "percentage": 81})
        self.assertEqual(out.retry_hints[-1], "⟳ compacting context · 81% of 500k")
        self._system(s, "compaction", {"message": "Context compacted: 404k → 11k tokens in 44s"})
        self.assertEqual(out.retry_hints[-1], "")

    def test_silence_near_the_window_names_compaction(self):
        s, out = self._session()
        self._system(s, "agent_silent",
                     {"seconds": 20, "tokens_used": 411333, "context_window": 500000})
        self.assertEqual(out.retry_hints[-1],
                         "⟳ no reply for 20s — probably compacting context (411k of 500k)")

    def test_silence_falls_back_to_the_saved_context(self):
        s, out = self._session()
        s.context_usage = {"total_tokens": 420000}
        self._system(s, "agent_silent", {"seconds": 20, "context_window": 500000})
        self.assertIn("probably compacting", out.retry_hints[-1])

    def test_silence_with_room_just_says_waiting(self):
        s, out = self._session()
        self._system(s, "agent_silent",
                     {"seconds": 20, "tokens_used": 50000, "context_window": 500000})
        self.assertEqual(out.retry_hints[-1], "⋯ no reply for 20s — still waiting on the agent")

    def test_the_hint_goes_when_output_or_the_turn_end_arrives(self):
        s, out = self._session()
        self._system(s, "agent_silent", {"seconds": 20})
        s.events.dispatch("message", {"type": "text_delta", "text": "hi"})
        self.assertEqual(out.retry_hints[-1], "")
        self._system(s, "agent_silent", {"seconds": 20})
        s.events.dispatch("message", {"type": "result", "session_id": "x"})
        self.assertEqual(out.retry_hints[-1], "")


class KimiCompactionTest(HostHintTest):
    """Kimi compacts at the start of a turn and, in the same breath, sends
    "Compaction is blocked by the current turn" for a second trigger."""

    def _text(self, s, text):
        s.events.dispatch("message", {"type": "text_delta", "text": text})

    def test_the_blocked_line_after_a_start_is_dropped(self):
        s, out = self._session()
        s.backend = s.events.backend = "kimi"
        self._text(s, "Compacting conversation context…")
        self.assertEqual(out.texts[-1].strip(), "*Compacting conversation context…*")
        self.assertTrue(out.retry_hints[-1].startswith("⟳ compacting context"))
        self._text(s, "Compaction is blocked by the current turn; retry when the turn is idle.")
        self.assertNotIn("blocked", "".join(out.texts))
        self.assertTrue(out.retry_hints[-1].startswith("⟳ compacting context"),
                        "a dropped chunk does not clear the hint")
        self._text(s, "Here is the fix.")
        self.assertEqual(out.retry_hints[-1], "", "real output ends the hint")

    def test_a_blocked_line_on_its_own_still_shows(self):
        s, out = self._session()
        self._text(s, "Compaction is blocked by the current turn; retry when the turn is idle.")
        self.assertIn("blocked", "".join(out.texts))


if __name__ == "__main__":
    unittest.main()
