"""resolve_init_model chain; new session ignores leftover view stamp."""
from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.registry import resolve_init_model, resolve_spawn_model


class TestResolveInitModel(unittest.TestCase):
    def setUp(self):
        self.resolve = resolve_init_model

    def test_new_session_uses_default(self):
        self.assertEqual(
            self.resolve(default_model="grok-4.6"),
            "grok-4.6",
        )

    def test_profile_beats_default(self):
        self.assertEqual(
            self.resolve(
                profile_model="deepseek-v4-pro",
                default_model="grok-4.6",
            ),
            "deepseek-v4-pro",
        )

    def test_requested_beats_profile(self):
        self.assertEqual(
            self.resolve(
                requested_model="deepseek-v4-flash-vision-exp",
                profile_model="deepseek-v4-pro",
                default_model="grok-4.6",
            ),
            "deepseek-v4-flash-vision-exp",
        )

    def test_spawn_fork_inherits_source_model(self):
        self.assertEqual(
            resolve_spawn_model(
                source_model="deepseek-v4-flash",
                forking=True,
            ),
            "deepseek-v4-flash",
        )
        self.assertEqual(
            resolve_spawn_model(
                requested="deepseek-v4-pro",
                source_model="deepseek-v4-flash",
                forking=True,
            ),
            "deepseek-v4-pro",
        )
        self.assertIsNone(
            resolve_spawn_model(
                source_model="deepseek-v4-flash",
                forking=False,
            ),
        )

    def test_live_session_beats_default(self):
        self.assertEqual(
            self.resolve(
                session_model="deepseek-v4-flash",
                default_model="grok-4.6",
            ),
            "deepseek-v4-flash",
        )

    def test_new_session_ignores_view_stamp(self):
        self.assertEqual(
            self.resolve(
                view_model="deepseek-v4-pro",
                default_model="grok-4.6",
            ),
            "grok-4.6",
        )

    def test_resume_view_stamp_beats_default(self):
        self.assertEqual(
            self.resolve(
                view_model="deepseek-v4-pro",
                default_model="grok-4.6",
                resume=True,
            ),
            "deepseek-v4-pro",
        )

    def test_saved_beats_default(self):
        self.assertEqual(
            self.resolve(
                saved_model="deepseek-v4-pro",
                default_model="grok-4.6",
            ),
            "deepseek-v4-pro",
        )

    def test_resume_uses_default_if_nothing_saved(self):
        self.assertEqual(
            self.resolve(default_model="grok-4.6", resume=True),
            "grok-4.6",
        )

    def test_resume_keeps_saved_deepseek(self):
        self.assertEqual(
            self.resolve(
                saved_model="deepseek-v4-pro",
                default_model="grok-4.6",
                resume=True,
            ),
            "deepseek-v4-pro",
        )

    def test_profile_beats_live_and_saved(self):
        self.assertEqual(
            self.resolve(
                profile_model="deepseek-v4-flash",
                session_model="grok-4.6",
                saved_model="grok-4.6",
                default_model="grok-4.6",
            ),
            "deepseek-v4-flash",
        )

    def test_blank_strings_are_skipped(self):
        self.assertEqual(
            self.resolve(
                session_model="  ",
                view_model="",
                saved_model="deepseek-v4-pro",
                default_model="grok-4.6",
            ),
            "deepseek-v4-pro",
        )


class _ModelBridge:
    """Minimal stand-in for AcpBridge.normalize/resolve_applied_model."""

    def __init__(self):
        from acp_base import AcpBridge
        self.DEFAULT_MODEL = "grok-4.6"
        self.MODEL_ALIASES = dict(AcpBridge.MODEL_ALIASES)
        self.MODEL_ALIASES.update({
            "grok-4.5": "grok-4.6",
            "deepseek-pro": "deepseek-v4-pro",
        })
        self.normalize_model = AcpBridge.normalize_model.__get__(self, _ModelBridge)
        self.resolve_applied_model = AcpBridge.resolve_applied_model.__get__(
            self, _ModelBridge)


