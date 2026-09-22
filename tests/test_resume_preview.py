"""Resume-preview: last turn, plus earlier if the tail is short."""
import json
import os
import sys
import tempfile
import types
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from features.resume import (
    display_prompt, select_preview, parse_claude_jsonl, parse_grok_chat,
    parse_kimi_wire, load_turns, format_turn_body,
    find_session_jsonl, find_claude_jsonl, paint_resume_preview,
    attach_resume_preview, grok_session_cwd, transcript_cwd,
)


class TestGrokResumeChain(unittest.TestCase):
    """A resumed grok session keeps its history.

    The CLI mints a new session id per resume, so the resumed id's own
    chat_history starts empty; the earlier turns live under the id it was
    resumed from. Records sharing an agent_id are that chain.
    """

    def setUp(self):
        import core.records as records
        from features import resume
        self.resume = resume
        self._root = resume.grok_sessions_root
        self._rows = records.load_saved_sessions
        self._td = tempfile.TemporaryDirectory(prefix="grok-chain-")
        resume.grok_sessions_root = lambda: self._td.name

    def tearDown(self):
        import core.records as records
        self.resume.grok_sessions_root = self._root
        records.load_saved_sessions = self._rows
        self._td.cleanup()

    def _session_dir(self, sid, turns):
        path = os.path.join(self._td.name, "%2Fproj", sid)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "chat_history.jsonl"), "w",
                  encoding="utf-8") as f:
            f.write(json.dumps({"type": "system", "content": "prompt"}) + "\n")
            for i in range(turns):
                f.write(json.dumps({
                    "type": "user",
                    "content": [{"type": "text", "text": "question %d" % i}],
                }) + "\n")
                f.write(json.dumps({
                    "type": "assistant",
                    "content": [{"type": "text", "text": "answer %d" % i}],
                }) + "\n")
        return path

    def _store(self, rows):
        import core.records as records
        records.load_saved_sessions = lambda plugin_dir=None: rows

    def test_resumed_id_reads_the_earlier_transcript(self):
        self._session_dir("old-sid", 3)
        self._session_dir("new-sid", 0)
        self._store([
            {"session_id": "old-sid", "agent_id": "agent-1", "last_activity": 1},
            {"session_id": "new-sid", "agent_id": "agent-1", "last_activity": 2},
        ])
        # The stored record names the agent, so even a caller that passes no
        # agent_id (the history quick panel) reaches the earlier transcript.
        self.assertEqual(len(load_turns("new-sid", "grok", "/proj")), 3)
        turns = load_turns("new-sid", "grok", "/proj", agent_id="agent-1")
        self.assertEqual(len(turns), 3)
        self.assertEqual(display_prompt(turns[0]["prompt"]), "question 0")
        # reveal/open the transcript: the file that actually holds the turns
        found = find_session_jsonl("new-sid", "grok", "/proj", "agent-1")
        self.assertTrue(found.endswith(os.path.join("old-sid", "chat_history.jsonl")))

    def test_chain_concatenates_in_order(self):
        self._session_dir("old-sid", 2)
        self._session_dir("new-sid", 1)
        self._store([
            {"session_id": "old-sid", "agent_id": "agent-1", "last_activity": 1},
            {"session_id": "new-sid", "agent_id": "agent-1", "last_activity": 2},
        ])
        turns = load_turns("new-sid", "grok", "/proj", agent_id="agent-1")
        self.assertEqual([display_prompt(t["prompt"]) for t in turns],
                         ["question 0", "question 1", "question 0"])

    def test_a_fork_finds_the_chain_through_the_resumed_id(self):
        """A fork carries a fresh agent_id; the resume target's record links it
        to the agent whose ids hold the transcript."""
        self._session_dir("old-sid", 2)
        self._session_dir("new-sid", 0)
        self._store([
            {"session_id": "old-sid", "agent_id": "agent-1", "last_activity": 1},
            {"session_id": "new-sid", "agent_id": "agent-1", "last_activity": 2},
        ])
        turns = load_turns("old-sid", "grok", "/proj", agent_id="agent-fork")
        self.assertEqual(len(turns), 2)

    def test_the_session_cwd_is_the_directory_the_cli_filed_it_under(self):
        """Resume sends the transcript's directory, not the window's project.

        Grok refuses `session/load` for a cwd the session is not filed under.
        """
        self._session_dir("old-sid", 2)
        self._store([
            {"session_id": "old-sid", "agent_id": "agent-1",
             "project": "/elsewhere", "last_activity": 1},
        ])
        self.assertEqual(grok_session_cwd("old-sid", "/elsewhere", "agent-1"),
                         "/proj")
        # An id the CLI never filed falls back to the id holding the turns.
        self.assertEqual(grok_session_cwd("fresh-sid", "/elsewhere", "agent-1"),
                         "/proj")
        # Other backends resolve their own way; resume cwd is left alone.
        self.assertEqual(transcript_cwd("claude", "old-sid", "/elsewhere"), "")

    def test_the_resumed_session_takes_the_transcript_directory(self):
        """A resume asks the features hook; a fork leaves the cwd alone."""
        from core.session import Session
        seen = {}

        def resolver(backend, session_id, cwd="", agent_id=""):
            seen.update(backend=backend, sid=session_id, cwd=cwd, agent=agent_id)
            return "/proj"

        class _S(object):
            resume_id = "old-sid"
            fork = False
            backend = "grok"
            cwd = "/elsewhere"
            agent_id = "agent-1"
            _transcript_cwd = staticmethod(resolver)

        self.assertEqual(Session._resume_cwd(_S(), {"project": "/elsewhere"}),
                         "/proj")
        self.assertEqual(seen, {"backend": "grok", "sid": "old-sid",
                                "cwd": "/elsewhere", "agent": "agent-1"})
        _S.fork = True
        self.assertEqual(Session._resume_cwd(_S(), {}), "")

    def test_a_session_without_agent_id_is_unchanged(self):
        self._session_dir("solo-sid", 2)
        self._store([])
        self.assertEqual(len(load_turns("solo-sid", "grok", "/proj")), 2)


