"""Upstream 2e6e415: real ACP session/fork + context-window 400 recovery."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from acp_base import AcpBridge  # noqa: E402
from backend.grok import (  # noqa: E402
    apply_deepseek_shared_window_config,
    format_input_too_large,
    format_shared_window_overflow,
    is_context_overflow_error,
    model_supports_vision,
    parse_input_too_large,
    parse_shared_window_overflow,
    patch_deepseek_shared_window_toml,
    rewrite_grok_query_error,
    usable_prompt_tokens,
)
from grok_main import GrokBridge  # noqa: E402


# ── ACP session/fork ──────────────────────────────────────────────────────────

class TestParseForkId(unittest.TestCase):
    def test_prefers_new_id(self):
        self.assertEqual(
            AcpBridge._parse_fork_session_id({"sessionId": "new-1"}, "old-1"),
            "new-1")

    def test_rejects_source_id(self):
        self.assertIsNone(AcpBridge._parse_fork_session_id(
            {"sessionId": "old-1"}, "old-1"))

    def test_grok_new_session_id(self):
        self.assertEqual(
            AcpBridge._parse_fork_session_id(
                {"newSessionId": "abc", "sessionId": "old-1"}, "old-1"),
            "abc")

    def test_nested_session_object(self):
        self.assertEqual(
            AcpBridge._parse_fork_session_id(
                {"session": {"sessionId": "nested-2"}}, "old-1"),
            "nested-2")

    def test_junk_is_none(self):
        self.assertIsNone(AcpBridge._parse_fork_session_id(None, "old-1"))
        self.assertIsNone(AcpBridge._parse_fork_session_id({}, "old-1"))


class TestTryForkSession(unittest.TestCase):
    def _bridge(self, responder):
        b = GrokBridge()
        b.cwd = "/tmp"
        b._additional_dirs = []
        b.session_id = "src-1"
        b._ingested = []
        b._send_acp = responder
        b._ingest_session_result = lambda r: b._ingested.append(r)
        b.file_log = lambda m: None
        b.log = lambda m: None
        return b

    def test_tries_acp_then_xai(self):
        calls = []

        async def _send(method, params, timeout=None):
            calls.append((method, params))
            if method == "session/fork":
                raise RuntimeError("Method not found")
            if method == "_x.ai/session/fork":
                return {"sessionId": "forked-9"}
            raise RuntimeError("nope")

        b = self._bridge(_send)
        self.assertTrue(asyncio.run(b._try_fork_session("src-1", [])))
        self.assertEqual(b.session_id, "forked-9")
        self.assertEqual([c[0] for c in calls][:2],
                         ["session/fork", "_x.ai/session/fork"])
        self.assertEqual(calls[0][1]["sessionId"], "src-1")
        self.assertEqual(calls[1][1]["sourceSessionId"], "src-1")

    def test_never_uses_session_load(self):
        calls = []

        async def _send(method, params, timeout=None):
            calls.append(method)
            raise RuntimeError("Method not found")

        b = self._bridge(_send)
        self.assertFalse(asyncio.run(b._try_fork_session("src-1", [])))
        self.assertNotIn("session/load", calls)

    def test_rejects_a_result_that_reuses_the_source(self):
        async def _send(method, params, timeout=None):
            return {"sessionId": "src-1"}

        b = self._bridge(_send)
        self.assertFalse(asyncio.run(b._try_fork_session("src-1", [])))
        self.assertEqual(b.session_id, "src-1")

    def test_fork_params_carry_cwd_and_mcp(self):
        b = self._bridge(None)
        p = b._fork_params_for_method("session/fork", "s", [{"name": "sub"}])
        self.assertEqual(p["sessionId"], "s")
        self.assertEqual(p["cwd"], "/tmp")
        self.assertEqual(p["mcpServers"], [{"name": "sub"}])

    def test_source_has_no_empty_fork_lie(self):
        with open(os.path.join(_BRIDGE, "acp", "session.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("_try_fork_session", src)
        self.assertNotIn("ACP has no fork", src)
        self.assertIn("session/fork", src)
        self.assertIn("_x.ai/session/fork", src)
        # initialize must route fork_session to the fork path, not load.
        self.assertIn("if resume_id and fork_session:", src)


# ── context overflow ──────────────────────────────────────────────────────────

_SHARED = (
    "Error code: 400 - maximum context length is 1048576 tokens. However, "
    "you requested 1134032 tokens (750032 in the messages, 384000 in the "
    "completion)."
)
_PACKED = (
    "400 input_too_large: prompt is too long. The prompt is too long for the "
    "context window (1200000 tokens > 1000000 tokens)"
)


class TestSharedWindowMath(unittest.TestCase):
    def test_usable_prompt_tokens(self):
        self.assertEqual(usable_prompt_tokens(1048576, 384000), 664576)
        self.assertEqual(usable_prompt_tokens("x", 1), 0)
        self.assertEqual(usable_prompt_tokens(10, 99), 0)

    def test_parse(self):
        parsed = parse_shared_window_overflow(_SHARED)
        self.assertEqual(parsed["max_context"], 1048576)
        self.assertEqual(parsed["messages"], 750032)
        self.assertEqual(parsed["completion"], 384000)
        self.assertEqual(parsed["over_by"], 85456)
        self.assertEqual(parsed["usable_input"], 664576)

    def test_parse_ignores_unrelated(self):
        self.assertIsNone(parse_shared_window_overflow("connection reset"))
        self.assertIsNone(parse_shared_window_overflow(""))

    def test_rewrite_explains_the_shared_window(self):
        out = rewrite_grok_query_error(_SHARED)
        self.assertIn("shares a 1048576-token window", out)
        self.assertIn("384000 completion", out)
        self.assertIn("session-cumulative", out)
        self.assertNotIn("maximum context length is", out)

    def test_rewrite_passthrough(self):
        self.assertEqual(rewrite_grok_query_error("boom"), "boom")


class TestPackedPromptOverflow(unittest.TestCase):
    def test_parse(self):
        parsed = parse_input_too_large(_PACKED)
        self.assertEqual(parsed["prompt"], 1200000)
        self.assertEqual(parsed["max_context"], 1000000)
        self.assertEqual(parsed["kind"], "input_too_large")

    def test_rewrite_explains_occupancy_miss(self):
        out = rewrite_grok_query_error(_PACKED)
        self.assertIn("850000", out)
        self.assertIn("occupancy", out)

    def test_is_context_overflow_error(self):
        self.assertTrue(is_context_overflow_error(_SHARED))
        self.assertTrue(is_context_overflow_error(_PACKED))
        self.assertFalse(is_context_overflow_error("agent_busy"))


class TestSharedWindowToml(unittest.TestCase):
    def test_shrinks_deepseek_and_leaves_others(self):
        text = (
            "[model.deepseek-v4-flash]\n"
            "context_window = 1000000\n"
            "max_completion_tokens = 384000\n"
            "\n"
            "[model.gemini-3]\n"
            "context_window = 200000\n"
        )
        out = patch_deepseek_shared_window_toml(text)
        self.assertIn("context_window = 664576", out)
        self.assertIn("[model.gemini-3]\ncontext_window = 200000", out)

    def test_idempotent(self):
        text = "[model.deepseek-v4-pro]\ncontext_window = 1000000\n"
        once = patch_deepseek_shared_window_toml(text)
        twice = patch_deepseek_shared_window_toml(once)
        self.assertEqual(once, twice)
        self.assertIn("context_window = 664576", once)

    def test_inserts_when_missing(self):
        out = patch_deepseek_shared_window_toml(
            "[model.deepseek-v4-flash]\nname = \"DS\"\n")
        self.assertIn("context_window = 664576", out)

    def test_apply_writes_only_when_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("[model.deepseek-v4-flash]\ncontext_window = 1000000\n")
            self.assertTrue(apply_deepseek_shared_window_config(path))
            self.assertFalse(apply_deepseek_shared_window_config(path))
            with open(path, encoding="utf-8") as f:
                self.assertIn("664576", f.read())

    def test_missing_file_is_false(self):
        self.assertFalse(
            apply_deepseek_shared_window_config("/no/such/config.toml"))


class TestVisionCatalog(unittest.TestCase):
    def test_native_and_v4(self):
        self.assertTrue(model_supports_vision(""))
        self.assertTrue(model_supports_vision("grok-4.6"))
        for mid in ("deepseek-v4-pro", "deepseek-v4-flash",
                    "deepseek-v4-flash-vision-exp", "ds-flash", "ds-vision"):
            self.assertTrue(model_supports_vision(mid), mid)

    def test_pre_v4_deepseek_stays_off(self):
        self.assertFalse(model_supports_vision("deepseek-chat"))
        self.assertFalse(model_supports_vision("deepseek-reasoner"))


class TestGrokBridgeHooks(unittest.TestCase):
    def _bridge(self):
        b = GrokBridge()
        b.BACKEND_NAME = "grok"
        b.session_id = "sess-1"
        b._prompt_cancelled = False
        b._overflow_compact_retried = False
        b._prompt_fut = None
        b.file_log = lambda m: None
        b.compacted = []
        b.retried = []
        return b

    def test_format_query_error_uses_rewrite(self):
        b = self._bridge()
        out = b.format_query_error(RuntimeError(_SHARED))
        self.assertIn("shares a 1048576-token window", out)

    def test_format_query_error_passthrough(self):
        b = self._bridge()
        out = b.format_query_error(RuntimeError("disk full"))
        self.assertIn("disk full", out)

    def test_recover_skips_non_overflow(self):
        b = self._bridge()

        async def _send_acp(method, params, timeout=None):
            raise AssertionError("must not compact")

        b._send_acp = _send_acp
        self.assertIsNone(asyncio.run(b.recover_prompt_error(
            RuntimeError("agent_busy"), [{"type": "text", "text": "x"}])))

    def test_recover_compacts_then_retries(self):
        b = self._bridge()

        async def _send_acp(method, params, timeout=None):
            b.compacted.append((method, params))
            return {}

        async def _send_prompt(blocks):
            b.retried.append(blocks)
            return {"stopReason": "end_turn"}

        b._send_acp = _send_acp
        b._send_prompt = _send_prompt
        out = asyncio.run(b.recover_prompt_error(
            RuntimeError(_SHARED), [{"type": "text", "text": "hello"}]))
        self.assertEqual(out, {"stopReason": "end_turn"})
        self.assertEqual(b.compacted[0][0], "_x.ai/compact_conversation")
        self.assertEqual(b.compacted[0][1]["sessionId"], "sess-1")
        self.assertEqual(len(b.retried), 1)

    def test_recover_is_one_shot(self):
        b = self._bridge()

        async def _send_acp(method, params, timeout=None):
            b.compacted.append(method)
            return {}

        b._send_acp = _send_acp
        b._send_prompt = lambda blocks: _async({})
        asyncio.run(b.recover_prompt_error(RuntimeError(_PACKED), []))
        self.assertIsNone(asyncio.run(b.recover_prompt_error(
            RuntimeError(_PACKED), [])))
        self.assertEqual(len(b.compacted), 1)

    def test_recover_falls_back_to_compact_prompt(self):
        b = self._bridge()

        async def _send_acp(method, params, timeout=None):
            raise RuntimeError("Method not found")

        async def _send_prompt(blocks):
            b.retried.append(blocks)
            return {"stopReason": "end_turn"}

        b._send_acp = _send_acp
        b._send_prompt = _send_prompt
        out = asyncio.run(b.recover_prompt_error(RuntimeError(_PACKED), []))
        self.assertEqual(out, {"stopReason": "end_turn"})
        self.assertIn("compact", str(b.retried[0]))

    def test_acp_base_exposes_the_hooks(self):
        class _Base(AcpBridge):
            def __init__(self):
                self.BACKEND_NAME = "acp-test"

        base = _Base()
        self.assertEqual(
            base.format_query_error(RuntimeError("x")),
            "acp-test query failed: x")
        self.assertIsNone(asyncio.run(
            base.recover_prompt_error(RuntimeError("x"), [])))

    def test_spawn_env_patches_the_config(self):
        b = self._bridge()
        b._mcp_enable_read_image = False
        calls = []
        import backend.grok as grok_backend
        orig = grok_backend.apply_deepseek_shared_window_config
        grok_backend.apply_deepseek_shared_window_config = (
            lambda: calls.append(True))
        try:
            b.spawn_env()
        finally:
            grok_backend.apply_deepseek_shared_window_config = orig
        self.assertEqual(calls, [True])


def _async(value):
    async def _coro():
        return value
    return _coro()


class TestCodexFallbackModel(unittest.TestCase):
    def test_spec_uses_gpt6_astra(self):
        from backend.specs import BACKENDS
        spec = BACKENDS["codex"]
        self.assertEqual(spec.fallback_model, "gpt-6-astra")
        self.assertIn("gpt-6-astra", [m for m, _l in spec.default_models])

    def test_bridge_maps_claude_aliases_to_astra(self):
        with open(os.path.join(_BRIDGE, "codex_main.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn('model = "gpt-6-astra"', src)


if __name__ == "__main__":
    unittest.main()
