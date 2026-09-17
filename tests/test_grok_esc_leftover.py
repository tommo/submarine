"""Upstream 2e6e415 theme A: Grok leftover after Esc must not re-busy.

Covers the bridge lid (`_drop_grok_leftover`), synthetic self-wake prompt ids,
the post-Esc tool_call_update drop (77f2dec), and the host-side closer
(`_pending_leftover_end` / self-wake idle timeout / stale-interrupt unstick).
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

import acp_base  # noqa: E402
from acp_base import AcpBridge  # noqa: E402
from tests.fakes import FakeClient, FakeScheduler, make_session  # noqa: E402


def _patch_notify(notes):
    import acp.query as query
    import acp.transport as transport
    import acp.updates as updates
    orig = []

    def _n(m, p):
        notes.append((m, p))

    for mod in (transport, updates, query):
        orig.append((mod, mod.send_notification))
        mod.send_notification = _n
    return orig


def _restore_notify(orig):
    for mod, fn in orig:
        mod.send_notification = fn


class _Stub(AcpBridge):
    """Minimal bridge: flags + call table, no process."""

    def __init__(self, backend="grok"):
        self.BACKEND_NAME = backend
        self.session_id = "sess-1"
        self._prompt_fut = None
        self._prompt_cancelled = False
        self._cancel_in_flight = False
        self._drop_grok_leftover = False
        self._orphan_turn_notified = False
        self._host_prompt_id = None
        self._leftover_end_pending = False
        self._query_req_id = None
        self._loading_session = False
        self._foreign_session_drops = 0
        self._calls = {}
        self._tool_id_alias = {}
        self._pending_execute_ids = []
        self._last_execute_id = None
        self._terminals = {}
        self._terminal_bg = {}
        self._released_terminals = set()
        self._detached_snaps = {}
        self._detached_procs = {}
        self._detached_slots = {}
        self._child_sessions = {}
        self._bg_notified_tasks = set()
        self._bg_notified_tools = set()
        self._tool_titles_by_id = {}
        self._last_bg_tool_id = None
        self.terminal_wait_timeout_s = 0
        self._finished = []
        self._systems = []
        self._overflow_compact_retried = False
        self.pending_permissions = {}
        self.pending_questions = {}
        self.pending_plan_approvals = {}
        self.sent = []
        self.TOOL_TO_CANONICAL = dict(AcpBridge.TOOL_TO_CANONICAL)

    def file_log(self, msg):
        pass

    def _emit_system(self, subtype, data=None, *a, **k):
        self._systems.append((subtype, data or {}))

    def _emit_bg_finished(self, *a, **k):
        self._finished.append(a)

    def _write_bg_output_file(self, *a, **k):
        return ""

    async def _spawn(self):
        raise AssertionError("_spawn must not run in these tests")

    async def _notify_acp(self, method, params):
        self.sent.append((method, params))

    async def _send_acp(self, method, params, timeout=None):
        raise RuntimeError("not found")

    async def _send_prompt(self, prompt_blocks):
        raise AssertionError("_send_prompt must not run in these tests")


def _open_row(b, tid, name="Bash"):
    call = b._ensure_call(tid)
    call.name = name
    call.emitted = True
    return call


class TestSyntheticGrokPromptId(unittest.TestCase):
    def test_detects_self_wake_ids(self):
        self.assertTrue(AcpBridge._is_synthetic_grok_prompt_id(
            "task-completed-term_abc"))
        self.assertTrue(AcpBridge._is_synthetic_grok_prompt_id(
            "TASK-COMPLETED-1"))
        self.assertFalse(AcpBridge._is_synthetic_grok_prompt_id("prompt-42"))
        self.assertFalse(AcpBridge._is_synthetic_grok_prompt_id(""))

    def test_self_wake_closer_emits_while_host_rpc_is_live(self):
        notes = []
        orig = _patch_notify(notes)
        try:
            b = _Stub()
            loop = asyncio.new_event_loop()
            try:
                fut = loop.create_future()
            finally:
                loop.close()
            b._prompt_fut = fut  # host RPC still live
            b._handle_grok_turn_end({}, {
                "prompt_id": "task-completed-term_x",
                "stop_reason": "end_turn",
            })
        finally:
            _restore_notify(orig)
        leftovers = [p for m, p in notes if p.get("leftover_end")]
        self.assertEqual(len(leftovers), 1)
        # A synthetic id must never be bound as the host prompt id.
        self.assertNotEqual(b._host_prompt_id, "task-completed-term_x")


class TestLeftoverLidSuppression(unittest.TestCase):
    def test_drop_grok_leftover_suppresses_tool_call_and_text(self):
        notes = []
        orig = _patch_notify(notes)
        try:
            b = _Stub()
            b._drop_grok_leftover = True
            b._forward_update({
                "sessionId": "sess-1",
                "update": {"sessionUpdate": "agent_message_chunk",
                           "content": {"text": "leftover prose"}},
            })
            b._forward_update({
                "sessionId": "sess-1",
                "update": {"sessionUpdate": "tool_call",
                           "toolCallId": "tool-after-esc",
                           "title": "run_terminal_command"},
            })
        finally:
            _restore_notify(orig)
        kinds = [p.get("type") for _m, p in notes]
        self.assertNotIn("text_delta", kinds)
        self.assertNotIn("tool_use", kinds)
        # Never agent_continue leftover after Esc either.
        self.assertNotIn("agent_continue", kinds)

    def test_post_esc_failed_update_does_not_open_a_row(self):
        notes = []
        orig = _patch_notify(notes)
        try:
            b = _Stub()
            b._cancel_in_flight = True
            b._prompt_cancelled = True
            b._prompt_fut = None
            b._forward_update({
                "sessionId": "sess-1",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-after-esc",
                    "title": "run_terminal_command",
                    "status": "failed",
                    "content": [{"type": "content", "content": {
                        "type": "text",
                        "text": "terminal/create rejected: turn cancelled",
                    }}],
                },
            })
        finally:
            _restore_notify(orig)
        kinds = [p.get("type") for _m, p in notes]
        self.assertNotIn("tool_use", kinds)
        self.assertNotIn("tool_result", kinds)

    def test_post_esc_failed_update_still_settles_an_open_row(self):
        notes = []
        orig = _patch_notify(notes)
        try:
            b = _Stub()
            b._cancel_in_flight = True
            b._prompt_cancelled = True
            b._prompt_fut = None
            tid = "call-open-bg"
            _open_row(b, tid)
            b._forward_update({
                "sessionId": "sess-1",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tid,
                    "title": "Bash",
                    "status": "failed",
                },
            })
        finally:
            _restore_notify(orig)
        kinds = [p.get("type") for _m, p in notes]
        self.assertIn("tool_result", kinds)

    def test_fs_and_terminal_refused_while_leftover(self):
        b = _Stub()
        b._cancel_in_flight = False
        b._drop_grok_leftover = True
        b.fs_write_max_chars = 0
        with self.assertRaises(ValueError):
            asyncio.run(b._acp_fs_write({"path": "/tmp/x.md", "content": "x"}))
        with self.assertRaises(ValueError):
            asyncio.run(b._acp_terminal_create({"command": "echo", "args": []}))


class TestPrecancelDecision(unittest.TestCase):
    def _fut(self, done):
        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            if done:
                fut.set_result({})
            return fut
        finally:
            loop.close()

    def test_no_flag_no_cancel(self):
        b = _Stub()
        self.assertFalse(b._should_precancel_before_prompt())

    def test_grok_skips_stale_orphan_cancel(self):
        b = _Stub(backend="grok")
        b._cancel_in_flight = True
        self.assertFalse(b._should_precancel_before_prompt())

    def test_grok_cancels_live_prompt(self):
        b = _Stub(backend="grok")
        b._cancel_in_flight = True
        b._prompt_fut = self._fut(False)
        self.assertTrue(b._should_precancel_before_prompt())

    def test_grok_cancels_orphan_leftover_turn(self):
        b = _Stub(backend="grok")
        b._cancel_in_flight = True
        b._orphan_turn_notified = True
        self.assertTrue(b._should_precancel_before_prompt())

    def test_kimi_still_settles_after_forced_local(self):
        b = _Stub(backend="kimi")
        b._cancel_in_flight = True
        self.assertTrue(b._should_precancel_before_prompt())

    def test_cancel_sets_drop_grok_leftover(self):
        async def _go():
            b = _Stub(backend="grok")
            b._prompt_fut = asyncio.get_running_loop().create_future()
            b._cancel_in_flight = True
            await b._cancel_agent_turn(
                reason="interrupt", wait_s=0.01, settle_s=0.01,
                force_local=True)
            return b

        b = asyncio.run(_go())
        self.assertTrue(b._drop_grok_leftover)

    def test_query_source_uses_the_helper(self):
        path = os.path.join(_BRIDGE, "acp", "query.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("_should_precancel_before_prompt()", src)


class TestInterruptSource(unittest.TestCase):
    def test_handle_interrupt_spares_leftover_and_the_agent(self):
        import inspect
        src = inspect.getsource(AcpBridge.handle_interrupt)
        live = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#"))
        self.assertIn("_terminal_close", live)
        self.assertIn("grok_leftover", live)
        self.assertNotIn("self.proc.terminate", live)
        self.assertNotIn("self.proc.kill", live)
        self.assertIn("_cancel_agent_turn", live)

    def test_idle_interrupt_reaps_shells_without_cancel(self):
        notes = []
        orig = _patch_notify(notes)
        try:
            b = _Stub()
            asyncio.run(b.handle_interrupt(3, {}))
        finally:
            _restore_notify(orig)
        self.assertEqual([m for m, _p in notes if m == "session/cancel"], [])


class TestHostLeftoverCloser(unittest.TestCase):
    def test_pending_leftover_end_skips_self_wake(self):
        s = make_session(initialized=True, client=FakeClient(), backend="grok")
        s._pending_leftover_end = True
        s._resume_interrupt_stream()
        self.assertFalse(s.working)
        self.assertEqual(s.turn.kind, "idle")
        self.assertFalse(s._pending_leftover_end)

    def test_result_stores_pending_when_idle(self):
        s = make_session(initialized=True, client=FakeClient(), backend="grok")
        s._on_done({"status": "complete"}, _expected_gen=s.turn.gen)
        s.events.result({
            "leftover_end": True,
            "stop_reason": "end_turn",
            "is_error": False,
        })
        self.assertTrue(s._pending_leftover_end)
        self.assertFalse(s.working)

    def test_self_wake_idle_timeout_closes_the_sheet(self):
        sched = FakeScheduler()
        s = make_session(
            initialized=True, client=FakeClient(), backend="grok",
            scheduler=sched)
        s.query("hello")
        s.turn.awaiting_rpc = False
        s._arm_self_wake_idle()
        self.assertTrue(s.working)
        sched.fire_due(max_ms=6000)
        self.assertFalse(s.working)
        self.assertEqual(s.turn.kind, "idle")

    def test_stale_generation_does_not_close_new_turn(self):
        sched = FakeScheduler()
        s = make_session(
            initialized=True, client=FakeClient(), backend="grok",
            scheduler=sched)
        s.query("hello")
        s.turn.awaiting_rpc = False
        s._arm_self_wake_idle()
        s._self_wake_idle_gen += 5
        sched.fire_due(max_ms=6000)
        self.assertTrue(s.working)


class TestUnstickStaleInterrupt(unittest.TestCase):
    def test_unstick_settles_a_stuck_interrupt(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="grok")
        s.query("first")
        s.interrupt()
        self.assertEqual(s.turn.kind, "interrupting")
        self.assertTrue(s.working)
        s._unstick_stale_interrupt()
        self.assertEqual(s.turn.kind, "idle")
        self.assertFalse(s.working)
        self.assertFalse(s._interrupting)

    def test_unstick_flushes_a_queue_that_never_ran(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="grok")
        s.query("first")
        s.interrupt()
        s._queued_prompts = ["follow-up"]
        s._unstick_stale_interrupt()
        self.assertEqual(s._queued_prompts, [])
        prompts = [t[1].get("prompt")
                   for t in client.sent if t[0] == "query"]
        self.assertIn("follow-up", prompts)
        self.assertEqual(s.turn.kind, "live")

    def test_unstick_does_not_kill_live_turn(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, backend="grok")
        s.query("first")
        s._unstick_stale_interrupt()
        self.assertTrue(s.working)
        self.assertEqual(s.turn.kind, "live")

    def test_settle_callback_uses_unstick_before_the_busy_guard(self):
        path = os.path.join(_ROOT, "core", "session.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        body = src.split("def _on_interrupt_settle", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("_unstick_stale_interrupt()", body)
        self.assertLess(
            body.index("_unstick_stale_interrupt()"),
            body.index("if self.working:"))


class TestKimiAgentCloser(unittest.TestCase):
    def test_native_agent_is_not_a_spawn(self):
        from kimi_main import KimiBridge
        for title in ("Agent", "Agent: explore", "Launching explore agent: x"):
            self.assertFalse(
                KimiBridge._is_subagent_spawn("Task", {"title": title}, {}),
                title)

    def test_grok_spawn_subagent_still_is(self):
        from kimi_main import KimiBridge
        self.assertTrue(KimiBridge._is_subagent_spawn(
            "Subagent", {"title": "spawn_subagent"}, {}))

    def test_agent_result_demotes_background_to_a_closer(self):
        notes = []
        orig = _patch_notify(notes)
        try:
            b = _Stub(backend="kimi")
            tid = "tool-agent-1"
            call = _open_row(b, tid, name="Task")
            call.background = True
            text = "agent_id: agent-0\nstatus: completed"
            b._forward_update({
                "sessionId": "sess-1",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tid,
                    "title": "Agent",
                    "status": "completed",
                    "content": [{"type": "content", "content": {
                        "type": "text", "text": text}}],
                },
            })
        finally:
            _restore_notify(orig)
        self.assertFalse(call.background)
        self.assertIn("tool_result", [p.get("type") for _m, p in notes])


class TestFsReadDirectory(unittest.TestCase):
    def test_directory_lists_instead_of_errno_21(self):
        import tempfile
        b = _Stub()
        b.fs_read_max_chars = 2 * 1024 * 1024
        b._mcp_enable_read_image = False
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, "sub"))
            with open(os.path.join(tmp, "a.txt"), "w") as f:
                f.write("x")
            out = asyncio.run(b._acp_fs_read({"path": tmp}))
        text = out["content"]
        self.assertIn("Directory:", text)
        self.assertIn("a.txt", text)
        self.assertIn("sub/", text)
        self.assertIn("read a file inside", text)


if __name__ == "__main__":
    unittest.main()