class TestRestoreIdentity(unittest.TestCase):
    """A saved session_id is only resumable by the backend that made it.

    Regression: the view's backend stamp won, so a Grok session id was sent to
    the Claude bridge with the Grok model and died with
    "No conversation found with session ID: …".
    """

    def setUp(self):
        from tests.stubs import install_sublime

        install_sublime()
        from ui.listeners import resolve_restore_identity

        self.resolve = resolve_restore_identity

    def test_id_backend_wins_over_the_view_stamp(self):
        backend, rid, model, overrode = self.resolve(
            "claude", "grok-4.6",
            {"session_id": "01a09bd4", "backend": "grok", "model": "grok-4.6"})
        self.assertEqual(backend, "grok")
        self.assertEqual(rid, "01a09bd4")
        self.assertEqual(model, "grok-4.6")
        self.assertTrue(overrode)

    def test_foreign_model_is_not_applied_on_override(self):
        backend, rid, model, overrode = self.resolve(
            "claude", "grok-4.6",
            {"session_id": "z", "backend": "grok"})
        self.assertEqual(backend, "grok")
        self.assertIsNone(model, "the other backend's model leaked through")
        self.assertTrue(overrode)

    def test_matching_backend_is_untouched(self):
        backend, rid, model, overrode = self.resolve(
            "grok", "grok-4.6",
            {"session_id": "x", "backend": "grok", "model": "grok-4.6"})
        self.assertEqual((backend, rid, model, overrode),
                         ("grok", "x", "grok-4.6", False))

    def test_no_saved_row_means_no_resume(self):
        backend, rid, model, overrode = self.resolve("kimi", "k3", None)
        self.assertEqual(rid, None)
        self.assertEqual(backend, "kimi")
        self.assertEqual(model, "k3")
        self.assertFalse(overrode)

    def test_view_stamp_supplies_backend_when_row_has_none(self):
        backend, rid, model, overrode = self.resolve(
            None, None, {"session_id": "y", "backend": "kimi", "model": "k3"})
        self.assertEqual((backend, rid, model, overrode),
                         ("kimi", "y", "k3", False))

    def test_defaults_to_claude_with_nothing_known(self):
        self.assertEqual(self.resolve(None, None, {})[0], "claude")


