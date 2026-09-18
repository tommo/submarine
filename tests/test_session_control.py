"""The outside-Sublime control surface: `op:"sessions"` and its CLI.

Everything here runs with fake sessions on the real registry: the point of the
surface is that it works from registry and store state alone, so the tests never
need a sheet, a bridge or a live client.
"""
from __future__ import annotations

import io
import json
import os
import shlex
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import features.session_control as sc
from core.registry import default_registry


class FakeView(object):
    def __init__(self, vid, valid=True):
        self._id = vid
        self._valid = valid

    def id(self):
        return self._id

    def is_valid(self):
        return self._valid


class FakeSession(object):
    """Duck-typed stand-in: only what the control surface reads or calls."""

    def __init__(self, agent_id, session_id="", name="S", backend="grok",
                 working=False, initialized=True, sleeping=False, view=None,
                 phase="idle", **kw):
        self.agent_id = agent_id
        self.session_id = session_id
        self.subsession_id = kw.pop("subsession_id", None)
        self.parent_agent_id = kw.pop("parent_agent_id", None)
        self.name = name
        self.backend = backend
        self.working = working
        self.initialized = initialized
        self.is_sleeping = sleeping
        self.query_count = kw.pop("query_count", 3)
        self.last_access = kw.pop("last_access", time.time())
        self.model = kw.pop("model", "m")
        self.window = None
        self.turn = types.SimpleNamespace(kind=phase)
        self.output = types.SimpleNamespace(
            view=view, conversations=kw.pop("conversations", []), current=None)
        self.store = types.SimpleNamespace(
            find=lambda sid: kw.pop("record", None) if False else None)
        self.calls = []

    # delivered work
    def query(self, prompt, display_prompt=None, **kw):
        self.calls.append(("query", prompt, display_prompt))

    def queue_prompt(self, prompt):
        self.calls.append(("queue", prompt))

    def send_now(self, prompt=""):
        self.calls.append(("send_now", prompt))
        return True

    def wake(self):
        self.calls.append(("wake",))
        return True

    def interrupt(self):
        self.calls.append(("interrupt",))
        self.working = False
        self.turn.kind = "idle"


class ControlTestCase(unittest.TestCase):
    def setUp(self):
        self._prev = list(default_registry.by_agent.items())
        self._prev_binding = dict(default_registry.binding)
        default_registry.clear()
        self._saved = sc._saved_rows
        self._idem = dict(sc._idem_state())
        sc._idem_state().clear()

    def tearDown(self):
        default_registry.clear()
        for aid, s in self._prev:
            default_registry.by_agent[aid] = s
        default_registry.binding.clear()
        default_registry.binding.update(self._prev_binding)
        sc._saved_rows = self._saved
        sc._idem_state().clear()
        sc._idem_state().update(self._idem)

    def _add(self, session):
        default_registry.register_session(session)
        return session

    def _saved_rows(self, rows):
        sc._saved_rows = lambda: list(rows)