class TestResolveAppliedModel(unittest.TestCase):
    def setUp(self):
        self.b = _ModelBridge()

    def test_trusts_agent_current(self):
        self.assertEqual(
            self.b.resolve_applied_model("deepseek-v4-pro", "grok-4.6"),
            "grok-4.6",
        )

    def test_accepts_alias_of_requested(self):
        self.assertEqual(
            self.b.resolve_applied_model("deepseek-pro", "deepseek-v4-pro"),
            "deepseek-v4-pro",
        )

    def test_empty_current_keeps_requested(self):
        self.assertEqual(
            self.b.resolve_applied_model("deepseek-v4-pro", None),
            "deepseek-v4-pro",
        )


class TestClearDispatch(unittest.TestCase):
    def test_acp_exposes_clear(self):
        from acp_base import AcpBridge

        class _B(AcpBridge):
            def __init__(self):
                pass

        table = AcpBridge.extra_dispatch(_B())
        self.assertIn("clear", table)
        self.assertEqual(table["clear"].__name__, "handle_clear")


class TestGrokSpawnModelFlag(unittest.TestCase):
    def test_spawn_passes_session_model(self):
        from grok_main import GrokBridge

        class _Spawn(GrokBridge):
            def __init__(self):
                self.model = "deepseek-v4-pro"
                self.effort = ""
                self.permission_mode = "default"
                self._always_approve = False

        argv = _Spawn().agent_argv()
        self.assertIn("--model", argv)
        self.assertIn("deepseek-v4-pro", argv)


class TestGrokVisionCatalog(unittest.TestCase):
    def test_flash_vision_is_vision(self):
        from backend.grok import (
            GROK_MODELS, model_supports_vision, normalize_grok_model,
        )
        ids = [mid for mid, _label in GROK_MODELS]
        self.assertIn("deepseek-v4-flash-vision-exp", ids)
        self.assertTrue(model_supports_vision("deepseek-v4-flash-vision-exp"))
        self.assertTrue(model_supports_vision("ds-vision"))
        # DeepSeek V4 / V4.1 BYOK advertise read_image (ACP read_file still
        # rejects binary). Pre-v4 DeepSeek stays off — the tool call can
        # hard-fail the turn.
        self.assertTrue(model_supports_vision("deepseek-v4-flash"))
        self.assertTrue(model_supports_vision("deepseek-v4-pro"))
        self.assertTrue(model_supports_vision("ds-flash"))
        self.assertFalse(model_supports_vision("deepseek-chat"))
        self.assertFalse(model_supports_vision("deepseek-reasoner"))
        self.assertEqual(
            normalize_grok_model("ds-flash-vision"),
            "deepseek-v4-flash-vision-exp",
        )


if __name__ == "__main__":
    unittest.main()


class ProviderLineTest(unittest.TestCase):
    """A fresh sheet says what it runs on before the first prompt."""

    def test_new_session_prints_provider_model_and_effort(self):
        from tests.fakes import FakeClient, make_session
        s = make_session(client=FakeClient(), backend="grok",
                         settings={"effort": "high"})
        s.start()
        s._on_init({"status": "initialized", "session_id": "y", "model": "grok-4.7"})
        self.assertEqual(s.output.texts, ["\n*Grok · Grok 4.7 (grok-4.7) · effort high*\n"])

    def test_a_resumed_session_does_not(self):
        from tests.fakes import FakeClient, make_session
        s = make_session(client=FakeClient(), backend="claude", resume_id="old")
        s.start()
        s._on_init({"status": "initialized", "session_id": "old"})
        self.assertEqual(s.output.texts, [])


class EffortOnResumeTest(unittest.TestCase):
    """Effort is a process option, not transcript state: a woken Claude
    session has to send it again. Opus 5.5 defaults to `medium` (Opus 5 to
    `high`), so a resume that skipped it quietly ran one level lower."""

    def test_a_resumed_claude_session_sends_the_configured_effort(self):
        from tests.fakes import FakeClient, make_session
        s = make_session(client=FakeClient(), backend="claude",
                         resume_id="old", settings={"effort": "high"})
        s.start()
        params = [p for m, p, _cb in s.client.sent if m == "initialize"][0]
        self.assertEqual(params.get("resume"), "old")
        self.assertEqual(params.get("effort"), "high")

    def test_the_opus_alias_is_labelled_opus_5_5(self):
        from backend import specs
        models = dict(specs.BACKENDS["claude"].default_models)
        self.assertEqual(models["opus"], "Opus 5.5")
        self.assertIn("claude-opus-5-5", models)
