"""Moving a live session between Claude-bridge providers.

Official Claude and every `(CC) …` custom provider run the same bridge and
the same transcript, so a session can change provider and keep its history.
What cannot move is a concrete model id: `gpt-5.6-sol` means nothing to
Anthropic and `claude-opus-5` nothing to DeepSeek. The session used to carry
the old provider's model into the restart, which then failed at handshake.
"""
from __future__ import annotations

import os
import unittest

from tests.fakes import FakeClient, make_session

SETTINGS = {
    "custom_providers": {
        "deepseek": {
            "abbrev": "DS", "auth_env_var": "DEEPSEEK_API_KEY",
            "base_url": "https://api.deepseek.com/anthropic",
            "label": "DeepSeek", "opus_model": "deepseek-v4-pro[1m]",
            "sonnet_model": "deepseek-v4-pro[1m]", "haiku_model": "deepseek-v4-flash",
        },
        "stepfun": {
            "abbrev": "SF", "auth_env_var": "STEPFUN_API_KEY",
            "base_url": "https://api.stepfun.com/anthropic",
            "label": "StepFun", "opus_model": "gpt-5.6-sol",
            "sonnet_model": "gpt-5.6-sol", "haiku_model": "step-3",
        },
    },
    "default_models": {"claude": "opus"},
}


class ChangeProviderTest(unittest.TestCase):
    def setUp(self):
        for var in ("DEEPSEEK_API_KEY", "STEPFUN_API_KEY"):
            os.environ.setdefault(var, "test-key")

    def _session(self, backend, reported_model):
        s = make_session(client=FakeClient(), initialized=True,
                         backend=backend, settings=SETTINGS)
        s.start()
        s._on_init({"status": "initialized", "session_id": "sess-1",
                    "model": reported_model})
        return s

    def _init_models(self, s):
        return [(p or {}).get("model")
                for m, p, _cb in s.client.sent if m == "initialize"]

    def test_a_cc_session_moves_to_official_with_a_model_it_understands(self):
        s = self._session("stepfun", "gpt-5.6-sol")
        self.assertEqual(s.model, "gpt-5.6-sol")     # what StepFun reported
        ok, detail = s.change_backend("claude")
        self.assertTrue(ok)
        self.assertEqual(detail, "Claude · opus")
        self.assertEqual(s.backend, "claude")
        self.assertEqual(s.provider_label, "Claude")
        self.assertEqual(s.model, "opus", "the foreign model id was carried over")

    def test_official_moves_to_a_cc_provider(self):
        s = self._session("claude", "claude-opus-5")
        ok, detail = s.change_backend("deepseek")
        self.assertTrue(ok)
        self.assertEqual(s.backend, "deepseek")
        self.assertEqual(s.provider_label, "(CC) DeepSeek")
        self.assertEqual(s.model, "opus")
        self.assertEqual(detail, "(CC) DeepSeek · opus")

    def test_a_concrete_pick_falls_back_to_the_new_default(self):
        s = make_session(client=FakeClient(), initialized=True,
                         backend="claude", settings=SETTINGS,
                         model="claude-sonnet-5")
        s.start()
        s._on_init({"status": "initialized", "session_id": "s", "model": "claude-sonnet-5"})
        self.assertEqual(s.model_request, "claude-sonnet-5")
        s.change_backend("deepseek")
        self.assertEqual(s.model, "opus")

    def test_an_alias_survives_the_move(self):
        s = make_session(client=FakeClient(), initialized=True,
                         backend="claude", settings=SETTINGS, model="haiku")
        s.start()
        s._on_init({"status": "initialized", "session_id": "s", "model": "claude-haiku-4-5"})
        s.change_backend("stepfun")
        self.assertEqual(s.model, "haiku", "every CC provider maps the aliases")

    def test_the_restart_resumes_the_history_on_the_new_provider(self):
        s = self._session("stepfun", "gpt-5.6-sol")
        s.change_backend("claude")
        s.scheduler.fire_all()                 # sleep → wake
        params = [p for m, p, _cb in s.client.sent if m == "initialize"][-1]
        self.assertEqual(params.get("model"), "opus")
        self.assertEqual(params.get("resume"), "sess-1", "same transcript")

    def test_a_session_with_no_turn_yet_still_switches(self):
        s = make_session(client=FakeClient(), backend="stepfun", settings=SETTINGS)
        s.start()
        self.assertIsNone(s.session_id)
        ok, _detail = s.change_backend("claude")
        self.assertTrue(ok, "a fresh sheet has nothing to resume — just restart")
        self.assertEqual(self._init_models(s)[-1], "opus")
        self.assertIsNone(
            [p for m, p, _cb in s.client.sent if m == "initialize"][-1].get("resume"))

    def test_refusals_say_why(self):
        s = self._session("claude", "claude-opus-5")
        self.assertEqual(s.change_backend("claude"),
                         (False, "already on this provider"))
        ok, why = s.change_backend("grok")
        self.assertFalse(ok)
        self.assertIn("start a new session", why)
        ok, why = s.change_backend("nope")
        self.assertFalse(ok)
        self.assertIn("unknown provider", why)
        s.query("busy now")
        ok, why = s.change_backend("deepseek")
        self.assertFalse(ok)
        self.assertIn("mid-turn", why)


if __name__ == "__main__":
    unittest.main()