class TestResolve(ControlTestCase):
    def test_ids_name_and_view_all_resolve(self):
        s = self._add(FakeSession("agent-1", "sid-1", name="GUEST",
                                  view=FakeView(59)))
        self.assertIs(sc._resolve_live({"agent_id": "agent-1"}), s)
        self.assertIs(sc._resolve_live({"session_id": "sid-1"}), s)
        self.assertIs(sc._resolve_live("agent-1"), s)
        self.assertIs(sc._resolve_live("59"), s)
        self.assertIs(sc._resolve_live({"view_id": 59}), s)
        self.assertIs(sc._resolve_live({"name": "GUEST"}), s)
        self.assertIs(sc._resolve_live({"name": "guest"}), s)

    def test_subsession_id_is_an_alias(self):
        s = self._add(FakeSession("agent-1", "sid-1", subsession_id="agent-1"))
        self.assertIs(sc._resolve_live({"agent_id": "agent-1"}), s)
        self.assertIs(sc._resolve_live("agent-1"), s)

    def test_a_shared_name_is_ambiguous_not_a_guess(self):
        self._add(FakeSession("a1", "s1", name="hello"))
        self._add(FakeSession("a2", "s2", name="hello", backend="claude"))
        with self.assertRaises(sc.ControlError) as ctx:
            sc._resolve_live({"name": "hello"})
        self.assertEqual(ctx.exception.code, "ambiguous")
        self.assertEqual(len(ctx.exception.data["candidates"]), 2)
        # A backend hint disambiguates the same way a user would.
        self.assertIs(sc._resolve_live({"name": "hello", "backend": "claude"}).agent_id, "a2")

    def test_unknown_ref_lists_candidates(self):
        self._add(FakeSession("a1", "s1", name="only"))
        with self.assertRaises(sc.ControlError) as ctx:
            sc._resolve_live("nope")
        self.assertEqual(ctx.exception.code, "not_found")
        self.assertEqual([c["agent_id"] for c in ctx.exception.data["candidates"]], ["a1"])

    def test_saved_sessions_resolve_for_reading(self):
        self._saved_rows([{"session_id": "old", "agent_id": "gone",
                           "name": "GUEST", "backend": "grok",
                           "project": "/p", "state": "closed"}])
        target = sc._resolve_any({"session_id": "old"})
        self.assertEqual(target["kind"], "saved")
        self.assertIsNone(target["session"])
        self.assertEqual(target["cwd"], "/p")


class TestList(ControlTestCase):
    def test_list_reports_viewless_sessions_and_saved_rows(self):
        self._add(FakeSession("a1", "s1", name="live", view=FakeView(7)))
        self._add(FakeSession("a2", "s2", name="background", working=True,
                              view=None, phase="live"))
        self._add(FakeSession("a3", "s3", name="asleep", sleeping=True))
        self._saved_rows([{"session_id": "old", "agent_id": "gone", "name": "closed",
                           "backend": "kimi", "project": "/p", "state": "closed",
                           "query_count": 5},
                          {"session_id": "s1", "agent_id": "a1", "name": "live",
                           "state": "open"}])
        body = sc.action_list({})
        by_name = {r["name"]: r for r in body["sessions"]}
        self.assertEqual(body["count"], 4, "the live row is not also a saved row")
        self.assertEqual(by_name["background"]["state"], "working")
        self.assertEqual(by_name["background"]["turn_phase"], "live")
        self.assertFalse(by_name["background"]["view"]["bound"])
        self.assertEqual(by_name["asleep"]["state"], "sleeping")
        self.assertEqual(by_name["closed"]["kind"], "saved")
        self.assertEqual(by_name["closed"]["query_count"], 5)

    def test_scope_children_filters_by_parent(self):
        parent = self._add(FakeSession("p1", "sp", name="parent"))
        self._add(FakeSession("c1", "sc", name="child", parent_agent_id="p1"))
        self._add(FakeSession("x1", "sx", name="other"))
        body = sc.action_list({"scope": "children", "parent": "p1"})
        self.assertEqual([r["name"] for r in body["sessions"]], ["child"])
        self.assertIsNotNone(parent)

    def test_unknown_scope_is_refused(self):
        with self.assertRaises(sc.ControlError) as ctx:
            sc.action_list({"scope": "everything"})
        self.assertEqual(ctx.exception.code, "bad_request")


