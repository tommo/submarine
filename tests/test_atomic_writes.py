"""Persisted JSON and Grok history rewrites must be atomic and report failure.

``plat.jsonio.safe_json_dump`` used to truncate the destination before
serializing and swallow every error: a crash, a full disk or an unserializable
payload left an empty file that ``safe_json_load`` then read as "no history".
The Grok rewind rewrote its three transcript files in place and answered
``ok: True`` even when nothing was cut, so the host restarted as rewound.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from plat.jsonio import safe_json_dump, safe_json_load  # noqa: E402
from core.records import (  # noqa: E402
    SessionStore,
    load_bookmarks,
    save_bookmarks,
    toggle_bookmark,
)
from core.rewind import RewindService  # noqa: E402
import acp.rewind as acp_rewind  # noqa: E402
from acp.rewind import RewindMixin  # noqa: E402


def _read(path):
    with open(path, "rb") as f:
        return f.read()


class _ReadOnlyDir(unittest.TestCase):
    """A directory the test can read but not create files in."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="submarine-atomic-")
        self.dir = self._td.name
        self._locked = []

    def tearDown(self):
        for d in self._locked:
            os.chmod(d, stat.S_IRWXU)
        self._td.cleanup()

    def lock(self, d):
        os.chmod(d, stat.S_IRUSR | stat.S_IXUSR)
        self._locked.append(d)
        if os.access(d, os.W_OK):
            self.skipTest("cannot make a read-only directory here (root?)")


class TestSafeJsonDump(_ReadOnlyDir):
    def test_unwritable_directory_keeps_the_old_file(self):
        path = os.path.join(self.dir, "s.json")
        self.assertTrue(safe_json_dump([{"session_id": "a"}], path))
        before = _read(path)
        self.lock(self.dir)
        self.assertFalse(safe_json_dump([{"session_id": "b"}], path))
        self.assertEqual(_read(path), before)
        self.assertEqual(safe_json_load(path, default=[]), [{"session_id": "a"}])

    def test_unserializable_payload_keeps_the_old_file(self):
        path = os.path.join(self.dir, "s.json")
        self.assertTrue(safe_json_dump({"k": 1}, path))
        before = _read(path)
        self.assertFalse(safe_json_dump({"k": object()}, path))
        self.assertEqual(_read(path), before)
        # No staged temp file is left behind either.
        self.assertEqual(os.listdir(self.dir), ["s.json"])

    def test_success_replaces_the_file(self):
        path = os.path.join(self.dir, "s.json")
        self.assertTrue(safe_json_dump({"k": 1}, path))
        self.assertTrue(safe_json_dump({"k": 2}, path))
        self.assertEqual(json.load(open(path, encoding="utf-8")), {"k": 2})
        self.assertEqual(os.listdir(self.dir), ["s.json"])


class TestStoreReportsFailure(_ReadOnlyDir):
    def test_remove_reports_a_failed_save(self):
        path = os.path.join(self.dir, ".sessions.json")
        store = SessionStore(path)
        store.upsert({"session_id": "a", "project": self.dir})
        store.upsert({"session_id": "b", "project": self.dir})
        before = _read(path)
        self.lock(self.dir)
        self.assertFalse(store.remove("a"))
        self.assertEqual(_read(path), before)
        self.assertEqual(len(store.load()), 2)

    def test_rename_reports_a_failed_save(self):
        path = os.path.join(self.dir, ".sessions.json")
        store = SessionStore(path)
        store.upsert({"session_id": "a", "project": self.dir})
        self.lock(self.dir)
        self.assertFalse(store.rename("a", "new name"))

    def test_bookmarks_report_a_failed_save(self):
        proj = os.path.join(self.dir, "proj")
        os.makedirs(os.path.join(proj, ".claude"))
        self.assertTrue(save_bookmarks({"s1"}, proj))
        self.assertEqual(load_bookmarks(proj), {"s1"})
        bm = os.path.join(proj, ".claude", "bookmarks.json")
        before = _read(bm)
        self.lock(os.path.join(proj, ".claude"))
        self.assertFalse(save_bookmarks({"s1", "s2"}, proj))
        self.assertEqual(_read(bm), before)
        # toggle reports the state that is actually on disk.
        self.assertTrue(toggle_bookmark("s1", proj), "still starred: save failed")
        self.assertFalse(toggle_bookmark("s2", proj), "still unstarred")
        self.assertEqual(load_bookmarks(proj), {"s1"})


# ── Grok rewind ───────────────────────────────────────────────────────────────

class _Rewind(RewindMixin):
    def __init__(self, sdir, acp=None):
        self.session_id = "sess-1"
        self.cwd = "/proj"
        self.sdir = sdir
        self.acp = acp
        self.logs = []

    def _grok_session_dir(self):
        return self.sdir

    def file_log(self, msg):
        self.logs.append(msg)

    async def _send_acp(self, method, params):
        if isinstance(self.acp, Exception):
            raise self.acp
        if self.acp == "hang":
            await asyncio.sleep(3600)
        return self.acp