class TestResumePreview(unittest.TestCase):
    def test_display_prompt_unwraps_user_query(self):
        raw = "<user_query>\npush\n</user_query>"
        self.assertEqual(display_prompt(raw), "push")

    def test_select_extends_short_tail(self):
        turns = [
            {"prompt": "aaa " * 80, "reply": "bbb " * 80, "tools": []},
            {"prompt": "push", "reply": "done", "tools": []},
        ]
        chosen = select_preview(turns, min_chars=200, max_turns=8)
        self.assertEqual(len(chosen), 2)
        self.assertEqual(chosen[-1]["prompt"], "push")
        long_last = [
            {"prompt": "old", "reply": "x", "tools": []},
            {"prompt": "new", "reply": "y" * 400, "tools": []},
        ]
        only = select_preview(long_last, min_chars=200, max_turns=8)
        self.assertEqual(len(only), 1)
        self.assertEqual(only[0]["prompt"], "new")
        nudge = [
            {"prompt": "https://github.com/ands/sproutline extract",
             "reply": "x" * 400, "tools": ["Read"]},
            {"prompt": "resume", "reply": "y" * 400, "tools": []},
        ]
        both = select_preview(nudge, min_chars=200, max_turns=8)
        self.assertEqual(len(both), 2)
        self.assertIn("sproutline", both[0]["prompt"])
        self.assertEqual(both[-1]["prompt"], "resume")

    def test_parse_claude_jsonl(self):
        recs = [
            {"type": "user", "message": {"content": [{"type": "text", "text": "hello"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read", "id": "1"},
                {"type": "text", "text": "world"},
            ]}},
        ]
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            with open(path, "w") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            turns = parse_claude_jsonl(path)
        finally:
            os.remove(path)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["prompt"], "hello")
        self.assertEqual(turns[0]["reply"], "world")
        self.assertEqual(turns[0]["tools"], ["Read"])

    def test_a_turn_replays_in_order_and_never_merges_across_text(self):
        """A resumed turn is text *between* tool calls. The preview used to
        pool every tool name above the whole reply — reordering the turn and
        turning calls pages apart into `×N` runs that never happened."""
        recs = [
            {"type": "user", "message": {"content": [{"type": "text", "text": "check both"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Reading the first file."},
                {"type": "tool_use", "name": "Read", "id": "1"},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "1", "content": "…"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Now the second."},
                {"type": "tool_use", "name": "Read", "id": "2"},
                {"type": "tool_use", "name": "Read", "id": "3"},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "2", "content": "…"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Both match."}]}},
        ]
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            with open(path, "w") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            turns = parse_claude_jsonl(path)
        finally:
            os.remove(path)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["events"], [
            ("text", "Reading the first file."),
            ("tool", "Read"),
            ("text", "Now the second."),
            ("tool", "Read"),
            ("tool", "Read"),
            ("text", "Both match."),
        ])
        self.assertEqual(format_turn_body(turns[0]), "\n\n".join([
            "Reading the first file.",
            "⚙ Read",
            "Now the second.",
            "⚙ Read ×2",          # these two really were consecutive
            "Both match.",
        ]))
        # The flattened views the CLI and the length check use are unchanged.
        self.assertEqual(turns[0]["tools"], ["Read", "Read", "Read"])
        self.assertEqual(
            turns[0]["reply"],
            "Reading the first file.Now the second.Both match.")

    def test_a_turn_parsed_without_events_still_renders(self):
        self.assertEqual(
            format_turn_body({"prompt": "p", "reply": "hi", "tools": ["Bash", "Bash"]}),
            "⚙ Bash ×2\n\nhi")

    def test_parse_grok_chat(self):
        recs = [
            {"type": "user", "content": [{"type": "text", "text": "hi"}]},
            {"type": "assistant", "content": "yo",
             "tool_calls": [{"name": "read_file"}]},
        ]
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            with open(path, "w") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            turns = parse_grok_chat(path)
        finally:
            os.remove(path)
        self.assertEqual(turns[0]["prompt"], "hi")
        self.assertEqual(turns[0]["reply"], "yo")
        self.assertIn("⚙ read_file", format_turn_body(turns[0]))
        self.assertIn("yo", format_turn_body(turns[0]))

    def test_parse_grok_skips_system_reminder_user(self):
        recs = [
            {"type": "user", "content": [{"type": "text", "text": "move the sim hands."}]},
            {"type": "assistant", "content": "ok",
             "tool_calls": [{"name": "run_terminal_command"}]},
            {"type": "user", "content": [{
                "type": "text",
                "text": "<system-reminder>\nBackground task \"term_d976b6dc3d\" "
                        "completed (terminated by signal SIGTERM).\n</system-reminder>",
            }]},
            {"type": "assistant", "tool_calls": [
                {"name": "run_terminal_command"},
                {"name": "run_terminal_command"},
            ]},
        ]
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            with open(path, "w") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            turns = parse_grok_chat(path)
        finally:
            os.remove(path)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["prompt"], "move the sim hands.")
        self.assertEqual(turns[0]["reply"], "ok")
        self.assertEqual(
            turns[0]["tools"],
            ["run_terminal_command", "run_terminal_command", "run_terminal_command"],
        )
        body = format_turn_body(turns[0])
        self.assertIn("⚙ run_terminal_command ×3", body)
        self.assertNotIn("system-reminder", body)
        self.assertNotIn("term_d976b6dc3d", body)

    def test_parse_kimi_wire(self):
        path = os.path.join(_ROOT, "tests", "fixtures", "kimi_wire_preview.jsonl")
        turns = parse_kimi_wire(path)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["prompt"], "what's the performance?")
        self.assertEqual(turns[0]["tools"], ["Read"])
        self.assertEqual(turns[0]["reply"], "Let me check RichLabel.")
        self.assertEqual(turns[1]["prompt"], "so richlabel has no cache?")
        self.assertEqual(turns[1]["tools"], ["Grep"])
        self.assertIn("no cache", turns[1]["reply"])

    def test_parse_kimi_wire_drops_cancelled_prompt(self):
        recs = [
            {"type": "turn.prompt", "input": "keep me", "origin": {"kind": "user"}},
            {"type": "context.append_loop_event",
             "event": {"type": "content.part",
                       "part": {"type": "text", "text": "ok"}}},
            {"type": "turn.prompt", "input": "go", "origin": {"kind": "user"}},
            {"type": "turn.cancel"},
        ]
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            with open(path, "w") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            turns = parse_kimi_wire(path)
        finally:
            os.remove(path)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["prompt"], "keep me")
        self.assertEqual(turns[0]["reply"], "ok")

    def test_load_turns_kimi_empty_without_store(self):
        self.assertEqual(load_turns("session_missing", "kimi"), [])


    def test_find_session_jsonl_missing(self):
        self.assertIsNone(find_session_jsonl("", "grok"))
        self.assertIsNone(find_session_jsonl("no-such-session", "kimi"))
        self.assertIsNone(find_claude_jsonl(""))

    def test_load_turns_claude_finds_jsonl_when_path_omitted(self):
        recs = [
            {"type": "user", "message": {"content": [{"type": "text", "text": "q"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "a"}]}},
        ]
        sid = "sess-find-me"
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, sid + ".jsonl")
            with open(path, "w") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            turns = load_turns(sid, "glm", cwd="", claude_jsonl=path)
            self.assertEqual(turns[0]["prompt"], "q")
            self.assertEqual(turns[0]["reply"], "a")

    def test_paint_resume_preview_writes_last_turn(self):
        class _Out(object):
            def __init__(self):
                self.calls = []
                self.conversations = []

            def prompt(self, text, context_names=None, context_refs=None):
                self.calls.append(("prompt", text))
                self.conversations.append(text)

            def text(self, content):
                self.calls.append(("text", content))

            def meta(self, duration, cost=None, usage=None):
                self.calls.append(("meta", duration))

        class _S(object):
            resume_id = "sid"
            session_id = "sid"
            fork = False
            quick_mode = False
            backend = "grok"
            cwd = ""
            output = None
            on_init = []

            def _cwd(self):
                return ""

            def _find_jsonl_path(self):
                return ""

        s = _S()
        s.output = _Out()
        import features.resume as resume

        orig = resume.load_turns
        resume.load_turns = lambda *a, **k: [
            {"prompt": "hello there", "reply": "hi back", "tools": ["Read"]},
        ]
        try:
            self.assertTrue(paint_resume_preview(s))
        finally:
            resume.load_turns = orig
        kinds = [c[0] for c in s.output.calls]
        self.assertEqual(kinds, ["prompt", "text", "meta"])
        self.assertEqual(s.output.calls[0][1], "hello there")
        self.assertIn("hi back", s.output.calls[1][1])
        self.assertFalse(paint_resume_preview(s))  # already has conversations

    def test_undo_reconnect_does_not_repaint_the_stripped_turn(self):
        class _Out(object):
            conversations = []
            current = None
            view = None

            def prompt(self, *a, **k):
                raise AssertionError("undo must not paint JSONL back")

        class _S(object):
            resume_id = "sid"
            session_id = "sid"
            fork = False
            quick_mode = False
            backend = "claude"
            cwd = ""
            output = None
            _park_composer_after_init = True

            def _find_jsonl_path(self):
                return "/tmp/x.jsonl"

        s = _S()
        s.output = _Out()
        self.assertFalse(paint_resume_preview(s))

    def test_existing_transcript_on_the_sheet_is_not_replaced(self):
        class _View(object):
            def substr(self, region):
                return "◎ old ▶\nhello\n◎ newer ▶\nworld\n"

        class _Out(object):
            conversations = []
            current = None
            view = _View()

            def prompt(self, *a, **k):
                raise AssertionError("must not overwrite the sheet")

        class _S(object):
            resume_id = "sid"
            session_id = "sid"
            fork = False
            quick_mode = False
            backend = "claude"
            cwd = ""
            output = None
            _park_composer_after_init = False

            def _find_jsonl_path(self):
                return "/tmp/x.jsonl"

        s = _S()
        s.output = _Out()
        self.assertFalse(paint_resume_preview(s))

    def test_paint_uses_the_resumed_id_not_the_reopened_one(self):
        """Closed session reopened fresh: history lives under resume_id.

        Regression: _on_init overwrites session_id with the backend's new id
        (resume-fallback), and the preview then looked for a transcript of a
        session that had none — so a reopened closed session painted nothing.
        """
        seen = {}

        class _Out(object):
            def __init__(self):
                self.calls = []
                self.conversations = []
                self.current = None

            def prompt(self, text, context_names=None, context_refs=None):
                self.calls.append(("prompt", text))

            def text(self, content):
                self.calls.append(("text", content))

            def meta(self, duration, cost=None, usage=None):
                self.calls.append(("meta", duration))

        class _S(object):
            resume_id = "closed-sid"
            session_id = "brand-new-sid"
            fork = False
            quick_mode = False
            backend = "grok"
            cwd = "/proj"
            output = None
            on_init = []

            def _cwd(self):
                return "/proj"

            def _find_jsonl_path(self):
                raise AssertionError(
                    "must not resolve a transcript from the reopened id")

        s = _S()
        s.output = _Out()
        import features.resume as resume

        orig = resume.load_turns

        def _fake(sid, backend, cwd="", claude_jsonl="", **kw):
            seen["sid"] = sid
            seen["claude_jsonl"] = claude_jsonl
            return [{"prompt": "the closed session's last ask",
                     "reply": "and its answer", "tools": []}]

        resume.load_turns = _fake
        try:
            self.assertTrue(paint_resume_preview(s))
        finally:
            resume.load_turns = orig
        self.assertEqual(seen["sid"], "closed-sid")
        self.assertEqual(seen["claude_jsonl"], "")
        self.assertEqual(s.output.calls[0], ("prompt", "the closed session's last ask"))

    def test_paint_without_resume_id_uses_the_live_id(self):
        seen = {}

        class _Out(object):
            def __init__(self):
                self.conversations = []
                self.current = None

            def prompt(self, text, context_names=None, context_refs=None):
                pass

            def text(self, content):
                pass

            def meta(self, duration, cost=None, usage=None):
                pass

        class _S(object):
            resume_id = None
            session_id = "live-sid"
            fork = False
            quick_mode = False
            backend = "grok"
            cwd = ""
            output = None
            on_init = []

            def _cwd(self):
                return ""

            def _find_jsonl_path(self):
                return "/tmp/live.jsonl"

        import features.resume as resume
        # A non-resumed session has no history to paint.
        s = _S()
        s.output = _Out()
        self.assertFalse(paint_resume_preview(s))

        s.resume_id = "resumed"
        s.session_id = "live-sid"
        orig = resume.load_turns
        resume.load_turns = lambda sid, be, cwd="", j="", **kw: seen.update(
            sid=sid, j=j) or []
        try:
            paint_resume_preview(s)
        finally:
            resume.load_turns = orig
        self.assertEqual(seen["sid"], "resumed")

    def test_on_init_paints_after_a_fallback_reopen(self):
        """End to end through the real output view and the real parser.

        The reopened id has no transcript; only the resumed id does.
        """
        from core.registry import default_registry
        from tests.fakes import make_session
        from tests.test_single_view import RecordingWindow
        from ui.host import HostView, set_ui_mode_override
        from ui.view import SubmarineOutputView
        import features.resume as resume

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "chat_history.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                for rec in (
                    {"type": "system", "content": "sys"},
                    {"type": "user", "content": "finished question"},
                    {"type": "assistant", "content": "final answer",
                     "tool_calls": [{"name": "Bash"}]},
                ):
                    f.write(json.dumps(rec) + "\n")

            def _find(sid, cwd="", agent_id=""):
                return path if sid == "closed-sid" else None

            default_registry.clear()
            HostView.reset()
            set_ui_mode_override("single")
            try:
                win = RecordingWindow()
                out = SubmarineOutputView(win)
                s = make_session(output=out, chrome=out, window=win,
                                 registry=default_registry)
                s.resume_id = "closed-sid"
                s.session_id = "closed-sid"
                s.backend = "grok"
                s.cwd = "/proj"
                s.client = object()
                attach_resume_preview(s)

                orig = resume.find_grok_chat
                resume.find_grok_chat = _find
                try:
                    s._on_init({
                        "session_id": "brand-new-sid",
                        "resume_fallback": True,
                        "model": "grok-4.6",
                    })
                finally:
                    resume.find_grok_chat = orig

                self.assertEqual(s.session_id, "brand-new-sid")
                cur = out.current
                painted = (cur.prompt or "") if cur is not None else ""
                self.assertEqual(painted, "finished question")
                body = "\n".join(
                    str(e) for e in (getattr(cur, "events", None) or []))
                self.assertIn("final answer", body)
            finally:
                default_registry.clear()
                HostView.reset()

    def test_paint_fork_uses_parent_resume_id(self):
        class _Out(object):
            def __init__(self):
                self.calls = []
                self.conversations = []
                self.current = None

            def prompt(self, text, context_names=None, context_refs=None):
                self.calls.append(("prompt", text))
                self.conversations.append(text)

            def text(self, content):
                self.calls.append(("text", content))

            def meta(self, duration, cost=None, usage=None):
                self.calls.append(("meta", duration))

        class _S(object):
            resume_id = "parent-sid"
            session_id = "new-fork-sid"
            fork = True
            quick_mode = False
            backend = "grok"
            cwd = ""
            output = None
            on_init = []

            def _cwd(self):
                return ""

            def _find_jsonl_path(self):
                raise AssertionError("fork must not read the new empty jsonl")

        s = _S()
        s.output = _Out()
        import features.resume as resume

        seen = []
        orig = resume.load_turns

        def _load(sid, backend, cwd="", claude_jsonl="", **kw):
            seen.append(sid)
            return [{"prompt": "hello", "reply": "hi", "tools": []}]

        resume.load_turns = _load
        try:
            self.assertTrue(paint_resume_preview(s))
        finally:
            resume.load_turns = orig
        self.assertEqual(seen, ["parent-sid"])
        self.assertEqual(s.output.calls[0], ("prompt", "hello"))

    def test_attach_resume_preview_hooks_on_init(self):
        class _S(object):
            on_init = None
            resume_id = "x"
            fork = False
            quick_mode = False
            scheduler = None

        s = _S()
        s.on_init = []
        attach_resume_preview(s)
        attach_resume_preview(s)
        self.assertEqual(len(s.on_init), 1)