class TestView(ControlTestCase):
    def _patch_transcript(self, turns, path="/tmp/t.jsonl"):
        import features.resume as resume
        self._orig = (resume.load_turns, resume.find_session_jsonl)
        resume.load_turns = lambda *a, **k: list(turns)
        resume.find_session_jsonl = lambda *a, **k: path

    def tearDown(self):
        if hasattr(self, "_orig"):
            import features.resume as resume
            resume.load_turns, resume.find_session_jsonl = self._orig
        ControlTestCase.tearDown(self)

    def test_tail_reads_a_series_that_is_not_running(self):
        self._saved_rows([{"session_id": "old", "agent_id": "gone",
                           "name": "GUEST", "backend": "grok",
                           "project": "/p", "state": "closed"}])
        self._patch_transcript([
            {"prompt": "one", "reply": "first", "tools": ["Read"]},
            {"prompt": "two", "reply": "second", "tools": []},
            {"prompt": "three", "reply": "x" * 50, "tools": ["Bash"]},
        ])
        body, ref = sc.action_view({"ref": {"session_id": "old"}, "turns": 2})
        self.assertEqual(ref["kind"], "saved")
        self.assertEqual(body["turn_count"], 3)
        self.assertEqual([t["prompt"] for t in body["turns"]], ["two", "three"])
        self.assertEqual(body["turns"][0]["reply"], "second")
        self.assertEqual(body["transcript"], "/tmp/t.jsonl")

    def test_max_chars_truncates_and_says_so(self):
        self._saved_rows([{"session_id": "old", "backend": "claude",
                           "name": "n", "project": "/p"}])
        self._patch_transcript([{"prompt": "p", "reply": "y" * 100}])
        body, _ = sc.action_view({"ref": "old", "max_chars": 10})
        self.assertEqual(len(body["turns"][0]["reply"]), 10)
        self.assertTrue(body["turns"][0]["reply_truncated"])

    def test_text_mode_needs_a_sheet_and_never_makes_one(self):
        self._add(FakeSession("a1", "s1", name="viewless", view=None))
        with self.assertRaises(sc.ControlError) as ctx:
            sc.action_view({"ref": "a1", "mode": "text"})
        self.assertEqual(ctx.exception.code, "no_view")

    def test_edits_mode_is_viewless(self):
        conv = types.SimpleNamespace(events=[types.SimpleNamespace(
            name="Edit",
            tool_input={"file_path": "/p/x.py", "old_string": "a", "new_string": "b"},
        )])
        self._add(FakeSession("a1", "s1", name="editor", view=None,
                              conversations=[conv]))
        body, _ = sc.action_view({"ref": "a1", "mode": "edits"})
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["edits"][0]["file_path"], "/p/x.py")

    def test_unknown_mode_is_refused(self):
        self._add(FakeSession("a1", "s1"))
        with self.assertRaises(sc.ControlError) as ctx:
            sc.action_view({"ref": "a1", "mode": "scrollback"})
        self.assertEqual(ctx.exception.code, "bad_request")


