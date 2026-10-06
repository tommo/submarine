"""TODO: sessions parked to come back to, listed on top of the Sessions list."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import records  # noqa: E402
from ui import session_list as sl  # noqa: E402


def _row(sid, kind="saved", name=None, **kw):
    r = {"kind": kind, "session_id": sid, "agent_id": "submarine::" + sid,
         "name": name or sid, "backend": "claude", "status": "closed",
         "query_count": 3, "project": "/p"}
    r.update(kw)
    return r


class TodoSectionTest(unittest.TestCase):
    def test_parked_rows_move_to_a_todo_section_on_top(self):
        live = [_row("a", kind="live", status="ready"), _row("b", kind="live", status="ready")]
        here = [_row("c"), _row("d")]
        text, index = sl.render_list(live, here, [], set(), cols=100, todo={"b", "d"})
        order = [l.split(" (")[0] for l in text.splitlines()
                 if l.startswith(("TODO", "CURRENT", "HISTORY"))]
        self.assertEqual(order, ["TODO", "CURRENT", "HISTORY"])
        by = {r["session_id"]: r for r in index}
        self.assertEqual((by["b"]["section"], by["b"]["home"]), ("TODO", "CURRENT"))
        self.assertEqual((by["d"]["section"], by["d"]["home"]), ("TODO", "HISTORY"))
        self.assertEqual(by["a"]["section"], "CURRENT")
        self.assertEqual(sum(1 for r in index if r["session_id"] == "b"), 1)

    def test_no_todo_no_section(self):
        text, _ = sl.render_list([_row("a", kind="live")], [], [], set(), cols=100)
        self.assertNotIn("TODO", text)

    def test_a_chain_is_parked_by_any_of_its_ids(self):
        here = [_row("new", chain_ids=["old", "new"])]
        _t, index = sl.render_list([], here, [], set(), cols=100, todo={"old"})
        self.assertEqual(index[0]["section"], "TODO")


class TodoStoreTest(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp(prefix="submarine-todo-")

    def test_round_trip_and_snapshots_survive_a_star_change(self):
        records.save_todos({"x"}, self.proj, records={"x": {"name": "parked"}})
        self.assertEqual(records.load_todos(self.proj), {"x"})
        records.toggle_bookmark("y", self.proj, record={"name": "starred"})
        records.toggle_bookmark("y", self.proj)            # unstar
        self.assertEqual(records.load_bookmark_records(self.proj).get("x"),
                         {"name": "parked"}, "a star change dropped the TODO snapshot")
        records.save_todos(set(), self.proj)
        self.assertEqual(records.load_todos(self.proj), set())
        self.assertNotIn("x", records.load_bookmark_records(self.proj))

    def test_closing_a_parked_history_row_removes_it_like_history(self):
        calls = []
        orig = sl.remove_saved_session
        sl.remove_saved_session = lambda sid: calls.append(sid) or True
        try:
            sl.close_row(None, {"kind": "saved", "session_id": "h1",
                                "section": "TODO", "home": "HISTORY"})
        except Exception:
            pass
        finally:
            sl.remove_saved_session = orig
        self.assertIn("h1", calls)


if __name__ == "__main__":
    unittest.main()


class ManualOrderTest(unittest.TestCase):
    """Alt+Up/Down: a row swaps with its sibling; the saved order wins over
    band and recency, rows without one follow in the usual order."""

    def _live(self, sid, ts, parent=None):
        return _row(sid, kind="live", status="ready", last_access=ts,
                    parent_agent_id=parent)

    def _names(self, live, order):
        _t, index = sl.render_list(live, [], [], set(), cols=100, order=order)
        return [r["session_id"] for r in index if r["section"] == "CURRENT"], index

    def _move(self, index, sid, step):
        saved = {}
        cmd = sl.SubmarineSessionListStarCommand.__new__(sl.SubmarineSessionListStarCommand)
        cmd.view = None
        orig = (sl.load_order, sl.save_order, sl.refresh_session_list, sl._place_caret_on_session)
        sl.load_order = lambda cwd=None: {}
        sl.save_order = lambda order, cwd=None: saved.update(order) or True
        sl.refresh_session_list = lambda win: None
        sl._place_caret_on_session = lambda *a, **k: None
        try:
            row = next(r for r in index if r["session_id"] == sid)
            cmd._move(object(), index, row, "", step)
        finally:
            sl.load_order, sl.save_order, sl.refresh_session_list, sl._place_caret_on_session = orig
        return saved

    def test_move_up_and_down(self):
        live = [self._live("a", 300), self._live("b", 200), self._live("c", 100)]
        names, index = self._names(live, None)
        self.assertEqual(names, ["a", "b", "c"])                 # recency
        order = self._move(index, "c", -1)
        names, index = self._names(live, order)
        self.assertEqual(names, ["a", "c", "b"])
        order = self._move(index, "a", 1)
        self.assertEqual(self._names(live, order)[0], ["c", "a", "b"])

    def test_edges_and_new_rows(self):
        live = [self._live("a", 300), self._live("b", 200)]
        _n, index = self._names(live, None)
        self.assertEqual(self._move(index, "a", -1), {}, "top row cannot go up")
        order = self._move(index, "b", -1)
        live.append(self._live("new", 999))                    # newest, unplaced
        self.assertEqual(self._names(live, order)[0], ["b", "a", "new"])

    def test_a_child_moves_among_its_siblings_only(self):
        live = [self._live("p", 300), self._live("k1", 200, parent="submarine::p"),
                self._live("k2", 100, parent="submarine::p"), self._live("q", 50)]
        names, index = self._names(live, None)
        self.assertEqual(names, ["p", "k1", "k2", "q"])
        order = self._move(index, "k2", -1)
        self.assertEqual(self._names(live, order)[0], ["p", "k2", "k1", "q"])
        self.assertEqual(self._move(index, "k1", -1), {}, "first child stays under its parent")