class TestResumeTailScroll(unittest.TestCase):
    """Opening a session by resuming lands on the tail.

    The preview is painted into a sheet whose viewport is still at the top, so
    without this the session opens on the oldest line it painted.
    """

    def _session(self, resume_id="sid", following=(False,)):
        from tests.fakes import FakeScheduler
        scrolled = []
        state = {"following": following}

        class _Composer(object):
            def scroll_to_end(self, force=False):
                scrolled.append(bool(force))

        class _Sheet(object):
            def is_following_tail(self, slack=120):
                return state["following"][0]

        class _Out(object):
            composer = _Composer()
            sheet = _Sheet()

        class _S(object):
            quick_mode = False
            on_init = []
            output = _Out()

        s = _S()
        s.resume_id = resume_id
        s.scheduler = FakeScheduler()
        return s, scrolled, state

    def _paint(self, s):
        import features.resume as resume
        orig = resume.paint_resume_preview
        resume.paint_resume_preview = lambda session: True
        try:
            resume._on_init_paint(s, {"session_id": "sid"})
        finally:
            resume.paint_resume_preview = orig

    def test_a_resumed_sheet_scrolls_to_its_tail(self):
        s, scrolled, _state = self._session()
        self._paint(s)
        self.assertEqual(scrolled, [], "scrolling must wait for layout")
        s.scheduler.fire_due()
        self.assertEqual(scrolled, [True], "force, so history ownership cannot veto")

    def test_a_sheet_already_at_the_tail_is_left_alone(self):
        s, scrolled, _state = self._session(following=([True],))
        self._paint(s)
        s.scheduler.fire_due()
        self.assertEqual(scrolled, [])

    def test_a_fresh_session_does_not_scroll(self):
        s, scrolled, _state = self._session(resume_id=None)
        self._paint(s)
        s.scheduler.fire_due()
        self.assertEqual(scrolled, [])

    def test_a_viewless_session_is_not_touched(self):
        s, scrolled, _state = self._session()
        s.output = None
        self._paint(s)
        s.scheduler.fire_due()
        self.assertEqual(scrolled, [])