class TestChat(ControlTestCase):
    def test_idle_target_is_queried_with_the_display_line(self):
        s = self._add(FakeSession("a1", "s1", name="GUEST"))
        env = sc.action_chat({"ref": "a1", "prompt": "  do it  "},
                             {"kind": "external", "name": "claude-code"})
        self.assertTrue(env["ok"])
        self.assertEqual(s.calls, [("query", "do it", "📨 from claude-code")])
        self.assertEqual(env["data"]["action"], "sent")
        self.assertEqual(env["ref_resolved"]["agent_id"], "a1")

    def test_busy_target_follows_the_queue_policy(self):
        s = self._add(FakeSession("a1", "s1", working=True))
        env = sc.action_chat({"ref": "a1", "prompt": "p"})
        self.assertEqual(env["data"]["action"], "queued")
        self.assertEqual(s.calls, [("queue", "p")])

        s.calls = []
        env = sc.action_chat({"ref": "a1", "prompt": "p", "queue": "interrupt"})
        self.assertEqual(env["data"]["action"], "send_now")
        self.assertEqual(s.calls, [("send_now", "p")])

        with self.assertRaises(sc.ControlError) as ctx:
            sc.action_chat({"ref": "a1", "prompt": "p", "queue": "reject"})
        self.assertEqual(ctx.exception.code, "busy")

    def test_interrupt_policy_delivers_synchronously(self):
        """`queue=interrupt` must cancel *and* send, with no hand-off to wait on."""
        s = self._add(FakeSession("a1", "s1", working=True))
        env = sc.action_chat({"ref": "a1", "prompt": "p", "queue": "interrupt",
                              "wait": True})
        self.assertEqual(s.calls, [("send_now", "p")])
        self.assertNotIn("_wait_for_init", env)
        self.assertFalse(env["data"]["waited"])

    def test_interrupt_policy_on_an_idle_session_queries(self):
        s = self._add(FakeSession("a1", "s1"))
        env = sc.action_chat({"ref": "a1", "prompt": "p", "queue": "interrupt"})
        self.assertEqual(s.calls, [("query", "p", "📨 from outside agent")])
        self.assertEqual(env["data"]["action"], "sent")

    def test_reject_with_wait_still_refuses_a_busy_target(self):
        self._add(FakeSession("a1", "s1", working=True))
        with self.assertRaises(sc.ControlError) as ctx:
            sc.action_chat({"ref": "a1", "prompt": "p", "queue": "reject", "wait": True})
        self.assertEqual(ctx.exception.code, "busy")

    def test_a_sleeping_target_is_woken_then_handed_to_the_socket_thread(self):
        """The hand-off only polls `initialized`, so the wake has to happen
        here — otherwise the bridge never comes up and the prompt is dropped
        when that poll times out."""
        s = self._add(FakeSession("a1", "s1", sleeping=True, initialized=False))
        env = sc.action_chat({"ref": "a1", "prompt": "wake up", "wait": True})
        self.assertTrue(env["ok"])
        self.assertTrue(env["_wait_for_init"], "the socket thread delivers it")
        self.assertIs(env["_session"], s)
        self.assertEqual(env["_prompt"], "wake up")
        self.assertTrue(env["_wait_for_completion"])
        self.assertEqual(s.calls, [("wake",)], "started, then handed over")
        self.assertEqual(env["data"]["action"], "woke")

    def test_a_connecting_session_is_not_woken_twice(self):
        """`initialized` false but awake is a session mid-connect: the wake
        would be redundant, so only a genuinely sleeping one is started."""
        s = self._add(FakeSession("a1", "s1", initialized=False, sleeping=False))
        env = sc.action_chat({"ref": "a1", "prompt": "hello"})
        self.assertTrue(env["_wait_for_init"])
        self.assertEqual(s.calls, [])
        # The label describes the delivery promise ("once the bridge is up"),
        # not which call was made: a session mid-connect is not woken.
        self.assertEqual(env["data"]["action"], "woke")

    def test_waiting_hands_off_even_when_awake(self):
        s = self._add(FakeSession("a1", "s1"))
        env = sc.action_chat({"ref": "a1", "prompt": "p", "wait": True})
        self.assertTrue(env["_wait_for_init"])
        self.assertEqual(s.calls, [])

    def test_an_empty_prompt_is_refused(self):
        self._add(FakeSession("a1", "s1"))
        with self.assertRaises(sc.ControlError) as ctx:
            sc.action_chat({"ref": "a1", "prompt": "   "})
        self.assertEqual(ctx.exception.code, "bad_request")

    def test_the_same_idem_key_never_sends_twice(self):
        s = self._add(FakeSession("a1", "s1"))
        first = sc.action_chat({"ref": "a1", "prompt": "once", "idem": "k1"})
        second = sc.action_chat({"ref": "a1", "prompt": "once", "idem": "k1"})
        self.assertFalse(first["data"].get("duplicate"))
        self.assertTrue(second["data"]["duplicate"])
        self.assertEqual([c for c in s.calls if c[0] == "query"], [("query", "once", "📨 from outside agent")])


