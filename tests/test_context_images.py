"""Pasting an image into a session: clipboard helper → chip → model.

Two gaps made image paste a no-op: the port dropped `helpers/` (so
`_get_clipboard_image` found no script and fell back to pasting text), and
`Session.query` took an `images` argument nothing ever passed, so even an
attached image never reached the bridge. `context.build_prompt` was dead code.
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from features.context import ContextManager
from tests.fakes import FakeClient, make_session

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPERS = os.path.join(ROOT, "helpers")
PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class ClipboardHelperTest(unittest.TestCase):
    """The scripts the paste command shells out to."""

    def test_every_platform_helper_is_present(self):
        for name in ("clipboard_image.js", "clipboard_image_linux.sh",
                     "clipboard_image_windows.ps1"):
            self.assertTrue(os.path.isfile(os.path.join(HELPERS, name)),
                            "%s is missing: image paste falls back to text" % name)

    def test_the_command_shells_out_to_the_platform_helper(self):
        """Without this the paste silently falls back to text — the exact bug.

        The helper process is stubbed: running the real one would read whatever
        the developer happens to have on the clipboard.
        """
        from tests.stubs import install
        install()
        import commands.text_cmds as tc
        import platform as _p

        cmd = tc.SubmarinePasteImageCommand.__new__(tc.SubmarinePasteImageCommand)
        seen = []

        class _Result(object):
            returncode = 0
            stdout = "no_image\n"
            stderr = ""

        def _run(argv, **kw):
            seen.append(argv)
            return _Result()

        # The command imports subprocess locally, so patch the module itself.
        real_system, real_run = _p.system, subprocess.run
        try:
            _p.system = lambda: "Darwin"
            subprocess.run = _run
            data, mime, paths = cmd._get_clipboard_image()
        finally:
            _p.system, subprocess.run = real_system, real_run
        self.assertIsNone(data)
        self.assertIsNone(mime)
        self.assertIsNone(paths)
        self.assertEqual(len(seen), 1, "the helper must actually run")
        self.assertIn(os.path.join(HELPERS, "clipboard_image.js"), seen[0])
        self.assertEqual(seen[0][:2], ["osascript", "-l"])

    def test_a_missing_helper_yields_no_image_and_says_so(self):
        """The reported symptom: `[Submarine] clipboard helper missing: …`."""
        import contextlib
        import io
        from tests.stubs import install
        install()
        import commands.text_cmds as tc
        import platform as _p

        cmd = tc.SubmarinePasteImageCommand.__new__(tc.SubmarinePasteImageCommand)
        real_system, real_file = _p.system, tc.__file__
        out = io.StringIO()
        try:
            _p.system = lambda: "Darwin"
            tc.__file__ = "/nonexistent/commands/text_cmds.py"
            with contextlib.redirect_stdout(out):
                data, mime, paths = cmd._get_clipboard_image()
        finally:
            _p.system, tc.__file__ = real_system, real_file
        self.assertEqual((data, mime, paths), (None, None, None),
                         "no helper, no image: pasting falls back to text")
        self.assertIn("clipboard helper missing", out.getvalue())

    def test_the_linux_helper_answers_no_image_when_the_clipboard_is_empty(self):
        script = os.path.join(HELPERS, "clipboard_image_linux.sh")
        result = subprocess.run(["bash", script], capture_output=True, text=True,
                                timeout=20)
        self.assertEqual(result.returncode, 0)
        self.assertIn(result.stdout.strip(), ("no_image", "image/png"))


class ContextImageTest(unittest.TestCase):
    def _session_with_image(self):
        s = make_session(client=FakeClient(), initialized=True, backend="claude")
        ctx = ContextManager(s)
        s.context = ctx
        ctx.add_image(base64.b64decode(PNG_B64), "image/png")
        return s, ctx

    def test_an_attached_image_reaches_the_bridge_as_an_image(self):
        s, _ctx = self._session_with_image()
        s.query("what is in this image?", display_prompt="what is in this image?")
        sent = [p for (m, p, _cb) in s.client.sent if m == "query"]
        self.assertEqual(len(sent), 1)
        images = sent[0].get("images") or []
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["mime_type"], "image/png")
        self.assertEqual(images[0]["data"], PNG_B64)
        self.assertTrue(images[0].get("path"), "the temp path rides along")
        # The prompt itself stays the user's text: the image is a block.
        self.assertEqual(sent[0]["prompt"], "what is in this image?")

    def test_a_text_context_item_is_folded_into_the_prompt(self):
        s = make_session(client=FakeClient(), initialized=True, backend="claude")
        ctx = ContextManager(s)
        s.context = ctx
        ctx.add_selection("/p/x.py", "print(1)")
        s.query("explain", display_prompt="explain")
        sent = [p for (m, p, _cb) in s.client.sent if m == "query"][0]
        self.assertIn("print(1)", sent["prompt"])
        self.assertTrue(sent["prompt"].endswith("explain"))
        self.assertEqual(sent.get("images"), None)

    def test_the_transcript_shows_the_typed_prompt_not_the_folded_one(self):
        s = make_session(client=FakeClient(), initialized=True, backend="claude")
        ctx = ContextManager(s)
        s.context = ctx
        ctx.add_selection("/p/x.py", "print(1)")
        s.query("explain", display_prompt="explain")
        self.assertIn("explain", s.output.prompts[-1])
        self.assertNotIn("print(1)", s.output.prompts[-1])

    def test_the_context_is_consumed_once(self):
        s, ctx = self._session_with_image()
        s.query("first", display_prompt="first")
        self.assertEqual(list(ctx.items), [], "chips clear when they are sent")
        s.turn.end_live()          # first turn over: the next query sends
        s.query("second", display_prompt="second")
        sent = [p for (m, p, _cb) in s.client.sent if m == "query"]
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(sent[0].get("images") or []), 1)
        self.assertIsNone(sent[1].get("images"), "not attached twice")

    def test_a_queued_prompt_keeps_its_context_for_the_send(self):
        """Queued prompts keep the user's text (a readable chip) and the
        context stays pending, so it rides the prompt that actually goes out."""
        client = FakeClient()
        s = make_session(client=client, initialized=True, backend="claude")
        ctx = ContextManager(s)
        s.context = ctx
        ctx.add_image(base64.b64decode(PNG_B64), "image/png")
        s.turn.begin_query()          # mid-turn: the next prompt queues
        s.query("later", display_prompt="later")
        self.assertEqual(s._queued_prompts, ["later"], "the chip stays readable")
        self.assertEqual(len(list(ctx.items)), 1, "the image is still attached")
        self.assertEqual([p for (m, p, _cb) in client.sent if m == "query"], [])

    def test_a_session_without_a_context_manager_is_unchanged(self):
        s = make_session(client=FakeClient(), initialized=True, backend="claude")
        self.assertIsNone(getattr(s, "context", None))
        s.query("plain", display_prompt="plain")
        sent = [p for (m, p, _cb) in s.client.sent if m == "query"][0]
        self.assertEqual(sent["prompt"], "plain")
        self.assertIsNone(sent.get("images"))


if __name__ == "__main__":
    unittest.main()