if __name__ == "__main__":
    unittest.main()


class TestSyntheticPromptLabel(unittest.TestCase):
    """A resumed transcript shows host-injected prompts the way the live
    sheet did (`⚙ …`), never the raw <task-notification> block."""

    def test_task_notification_becomes_the_sheet_label(self):
        from features.resume import display_prompt
        raw = ("<task-notification>\n<task-id>x1</task-id>\n<status>completed</status>\n"
               "<summary>Background command \"grep flexbox\" completed (exit code 0)</summary>\n"
               "</task-notification>")
        self.assertEqual(display_prompt(raw),
                         "⚙ 1 task notification: Background command \"grep flexbox\" completed (exit code 0)")
        two = raw + "\n<task-notification><summary>second</summary></task-notification>"
        self.assertTrue(display_prompt(two).startswith("⚙ 2 task notifications: "))

    def test_other_injects_and_real_prompts(self):
        from features.resume import display_prompt
        self.assertEqual(display_prompt("[Request interrupted by user]"), "[Request interrupted by user]")
        self.assertEqual(display_prompt("<wake>\ncarry on\n</wake>"), "⚙ wake: carry on")
        self.assertEqual(display_prompt("fix the bug"), "fix the bug")
        self.assertEqual(display_prompt("<user_query>\nreal\n</user_query>"), "real")

    def test_painted_turn_uses_the_label(self):
        from features import resume as r
        painted = []

        class _Out(object):
            def prompt(self, p): painted.append(p)
            def text(self, t): pass
            def meta(self, d): pass

        s = types.SimpleNamespace(output=_Out(), resume_id="sid", session_id="sid",
                                  backend="claude", cwd="", agent_id="a", fork=False)
        turns = [{"prompt": "<task-notification><summary>done</summary></task-notification>",
                  "reply": "x" * 600, "tools": [], "ts": 1}]
        orig = r.load_turns
        r.load_turns = lambda *a, **k: turns
        try:
            r.paint_resume_preview(s)
        finally:
            r.load_turns = orig
        self.assertEqual(painted, ["⚙ 1 task notification: done"])

