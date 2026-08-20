#!/usr/bin/env python3
"""Unit tests for Kimi Code ACP pure helpers + static wiring."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import kimi as kimi_backend  # noqa: E402
from backend import specs  # noqa: E402


class TestKimiBackendHelpers(unittest.TestCase):
    def test_agent_argv_ends_with_acp(self):
        argv = kimi_backend.agent_argv()
        self.assertGreaterEqual(len(argv), 2)
        self.assertEqual(argv[-1], "acp")
        joined = " ".join(argv)
        self.assertNotIn("main.py", joined)
        self.assertNotIn("claude-agent-sdk", joined)

    def test_agent_argv_uses_resolved_bin(self):
        with mock.patch.object(kimi_backend, "resolve_kimi_bin", return_value="/opt/kimi"):
            self.assertEqual(kimi_backend.agent_argv(), ["/opt/kimi", "acp"])

    def test_available_false_when_missing(self):
        with mock.patch.object(kimi_backend, "resolve_kimi_bin", return_value="kimi"):
            with mock.patch("backend.kimi.shutil.which", return_value=None):
                self.assertFalse(kimi_backend.kimi_available())

    def test_available_true_for_executable(self):
        with mock.patch.object(
            kimi_backend, "resolve_kimi_bin", return_value="/bin/kimi"
        ):
            with mock.patch("backend.kimi.os.path.isfile", return_value=True):
                with mock.patch("backend.kimi.os.access", return_value=True):
                    self.assertTrue(kimi_backend.kimi_available())

    def test_normalize_model_aliases(self):
        self.assertEqual(kimi_backend.normalize_model(None), "kimi-code/k3")
        self.assertEqual(kimi_backend.normalize_model("k3"), "kimi-code/k3")
        self.assertEqual(
            kimi_backend.normalize_model("k2.7"),
            "kimi-code/kimi-for-coding",
        )
        self.assertEqual(
            kimi_backend.normalize_model("highspeed"),
            "kimi-code/kimi-for-coding-highspeed",
        )
        ids = {m[0] for m in kimi_backend.KIMI_MODELS}
        self.assertEqual(
            ids,
            {
                "kimi-code/k3",
                "kimi-code/kimi-for-coding",
                "kimi-code/kimi-for-coding-highspeed",
            },
        )

    def test_resolve_honors_kimi_bin_env(self):
        with mock.patch.dict(os.environ, {"KIMI_BIN": "/custom/kimi"}, clear=False):
            with mock.patch("backend.kimi.os.path.isfile", return_value=True):
                with mock.patch("backend.kimi.os.access", return_value=True):
                    self.assertEqual(kimi_backend.resolve_kimi_bin(), "/custom/kimi")


class TestKimiStaticWiring(unittest.TestCase):
    def test_bridge_script_registered(self):
        spec = specs.get("kimi")
        self.assertEqual(spec.name, "kimi")
        self.assertEqual(spec.bridge_script, "kimi_main.py")
        self.assertEqual(spec.label, "Kimi Code")
        self.assertEqual(spec.abbrev, "KM")
        self.assertNotEqual(spec.bridge_script, "claude_main.py")
        self.assertNotEqual(spec.bridge_script, "main.py")
        argv = kimi_backend.agent_argv()
        self.assertEqual(argv[-1], "acp")

    def test_backends_registry_source(self):
        """Assert BACKENDS registry wires kimi without importing sublime."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "backend", "specs.py")
        with open(path) as f:
            src = f.read()
        self.assertIn('"kimi"', src)
        self.assertIn('bridge_script="kimi_main.py"', src)
        self.assertIn('label="Kimi Code"', src)
        self.assertIn('abbrev="KM"', src)
        self.assertIn("_kimi_available", src)
        argv = kimi_backend.agent_argv()
        self.assertEqual(argv[-1], "acp")

    def test_kimi_bridge_wraps_mcp_as_http(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "bridge", "kimi_main.py")
        with open(path) as f:
            src = f.read()
        self.assertIn("AcpBridge", src)
        self.assertIn("agent_argv", src)
        self.assertIn("acp", src)
        self.assertIn("stdio_http_mcp", src)
        self.assertIn('"type": "http"', src)
        self.assertNotIn('bridge_script="main.py"', src)
        self.assertNotIn("install_kimi_stdio_mcp", src)


if __name__ == "__main__":
    unittest.main()