class TestInterrupt(ControlTestCase):
    def test_a_working_session_reports_what_it_leaves_behind(self):
        s = self._add(FakeSession("a1", "s1", working=True, phase="live"))

        def _interrupt():
            s.calls.append(("interrupt",))
            s.turn.kind = "interrupting"      # the bridge ack is outstanding
            s._user_cancelled_turn = True

        s.interrupt = _interrupt
        body, ref = sc.action_interrupt({"ref": "a1"})
        self.assertTrue(body["interrupted"])
        self.assertTrue(body["settling"])
        self.assertEqual(body["turn_phase"], "interrupting")
        self.assertTrue(body["user_cancelled"])
        self.assertEqual(s.calls, [("interrupt",)])
        self.assertEqual(ref["agent_id"], "a1")

    def test_idle_interrupt_is_honest(self):
        self._add(FakeSession("a1", "s1"))
        body, _ = sc.action_interrupt({"ref": "a1"})
        self.assertFalse(body["interrupted"])
        self.assertFalse(body["settling"])


class TestDispatch(ControlTestCase):
    def test_unknown_action_is_refused_with_the_list(self):
        env = sc.dispatch({"action": "kill"})
        self.assertFalse(env["ok"])
        self.assertIn("list", env["data"]["actions"])
        self.assertIn("unknown_action", env["error"])

    def test_the_op_never_evaluates_code(self):
        self._add(FakeSession("a1", "s1", name="x"))
        self._saved_rows([])
        env = sc.dispatch({"action": "list",
                           "code": "raise SystemExit(3)",
                           "tool": "sublime_eval"})
        self.assertTrue(env["ok"], "a smuggled code field is ignored, not run")
        self.assertEqual(env["data"]["count"], 1)

    def test_errors_come_back_as_an_envelope(self):
        env = sc.dispatch({"action": "view", "ref": "nope", "mode": "tail"})
        self.assertFalse(env["ok"])
        self.assertTrue(env["error"])
        self.assertEqual(env["data"]["code"], "not_found")

    def test_the_socket_routes_the_op(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "mcp", "socket_server.py")).read()
        self.assertIn('op == "sessions"', src)
        self.assertIn("features.session_control import dispatch", src)


