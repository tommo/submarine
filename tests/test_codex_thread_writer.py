"""Codex thread-writer conflicts: fork recovery, clear errors, no orphan bridges.

A soft plugin reload replaces the plugin's modules while bridge subprocesses
keep running (the host process still owns their pipes, so they never see EOF).
A leaked codex app-server keeps its thread writer, and the next resume of that
thread fails with -32600 "already has an active writer".
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

import codex_main  # noqa: E402
from codex_main import (  # noqa: E402
    CodexBridge,
    _codex_error_text,
    _is_writer_conflict,
)

THREAD = "01a0aeb6-4682-71c1-a5d1-b00876bbfc00"
WRITER_ERR = {"code": -32600,
              "message": "thread %s already has an active writer" % THREAD}


class _StubBridge(CodexBridge):
    """Init path only: no subprocess, scripted codex responses."""

    def __init__(self, responses):
        super().__init__()
        self._pending_responses = {}
        self._request_errors = {}
        self.script = list(responses)   # [(result, error), …] for thread/* calls
        self._script_i = 0
        self.req_methods = {}
        self.sent = []                  # methods we asked codex for
        self.results = []               # results sent back to the host
        self.errors = []
        self.notes = []
        self.exited = None

    async def start_codex(self, cwd, config_overrides=None):
        self.codex_proc = None

    async def _read_codex(self):
        return

    async def _read_codex_stderr(self):
        return

    async def codex_request(self, method, params=None):
        self.sent.append((method, dict(params or {})))
        rid = len(self.sent)
        self.req_methods[rid] = method
        return rid

    async def _wait_for_response(self, req_id, timeout=30):
        if self.req_methods.get(req_id) == "initialize":
            return {}                   # handshake succeeds
        item = self.script[self._script_i] if self._script_i < len(self.script) else (None, None)
        self._script_i += 1
        result, error = (list(item) + [None, None])[:2]
        if error is not None:
            self._request_errors[req_id] = error
            return None
        return result

    async def _exit_after_error(self, delay=0.5):
        self.exited = True


def _patch_sends(b):
    """Capture host-bound sends instead of writing to stdout."""
    codex_main.send_result = lambda rid, res, _b=b: _b.results.append((rid, res))
    codex_main.send_error = lambda rid, code, msg, _b=b: _b.errors.append(
        (rid, code, msg))
    codex_main.send_notification = lambda method, params, _b=b: _b.notes.append(
        (method, params))


class TestWriterConflictDetection(unittest.TestCase):
    def test_detects_the_app_server_refusal(self):
        self.assertTrue(_is_writer_conflict(WRITER_ERR))
        self.assertTrue(_is_writer_conflict(
            {"message": "Thread X already has an active writer."}))
        self.assertTrue(_is_writer_conflict("already has an active writer"))

    def test_ignores_other_errors(self):
        self.assertFalse(_is_writer_conflict(None))
        self.assertFalse(_is_writer_conflict({}))
        self.assertFalse(_is_writer_conflict({"message": "model not found"}))
        self.assertFalse(_is_writer_conflict({"message": "writer"}))

    def test_error_text(self):
        self.assertEqual(_codex_error_text(WRITER_ERR), WRITER_ERR["message"])
        self.assertEqual(_codex_error_text(None), "")


class TestInitializeThreadOpening(unittest.TestCase):
    def setUp(self):
        self._orig = (codex_main.send_result, codex_main.send_error,
                      codex_main.send_notification)
        # The bridge's log() appends to the shared bridge log file; a test must
        # not write there.
        self._orig_log = codex_main.log
        codex_main.log = lambda msg: None

    def tearDown(self):
        (codex_main.send_result, codex_main.send_error,
         codex_main.send_notification) = self._orig
        codex_main.log = self._orig_log

    def _run(self, params, responses):
        b = _StubBridge(responses)
        _patch_sends(b)
        asyncio.run(b.handle_initialize(1, params))
        return b

    def test_fresh_thread_uses_thread_start(self):
        b = self._run({"cwd": "/proj"}, [({"thread": {"id": THREAD}}, None)])
        self.assertEqual([m for m, _p in b.sent], ["initialize", "thread/start"])
        self.assertEqual(b.results[-1][1]["session_id"], THREAD)
        self.assertFalse(b.results[-1][1]["forked"])

    def test_resume_uses_thread_resume(self):
        b = self._run({"cwd": "/proj", "resume": THREAD},
                      [({"thread": {"id": THREAD}}, None)])
        self.assertIn("thread/resume", [m for m, _p in b.sent])
        self.assertNotIn("thread/fork", [m for m, _p in b.sent])
        self.assertEqual(b.results[-1][1]["session_id"], THREAD)

    def test_fork_session_uses_thread_fork_not_resume(self):
        """Forking used to resume the source thread — which fails whenever the
        source session is still open and holding the writer."""
        new_id = "01a0dead-0000-7000-8000-000000000001"
        b = self._run({"cwd": "/proj", "resume": THREAD, "fork_session": True},
                      [({"thread": {"id": new_id}}, None)])
        methods = [m for m, _p in b.sent]
        self.assertIn("thread/fork", methods)
        self.assertNotIn("thread/resume", methods)
        self.assertEqual(b.sent[-1][1]["threadId"], THREAD)
        self.assertEqual(b.results[-1][1]["session_id"], new_id)

    def test_writer_conflict_on_resume_forks_and_reports(self):
        new_id = "01a0dead-0000-7000-8000-000000000002"
        b = self._run(
            {"cwd": "/proj", "resume": THREAD},
            [(None, WRITER_ERR), ({"thread": {"id": new_id}}, None)],
        )
        methods = [m for m, _p in b.sent]
        self.assertEqual(methods[1], "thread/resume")
        self.assertEqual(methods[2], "thread/fork")
        out = b.results[-1][1]
        self.assertEqual(out["session_id"], new_id)
        self.assertTrue(out["forked"])
        # The user is told why the thread id changed.
        self.assertTrue(b.notes)
        msg = b.notes[-1][1]["data"]["message"]
        self.assertIn(THREAD, msg)
        self.assertIn(new_id, msg)
        self.assertIn("Forked", msg)

    def test_writer_conflict_with_failing_fork_is_an_actionable_error(self):
        b = self._run(
            {"cwd": "/proj", "resume": THREAD},
            [(None, WRITER_ERR), (None, WRITER_ERR)],
        )
        self.assertFalse(b.results)
        _rid, code, msg = b.errors[-1]
        self.assertEqual(code, -32000)
        self.assertIn("resume codex thread", msg)
        self.assertIn("active writer", msg)
        self.assertIn("Quit that session", msg)
        # A bridge with no thread must not linger holding the writer.
        self.assertTrue(b.exited)

    def test_other_resume_error_is_reported_verbatim(self):
        b = self._run(
            {"cwd": "/proj", "resume": THREAD},
            [(None, {"code": -32000, "message": "rollout is corrupt"})],
        )
        self.assertNotIn("thread/fork", [m for m, _p in b.sent])
        self.assertIn("rollout is corrupt", b.errors[-1][2])
        self.assertTrue(b.exited)

    def test_invalid_thread_id_is_rejected_before_asking_codex(self):
        b = self._run({"cwd": "/proj", "resume": "not-a-uuid"}, [({}, None)])
        self.assertEqual([m for m, _p in b.sent], ["initialize"])
        self.assertIn("not a thread id", b.errors[-1][2])
        self.assertTrue(b.exited)

    def test_initialize_failure_exits_the_bridge(self):
        b = self._run({"cwd": "/proj"}, [(None, {"message": "boom"})])
        self.assertFalse(b.results)
        self.assertTrue(b.exited)


class TestOrphanReaper(unittest.TestCase):
    """The reaper lives in main.py, which imports sublime — load it by path."""

    def _fn(self):
        import ast
        src = open(os.path.join(_ROOT, "main.py"), encoding="utf-8").read()
        ns = {}
        for node in ast.parse(src).body:
            if isinstance(node, ast.FunctionDef) and node.name == "_orphan_bridge_pids":
                exec(compile(ast.Module(body=[node], type_ignores=[]),
                             "main.py", "exec"), ns)
        return ns["_orphan_bridge_pids"]

    PS = (
        "  100     1 /usr/sbin/cupsd\n"
        # our bridges (children of plugin_host 999) and their codex children
        "14259   999 /Python /x/Packages/Submarine/bridge/codex_main.py\n"
        "14260 14259 /opt/homebrew/bin/codex app-server -c "
        "mcp_servers.submarine.args=[\"/x/mcp/server.py\", \"--agent-id=agent-a\"]\n"
        "18537   999 /Python /x/Packages/Submarine/bridge/codex_main.py\n"
        "18538 18537 /opt/homebrew/bin/codex app-server -c "
        "mcp_servers.submarine.args=[\"/x/mcp/server.py\", \"--agent-id=agent-b\"]\n"
        # not ours: a plain codex app-server, another plugin_host, a claude bridge
        "20000   999 /opt/homebrew/bin/codex app-server -c model=\"gpt-6-astra\"\n"
        "20001   777 /opt/homebrew/bin/codex app-server -c "
        "mcp_servers.submarine.args=[\"/x/server.py\", \"--agent-id=agent-c\"]\n"
        "20002   999 /usr/bin/python3 /x/Packages/Submarine/bridge/claude_main.py\n"
        "20003   888 /usr/bin/python3 /x/Packages/Submarine/bridge/codex_main.py\n"
    )

    def test_reaps_our_bridges_and_their_codex_children(self):
        pids = self._fn()(self.PS, 999)
        # Both bridges, both codex children, and the claude bridge — all ours.
        self.assertEqual(set(pids), {14259, 14260, 18537, 18538, 20002})

    def test_codex_child_is_killed_before_its_bridge(self):
        pids = self._fn()(self.PS, 999)
        self.assertLess(pids.index(14260), pids.index(14259))
        self.assertLess(pids.index(18538), pids.index(18537))

    def test_other_plugin_host_is_untouched(self):
        self.assertEqual(self._fn()(self.PS, 777), [20001])
        self.assertEqual(self._fn()(self.PS, 888), [20003])

    def test_plain_codex_app_server_is_kept(self):
        # No --agent-id marker → not ours (e.g. the Codex desktop app).
        self.assertNotIn(20000, self._fn()(self.PS, 999))

    def test_claude_bridge_is_ours_but_codex_only_rule_does_not_crash(self):
        # A claude bridge is still a leftover and is reaped.
        self.assertIn(20002, self._fn()(self.PS, 999))

    def test_junk_input(self):
        f = self._fn()
        self.assertEqual(f("", 1), [])
        self.assertEqual(f(None, 1), [])
        self.assertEqual(f("garbage\n\n123 456\n", 456), [])
        # A cycle in the parent chain must not hang.
        self.assertEqual(f("5 6 /x/bridge/a_main.py\n6 5 /x/bridge/b_main.py\n", 99), [])


if __name__ == "__main__":
    unittest.main()
