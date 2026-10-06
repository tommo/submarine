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
            kimi_backend.normalize_model("k3-256k"),
            "kimi-code/k3-256k",
        )
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
                "kimi-code/k3-256k",
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
        # The HTTP wrap is shared (Antigravity takes MCP over HTTP only too).
        self.assertIn("HttpMcpMixin", src)
        with open(os.path.join(root, "bridge", "acp", "http_mcp.py")) as f:
            shared = f.read()
        self.assertIn("stdio_http_mcp", shared)
        self.assertIn('"type": "http"', shared)
        self.assertNotIn('bridge_script="main.py"', src)
        self.assertNotIn("install_kimi_stdio_mcp", src)


if __name__ == "__main__":
    unittest.main()


class AntigravityBridgeTest(unittest.TestCase):
    """antigravity-acp tool updates → our formatters (shapes from a live run)."""

    def _bridge(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for p in (os.path.join(root, "bridge"), root):
            if p not in sys.path:
                sys.path.insert(0, p)
        import antigravity_main
        return antigravity_main.AntigravityBridge()

    def test_tool_titles(self):
        b = self._bridge()
        cases = {
            ("Running find_file", "search"): "Glob",
            ("Running view_file", "read"): "Read",
            ("pwd && ls -la", "execute"): "Bash",
            ("Running grep_search", "search"): "Grep",
            ("Running browser_get_dom", "other"): "browser_get_dom",
        }
        ask = {"toolCallId": "interaction_1", "title": "What is your favorite color?"}
        self.assertEqual(b._normalize_tool_name(ask), "AskUserQuestion")
        self.assertEqual(b._tool_input_from_update(ask, "AskUserQuestion")["question"],
                         "What is your favorite color?")
        for (title, kind), want in cases.items():
            self.assertEqual(b._normalize_tool_name({"title": title, "kind": kind}), want)

    def test_tool_inputs(self):
        b = self._bridge()
        self.assertEqual(b._normalize_tool_input(
            {"CommandLine": "ls", "Cwd": "/w"}, "Bash")["command"], "ls")
        self.assertEqual(b._normalize_tool_input(
            {"absolute_path": "/w/a.txt"}, "Read")["file_path"], "/w/a.txt")
        self.assertEqual(b._normalize_tool_input(
            {"directory_path": "/w", "query": "*note*"}, "Glob")["pattern"], "*note*")

    def test_client_fs_is_off_and_modes_map(self):
        b = self._bridge()
        self.assertFalse(b.CLIENT_FS)
        self.assertEqual(b.PERM_TO_MODE["acceptEdits"], "auto_edit")
        self.assertEqual(b.PERM_TO_MODE["bypassPermissions"], "yolo")


class AntigravityAskTest(unittest.TestCase):
    """Antigravity's ask tool: a request_permission whose options are the
    answers (shapes from a live session). It was cancelled without UI."""

    PARAMS = {
        "sessionId": "s",
        "toolCall": {"toolCallId": "interaction_c8446eb2", "status": "pending",
                     "title": "What is your favorite color?", "rawInput": {}},
        "options": [
            {"optionId": "1", "name": "(Recommended) Blue", "kind": "allow_once"},
            {"optionId": "2", "name": "Green", "kind": "allow_once"},
        ],
    }

    def _run(self, answers):
        import asyncio
        b = AntigravityBridgeTest._bridge(self)
        shown = []

        async def ui(questions):
            shown.append(questions)
            return answers
        b._ask_question_ui = ui
        out = asyncio.run(b._acp_request_permission(self.PARAMS))
        return b, shown, out

    def test_the_question_is_shown_and_a_pick_selects_its_option(self):
        b, shown, out = self._run({"What is your favorite color?": "Green"})
        (q,), = shown
        self.assertEqual(q["question"], "What is your favorite color?")
        self.assertEqual([o["label"] for o in q["options"]], ["Blue", "Green"])
        self.assertEqual(q["options"][0]["description"], "Recommended")
        self.assertEqual(out, {"outcome": {"outcome": "selected", "optionId": "2"}})

    def test_free_text_cancels_and_follows_up(self):
        b, _shown, out = self._run({"What is your favorite color?": "teal, really"})
        self.assertEqual(out, {"outcome": {"outcome": "cancelled"}})
        self.assertIn("teal, really", b._pending_ask_followup)
        self.assertIn("What is your favorite color?", b._pending_ask_followup)

    def test_dismissed_cancels(self):
        _b, _shown, out = self._run(None)
        self.assertEqual(out, {"outcome": {"outcome": "cancelled"}})


class ApplyModelSkipTest(unittest.TestCase):
    """No set_model for the model the session already runs (Antigravity
    restarts its agent session on each one); a real change still goes out."""

    def test_skip_and_send(self):
        import asyncio
        b = AntigravityBridgeTest._bridge(self)
        sent = []

        async def fake(method, params, **kw):
            sent.append((method, params.get("modelId")))
            return {}
        b._send_acp = fake
        b.session_id = "s"
        b._agent_model = "gemini-3.8-flash-high"
        b.model = "gemini-3.8-flash-high"
        self.assertTrue(asyncio.run(b.apply_model()))
        self.assertEqual(sent, [])
        b.model = "gemini-3.8-flash-low"
        asyncio.run(b.apply_model())
        self.assertEqual(sent, [("session/set_model", "gemini-3.8-flash-low")])


class AntigravityForkTest(unittest.TestCase):
    """Fork = copy the conversation db under a new id (the server's
    session/fork returns {}), ids swapped inside, then load the copy."""

    def test_copy_swaps_the_id_everywhere_and_keeps_the_source(self):
        import sqlite3
        import tempfile
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        from backend import antigravity as ag
        home = tempfile.mkdtemp(prefix="agy-home-")
        base = ag.conversations_dir(home)
        os.makedirs(base)
        src = "11111111-2222-3333-4444-555555555555"
        db = sqlite3.connect(os.path.join(base, src + ".db"))
        db.execute("create table trajectory_meta (trajectory_id text, cascade_id text)")
        db.execute("create table steps (idx integer primary key, step_payload blob)")
        db.execute("insert into trajectory_meta values (?, ?)", (src, src))
        db.execute("insert into steps values (0, ?)", (b"\x0a\x24" + src.encode() + b"\x12\x02hi",))
        db.commit(); db.close()
        with open(os.path.join(base, src + ".meta"), "w") as f:
            f.write('{"cwd": "/w"}')
        new = ag.fork_conversation(src, gemini_home=home)
        self.assertNotEqual(new, src)
        c = sqlite3.connect(os.path.join(base, new + ".db"))
        self.assertEqual(c.execute("select trajectory_id, cascade_id from trajectory_meta").fetchone(), (new, new))
        payload = c.execute("select step_payload from steps").fetchone()[0]
        self.assertEqual(payload, b"\x0a\x24" + new.encode() + b"\x12\x02hi")   # same length
        c.close()
        s = sqlite3.connect(os.path.join(base, src + ".db"))
        self.assertEqual(s.execute("select trajectory_id from trajectory_meta").fetchone()[0], src)
        self.assertTrue(os.path.isfile(os.path.join(base, new + ".meta")))

    def test_bridge_loads_the_copy_not_the_source(self):
        import asyncio
        b = AntigravityBridgeTest._bridge(self)
        import antigravity_main
        loaded = []

        async def load(sid, mcp):
            loaded.append(sid)
            return True
        b._try_load_session = load
        orig = antigravity_main.ag.fork_conversation
        antigravity_main.ag.fork_conversation = lambda s: "new-id"
        try:
            self.assertTrue(asyncio.run(b._try_fork_session("src-id", [])))
        finally:
            antigravity_main.ag.fork_conversation = orig
        self.assertEqual(loaded, ["new-id"])
        self.assertFalse(b._resumed)


class AcpOutputBodyTest(unittest.TestCase):
    """A rawOutput object shows its body, not itself as JSON (Antigravity's
    run_command: commandLine, workingDir, exitCode, combinedOutput…)."""

    def _text(self, raw, tool="Bash"):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for p in (os.path.join(root, "bridge"), root):
            if p not in sys.path:
                sys.path.insert(0, p)
        from acp.tools import ToolsMixin
        return ToolsMixin._extract_tool_content({"rawOutput": raw}, tool)

    def test_the_body_is_shown(self):
        raw = {"commandLine": "pil man show knowledge.md", "workingDir": "/w",
               "exitCode": 0, "exit_code": 0, "combinedOutput": "doc text\n",
               "formatted_output": "doc text\n"}
        self.assertEqual(self._text(raw), "doc text\n")

    def test_a_failed_command_says_its_exit_code(self):
        raw = {"commandLine": "pil man show knowledge", "exitCode": 1,
               "combinedOutput": "Multiple matches:\nknowledge.md\n"}
        self.assertEqual(self._text(raw), "Multiple matches:\nknowledge.md\n[exit 1]")

    def test_an_unknown_shape_is_still_json(self):
        self.assertIn('"weird": 1', self._text({"weird": 1}))
