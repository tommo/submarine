"""`create` / `rename` / `close` / `backends` on the session-control surface."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

import features.session_control as sc
from core.records import SessionStore
from core.registry import default_registry
from tests.test_single_view import RecordingWindow, _SingleViewCase, _session
from ui.host import HostView


class TestRename(_SingleViewCase):
    def test_live_session_is_renamed_and_saved(self):
        win = RecordingWindow()
        s = _session(win, "old")
        s.query_count = 2
        default_registry.register_session(s)
        body, ref = sc.action_rename({"ref": s.agent_id, "name": "  new name "})
        self.assertEqual(body["name"], "new name")
        self.assertEqual(body["from"], "old")
        self.assertEqual(s.name, "new name")
        self.assertEqual(ref["name"], "new name")
        self.assertEqual(s.store.find(s.session_id)["name"], "new name")

    def test_saved_row_is_renamed_in_the_store(self):
        d = tempfile.mkdtemp(prefix="submarine-test-")
        path = os.path.join(d, ".sessions.json")
        with open(path, "w") as f:
            json.dump([{"session_id": "s-old", "agent_id": "a-old", "name": "x",
                        "state": "closed", "query_count": 1}], f)
        prev_rows, prev_rename = sc._saved_rows, None
        import core.records as rec
        prev_rename = rec.rename_saved_session
        sc._saved_rows = lambda: json.load(open(path))
        rec.rename_saved_session = lambda sid, name, plugin_dir=None: SessionStore(path).rename(sid, name)
        try:
            body, _ = sc.action_rename({"ref": "s-old", "name": "renamed"})
        finally:
            sc._saved_rows = prev_rows
            rec.rename_saved_session = prev_rename
        self.assertTrue(body["renamed"])
        self.assertEqual(SessionStore(path).find("s-old")["name"], "renamed")

    def test_empty_name_is_refused(self):
        with self.assertRaises(sc.ControlError) as cm:
            sc.action_rename({"ref": "x", "name": "   "})
        self.assertEqual(cm.exception.code, "bad_request")


class TestClose(_SingleViewCase):
    def test_bound_session_closes_and_host_hands_off(self):
        win = RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        default_registry.register_session(a)
        default_registry.register_session(b)
        hv = HostView.for_window(win)
        hv.attach(win, a)
        hv.attach(win, b)
        stopped = []
        b.stop = lambda: stopped.append("b")
        body, ref = sc.action_close({"ref": b.agent_id})
        self.assertTrue(body["closed"])
        self.assertEqual(stopped, ["b"])
        self.assertIs(hv.bound_session(win), a)

    def test_saved_row_needs_remove(self):
        prev = sc._saved_rows
        sc._saved_rows = lambda: [{"session_id": "s-h", "agent_id": "a-h", "name": "h",
                                   "state": "closed", "query_count": 1}]
        try:
            with self.assertRaises(sc.ControlError) as cm:
                sc.action_close({"ref": "s-h"})
            self.assertEqual(cm.exception.code, "bad_request")
            import core.records as rec
            prev_rm = rec.remove_saved_session
            gone = []
            rec.remove_saved_session = lambda sid, plugin_dir=None: gone.append(sid) or True
            try:
                body, _ = sc.action_close({"ref": "s-h", "remove": True})
            finally:
                rec.remove_saved_session = prev_rm
        finally:
            sc._saved_rows = prev
        self.assertTrue(body["removed"])
        self.assertEqual(gone, ["s-h"])


class TestCreateAndBackends(_SingleViewCase):
    def test_backends_lists_specs_and_windows(self):
        body = sc.action_backends({})
        names = [b["name"] for b in body["backends"]]
        self.assertIn("claude", names)
        for b in body["backends"]:
            self.assertIn("available", b)
            self.assertIsInstance(b["models"], list)
        self.assertIn("windows", body)

    def test_create_uses_the_factory_and_names_the_session(self):
        import ui.session_api as api
        win = RecordingWindow()
        win._folders = ["/proj/x"]
        made = []

        def fake_create(window, **kw):
            s = _session(window, "fresh")
            made.append((window, kw))
            default_registry.register_session(s)
            return s

        prev_create, prev_windows = api.create_session, sc._windows
        api.create_session = fake_create
        sc._windows = lambda: [win]
        try:
            env = sc.action_create({"backend": "claude", "model": "opus",
                                    "name": "planner", "project": "/proj/x"})
        finally:
            api.create_session = prev_create
            sc._windows = prev_windows
        self.assertTrue(env["ok"])
        self.assertTrue(env["data"]["created"])
        self.assertEqual(made[0][1]["backend"], "claude")
        self.assertEqual(made[0][1]["model"], "opus")
        self.assertFalse(made[0][1]["show"])
        self.assertEqual(env["data"]["session"]["name"], "planner")

    def test_create_refuses_an_unknown_backend_and_window(self):
        win = RecordingWindow()
        prev = sc._windows
        sc._windows = lambda: [win]
        try:
            with self.assertRaises(sc.ControlError) as cm:
                sc.action_create({"backend": "nope"})
            self.assertEqual(cm.exception.code, "bad_request")
            with self.assertRaises(sc.ControlError) as cm:
                sc.action_create({"window": 999})
            self.assertEqual(cm.exception.code, "not_found")
        finally:
            sc._windows = prev


if __name__ == "__main__":
    unittest.main()


class TestRead(unittest.TestCase):
    def test_reads_text_and_refuses_binary_and_missing(self):
        d = tempfile.mkdtemp(prefix="submarine-test-")
        t = os.path.join(d, "a.nim")
        with open(t, "w") as f:
            f.write("proc x() =\n  discard\n")
        body, ref = sc.action_read({"file_path": t})
        self.assertEqual(body["text"], "proc x() =\n  discard\n")
        self.assertEqual(body["lines"], 2)
        self.assertFalse(body["truncated"])
        self.assertIsNone(ref)
        b = os.path.join(d, "b.bin")
        with open(b, "wb") as f:
            f.write(b"\x00\x01\x02")
        with self.assertRaises(sc.ControlError) as cm:
            sc.action_read({"file_path": b})
        self.assertEqual(cm.exception.code, "bad_request")
        with self.assertRaises(sc.ControlError) as cm:
            sc.action_read({"file_path": os.path.join(d, "nope")})
        self.assertEqual(cm.exception.code, "not_found")

    def test_clips_to_max_bytes(self):
        d = tempfile.mkdtemp(prefix="submarine-test-")
        t = os.path.join(d, "big.txt")
        with open(t, "w") as f:
            f.write("x" * 5000)
        body, _ = sc.action_read({"file_path": t, "max_bytes": 2048})
        self.assertTrue(body["truncated"])
        self.assertEqual(len(body["text"]), 2048)