class TestCli(unittest.TestCase):
    """The client half: request shape and rendering, with the socket faked."""

    def setUp(self):
        import features.sessions_cli as cli
        self.cli = cli
        self.sent = []
        self.original = cli.send

        def _send(req, timeout=30.0, path=""):
            self.sent.append((req, timeout, path))
            return {"result": self.reply}

        cli.send = _send

    def tearDown(self):
        self.cli.send = self.original

    def _run(self, argv):
        out = io.StringIO()
        prev = sys.stdout
        sys.stdout = out
        try:
            code = self.cli.main(argv)
        finally:
            sys.stdout = prev
        return code, out.getvalue()

    def test_list_sends_a_scoped_request_and_renders_rows(self):
        self.reply = {"ok": True, "data": {
            "count": 1, "states": {"idle": 1},
            "sessions": [{"kind": "live", "state": "idle", "name": "GUEST",
                          "backend": "grok", "agent_id": "agent-1",
                          "session_id": "sid-1", "query_count": 43,
                          "last_access": time.time() - 90,
                          "view": {"bound": True, "view_id": 59, "window": 2}}]}}
        code, text = self._run(["list", "--scope", "window", "--window", "2"])
        self.assertEqual(code, 0)
        req = self.sent[0][0]
        self.assertEqual(req["op"], "sessions")
        self.assertEqual(req["action"], "list")
        self.assertEqual(req["window"], "2")
        self.assertEqual(req["caller"]["kind"], "cli")
        self.assertIn("GUEST", text)
        self.assertIn("grok", text)

    def test_chat_with_wait_uses_a_second_request_for_the_reply(self):
        self.reply = {"ok": True, "data": {"action": "pending", "session": {"name": "G"}}}
        code, text = self._run(["chat", "GUEST", "hello", "--wait"])
        self.assertEqual(code, 0)
        self.assertEqual([r["action"] for r, _t, _p in self.sent], ["chat", "view"])
        self.assertEqual(self.sent[0][0]["prompt"], "hello")
        self.assertEqual(self.sent[1][0]["mode"], "tail")
        self.assertEqual(self.sent[1][0]["turns"], 1)

    def test_an_error_envelope_exits_non_zero(self):
        self.reply = {"ok": False, "error": "no live session 'x'",
                      "data": {"code": "not_found", "candidates": []}}
        code, _text = self._run(["view", "x"])
        self.assertEqual(code, 1)

    def test_json_mode_prints_the_envelope(self):
        self.reply = {"ok": True, "data": {"count": 0, "states": {}, "sessions": []}}
        code, text = self._run(["list", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["count"], 0)

    def test_no_command_prints_usage(self):
        code, text = self._run([])
        self.assertEqual(code, 2)
        self.assertIn("usage", text.lower())

    def test_manual_prints_the_file_without_a_socket(self):
        code, text = self._run(["--manual"])
        self.assertEqual(code, 0)
        self.assertEqual(self.sent, [], "the manual must not need Sublime")
        self.assertIn("# Submarine session control", text)
        self.assertIn("## 4. Commands", text)
        self.assertNotIn("```", text, "fences are dropped for the terminal")

    def test_manual_is_reachable_after_a_subcommand_too(self):
        code, text = self._run(["list", "--manual"])
        self.assertEqual(code, 0)
        self.assertIn("# Submarine session control", text)
        self.assertEqual(self.sent, [])

    def test_help_subcommand_points_at_the_manual(self):
        code, text = self._run(["help"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", text.lower())
        self.assertIn("--manual", text)

    def test_a_missing_manual_degrades_instead_of_crashing(self):
        original = self.cli.manual_path
        self.cli.manual_path = lambda: "/nonexistent/manual.md"
        try:
            code, text = self._run(["--manual"])
        finally:
            self.cli.manual_path = original
        self.assertEqual(code, 0)
        self.assertIn("manual not readable", text)
        self.assertIn("Expected it at", text)

    def test_the_manual_documents_every_command_and_option(self):
        text = self.cli.manual_text()
        parser = self.cli.build_parser()
        for action in parser._subparsers._group_actions:
            for name in action.choices:
                self.assertIn(name, text, "command %r is undocumented" % name)
        for opt in ("--scope", "--window", "--parent", "--mode", "--turns",
                    "--max-chars", "--offset", "--limit", "--file", "--queue",
                    "--wait", "--wait-timeout", "--idem", "--timeout",
                    "--socket", "--json", "--manual"):
            self.assertIn(opt, text, "option %s is undocumented" % opt)

    def test_every_command_example_in_the_manual_parses(self):
        """Drift guard: a wrong example is worse than none."""
        parser = self.cli.build_parser()
        examples = self._manual_commands()
        self.assertGreater(len(examples), 5, "expected worked examples")
        for cmd in examples:
            argv = shlex.split(cmd)[1:]
            try:
                parser.parse_args(argv)
            except SystemExit:
                self.fail("manual example does not parse: %s" % cmd)

    def _manual_commands(self):
        """Command lines from the manual, continuations joined, synopses out."""
        out = []
        pending = None
        for line in open(self.cli.manual_path()).read().splitlines():
            text = line.split(" #")[0].strip()   # trailing shell comment
            if pending is not None:
                pending = "%s %s" % (pending, text.rstrip("\\").strip())
                if not text.endswith("\\"):
                    out.append(pending)
                    pending = None
                continue
            if text.startswith("$ "):
                text = text[2:]
            if not text.startswith("submarine_sessions "):
                continue
            if "[" in text or "…" in text:
                continue      # a usage synopsis, not something to run
            if text.endswith("\\"):
                pending = text[:-1].strip()
                continue
            out.append(text)
        return out


if __name__ == "__main__":
    unittest.main()