def _write_history(sdir):
    rp = [{"prompt_index": 0, "prompt_preview": "first"},
          {"prompt_index": 1, "prompt_preview": "second"},
          {"prompt_index": 2, "prompt_preview": "third"}]
    chat = [{"type": "user", "prompt_index": 0, "content": "<user_query>first</user_query>"},
            {"type": "assistant", "prompt_index": 0, "content": "a0"},
            {"type": "user", "prompt_index": 1, "content": "<user_query>second</user_query>"},
            {"type": "assistant", "prompt_index": 1, "content": "a1"},
            {"type": "user", "prompt_index": 2, "content": "<user_query>third</user_query>"}]
    ups = [{"params": {"update": {"_meta": {"promptIndex": 0}, "x": 1}}},
           {"params": {"update": {"_meta": {"promptIndex": 1}, "x": 2}}},
           {"params": {"update": {"tool": "later"}}}]
    names = ("rewind_points.jsonl", "chat_history.jsonl", "updates.jsonl")
    for name, rows in zip(names, (rp, chat, ups)):
        with open(os.path.join(sdir, name), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    return {n: _read(os.path.join(sdir, n)) for n in names}


class TestGrokRewind(_ReadOnlyDir):
    def setUp(self):
        super().setUp()
        self._orig_send = acp_rewind.send_result
        self._orig_wait = asyncio.wait_for
        self.results = []
        acp_rewind.send_result = lambda rid, res: self.results.append((rid, res))

    def tearDown(self):
        acp_rewind.send_result = self._orig_send
        acp_rewind.asyncio.wait_for = self._orig_wait
        super().tearDown()

    def _run(self, b, target=1):
        asyncio.run(b.handle_rewind_execute(7, {"prompt_index": target}))
        return self.results[-1][1]

    def _fast_timeout(self):
        orig = self._orig_wait

        async def _wait(coro, timeout=None):
            return await orig(coro, timeout=0.01)

        acp_rewind.asyncio.wait_for = _wait

    def test_cut_stages_and_replaces_every_file(self):
        sdir = os.path.join(self.dir, "sess-1")
        os.makedirs(sdir)
        _write_history(sdir)
        out = self._run(_Rewind(sdir, acp={"success": False}), target=1)
        self.assertTrue(out["ok"])
        self.assertEqual(out["draft_prompt"], "second")
        rp = open(os.path.join(sdir, "rewind_points.jsonl")).read().splitlines()
        chat = open(os.path.join(sdir, "chat_history.jsonl")).read().splitlines()
        ups = open(os.path.join(sdir, "updates.jsonl")).read().splitlines()
        self.assertEqual(len(rp), 1)
        self.assertEqual(len(chat), 2)
        self.assertEqual(len(ups), 1)
        self.assertEqual(sorted(os.listdir(sdir)),
                         ["chat_history.jsonl", "rewind_points.jsonl", "updates.jsonl"])

    def test_failed_staging_leaves_every_file_untouched_and_reports(self):
        sdir = os.path.join(self.dir, "sess-1")
        os.makedirs(sdir)
        before = _write_history(sdir)
        self.lock(sdir)   # staging in the session dir is impossible
        out = self._run(_Rewind(sdir, acp={"success": False, "error": "nope"}),
                        target=1)
        self.assertFalse(out["ok"])
        self.assertIn("disk:", out["error"])
        self.assertIn("acp: nope", out["error"])
        for name, data in before.items():
            self.assertEqual(_read(os.path.join(sdir, name)), data, name)

    def test_one_bad_file_aborts_before_any_replace(self):
        sdir = os.path.join(self.dir, "sess-1")
        os.makedirs(sdir)
        before = _write_history(sdir)
        b = _Rewind(sdir, acp={})
        orig = b._replace_history_files

        # Make the last staged file fail: none of the earlier ones may be
        # swapped in.
        def _replace(pending):
            pending = list(pending)
            pending[-1] = (os.path.join(sdir, "missing-dir", "updates.jsonl"), [])
            return orig(pending)

        b._replace_history_files = _replace
        out = self._run(b, target=1)
        self.assertFalse(out["ok"])
        for name, data in before.items():
            self.assertEqual(_read(os.path.join(sdir, name)), data, name)
        self.assertEqual(sorted(os.listdir(sdir)),
                         ["chat_history.jsonl", "rewind_points.jsonl", "updates.jsonl"],
                         "staged temp files must be cleaned up")

    def test_no_session_dir_and_acp_timeout_is_a_failure(self):
        self._fast_timeout()
        out = self._run(_Rewind(None, acp="hang"), target=1)
        self.assertFalse(out["ok"])
        self.assertIn("session dir not found", out["error"])
        self.assertIn("acp timeout", out["error"])

    def test_acp_success_alone_is_ok(self):
        out = self._run(_Rewind(None, acp={"success": True, "prompt_text": "p"}),
                        target=1)
        self.assertTrue(out["ok"])
        self.assertEqual(out["draft_prompt"], "p")


class TestHostHonoursRewindFailure(unittest.TestCase):
    def _service(self, response):
        applied = []
        failed = []

        def send(method, params, cb):
            cb(response)
            return True

        svc = RewindService(send, lambda *_a: None,
                            lambda idx, d: applied.append((idx, d)),
                            on_fail=lambda m: failed.append(m))
        return svc, applied, failed

    def test_ok_false_does_not_restart_as_rewound(self):
        svc, applied, failed = self._service(
            {"ok": False, "error": "disk: session dir not found; acp: acp timeout",
             "draft_prompt": ""})
        svc.grok_undo_async(prompt_index=1)
        self.assertEqual(applied, [])
        self.assertEqual(failed, ["disk: session dir not found; acp: acp timeout"])
        self.assertFalse(svc._grok_busy)

    def test_bare_ok_false_is_still_a_failure(self):
        svc, applied, failed = self._service({"ok": False, "draft_prompt": "x"})
        svc.grok_undo_async(prompt_index=2)
        self.assertEqual(applied, [])
        self.assertEqual(failed, ["rewind failed"])

    def test_ok_true_applies(self):
        svc, applied, failed = self._service(
            {"ok": True, "draft_prompt": "second"})
        svc.grok_undo_async(prompt_index=1)
        self.assertEqual(applied, [(1, "second")])
        self.assertEqual(failed, [])


if __name__ == "__main__":
    unittest.main()
