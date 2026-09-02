"""Session list scratch: render + line index (no Sublime runtime)."""
from __future__ import annotations

import types
import unittest

from ui import session_list as sl


def _live(sid, name, **kw):
    row = {
        "kind": "live",
        "session_id": sid,
        "agent_id": kw.pop("aid", sid),
        "parent_agent_id": kw.pop("parent", None),
        "view_id": kw.pop("view_id", None),
        "name": name,
        "backend": kw.pop("backend", "grok"),
        "status": kw.pop("status", "ready"),
        "query_count": kw.pop("queries", 1),
        "same_window": True,
        "last_access": kw.pop("access", 1),
        "last_activity": kw.pop("activity", None),
    }
    if row["last_activity"] is None:
        row["last_activity"] = row["last_access"]
    row.update(kw)
    return row


def _saved(sid, name, **kw):
    row = _live(sid, name, **kw)
    row["kind"] = "saved"
    row["status"] = kw.get("status", "closed")
    row["view_id"] = None
    return row


def _session_lines(text):
    marks = ("○", "●", "?", "!", "⏸", "▸", "⊡", "·", "⚙")
    return [ln for ln in text.splitlines() if ln[:1] in marks]


class TestRenderSessionList(unittest.TestCase):
    def tearDown(self):
        import sys
        sm = sys.modules.get("sublime")
        if sm is not None and getattr(sm, "_submarine_stub", False):
            sys.modules.pop("sublime", None)
            sys.modules.pop("sublime_plugin", None)

    def test_render_assigns_lines(self):
        live = [{
            "kind": "live", "session_id": "s1", "view_id": 1,
            "name": "Skin editor", "backend": "grok", "status": "working",
            "query_count": 3, "same_window": True,
        }]
        here = [{
            "kind": "saved", "session_id": "s2", "view_id": None,
            "name": "old plan", "backend": "kimi", "status": "closed",
            "query_count": 1, "project": "/x/pil", "last_activity": 1700000000,
        }]
        text, index = sl.render_list(live, here, [], starred={"s2"})
        self.assertNotIn("STARRED", text)
        self.assertIn("CURRENT (1)", text)
        self.assertIn("HISTORY (1)", text)
        self.assertIn("Skin editor", text)
        self.assertIn("△ old plan", text)
        self.assertNotIn("★", text)
        self.assertIn("r rename", text)
        self.assertNotIn("refresh", text)
        self.assertIn("s star", text)
        self.assertIn("f fork", text)
        self.assertNotIn("jsonl", text)
        self.assertNotIn("c compact", text)
        compact, cidx = sl.render_list(live, here, [], starred={"s2"}, cols=24)
        self.assertIn("GR", compact)
        self.assertIn("KM", compact)
        self.assertNotIn("working", compact.split("Skin editor")[-1][:20])
        self.assertEqual(len(cidx), 2)
        self.assertEqual(len(index), 2)
        self.assertEqual(sl.row_at_line(index, index[0]["line"])["session_id"], "s1")
        self.assertEqual(sl.row_at_line(index, index[1]["line"])["session_id"], "s2")
        self.assertEqual(sl.format_when(100, now=100), "now")
        self.assertEqual(sl.format_when(100, now=130), "<1m")
        self.assertEqual(sl.format_when(100, now=159), "<1m")
        self.assertEqual(sl.format_when(100, now=100 + 5 * 60), "5m")
        self.assertEqual(sl.format_when(100, now=100 + 3 * 3600), "3h")
        self.assertEqual(sl.format_when(100, now=100 + 2 * 86400), "2d")
        self.assertEqual(sl.format_when(100, now=100 + 21 * 86400), "3w")

    def test_access_ts_prefers_focus(self):
        self.assertEqual(sl.access_ts({"last_activity": 10, "last_access": 20}), 20)
        self.assertEqual(sl.access_ts({"last_activity": 100, "last_access": 5}), 5)
        self.assertEqual(sl.access_ts({"last_activity": 10}), 10)
        self.assertEqual(sl.access_ts({}), 0)

    def test_session_title_recovers_truncated_name(self):
        conv = types.SimpleNamespace(
            prompt="check pui prop editor change, then wire the inspector")
        s = types.SimpleNamespace(
            name="check pui prop editor change,...",
            output=types.SimpleNamespace(conversations=[conv], current=None),
        )
        self.assertEqual(
            sl.session_title(s),
            "check pui prop editor change, then wire the inspector")
        s.name = "read pml_editor package"
        self.assertEqual(sl.session_title(s), "read pml_editor package")
        later = types.SimpleNamespace(
            prompt="Reply with exactly the word SWAPOK and nothing else.")
        s.name = "check pui prop editor change,..."
        s.output.conversations = [conv, later]
        self.assertEqual(
            sl.session_title(s),
            "check pui prop editor change, then wire the inspector")
        # Later turns must not steal the title; first_prompt / jsonl stem wins.
        s.name = "what solution can we use for a..."
        s.output.conversations = [types.SimpleNamespace(prompt="commit")]
        s.output.current = later
        s.first_prompt = (
            "what solution can we use for adding audio midi processing "
            "to pil? c/c++/nim")
        s._recovered_title = None
        self.assertEqual(
            sl.session_title(s),
            "what solution can we use for adding audio midi processing "
            "to pil? c/c++/nim")

    def test_saved_title_uses_first_prompt(self):
        self.assertEqual(
            sl.saved_title({
                "name": "https://github.com/ands/sprout...",
                "first_prompt": (
                    "https://github.com/ands/sproutline <- extract algorithm "
                    "from this repo"),
            }),
            "https://github.com/ands/sproutline <- extract algorithm "
            "from this repo")
        self.assertEqual(
            sl.saved_title({"name": "review", "first_prompt": "review"}),
            "review")
        self.assertEqual(
            sl.recover_title("short name", "short name but actually longer"),
            "short name")

    def test_backend_cell_aligns_deepseek(self):
        self.assertEqual(len(sl.backend_cell("grok")), sl.BACKEND_COL)
        self.assertEqual(len(sl.backend_cell("deepseek")), sl.BACKEND_COL)
        self.assertEqual(sl.backend_cell("deepseek"), "deepseek")
        live = [{
            "kind": "live", "session_id": "s1", "view_id": 1,
            "name": "fitler", "backend": "deepseek", "status": "ready",
            "query_count": 1, "same_window": True,
        }, {
            "kind": "live", "session_id": "s2", "view_id": 2,
            "name": "hello", "backend": "grok", "status": "ready",
            "query_count": 1, "same_window": True,
        }]
        text, _ = sl.render_list(live, [], [], cols=80)
        rows = [ln for ln in text.splitlines()
                if ln[:1] in ("○", "●") and "CURRENT" not in ln]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0].rstrip()), len(rows[1].rstrip()))
        self.assertTrue(rows[0].startswith("○ deepseek "))
        self.assertTrue(rows[1].startswith("○ grok     "))
        self.assertEqual(rows[0].index("fitler"), rows[1].index("hello"))

    def test_one_line_title_escapes_newline(self):
        self.assertEqual(
            sl.one_line_title("merge\n- **P1 — `merge` 1"),
            "merge↵- **P1 — `merge` 1")
        self.assertNotIn("\n", sl.one_line_title("a\r\nb\nc"))
        here = [{
            "kind": "saved", "session_id": "n1", "view_id": None,
            "name": "head\n- **P1 — merge", "backend": "grok",
            "status": "closed", "query_count": 0, "project": "/p",
            "last_activity": 1, "last_access": 1,
        }]
        text, _ = sl.render_list([], here, [], cols=80)
        self.assertEqual(text.count("\n- **P1"), 0)
        self.assertIn("↵", text)

    def test_same_view_compares_ids(self):
        class _V:
            def __init__(self, i):
                self._i = i

            def id(self):
                return self._i

        a, b, c = _V(1), _V(1), _V(2)
        self.assertTrue(sl._same_view(a, a))
        self.assertTrue(sl._same_view(a, b))
        self.assertFalse(sl._same_view(a, c))
        self.assertFalse(sl._same_view(a, None))

    def test_backend_abbrev(self):
        self.assertEqual(sl.backend_abbrev("grok"), "GR")
        self.assertEqual(sl.backend_abbrev("kimi"), "KM")
        self.assertEqual(sl.backend_abbrev("claude"), "CL")
        self.assertEqual(sl.backend_abbrev("codex"), "CX")

    def test_long_title_not_cut_while_space_remains(self):
        name = "=== PIL UNCAUGHT EXCEPTION === type: Library"
        live = [{
            "kind": "live", "session_id": "s1", "view_id": 1,
            "name": name, "backend": "grok", "status": "ready",
            "query_count": 1, "same_window": True,
        }]
        text, _ = sl.render_list(live, [], [], cols=80)
        row = [ln for ln in text.splitlines()
               if ln.startswith("○") and "CURRENT" not in ln][0]
        self.assertIn("Library", row)
        self.assertNotIn("…", row)
        self.assertIn(" 1q", row)
        self.assertTrue(row.rstrip().endswith("idle"))
        i_q = row.index("1q")
        i_st = row.rindex("idle")
        self.assertGreater(i_st - (i_q + 2), 1)

    def test_nq_closer_to_title_than_state(self):
        live = [{
            "kind": "live", "session_id": "s1", "view_id": 1,
            "name": "GUEST", "backend": "kimi", "status": "ready",
            "query_count": 10, "same_window": True,
        }]
        text, _ = sl.render_list(live, [], [], cols=80)
        row = [ln for ln in text.splitlines()
               if ln.startswith("○") and "CURRENT" not in ln][0]
        i_q = row.rfind("10q")
        i_st = row.rfind("idle")
        self.assertGreater(i_q, 0)
        self.assertGreater(i_st, i_q)
        self.assertEqual(row[i_q - 1], " ")
        self.assertGreater(i_st - (i_q + 3), 1)

    def test_title_uses_full_leftover(self):
        pre = "○ grok     "
        extra = sl._right_meta({
            "kind": "live", "status": "ready", "query_count": 1,
        })
        self.assertEqual(
            sl._name_budget(pre, extra, 80, False),
            80 - len(pre) - len(extra))
        live = [{
            "kind": "live", "session_id": "s1", "view_id": 1,
            "name": "GUEST", "backend": "kimi", "status": "ready",
            "query_count": 0, "same_window": True,
        }]
        text, _ = sl.render_list(live, [], [], cols=80)
        row = [ln for ln in text.splitlines()
               if ln.startswith("○") and "CURRENT" not in ln][0]
        self.assertEqual(len(row.rstrip()), 80)
        self.assertTrue(row.rstrip().endswith("idle"))

    def test_view_cols_no_double_margin(self):
        class _V:
            def viewport_extent(self):
                return (700.0, 400.0)

            def em_width(self):
                return 7.0

            def settings(self):
                return types.SimpleNamespace(get=lambda k, d=None: 6 if k == "margin" else d)

        cols = sl.view_cols(_V())
        self.assertEqual(cols, 98)

    def test_header_fits_view_cols(self):
        wide = sl.format_header(72)
        self.assertEqual(len(wide), 72)
        self.assertTrue(wide.startswith("SESSIONS"))
        self.assertTrue(wide.endswith("del close"))
        self.assertIn("enter", wide)
        self.assertIn("del", wide)
        self.assertIn("reveal", wide)
        narrow = sl.format_header(28)
        self.assertLessEqual(len(narrow), 28)
        self.assertTrue(narrow.startswith("SESSIONS"))

    def test_auto_compact_by_width(self):
        self.assertTrue(sl.use_compact(24))
        self.assertFalse(sl.use_compact(80))
        self.assertFalse(sl.use_compact(0))
        live = [{
            "kind": "live", "session_id": "s1", "view_id": 1,
            "name": "A" * 80, "backend": "grok", "status": "ready",
            "query_count": 0, "same_window": True,
        }]
        narrow, _ = sl.render_list(live, [], [], cols=24)
        nrow = [ln for ln in narrow.splitlines()
                if ln.startswith("○") and "CURRENT" not in ln][0]
        self.assertEqual(len(nrow), 24)
        self.assertIn("GR", nrow)
        self.assertNotIn("ready", nrow)
        self.assertTrue(nrow.rstrip().endswith("…"))
        wide, _ = sl.render_list(live, [], [], cols=80)
        wrow = [ln for ln in wide.splitlines()
                if ln.startswith("○") and "CURRENT" not in ln][0]
        self.assertGreater(len(wrow), len(nrow))
        self.assertIn("grok", wrow)
        self.assertIn("idle", wrow)
        self.assertEqual(len(wrow.rstrip()), 80)
        self.assertEqual(sl.fit_title("short", 10), "short     ")
        self.assertEqual(sl.fit_title("abcdefghij", 6), "abcde…")
        asleep = [{
            "kind": "live", "session_id": "z", "view_id": 2,
            "name": "nap", "backend": "kimi", "status": "sleeping",
            "query_count": 0, "same_window": True,
        }]
        sleep_txt, _ = sl.render_list(asleep, [], [], cols=80)
        srow = [ln for ln in sleep_txt.splitlines() if ln.startswith("⏸")][0]
        self.assertNotIn("sleeping", srow)

    def test_right_cols_align(self):
        live = [
            {"kind": "live", "session_id": "a", "view_id": 1,
             "name": "GUEST", "backend": "kimi", "status": "ready",
             "query_count": 0, "same_window": True,
             "last_access": 100, "last_activity": 100},
            {"kind": "live", "session_id": "b", "view_id": 2,
             "name": "inspector layout needs some polish here",
             "backend": "grok", "status": "ready",
             "query_count": 3, "same_window": True,
             "last_access": 50, "last_activity": 50},
        ]
        text, _ = sl.render_list(live, [], [], cols=80)
        rows = [ln for ln in text.splitlines()
                if ln[:1] in ("○", "●", "⏸") and "CURRENT" not in ln]
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].rstrip().endswith("idle"))
        self.assertTrue(rows[1].rstrip().endswith("idle"))
        self.assertEqual(len(rows[0].rstrip()), len(rows[1].rstrip()))
        for row in rows:
            self.assertNotRegex(row, r"\b\d+[smhdw]\b")
        asleep = {
            "kind": "live", "session_id": "z", "view_id": 3,
            "name": "nap", "backend": "kimi", "status": "sleeping",
            "query_count": 0, "same_window": True,
            "last_access": 1, "last_activity": 1,
        }
        mixed, _ = sl.render_list(live + [asleep], [], [], cols=80)
        srow = [ln for ln in mixed.splitlines() if ln.startswith("⏸")][0]
        self.assertRegex(srow, r"\b(\d+[smhdw]|now)\b")
        self.assertNotIn("idle", srow)
        self.assertEqual(len(srow.rstrip()), len(rows[0].rstrip()))

    def test_history_matches_running_cols(self):
        live = [{
            "kind": "live", "session_id": "a", "view_id": 1,
            "name": "edui-shader-theme", "backend": "grok",
            "status": "sleeping", "query_count": 0, "same_window": True,
            "last_access": 1, "last_activity": 1,
        }]
        here = [{
            "kind": "saved", "session_id": "b", "view_id": None,
            "name": "add middle button panning", "backend": "grok",
            "status": "closed", "query_count": 1, "project": "/x/pil",
            "last_access": 1, "last_activity": 1,
        }]
        text, _ = sl.render_list(live, here, [], cols=80)
        run = [ln for ln in text.splitlines() if ln.startswith("⏸")][0]
        hist = [ln for ln in text.splitlines()
                if ln.startswith("·") and "HISTORY" not in ln][0]
        self.assertNotIn("pil", hist)
        self.assertEqual(len(run.rstrip()), len(hist.rstrip()))

    def test_live_sorts_by_access_time(self):
        from tests.stubs import install
        sublime = install()
        older = types.SimpleNamespace(
            session_id="old", name="older", backend="grok",
            working=True, is_sleeping=False, query_count=9,
            last_activity=100, last_access=100,
            output=types.SimpleNamespace(view=None), window=None,
            quick_mode=False,
        )
        newer = types.SimpleNamespace(
            session_id="new", name="newer", backend="kimi",
            working=False, is_sleeping=True, query_count=1,
            last_activity=50, last_access=200,
            output=types.SimpleNamespace(view=None), window=None,
            quick_mode=False,
        )
        sublime._claude_sessions = {1: older, 2: newer}
        try:
            rows = sl.collect_live(None)
        finally:
            sublime._claude_sessions = {}
        self.assertEqual([r["session_id"] for r in rows], ["old", "new"])

    def test_input_waiting_status_and_sort(self):
        q = types.SimpleNamespace(callback=lambda *_a, **_k: None)
        waiting = types.SimpleNamespace(
            is_sleeping=False, working=True,
            output=types.SimpleNamespace(
                pending_permission=None, pending_question=q, pending_plan=None,
            ),
        )
        busy = types.SimpleNamespace(
            is_sleeping=False, working=True,
            output=types.SimpleNamespace(
                pending_permission=None, pending_question=None, pending_plan=None,
            ),
        )
        self.assertEqual(sl._status_of(waiting), "input")
        self.assertEqual(sl._status_of(busy), "working")
        compacting = types.SimpleNamespace(
            is_sleeping=False, working=False, _compacting=True,
            output=types.SimpleNamespace(
                pending_permission=None, pending_question=None, pending_plan=None,
            ),
        )
        self.assertEqual(sl._status_of(compacting), "working")
        self.assertEqual(sl._mark("input"), "?")
        self.assertEqual(sl._mark("unread"), "!")
        unread = types.SimpleNamespace(
            is_sleeping=False, working=False, unread=True, _compacting=False,
            output=types.SimpleNamespace(
                pending_permission=None, pending_question=None, pending_plan=None,
            ),
        )
        self.assertEqual(sl._status_of(unread), "unread")
        unread_working = types.SimpleNamespace(
            is_sleeping=False, working=True, unread=True, _compacting=False,
            output=types.SimpleNamespace(
                pending_permission=None, pending_question=None, pending_plan=None,
            ),
        )
        self.assertEqual(sl._status_of(unread_working), "working")
        bg_idle = types.SimpleNamespace(
            is_sleeping=False, working=False, unread=False, _compacting=False,
            output=types.SimpleNamespace(
                pending_permission=None, pending_question=None, pending_plan=None,
                active_background_tools=lambda: [object()],
            ),
        )
        self.assertEqual(sl._status_of(bg_idle), "bg")
        self.assertEqual(sl._mark("bg"), "⚙")
        row = {
            "kind": "live", "session_id": "ask", "view_id": 1,
            "name": "needs a choice", "backend": "kimi", "status": "input",
            "query_count": 2, "same_window": True,
            "last_access": 1, "last_activity": 1,
        }
        text, _ = sl.render_list([row], [], [], cols=80)
        self.assertIn("? ", text)
        self.assertIn("wait", text)
        urow = {
            "kind": "live", "session_id": "u1", "view_id": 2,
            "name": "done in background", "backend": "grok", "status": "unread",
            "query_count": 1, "same_window": True,
            "last_access": 1, "last_activity": 1,
        }
        utext, _ = sl.render_list([urow], [], [], cols=80)
        self.assertIn("! ", utext)
        self.assertIn("new", utext)
        bound_row = dict(urow)
        bound_row["bound"] = True
        bound_row["status"] = "ready"
        btext, _ = sl.render_list([bound_row], [], [], cols=80)
        self.assertIn("▸ ", btext)
        self.assertTrue(any(ln.startswith("▸") for ln in btext.splitlines()))
        self.assertFalse(any(ln.startswith("▸") for ln in utext.splitlines()))
        from tests.stubs import install
        sublime = install()
        ask = types.SimpleNamespace(
            session_id="ask", name="ask me", backend="kimi",
            working=True, is_sleeping=False, query_count=1,
            last_activity=1, last_access=1,
            output=types.SimpleNamespace(
                view=None, pending_permission=None,
                pending_question=q, pending_plan=None,
            ),
            window=None, quick_mode=False,
        )
        work = types.SimpleNamespace(
            session_id="work", name="busy", backend="grok",
            working=True, is_sleeping=False, query_count=2,
            last_activity=9, last_access=9,
            output=types.SimpleNamespace(
                view=None, pending_permission=None,
                pending_question=None, pending_plan=None,
            ),
            window=None, quick_mode=False,
        )
        sublime._claude_sessions = {1: work, 2: ask}
        try:
            rows = sl.collect_live(None)
        finally:
            sublime._claude_sessions = {}
        self.assertEqual([r["session_id"] for r in rows], ["ask", "work"])
        self.assertEqual(rows[0]["status"], "input")

    def test_history_sorts_by_access_time(self):
        prev = sl.load_saved_sessions
        sl.load_saved_sessions = lambda: [
            {"session_id": "a", "name": "old-access", "backend": "grok",
             "project": "/p", "last_activity": 300, "last_access": 300},
            {"session_id": "b", "name": "new-access", "backend": "kimi",
             "project": "/p", "last_activity": 100, "last_access": 400},
        ]
        try:
            here, other = sl.collect_history(set(), "/p")
        finally:
            sl.load_saved_sessions = prev
        self.assertEqual([r["session_id"] for r in here], ["b", "a"])
        self.assertEqual(other, [])
        self.assertEqual(sl.history_cap(), sl.HISTORY_CAP)
        self.assertEqual(sl.HISTORY_CAP, 200)

    def test_live_filters_to_window_project(self):
        from tests.stubs import install
        sublime = install()

        class _Win:
            def __init__(self, folders):
                self._folders = folders

            def folders(self):
                return self._folders

        here_win = _Win(["/proj/here"])
        other_win = _Win(["/proj/other"])
        here_s = types.SimpleNamespace(
            session_id="here", name="this project", backend="grok",
            working=False, is_sleeping=False, query_count=1,
            last_activity=1, last_access=1,
            output=types.SimpleNamespace(view=None), window=here_win,
            quick_mode=False,
        )
        other_s = types.SimpleNamespace(
            session_id="away", name="other project", backend="kimi",
            working=True, is_sleeping=False, query_count=2,
            last_activity=2, last_access=2,
            output=types.SimpleNamespace(view=None), window=other_win,
            quick_mode=False,
        )
        sublime._claude_sessions = {1: here_s, 2: other_s}
        try:
            rows = sl.collect_live(here_win)
        finally:
            sublime._claude_sessions = {}
        self.assertEqual([r["session_id"] for r in rows], ["here"])

    def test_render_hides_other_projects(self):
        here = [{
            "kind": "saved", "session_id": "h", "view_id": None,
            "name": "here", "backend": "grok", "status": "closed",
            "query_count": 0, "project": "/p", "last_activity": 1,
        }]
        other = [{
            "kind": "saved", "session_id": "o", "view_id": None,
            "name": "elsewhere", "backend": "kimi", "status": "closed",
            "query_count": 0, "project": "/q", "last_activity": 1,
        }]
        text, index = sl.render_list([], here, other, cols=80)
        self.assertIn("here", text)
        self.assertNotIn("elsewhere", text)
        self.assertNotIn("other projects", text)
        self.assertEqual([r["session_id"] for r in index], ["h"])

    def test_starred_pins_within_current_and_history(self):
        live = [{
            "kind": "live", "session_id": "pin", "view_id": 1,
            "name": "pinned live", "backend": "grok", "status": "ready",
            "query_count": 2, "same_window": True,
            "last_access": 9, "last_activity": 9,
        }, {
            "kind": "live", "session_id": "run", "view_id": 2,
            "name": "plain live", "backend": "kimi", "status": "working",
            "query_count": 1, "same_window": True,
            "last_access": 8, "last_activity": 8,
        }]
        here = [{
            "kind": "saved", "session_id": "oldpin", "view_id": None,
            "name": "pinned hist", "backend": "grok", "status": "closed",
            "query_count": 4, "project": "/p",
            "last_access": 1, "last_activity": 1,
        }, {
            "kind": "saved", "session_id": "old", "view_id": None,
            "name": "plain hist", "backend": "kimi", "status": "closed",
            "query_count": 0, "project": "/p",
            "last_access": 2, "last_activity": 2,
        }]
        text, index = sl.render_list(live, here, [], starred={"pin", "oldpin"}, cols=80)
        self.assertNotIn("STARRED", text)
        self.assertIn("CURRENT (2)", text)
        self.assertIn("HISTORY (2)", text)
        ids = [r["session_id"] for r in index]
        self.assertEqual(ids, ["pin", "run", "oldpin", "old"])
        self.assertIn("△ pinned live", text)
        self.assertIn("△ pinned hist", text)
        self.assertNotIn("△ plain live", text)
        self.assertNotIn("△ plain hist", text)
        cur = text.split("CURRENT")[1].split("HISTORY")[0]
        self.assertLess(cur.find("pinned live"), cur.find("plain live"))
        hist = text.split("HISTORY")[1]
        self.assertLess(hist.find("pinned hist"), hist.find("plain hist"))

    def test_drop_empty_sessions(self):
        rows = [
            {"session_id": "a", "query_count": 0, "name": "unused"},
            {"session_id": "b", "query_count": 2, "name": "used"},
            {"session_id": "c", "query_count": 0, "name": "pinned empty"},
        ]
        kept = sl.drop_empty_sessions(rows, starred={"c"})
        self.assertEqual([r["session_id"] for r in kept], ["b", "c"])
        # Live / open sheets are not filtered — only unused history.

    def test_pin_starred_empty(self):
        live = [{"session_id": "a", "kind": "live", "status": "ready"}]
        here = [{"session_id": "b", "kind": "saved", "status": "closed"}]
        self.assertEqual(sl.pin_starred(live, set()), live)
        self.assertEqual(sl.pin_starred(here, set()), here)
        rows = [{
            "kind": "live", "session_id": "a", "view_id": 1,
            "name": "x", "backend": "grok", "status": "ready",
            "query_count": 0, "same_window": True,
        }]
        text, _ = sl.render_list(rows, [], [], starred=set())
        self.assertNotIn("STARRED", text)

    def test_rename_row_saved(self):
        seen = []
        sl.rename_saved_session = lambda sid, name, s=seen: (
            s.append((sid, name)) or True)
        row = {
            "kind": "saved", "session_id": "s9", "name": "old",
            "backend": "grok",
        }
        self.assertTrue(sl.rename_row(None, row, "  new title  "))
        self.assertEqual(seen, [("s9", "new title")])
        self.assertFalse(sl.rename_row(None, row, "   "))
        self.assertFalse(sl.rename_row(None, None, "x"))

    def test_close_row_history_removes_saved(self):
        gone = []
        sl.remove_saved_session = lambda sid, g=gone: (g.append(sid) or True)
        row = {
            "kind": "saved", "session_id": "dead", "name": "old",
            "backend": "grok", "section": "HISTORY",
        }
        self.assertTrue(sl.close_row(None, row))
        self.assertEqual(gone, ["dead"])
        self.assertFalse(sl.close_row(None, None))
        self.assertFalse(sl.close_row(None, {"kind": "saved"}))
        self.assertFalse(sl.close_row(None, {
            "kind": "saved", "session_id": "keep", "section": "CURRENT",
        }))
        self.assertEqual(gone, ["dead"])

    def test_close_row_current_keeps_saved(self):
        gone = []
        prev_remove = sl.remove_saved_session
        prev_live = sl._live_session_for_row
        sl.remove_saved_session = lambda sid, g=gone: (g.append(sid) or True)

        class _Sess:
            output = None
            agent_id = None
            stopped = False

            def stop(self):
                self.stopped = True

        sess = _Sess()
        sl._live_session_for_row = lambda row, s=sess: s
        row = {
            "kind": "live", "session_id": "live1", "name": "x",
            "backend": "grok", "section": "CURRENT",
        }
        try:
            self.assertTrue(sl.close_row(None, row))
            self.assertTrue(sess.stopped)
            self.assertEqual(gone, [])
            starred_live = dict(row)
            starred_live["section"] = "CURRENT"
            sess.stopped = False
            self.assertTrue(sl.close_row(None, starred_live))
            self.assertTrue(sess.stopped)
            self.assertEqual(gone, [])
        finally:
            sl.remove_saved_session = prev_remove
            sl._live_session_for_row = prev_live

    def test_render_stamps_section(self):
        live = [{
            "kind": "live", "session_id": "cur", "view_id": 1,
            "name": "now", "backend": "grok", "status": "ready",
            "query_count": 1, "same_window": True,
        }]
        here = [{
            "kind": "saved", "session_id": "old", "name": "then",
            "backend": "grok", "query_count": 3, "project": "/p",
            "last_activity": 1.0,
        }]
        _, index = sl.render_list(live, here, [], starred=set())
        by_id = {r["session_id"]: r.get("section") for r in index}
        self.assertEqual(by_id["cur"], "CURRENT")
        self.assertEqual(by_id["old"], "HISTORY")

    def test_dclick_does_not_open_neighbor(self):
        opened = []
        sl._last_open = (0.0, None)
        orig_focus = sl.focus_live
        orig_wake = sl._wake_if_sleeping
        import ui.host as host
        orig_sm = host.is_single_mode
        sl.focus_live = lambda w, r, o=opened: (o.append(r["session_id"]) or True)
        sl._wake_if_sleeping = lambda s: None
        host.is_single_mode = lambda: False
        a = {"kind": "live", "session_id": "target", "agent_id": "a1"}
        b = {"kind": "live", "session_id": "neighbor", "agent_id": "a2"}
        try:
            self.assertTrue(sl.open_row(None, a))
            # Same-key debounce still swallows a second click on `a`.
            self.assertTrue(sl.open_row(None, a))
            self.assertEqual(opened, ["target"])
        finally:
            sl.focus_live = orig_focus
            sl._wake_if_sleeping = orig_wake
            host.is_single_mode = orig_sm
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "ui", "session_list.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn('view.run_command("submarine_session_list_open")', src)

    def test_tree_parent_children_grandchild_contiguous(self):
        live = [
            _live("g", "grand", aid="ag", parent="ac", access=10),
            _live("c2", "child-b", aid="ac2", parent="ap", access=20),
            _live("c1", "child-a", aid="ac", parent="ap", access=30),
            _live("p", "parent", aid="ap", access=5),
        ]
        text, index = sl.render_list(live, [], [], cols=80)
        ids = [r["session_id"] for r in index]
        self.assertEqual(ids, ["p", "c1", "g", "c2"])
        self.assertEqual([r.get("depth") for r in index], [0, 1, 2, 1])
        lines = _session_lines(text)
        by_name = {r["name"]: ln for r, ln in zip(index, lines)}
        self.assertNotIn(sl.CHILD_MARK, by_name["parent"])
        self.assertIn(sl.CHILD_MARK, by_name["child-a"])
        self.assertIn(sl.CHILD_MARK, by_name["grand"])
        self.assertIn(sl.CHILD_MARK, by_name["child-b"])
        self.assertLess(by_name["child-a"].index(sl.CHILD_MARK),
                        by_name["grand"].index(sl.CHILD_MARK))
        self.assertEqual(
            by_name["child-a"].index(sl.CHILD_MARK),
            by_name["child-b"].index(sl.CHILD_MARK))
        # Tree stays contiguous: grand sits between the two siblings.
        self.assertEqual(
            [ln for ln in lines if "child" in ln or "grand" in ln or "parent" in ln],
            lines)

    def test_tree_orphan_is_root(self):
        live = [
            _live("c", "orphan", aid="ac", parent="missing", access=10),
            _live("p", "other", aid="ap", access=20),
        ]
        text, index = sl.render_list(live, [], [], cols=80)
        self.assertEqual([r["session_id"] for r in index], ["p", "c"])
        self.assertEqual([r.get("depth") for r in index], [0, 0])
        self.assertNotIn(sl.CHILD_MARK, text)

    def test_tree_root_and_sibling_recency(self):
        live = [
            _live("r1", "older-root", aid="a1", access=10),
            _live("r2", "newer-root", aid="a2", access=50),
            _live("c1", "older-sib", aid="c1", parent="a2", access=20),
            _live("c2", "newer-sib", aid="c2", parent="a2", access=40),
        ]
        _, index = sl.render_list(live, [], [], cols=80)
        self.assertEqual(
            [r["session_id"] for r in index], ["r2", "c2", "c1", "r1"])
        self.assertEqual([r.get("depth") for r in index], [0, 1, 1, 0])

    def test_tree_cycle_no_hang_or_dup(self):
        live = [
            _live("a", "A", aid="aa", parent="ab", access=3),
            _live("b", "B", aid="ab", parent="aa", access=2),
            _live("c", "C", aid="ac", parent="ac", access=1),
            _live("d", "D", aid="ad", parent="ae", access=4),
            _live("e", "E", aid="ae", parent="af", access=5),
            _live("f", "F", aid="af", parent="ad", access=6),
        ]
        ordered = sl.tree_order(live)
        ids = [r["session_id"] for r in ordered]
        self.assertEqual(sorted(ids), ["a", "b", "c", "d", "e", "f"])
        self.assertEqual(len(ids), 6)
        self.assertEqual([r.get("depth") for r in ordered], [0] * 6)
        text, index = sl.render_list(live, [], [], cols=80)
        self.assertEqual(len(index), 6)
        self.assertNotIn(sl.CHILD_MARK, text)

    def test_tree_starred_child_pins_to_section_top(self):
        live = [
            _live("p", "parent", aid="ap", access=30),
            _live("c", "star-child", aid="ac", parent="ap", access=20),
            _live("g", "grand", aid="ag", parent="ac", access=15),
            _live("o", "other", aid="ao", access=10),
        ]
        text, index = sl.render_list(live, [], [], starred={"c"}, cols=80)
        self.assertEqual(
            [r["session_id"] for r in index], ["c", "g", "p", "o"])
        self.assertEqual([r.get("depth") for r in index], [0, 1, 0, 0])
        lines = _session_lines(text)
        child = [ln for ln in lines if "star-child" in ln][0]
        grand = [ln for ln in lines if "grand" in ln][0]
        parent = [ln for ln in lines if ln.endswith("parent") or " parent" in ln][0]
        self.assertIn("△", child)
        self.assertNotIn(sl.CHILD_MARK, child)
        self.assertIn(sl.CHILD_MARK, grand)
        self.assertNotIn(sl.CHILD_MARK, parent)

    def test_tree_starred_root_carries_subtree(self):
        live = [
            _live("p", "star-root", aid="ap", access=10),
            _live("c", "kid", aid="ac", parent="ap", access=9),
            _live("g", "grand", aid="ag", parent="ac", access=8),
            _live("o", "newer-other", aid="ao", access=50),
        ]
        text, index = sl.render_list(
            live, [], [], starred={"p"}, cols=80)
        self.assertEqual(
            [r["session_id"] for r in index], ["p", "c", "g", "o"])
        self.assertEqual([r.get("depth") for r in index], [0, 1, 2, 0])
        self.assertIn("△ star-root", text)
        self.assertNotIn("△ kid", text)
        kid = [ln for ln in _session_lines(text) if "kid" in ln][0]
        self.assertIn(sl.CHILD_MARK, kid)

    def test_tree_starred_child_under_starred_root_stays_indented(self):
        live = [
            _live("p", "star-root", aid="ap", access=10),
            _live("c", "star-kid", aid="ac", parent="ap", access=9),
            _live("o", "other", aid="ao", access=50),
        ]
        text, index = sl.render_list(
            live, [], [], starred={"p", "c"}, cols=80)
        self.assertEqual([r["session_id"] for r in index], ["p", "c", "o"])
        self.assertEqual([r.get("depth") for r in index], [0, 1, 0])
        kid = [ln for ln in _session_lines(text) if "star-kid" in ln][0]
        self.assertIn(sl.CHILD_MARK, kid)
        self.assertIn("△", kid)

    def test_tree_row_index_opens_grandchild(self):
        live = [
            _live("p", "parent", aid="ap", access=30),
            _live("c", "child", aid="ac", parent="ap", access=20),
            _live("g", "grand", aid="ag", parent="ac", access=10),
        ]
        _, index = sl.render_list(live, [], [], cols=80)
        grand = sl.row_at_line(index, index[2]["line"])
        self.assertEqual(grand["session_id"], "g")
        self.assertEqual(grand.get("depth"), 2)
        opened = []
        sl._last_open = (0.0, None)
        orig_focus = sl.focus_live
        orig_wake = sl._wake_if_sleeping
        import ui.host as host
        orig_sm = host.is_single_mode
        sl.focus_live = lambda w, r, o=opened: (o.append(r["session_id"]) or True)
        sl._wake_if_sleeping = lambda s: None
        host.is_single_mode = lambda: False
        try:
            self.assertTrue(sl.open_row(None, grand))
            self.assertEqual(opened, ["g"])
        finally:
            sl.focus_live = orig_focus
            sl._wake_if_sleeping = orig_wake
            host.is_single_mode = orig_sm

    def test_tree_column_alignment_siblings_and_deep_backend(self):
        live = [
            _live("p", "parent", aid="ap", access=10, backend="grok"),
            _live("c1", "sib-a", aid="ac1", parent="ap", access=9,
                  backend="deepseek"),
            _live("c2", "sib-b", aid="ac2", parent="ap", access=8,
                  backend="grok"),
        ]
        chain = [_live("d0", "deep-root", aid="d0", access=1, backend="grok")]
        for i in range(1, 8):
            chain.append(_live(
                "d%d" % i, "deep-%d" % i, aid="d%d" % i,
                parent="d%d" % (i - 1), access=1, backend="deepseek"))
        text, index = sl.render_list(live + chain, [], [], cols=80)
        lines = _session_lines(text)
        sib_a = [ln for ln in lines if "sib-a" in ln][0]
        sib_b = [ln for ln in lines if "sib-b" in ln][0]
        self.assertEqual(sib_a.index("deepseek"), sib_b.index("grok"))
        self.assertEqual(sib_a.index("sib-a"), sib_b.index("sib-b"))
        self.assertEqual(len(sib_a.rstrip()), 80)
        self.assertEqual(len(sib_b.rstrip()), 80)
        deep = [ln for ln in lines if "deep-7" in ln][0]
        self.assertIn(sl.CHILD_MARK, deep)
        self.assertEqual(len(deep.rstrip()), 80)
        self.assertEqual(
            sl.tree_prefix(7), sl.tree_prefix(sl.TREE_DEPTH_CAP))
        parent = [ln for ln in lines if "parent" in ln][0]
        self.assertEqual(len(parent.rstrip()), len(sib_a.rstrip()))

    def test_tree_parent_deleted_children_become_roots(self):
        kids = [
            _live("c1", "kid-a", aid="ac1", parent="ap", access=9),
            _live("c2", "kid-b", aid="ac2", parent="ap", access=8),
        ]
        with_p = [_live("p", "parent", aid="ap", access=10)] + kids
        text1, idx1 = sl.render_list(with_p, [], [], cols=80)
        self.assertEqual([r["session_id"] for r in idx1], ["p", "c1", "c2"])
        self.assertIn(sl.CHILD_MARK, text1)
        gone = []
        prev_remove = sl.remove_saved_session
        prev_live = sl._live_session_for_row

        class _Sess:
            output = None
            agent_id = "ap"
            stopped = False

            def stop(self):
                self.stopped = True

        sess = _Sess()
        sl.remove_saved_session = lambda sid, g=gone: (g.append(sid) or True)
        sl._live_session_for_row = lambda row, s=sess: s
        try:
            self.assertTrue(sl.close_row(None, idx1[0]))
            self.assertTrue(sess.stopped)
            self.assertEqual(gone, [])
        finally:
            sl.remove_saved_session = prev_remove
            sl._live_session_for_row = prev_live
        text2, idx2 = sl.render_list(kids, [], [], cols=80)
        self.assertEqual([r["session_id"] for r in idx2], ["c1", "c2"])
        self.assertEqual([r.get("depth") for r in idx2], [0, 0])
        self.assertNotIn(sl.CHILD_MARK, text2)

    def test_tree_history_uses_saved_parent_agent_id(self):
        here = [
            _saved("c", "hist-child", aid="ac", parent="ap", access=9, queries=2),
            _saved("p", "hist-parent", aid="ap", access=4, queries=3),
        ]
        text, index = sl.render_list([], here, [], cols=80)
        self.assertEqual([r["session_id"] for r in index], ["p", "c"])
        self.assertEqual([r.get("depth") for r in index], [0, 1])
        child = [ln for ln in _session_lines(text) if "hist-child" in ln][0]
        self.assertTrue(child.startswith("·"))
        self.assertIn(sl.CHILD_MARK, child)

    def test_collect_live_copies_parent_agent_id(self):
        from tests.stubs import install
        sublime = install()
        parent = types.SimpleNamespace(
            session_id="p", agent_id="ap", parent_agent_id=None,
            name="parent", backend="grok", working=False, is_sleeping=False,
            query_count=1, last_activity=1, last_access=2,
            output=types.SimpleNamespace(view=None), window=None,
            quick_mode=False,
        )
        child = types.SimpleNamespace(
            session_id="c", agent_id="ac", parent_agent_id="ap",
            name="child", backend="kimi", working=False, is_sleeping=False,
            query_count=1, last_activity=1, last_access=1,
            output=types.SimpleNamespace(view=None), window=None,
            quick_mode=False,
        )
        sublime._claude_sessions = {1: parent, 2: child}
        try:
            rows = sl.collect_live(None)
        finally:
            sublime._claude_sessions = {}
        by_id = {r["session_id"]: r for r in rows}
        self.assertEqual(by_id["p"]["agent_id"], "ap")
        self.assertFalse(by_id["p"].get("parent_agent_id"))
        self.assertEqual(by_id["c"]["parent_agent_id"], "ap")

    def test_tree_compact_keeps_child_mark(self):
        live = [
            _live("p", "parent", aid="ap", access=2),
            _live("c", "child", aid="ac", parent="ap", access=1),
        ]
        text, _ = sl.render_list(live, [], [], cols=24)
        child = [ln for ln in text.splitlines() if "ch" in ln or sl.CHILD_MARK in ln]
        self.assertTrue(any(sl.CHILD_MARK in ln for ln in child))


if __name__ == "__main__":
    unittest.main()
