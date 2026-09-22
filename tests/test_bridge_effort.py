"""Bridge side of a live effort change: ACP config-option mapping (Kimi),
Codex's per-turn `effort`, pi's spawn flag and level names."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

_BRIDGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge")
if _BRIDGE not in sys.path:
    sys.path.insert(0, _BRIDGE)

KIMI_THINKING = {"type": "select", "id": "thinking", "category": "thought_level",
                 "currentValue": "high",
                 "options": [{"value": "low"}, {"value": "high"}, {"value": "max"}]}


class AcpEffortTest(unittest.TestCase):
    def _bridge(self):
        from kimi_main import KimiBridge
        b = KimiBridge.__new__(KimiBridge)
        b._config_options = [dict(KIMI_THINKING, options=list(KIMI_THINKING["options"]))]
        b.session_id = "s1"
        b.effort = ""
        b.sent = []

        async def send_acp(method, params):
            b.sent.append((method, params))
            return {}
        b._send_acp = send_acp
        b.file_log = lambda *a, **k: None
        return b

    def test_levels_map_to_the_closest_advertised_value(self):
        b = self._bridge()
        opt = b._effort_option()
        self.assertEqual(opt["id"], "thinking")
        got = {lvl: b._effort_value(opt, lvl)
               for lvl in ("low", "medium", "high", "xhigh", "max", "minimal")}
        self.assertEqual(got, {"low": "low", "medium": "high", "high": "high",
                               "xhigh": "max", "max": "max", "minimal": "low"})

    def test_apply_sends_set_config_option_once(self):
        b = self._bridge()
        b.effort = "max"
        self.assertEqual(asyncio.run(b.apply_effort()), "max")
        self.assertEqual(b.sent, [("session/set_config_option",
                                   {"sessionId": "s1", "configId": "thinking", "value": "max"})])
        asyncio.run(b.apply_effort())            # already there: no second call
        self.assertEqual(len(b.sent), 1)

    def test_no_option_means_no_live_path(self):
        b = self._bridge()
        b._config_options = []
        b.effort = "high"
        self.assertEqual(asyncio.run(b.apply_effort()), "")
        self.assertEqual(b.sent, [])


class CodexEffortTest(unittest.TestCase):
    def test_host_levels_become_codex_reasoning_efforts(self):
        from codex_main import CodexBridge
        self.assertEqual(CodexBridge._codex_effort("max"), "xhigh")
        self.assertEqual(CodexBridge._codex_effort("medium"), "medium")
        self.assertEqual(CodexBridge._codex_effort(None), "")


class PiEffortTest(unittest.TestCase):
    def test_host_levels_become_pi_thinking_levels(self):
        from pi_main import PiBridge
        self.assertEqual(PiBridge._pi_level("max"), "xhigh")
        self.assertEqual(PiBridge._pi_level("off"), "off")
        self.assertEqual(PiBridge._pi_level("bogus"), "")


if __name__ == "__main__":
    unittest.main()
