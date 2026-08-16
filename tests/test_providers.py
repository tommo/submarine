#!/usr/bin/env python3
"""Pins for custom Anthropic-compatible provider env + name-collision remap."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from backend import providers, specs


def _glm_cfg(**extra):
    cfg = {
        "label": "GLM",
        "base_url": "https://open.bigmodel.cn/api/anthropic",
        "auth_token": "sk-test-glm",
        "opus_model": "glm-opus",
        "sonnet_model": "glm-sonnet",
        "haiku_model": "glm-haiku",
        "subagent_model": "glm-fast",
    }
    cfg.update(extra)
    return cfg


class TestResolveAuthToken(unittest.TestCase):
    def test_inline_token_wins(self):
        token = providers.resolve_auth_token({"auth_token": "sk-inline", "auth_env_var": "NOPE"})
        self.assertEqual(token, "sk-inline")

    def test_rejects_template_tokens(self):
        self.assertEqual(providers.resolve_auth_token({"auth_token": "from_env_var"}), "")
        self.assertEqual(providers.resolve_auth_token({"auth_token": "sk-your-key"}), "")
        self.assertEqual(
            providers.resolve_auth_token({"auth_token": "your-api-key-here"}),
            "",
        )

    def test_reads_auth_env_var(self):
        cfg = {"auth_env_var": "TEST_GLM_KEY"}
        with mock.patch.dict(os.environ, {"TEST_GLM_KEY": "sk-from-env"}, clear=False):
            self.assertEqual(providers.resolve_auth_token(cfg), "sk-from-env")

    def test_deepseek_legacy_setting(self):
        token = providers.resolve_auth_token(
            {}, name="deepseek", settings={"deepseek_api_key": "sk-legacy"}
        )
        self.assertEqual(token, "sk-legacy")


class TestDynamicEnv(unittest.TestCase):
    def test_sibling_auth_clear_token_mode(self):
        settings = {"custom_providers": {"glm": _glm_cfg()}}
        overwrite, defaults = providers.dynamic_env(settings, "glm")
        self.assertEqual(overwrite["ANTHROPIC_AUTH_TOKEN"], "sk-test-glm")
        self.assertEqual(overwrite["ANTHROPIC_API_KEY"], "")
        self.assertEqual(overwrite["ANTHROPIC_BASE_URL"], "https://open.bigmodel.cn/api/anthropic")

    def test_sibling_auth_clear_api_key_mode(self):
        settings = {"custom_providers": {"glm": _glm_cfg(auth_via_api_key=True)}}
        overwrite, _defaults = providers.dynamic_env(settings, "glm")
        self.assertEqual(overwrite["ANTHROPIC_API_KEY"], "sk-test-glm")
        self.assertEqual(overwrite["ANTHROPIC_AUTH_TOKEN"], "")

    def test_alias_env_names(self):
        settings = {"custom_providers": {"glm": _glm_cfg()}}
        overwrite, _defaults = providers.dynamic_env(settings, "glm")
        self.assertEqual(overwrite["ANTHROPIC_DEFAULT_OPUS_MODEL"], "glm-opus")
        self.assertEqual(overwrite["ANTHROPIC_DEFAULT_SONNET_MODEL"], "glm-sonnet")
        self.assertEqual(overwrite["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "glm-haiku")
        self.assertEqual(overwrite["CLAUDE_CODE_SUBAGENT_MODEL"], "glm-fast")

    def test_never_sets_anthropic_model(self):
        settings = {"custom_providers": {"glm": _glm_cfg()}}
        overwrite, defaults = providers.dynamic_env(settings, "glm")
        self.assertIn("ANTHROPIC_MODEL", overwrite)
        self.assertEqual(overwrite["ANTHROPIC_MODEL"], "")
        self.assertEqual(overwrite["ANTHROPIC_SMALL_FAST_MODEL"], "")
        self.assertNotIn("ANTHROPIC_MODEL", defaults)
        # Aliases go on ANTHROPIC_DEFAULT_*_MODEL only — never ANTHROPIC_MODEL.
        self.assertNotEqual(overwrite.get("ANTHROPIC_MODEL"), "glm-opus")

    def test_extra_env_goes_to_defaults(self):
        settings = {
            "custom_providers": {
                "glm": _glm_cfg(extra_env={"API_TIMEOUT_MS": "600000"})
            }
        }
        _overwrite, defaults = providers.dynamic_env(settings, "glm")
        self.assertEqual(defaults["API_TIMEOUT_MS"], "600000")
        self.assertEqual(defaults["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")


class TestProviderSpecAndCollision(unittest.TestCase):
    def test_pinned_default_false(self):
        spec = providers.provider_spec("glm", _glm_cfg())
        self.assertFalse(spec.pinned)
        spec_pinned = providers.provider_spec("glm", _glm_cfg(pinned=True))
        self.assertTrue(spec_pinned.pinned)

    def test_custom_uses_claude_bridge(self):
        spec = providers.provider_spec("glm", _glm_cfg())
        self.assertEqual(spec.bridge_script, "claude_main.py")

    def test_name_collision_remap(self):
        settings = {
            "custom_providers": {
                "kimi": {
                    "label": "Kimi",
                    "base_url": "https://api.moonshot.cn/anthropic",
                    "auth_token": "sk-moonshot",
                    "opus_model": "kimi-k2.5",
                }
            }
        }
        merged = specs.all_backends(settings)
        self.assertEqual(merged["kimi"].bridge_script, "kimi_main.py")
        self.assertEqual(merged["kimi"].name, "kimi")
        self.assertIn("kimi_api", merged)
        self.assertEqual(merged["kimi_api"].name, "kimi_api")
        self.assertTrue(merged["kimi_api"].label.endswith(" (API)"))
        self.assertEqual(merged["kimi_api"].bridge_script, "claude_main.py")
        self.assertFalse(merged["kimi_api"].pinned)

    def test_non_colliding_custom_keeps_name(self):
        settings = {"custom_providers": {"glm": _glm_cfg()}}
        merged = specs.all_backends(settings)
        self.assertIn("glm", merged)
        self.assertNotIn("glm_api", merged)
        self.assertEqual(merged["glm"].name, "glm")
        self.assertFalse(merged["glm"].pinned)

    def test_unknown_backend_falls_back_to_claude(self):
        self.assertEqual(specs.get("unknown").name, "claude")
        self.assertEqual(specs.get("grok").name, "grok")


if __name__ == "__main__":
    unittest.main()
