"""Kimi after Esc: the killed shells make Kimi start a turn of its own, and
while it runs Kimi answers every new prompt with an instant, empty
`end_turn` — the prompt never runs. The bridge now reads that as a refusal,
stops Kimi's turn and resends."""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

_BRIDGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge")
if _BRIDGE not in sys.path:
    sys.path.insert(0, _BRIDGE)


def _kimi():
    from kimi_main import KimiBridge
    b = KimiBridge.__new__(KimiBridge)
    b.file_log = lambda *a, **k: None
    b.session_id = "s1"
    b._last_session_tool_ts = 0.0
    b._last_interrupt_ts = 0.0
    return b


class RefusalTest(unittest.TestCase):
    def test_an_instant_empty_end_turn_after_esc_is_a_refusal(self):
        b = _kimi()
        b._last_interrupt_ts = time.time() - 5
        sent = time.time()
        self.assertTrue(b._silently_refused({"stopReason": "end_turn"}, sent))

    def test_not_a_refusal_without_a_recent_esc(self):
        b = _kimi()
        self.assertFalse(b._silently_refused({"stopReason": "end_turn"}, time.time()))

    def test_not_a_refusal_when_the_agent_said_something(self):
        b = _kimi()
        b._last_interrupt_ts = time.time() - 5
        sent = time.time() - 0.2
        b._last_session_tool_ts = time.time()
        self.assertFalse(b._silently_refused({"stopReason": "end_turn"}, sent))

    def test_not_a_refusal_for_other_backends(self):
        import grok_main
        g = grok_main.GrokBridge.__new__(grok_main.GrokBridge)
        g._last_interrupt_ts = time.time()
        g._last_session_tool_ts = 0
        self.assertFalse(g._silently_refused({"stopReason": "end_turn"}, time.time()))


class ResendTest(unittest.TestCase):
    def test_a_refused_prompt_is_resent_after_stopping_the_agent_turn(self):
        import acp.query as q
        b = _kimi()
        b._last_interrupt_ts = time.time() - 3
        b._prompt_fut = None
        b._query_req_id = None
        b._cancel_in_flight = False
        b._prompt_cancelled = False
        b._orphan_turn_notified = False
        b._pending_ask_followup = None
        b._build_prompt_blocks = lambda prompt, images: [{"type": "text", "text": prompt}]
        calls = {"send": 0, "cancel": []}

        async def send_prompt(blocks):
            calls["send"] += 1
            if calls["send"] == 1:
                return {"stopReason": "end_turn"}            # refused
            b._last_session_tool_ts = time.time() + 1         # it answered
            return {"stopReason": "end_turn"}
        b._send_prompt = send_prompt

        async def cancel(**kw):
            calls["cancel"].append(kw.get("reason"))
            b._prompt_cancelled = True
            b._cancel_in_flight = True
        b._cancel_agent_turn = cancel
        out = []
        saved = (q.send_result, q.send_notification, q.send_error)
        q.send_result = lambda rid, res: out.append(("result", res))
        q.send_notification = lambda m, p: out.append(("notify", p.get("type")))
        q.send_error = lambda rid, code, msg: out.append(("error", msg))
        self.addCleanup(lambda: (setattr(q, "send_result", saved[0]),
                                 setattr(q, "send_notification", saved[1]),
                                 setattr(q, "send_error", saved[2])))
        asyncio.run(b.handle_query(7, {"prompt": "you there?"}))
        self.assertEqual(calls["send"], 2, "the prompt was resent")
        self.assertEqual(calls["cancel"], ["refused_prompt"])
        results = [r for k, r in out if k == "result"]
        self.assertTrue(results)
        self.assertNotEqual(results[-1].get("status"), "interrupted",
                            "the resent prompt is not read as cancelled")


class PostInterruptTurnTest(unittest.TestCase):
    def _run(self, since_esc, host_live=False, kinds=("tool_call", "tool_call")):
        b = _kimi()
        b._last_interrupt_ts = time.time() - since_esc
        b._post_interrupt_cancelled = False
        reasons = []

        async def cancel(**kw):
            reasons.append(kw.get("reason"))
        b._cancel_agent_turn = cancel

        async def go():
            for k in kinds:
                b._stop_post_interrupt_turn(k, host_live)
            await asyncio.sleep(0.01)
        asyncio.run(go())
        return reasons

    def test_kimis_own_turn_right_after_esc_is_stopped_once(self):
        self.assertEqual(self._run(2), ["post_interrupt_agent_turn"])

    def test_later_or_during_our_prompt_it_is_left_alone(self):
        self.assertEqual(self._run(60), [])
        self.assertEqual(self._run(2, host_live=True), [])


if __name__ == "__main__":
    unittest.main()
