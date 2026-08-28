"""Agent artifacts store, MCP routing, convention path, cards, auto-open."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest

from core.artifacts import (
    ArtifactStore,
    format_bytes,
    handle_convention_write,
    handle_external_save,
    is_under_artifact_root,
    maybe_auto_open_session,
    publish_to_session,
    sanitize_slug,
    should_auto_open,
)
from tests.fakes import FakeOutput, make_session
from ui.models import ArtifactCard


class _TmpStore(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="submarine-art-")
        self.root = self._td.name
        self._old_env = os.environ.get("SUBMARINE_ARTIFACTS_DIR")
        os.environ["SUBMARINE_ARTIFACTS_DIR"] = self.root
        self.store = ArtifactStore(root=self.root, index_cap=500)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("SUBMARINE_ARTIFACTS_DIR", None)
        else:
            os.environ["SUBMARINE_ARTIFACTS_DIR"] = self._old_env
        self._td.cleanup()


class TestStoreWrite(_TmpStore):
    def test_create_append_rewrite(self):
        rec = self.store.write(
            "auth-analysis", "# v1\n", owner="agent-x",
            title="Auth", summary="flow")
        self.assertEqual(rec["op"], "create")
        self.assertTrue(os.path.isfile(rec["path"]))
        self.assertTrue(rec["path"].endswith("auth-analysis.md"))
        with open(rec["path"], encoding="utf-8") as f:
            self.assertEqual(f.read(), "# v1\n")

        rec2 = self.store.write(
            "auth-analysis", " more", owner="agent-x", mode="append")
        self.assertEqual(rec2["op"], "append")
        self.assertEqual(rec2["path"], rec["path"])
        with open(rec["path"], encoding="utf-8") as f:
            self.assertEqual(f.read(), "# v1\n more")

        rec3 = self.store.write(
            "auth-analysis", "# v2\n", owner="agent-x", summary="rewritten")
        self.assertEqual(rec3["op"], "rewrite")
        self.assertEqual(rec3["path"], rec["path"])
        with open(rec["path"], encoding="utf-8") as f:
            self.assertEqual(f.read(), "# v2\n")

        tail = self.store.journal_tail(rec["path"])
        ops = [e["op"] for e in tail]
        self.assertEqual(ops, ["create", "append", "rewrite"])
        self.assertEqual(tail[-1]["sha"], rec3["sha"])
        self.assertEqual(tail[-1]["bytes"], rec3["bytes"])

    def test_slug_collision_suffix(self):
        p1 = self.store.unique_path("agent-x", "dup.md")
        self.assertTrue(p1.endswith("dup.md"))
        os.makedirs(os.path.dirname(p1), exist_ok=True)
        with open(p1, "w", encoding="utf-8") as f:
            f.write("a")
        p2 = self.store.unique_path("agent-x", "dup.md")
        self.assertTrue(p2.endswith("dup-2.md"))
        with open(p2, "w", encoding="utf-8") as f:
            f.write("b")
        p3 = self.store.unique_path("agent-x", "dup.md")
        self.assertTrue(p3.endswith("dup-3.md"))
        self.assertEqual(sanitize_slug("Hello World!!"), "Hello-World.md")
        self.assertEqual(sanitize_slug("x"), "x.md")

    def test_index_mru_and_cap(self):
        store = ArtifactStore(root=self.root, index_cap=3)
        paths = []
        for i in range(5):
            rec = store.write("n%s" % i, "c%s" % i, owner="agent-x")
            paths.append(rec["path"])
        idx = store.load_index()
        self.assertEqual(len(idx), 3)
        self.assertTrue(idx[0]["path"].endswith("n4.md"))
        names = [e["name"] for e in idx]
        self.assertEqual(names, ["n4.md", "n3.md", "n2.md"])
        self.assertNotIn("n0.md", names)

    def test_ranged_read_and_sha(self):
        rec = self.store.write("bytes", "abcdefghij", owner="agent-x")
        page = self.store.read(rec["path"], offset=2, limit=4)
        self.assertEqual(page["content"], "cdef")
        self.assertEqual(page["offset"], 2)
        self.assertEqual(page["total_bytes"], 10)
        self.assertTrue(page["truncated"])
        self.assertEqual(page["sha"], rec["sha"])
        full = self.store.read(rec["path"], offset=0, limit=20)
        self.assertEqual(full["content"], "abcdefghij")
        self.assertFalse(full["truncated"])
        self.assertTrue(page["journal_tail"])


class TestEditOps(_TmpStore):
    def _doc(self, body):
        rec = self.store.write("doc", body, owner="agent-x")
        return rec["path"]

    def test_replace_text_unique_and_ambiguous(self):
        path = self._doc("one two one")
        with self.assertRaises(ValueError):
            self.store.replace_text(path, "one", "ONE")
        with self.assertRaises(ValueError):
            self.store.replace_text(path, "zzz", "q")
        self.store.replace_text(path, "two", "TWO")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "one TWO one")

        rec = self.store.edit(
            path, "replace_text", "agent-x",
            old="TWO", new="three", note="unique swap")
        self.assertEqual(rec["op"], "edit")
        self.assertEqual(rec["edit_op"], "replace_text")
        tail = self.store.journal_tail(path)
        self.assertEqual(tail[-1]["note"], "unique swap")
        self.assertEqual(tail[-1]["op"], "edit")

    def test_range_insert_delete_byte_math(self):
        path = self._doc("abcdefghij")
        # replace_range at the same offsets read() uses
        page = self.store.read(path, offset=2, limit=3)
        self.assertEqual(page["content"], "cde")
        self.store.replace_range(path, 2, 3, "XY")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"abXYfghij")
        self.store.insert_at(path, 4, "ZZ")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"abXYZZfghij")
        self.store.delete_range(path, 2, 4)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"abfghij")
        rec = self.store.edit(path, "insert_at", "agent-x", offset=2, new="12")
        self.assertEqual(rec["bytes"], 9)

    def test_replace_section_and_missing_heading(self):
        body = (
            "# Title\n"
            "intro\n"
            "## Auth\n"
            "old body\n"
            "still old\n"
            "## Next\n"
            "keep me\n"
        )
        path = self._doc(body)
        self.store.replace_section(path, "## Auth", "new body\n")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("## Auth\nnew body\n", text)
        self.assertIn("## Next\nkeep me\n", text)
        self.assertNotIn("old body", text)
        with self.assertRaises(ValueError):
            self.store.replace_section(path, "Missing", "x")
        rec = self.store.edit(
            path, "replace_section", "agent-x",
            heading="Next", new="swapped\n", note="section")
        self.assertEqual(rec["op"], "edit")
        self.assertEqual(self.store.journal_tail(path)[-1]["note"], "section")


class TestPublishAndAutoOpen(_TmpStore):
    def test_publish_goes_to_calling_session_only(self):
        a = make_session(output=FakeOutput())
        a.agent_id = "agent-a"
        b = make_session(output=FakeOutput())
        b.agent_id = "agent-b"
        rec = self.store.write("r", "hello", owner="agent-a", summary="sum")
        publish_to_session(a, rec)
        self.assertEqual(len(a.output.artifact_cards), 1)
        self.assertEqual(a.output.artifact_cards[0]["path"], rec["path"])
        self.assertEqual(a.output.artifact_cards[0]["summary"], "sum")
        self.assertEqual(b.output.artifact_cards, [])

    def test_auto_open_modes_and_override(self):
        self.assertTrue(should_auto_open("first", False))
        self.assertFalse(should_auto_open("first", True))
        self.assertTrue(should_auto_open("always", True))
        self.assertFalse(should_auto_open("never", False))
        self.assertTrue(should_auto_open("never", False, override=True))
        self.assertFalse(should_auto_open("always", False, override=False))
        self.assertTrue(should_auto_open("never", True, override="always"))
        self.assertFalse(should_auto_open("always", False, override="never"))

        class Win(object):
            def __init__(self):
                self.opened = []

            def open_file(self, path, flags=0):
                self.opened.append(path)
                return _FakeView()

            def get_view_index(self, view):
                return (0, 0)

            def views_in_group(self, g):
                return []

            def set_view_index(self, *a):
                pass

            def num_groups(self):
                return 1

        win = Win()
        s = make_session(
            window=win,
            settings={"artifacts_auto_open": "first"},
        )
        rec = self.store.write("o", "x", owner="agent-x")
        self.assertTrue(maybe_auto_open_session(s, rec["path"]))
        self.assertEqual(win.opened, [rec["path"]])
        self.assertFalse(maybe_auto_open_session(s, rec["path"]))
        self.assertEqual(len(win.opened), 1)
        s2 = make_session(
            window=Win(),
            settings={"artifacts_auto_open": "never"},
        )
        self.assertFalse(maybe_auto_open_session(s2, rec["path"]))
        self.assertEqual(s2.window.opened, [])
        self.assertTrue(maybe_auto_open_session(
            s2, rec["path"], override="always"))
        self.assertEqual(s2.window.opened, [rec["path"]])


class _FakeView(object):
    def is_loading(self):
        return False

    def is_valid(self):
        return True

    def settings(self):
        return _FakeSettings()


class _FakeSettings(object):
    def set(self, *a):
        pass

    def get(self, *a, **k):
        return None


class TestConventionAndUserEdit(_TmpStore):
    def test_convention_under_root_journals_and_cards(self):
        owner = "agent-conv"
        folder = os.path.join(self.root, owner)
        os.makedirs(folder)
        path = os.path.join(folder, "walkthrough.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# walk\n")
        s = make_session(output=FakeOutput())
        s.agent_id = owner
        rec = handle_convention_write(path, session=s, agent_id=owner)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["op"], "create")
        self.assertEqual(len(s.output.artifact_cards), 1)
        tail = self.store.journal_tail(path)
        self.assertEqual(tail[-1]["op"], "create")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# walk 2\n")
        rec2 = handle_convention_write(path, session=s, agent_id=owner)
        self.assertEqual(rec2["op"], "rewrite")
        self.assertEqual(len(s.output.artifact_cards), 2)

    def test_outside_root_untouched(self):
        fd, outside = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        try:
            with open(outside, "w", encoding="utf-8") as f:
                f.write("nope")
            s = make_session(output=FakeOutput())
            s.agent_id = "agent-x"
            self.assertFalse(is_under_artifact_root(outside, self.root))
            rec = handle_convention_write(
                outside, session=s, agent_id="agent-x", root=self.root)
            self.assertIsNone(rec)
            self.assertEqual(s.output.artifact_cards, [])
            self.assertFalse(os.path.isfile(
                outside[:-3] + ".journal.jsonl"
                if outside.endswith(".md") else outside + ".journal.jsonl"))
        finally:
            os.remove(outside)

    def test_user_edit_journals_external(self):
        rec = self.store.write("usered", "v1", owner="agent-x")
        with open(rec["path"], "w", encoding="utf-8") as f:
            f.write("v1 annotated")
        ext = handle_external_save(rec["path"], root=self.root)
        self.assertIsNotNone(ext)
        self.assertEqual(ext["op"], "external")
        tail = self.store.journal_tail(rec["path"])
        self.assertEqual(tail[-1]["op"], "external")
        self.assertEqual(tail[-1]["agent_id"], "user")

    def test_acp_fs_write_hook_notifies_only_under_root(self):
        from bridge.acp.fs import FsMixin

        notes = []
        fake = type(sys)("rpc_helpers")
        fake.send_notification = lambda m, p: notes.append((m, p))
        prev = sys.modules.get("rpc_helpers")
        sys.modules["rpc_helpers"] = fake

        class Dummy(FsMixin):
            def __init__(self):
                self._cancel_in_flight = False
                self.fs_write_max_chars = 0
                self._agent_id = "agent-hook"

            def file_log(self, m):
                pass

        dummy = Dummy()
        try:
            owner_dir = os.path.join(self.root, "agent-hook")
            os.makedirs(owner_dir)
            inside = os.path.join(owner_dir, "via-acp.md")
            asyncio.run(dummy._acp_fs_write({
                "path": inside, "content": "from acp",
            }))
            self.assertTrue(os.path.isfile(inside))
            self.assertEqual(len(notes), 1)
            self.assertEqual(notes[0][0], "artifact_write")
            self.assertEqual(os.path.realpath(notes[0][1]["path"]),
                             os.path.realpath(inside))

            fd, outside = tempfile.mkstemp(suffix=".md")
            os.close(fd)
            os.remove(outside)
            notes[:] = []
            asyncio.run(dummy._acp_fs_write({
                "path": outside, "content": "out",
            }))
            self.assertEqual(notes, [])
            os.remove(outside)
        finally:
            if prev is None:
                sys.modules.pop("rpc_helpers", None)
            else:
                sys.modules["rpc_helpers"] = prev


class TestMcpCallerInjection(_TmpStore):
    def test_socket_write_cards_calling_session(self):
        from tests.stubs import install_sublime
        install_sublime()
        from core.registry import default_registry
        import mcp.socket_server as ss

        default_registry.clear()
        a = make_session(output=FakeOutput())
        a.agent_id = "agent-mcp-a"
        b = make_session(output=FakeOutput())
        b.agent_id = "agent-mcp-b"
        default_registry.register(a)
        default_registry.register(b)
        orig = ss._get_session_by_agent_id

        def _by_aid(aid):
            if str(aid) == a.agent_id:
                return a
            if str(aid) == b.agent_id:
                return b
            return orig(aid)

        ss._get_session_by_agent_id = _by_aid
        try:
            srv = ss.MCPSocketServer()
            out = srv._write_artifact(
                name="report",
                content="# report\n",
                summary="one line",
                auto_open=False,
                _caller_agent_id=a.agent_id,
            )
            self.assertIn("path", out)
            self.assertTrue(out["path"].startswith(self.root))
            self.assertEqual(len(a.output.artifact_cards), 1)
            self.assertEqual(b.output.artifact_cards, [])
            listed = srv._list_artifacts(
                scope="self", _caller_agent_id=a.agent_id)
            self.assertEqual(listed["count"], 1)
            listed_all = srv._list_artifacts(
                scope="all", _caller_agent_id=b.agent_id)
            self.assertGreaterEqual(listed_all["count"], 1)
            page = srv._read_artifact(
                path=out["path"], offset=0, limit=20,
                _caller_agent_id=b.agent_id)
            self.assertIn("# report", page["content"])
            edited = srv._edit_artifact(
                path=out["path"],
                op="replace_text",
                old="# report\n",
                new="# REPORT\n",
                note="caps",
                _caller_agent_id=a.agent_id,
            )
            self.assertEqual(edited["op"], "replace_text")
            self.assertEqual(len(a.output.artifact_cards), 2)
            self.assertEqual(b.output.artifact_cards, [])
        finally:
            ss._get_session_by_agent_id = orig
            default_registry.clear()


class TestRendererCard(_TmpStore):
    def test_card_survives_detach_swap_reattach(self):
        from tests.test_headless_render import RecordingView, RecordingWindow
        from ui.view import SubmarineOutputView

        win = RecordingWindow()
        out = SubmarineOutputView(win)
        out.prompt("hello world")
        rec = self.store.write(
            "walkthrough", "# w\n", owner="agent-x", summary="auth flow")
        out.artifact_card(
            rec["path"], rec["name"], bytes=rec["bytes"],
            summary=rec["summary"])
        cards = [e for e in out.current.events if isinstance(e, ArtifactCard)]
        self.assertEqual(len(cards), 1)
        line = cards[0].line()
        self.assertIn("walkthrough.md", line)
        self.assertIn("[open]", line)
        self.assertIn("[path]", line)
        self.assertIn("auth flow", line)
        self.assertIn("📄", line)

        v1 = RecordingView(view_id=1)
        v1._window = win
        out.view = v1
        out.repaint_from_state()
        self.assertIn("walkthrough.md", v1._content)
        self.assertIn("[open]", v1._content)

        # Detach: state stays, buffer is gone.
        out.view = None
        cards = [e for e in out.current.events if isinstance(e, ArtifactCard)]
        self.assertEqual(len(cards), 1)
        self.assertIsNone(out.current.region)

        # Fast-path journal: card while detached, then catch-up.
        out.renderer.snapshot_detach()
        rec2 = self.store.write(
            "second", "x", owner="agent-x", summary="later")
        out.artifact_card(
            rec2["path"], rec2["name"], bytes=rec2["bytes"],
            summary=rec2["summary"])
        kinds = [j[0] for j in out.renderer._journal]
        self.assertIn("artifact_card", kinds)

        v2 = RecordingView(view_id=2)
        v2._window = win
        out.view = v2
        ok = out.renderer.catch_up_events(list(out.renderer._journal))
        self.assertTrue(ok)
        body = v2._content
        self.assertIn("walkthrough.md", body)
        self.assertIn("second.md", body)
        cards = [e for e in out.current.events if isinstance(e, ArtifactCard)]
        self.assertEqual(len(cards), 2)

        out.view = None
        v3 = RecordingView(view_id=3)
        v3._window = win
        out.view = v3
        out.repaint_from_state()
        self.assertIn("walkthrough.md", v3._content)
        self.assertIn("second.md", v3._content)

    def test_headless_card_does_not_write_buffer(self):
        from tests.test_headless_render import RecordingWindow
        from ui.view import SubmarineOutputView

        win = RecordingWindow()
        out = SubmarineOutputView(win)
        out.prompt("p")
        out.artifact_card("/tmp/x.md", "x.md", bytes=12, summary="s")
        self.assertEqual(win.new_file_calls, 0)
        self.assertIsNone(out.view)
        cards = [e for e in out.current.events if isinstance(e, ArtifactCard)]
        self.assertEqual(len(cards), 1)

    def test_format_bytes(self):
        self.assertEqual(format_bytes(12), "12 B")
        self.assertEqual(format_bytes(4300), "4.2 KB")


class TestListenerExternal(_TmpStore):
    def test_on_post_save_journals_artifact_views(self):
        rec = self.store.write("saved", "body", owner="agent-x")

        class V(object):
            def file_name(self):
                return rec["path"]

        from ui.listeners import SubmarineEventListener
        SubmarineEventListener().on_post_save(V())
        tail = self.store.journal_tail(rec["path"])
        self.assertEqual(tail[-1]["op"], "external")
        self.assertEqual(tail[-1]["agent_id"], "user")


class TestEventRouterConvention(_TmpStore):
    def test_artifact_write_method_routes_to_session(self):
        s = make_session(output=FakeOutput())
        s.agent_id = "agent-evt"
        folder = os.path.join(self.root, s.agent_id)
        os.makedirs(folder)
        path = os.path.join(folder, "via-evt.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("evt")
        s.events.dispatch("artifact_write", {"path": path, "agent_id": s.agent_id})
        self.assertEqual(len(s.output.artifact_cards), 1)
        tail = self.store.journal_tail(path)
        self.assertTrue(tail)


if __name__ == "__main__":
    unittest.main()
