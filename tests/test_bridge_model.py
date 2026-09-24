"""A model the agent no longer offers must not stick.

Regression: a Grok session saved on a retired alias
(`deepseek-v4.1-flash-expires-on-*`) pushed it on every start, the agent
refused it, the bridge still reported it in the init result, the host saved
it, and the next start sent it again. A live pick of a real model worked,
but the picker could not tell a refusal from success either.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

_BRIDGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge")
if _BRIDGE not in sys.path:
    sys.path.insert(0, _BRIDGE)

OFFERED = [{"modelId": "grok-4.7"}, {"modelId": "grok-4.6"}, {"modelId": "deepseek-v4-flash"}]


class GrokModelTest(unittest.TestCase):
    def _bridge(self, model, reject=False):
        import grok_main
        b = grok_main.GrokBridge.__new__(grok_main.GrokBridge)
        b.session_id = "s1"
        b.model = model
        b.effort = ""
        b._available_models = list(OFFERED)
        b._agent_model = "grok-4.7"
        b.sent = []
        b.file_log = lambda *a, **k: None
        b.log = lambda *a, **k: None

        async def send_acp(method, params):
            b.sent.append((method, params))
            if reject:
                raise RuntimeError("Invalid params: unknown model id")
            return {"_meta": {"model": {"Ok": params.get("modelId")}}}
        b._send_acp = send_acp
        self.replies = []
        self._patch()
        return b

    def _patch(self):
        import acp.session as acp_session
        self._saved = (acp_session.send_result, acp_session.send_error)
        acp_session.send_result = lambda rid, res: self.replies.append(("ok", res))
        acp_session.send_error = lambda rid, code, msg: self.replies.append(("err", msg))
        self.addCleanup(self._restore, acp_session)

    def _restore(self, mod):
        mod.send_result, mod.send_error = self._saved

    def test_a_retired_model_is_not_pushed_and_falls_back(self):
        b = self._bridge("deepseek-v4.1-flash-expires-on-0910")
        self.assertFalse(asyncio.run(b.apply_model()))
        self.assertEqual(b.sent, [], "no set_model for a model the agent does not offer")
        self.assertEqual(b.model, "grok-4.7", "the init result reports what runs")

    def test_an_offered_model_is_applied(self):
        b = self._bridge("grok-4.6")
        self.assertTrue(asyncio.run(b.apply_model()))
        self.assertEqual(b.sent[0][0], "session/set_model")
        self.assertEqual(b.model, "grok-4.6")

    def test_a_refusal_falls_back_to_the_running_model(self):
        b = self._bridge("grok-4.6", reject=True)
        self.assertFalse(asyncio.run(b.apply_model()))
        self.assertEqual(b.model, "grok-4.7")

    def test_set_model_reports_a_refusal(self):
        b = self._bridge("grok-4.7")
        asyncio.run(b.handle_set_model(9, {"model": "deepseek-v4.1-flash-expires-on-0910"}))
        self.assertEqual(self.replies[-1][0], "err")
        self.assertIn("not available", self.replies[-1][1])
        self.assertEqual(b.model, "grok-4.7")

    def test_set_model_confirms_an_offered_model(self):
        b = self._bridge("grok-4.7")
        asyncio.run(b.handle_set_model(9, {"model": "deepseek-v4-flash"}))
        self.assertEqual(self.replies[-1], ("ok", {"ok": True, "model": "deepseek-v4-flash",
                                                   "effort": None}))


if __name__ == "__main__":
    unittest.main()
