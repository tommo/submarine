"""Resume history for codex rollouts (both on-disk generations)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from features.resume import (  # noqa: E402
    codex_sessions_root,
    display_prompt,
    find_codex_rollout,
    find_session_jsonl,
    load_turns,
    paint_resume_preview,
    parse_codex_rollout,
)

SID = "01a0a8ee-02f9-7841-8f64-4f217b7f4507"

# ── older generation: event_msg/user_message + response_item ────────────────
OLD_ROLLOUT = [
    {"type": "session_meta", "payload": {
        "id": SID, "cwd": "/proj/eb", "cli_version": "0.116.0"}},
    {"type": "turn_context", "payload": {"turn_id": "t1", "cwd": "/proj/eb"}},
    {"type": "event_msg", "payload": {
        "type": "user_message", "message": "make the outline thicker"}},
    {"type": "response_item", "payload": {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "On it."}]}},
    {"type": "response_item", "payload": {
        "type": "function_call", "name": "exec_command", "call_id": "c1"}},
    {"type": "response_item", "payload": {
        "type": "custom_tool_call", "name": "apply_patch", "call_id": "c2"}},
    {"type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "AGENTS.md instructions"}]}},
    {"type": "event_msg", "payload": {
        "type": "user_message", "message": "now the fills"}},
    {"type": "response_item", "payload": {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "Adjusting fills."}]}},
]

# ── newer generation: event_msg/item_completed ──────────────────────────────
NEW_ROLLOUT = [
    {"type": "session_meta", "payload": {
        "id": SID, "cwd": "/proj/eb", "cli_version": "0.153.4"}},
    {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
    {"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "UserMessage",
        "content": [{"type": "text", "text": "Read-only design review"}]}}},
    {"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "AgentMessage", "phase": "commentary",
        "content": [{"type": "Text", "text": "Reading the docs first."}]}}},
    {"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "CommandExecution", "command": ["/bin/zsh", "-lc", "pil man"]}}},
    {"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "McpToolCall", "server": "irr", "tool": "list_projects"}}},
    {"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "Reasoning", "summary_text": []}}},
    {"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "ImageView", "path": "file:///tmp/x.png"}}},
    {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1",
                                     "last_agent_message": "Done, no edits."}},
    {"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "UserMessage",
        "content": [{"type": "text",
                     "text": "# AGENTS.md instructions for /proj/eb"}]}}},
    {"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "UserMessage",
        "content": [{"type": "text", "text": "second ask"}]}}},
    {"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "AgentMessage",
        "content": [{"type": "Text", "text": "Second answer."}]}}},
]


def _write_rollout(root, records, session_id=SID, day=("2026", "09", "16")):
    folder = os.path.join(root, "sessions", day[0], day[1], day[2])
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(
        folder, "rollout-2026-09-16T15-36-05-%s.jsonl" % session_id)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return path


class _CodexHome(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="submarine-codex-")
        self.home = self._td.name
        self._old = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = self.home

    def tearDown(self):
        if self._old is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self._old
        self._td.cleanup()


class TestParseOldGeneration(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="submarine-roll-")
        self.path = os.path.join(self._td.name, "rollout.jsonl")
        with open(self.path, "w", encoding="utf-8") as f:
            for rec in OLD_ROLLOUT:
                f.write(json.dumps(rec) + "\n")

    def tearDown(self):
        self._td.cleanup()

    def test_prompts_replies_and_tools(self):
        turns = parse_codex_rollout(self.path)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["prompt"], "make the outline thicker")
        self.assertEqual(turns[0]["reply"], "On it.")
        self.assertEqual(turns[0]["tools"], ["exec_command", "apply_patch"])
        self.assertEqual(turns[1]["prompt"], "now the fills")
        self.assertEqual(turns[1]["reply"], "Adjusting fills.")
        self.assertEqual(turns[1]["tools"], [])

    def test_user_role_injections_are_not_turns(self):
        turns = parse_codex_rollout(self.path)
        for t in turns:
            self.assertNotIn("AGENTS.md", t["prompt"])

    def test_task_complete_is_the_reply_fallback(self):
        recs = [
            {"type": "event_msg", "payload": {
                "type": "user_message", "message": "did it work"}},
            {"type": "event_msg", "payload": {
                "type": "task_complete", "last_agent_message": "Yes."}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "r.jsonl")
            with open(p, "w", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            turns = parse_codex_rollout(p)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["reply"], "Yes.")

    def test_missing_file_is_empty(self):
        self.assertEqual(parse_codex_rollout("/no/such/rollout.jsonl"), [])


class TestParseNewGeneration(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="submarine-roll-")
        self.path = os.path.join(self._td.name, "rollout.jsonl")
        with open(self.path, "w", encoding="utf-8") as f:
            for rec in NEW_ROLLOUT:
                f.write(json.dumps(rec) + "\n")

    def tearDown(self):
        self._td.cleanup()

    def test_item_completed_drives_turns(self):
        turns = parse_codex_rollout(self.path)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["prompt"], "Read-only design review")
        self.assertEqual(turns[0]["reply"], "Reading the docs first.")
        self.assertEqual(turns[0]["tools"],
                         ["exec_command", "irr.list_projects"])
        self.assertEqual(turns[1]["prompt"], "second ask")
        self.assertEqual(turns[1]["reply"], "Second answer.")

    def test_reasoning_and_image_items_are_ignored(self):
        turns = parse_codex_rollout(self.path)
        joined = " ".join(t["reply"] for t in turns)
        self.assertNotIn("x.png", joined)
        self.assertEqual(turns[0]["tools"].count("exec_command"), 1)

    def test_injected_user_item_skipped_without_losing_the_turn(self):
        turns = parse_codex_rollout(self.path)
        self.assertEqual([t["prompt"] for t in turns],
                         ["Read-only design review", "second ask"])

    def test_task_complete_does_not_overwrite_a_real_reply(self):
        turns = parse_codex_rollout(self.path)
        self.assertNotIn("Done, no edits.", turns[0]["reply"])

    def test_new_generation_wins_over_response_items(self):
        recs = NEW_ROLLOUT + [
            {"type": "response_item", "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "DUPLICATE"}]}},
            {"type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "exec", "call_id": "c9"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "r.jsonl")
            with open(p, "w", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            turns = parse_codex_rollout(p)
        self.assertNotIn("DUPLICATE", turns[0]["reply"])
        self.assertNotIn("exec", turns[0]["tools"])


class TestFindCodexRollout(_CodexHome):
    def test_root_honours_codex_home(self):
        self.assertEqual(codex_sessions_root(),
                         os.path.join(self.home, "sessions"))

    def test_finds_by_session_id(self):
        path = _write_rollout(self.home, NEW_ROLLOUT)
        self.assertEqual(find_codex_rollout(SID), path)

    def test_missing_id_or_root_returns_none(self):
        self.assertIsNone(find_codex_rollout(""))
        self.assertIsNone(find_codex_rollout(SID))
        _write_rollout(self.home, NEW_ROLLOUT)
        self.assertIsNone(find_codex_rollout("019d0ba3-f024-nope"))

    def test_prefers_the_rollout_recorded_in_cwd(self):
        other = "01a0dead-0000-7000-8000-000000000001"
        recs = [dict(NEW_ROLLOUT[0])]
        recs[0] = {"type": "session_meta",
                   "payload": {"id": SID, "cwd": "/proj/other"}}
        _write_rollout(self.home, recs, session_id=SID,
                       day=("2026", "09", "10"))
        recs2 = [{"type": "session_meta",
                  "payload": {"id": SID, "cwd": "/proj/eb"}}]
        want = _write_rollout(self.home, recs2, session_id=SID,
                              day=("2026", "09", "17"))
        # Newest is the /proj/eb one; ask for it explicitly and by cwd.
        self.assertEqual(find_codex_rollout(SID, "/proj/eb"), want)
        self.assertEqual(find_codex_rollout(SID), want)
        # cwd that matches neither still returns the newest.
        self.assertEqual(find_codex_rollout(SID, "/proj/nope"), want)
        del other


class TestCodexWiring(_CodexHome):
    def test_find_session_jsonl_routes_codex(self):
        path = _write_rollout(self.home, NEW_ROLLOUT)
        self.assertEqual(find_session_jsonl(SID, "codex", "/proj/eb"), path)

    def test_load_turns_reads_the_rollout(self):
        _write_rollout(self.home, NEW_ROLLOUT)
        turns = load_turns(SID, "codex", "/proj/eb", "")
        self.assertEqual(len(turns), 2)
        self.assertEqual(display_prompt(turns[0]["prompt"]),
                         "Read-only design review")

    def test_codex_is_not_stolen_by_the_claude_branch(self):
        # A claude jsonl path must not be parsed for a codex session.
        path = _write_rollout(self.home, NEW_ROLLOUT)
        turns = load_turns(SID, "codex", "/proj/eb", "/tmp/some-claude.jsonl")
        self.assertEqual(len(turns), 2)
        self.assertEqual(find_session_jsonl(SID, "codex"), path)

    def test_paint_resume_preview_paints_codex_history(self):
        _write_rollout(self.home, NEW_ROLLOUT)

        class _Out(object):
            def __init__(self):
                self.conversations = []
                self.current = None
                self.calls = []

            def prompt(self, text, context_names=None, context_refs=None):
                self.calls.append(("prompt", text))

            def text(self, content):
                self.calls.append(("text", content))

            def meta(self, duration, cost=None, usage=None):
                self.calls.append(("meta", duration))

        class _S(object):
            resume_id = SID
            session_id = SID
            fork = False
            quick_mode = False
            backend = "codex"
            cwd = "/proj/eb"
            output = None
            on_init = []

            def _cwd(self):
                return "/proj/eb"

            def _find_jsonl_path(self):
                return ""

        s = _S()
        s.output = _Out()
        self.assertTrue(paint_resume_preview(s))
        prompts = [c[1] for c in s.output.calls if c[0] == "prompt"]
        # The tail is under MIN_CHARS, so select_preview walks back one turn
        # (documented behaviour) — both codex turns are painted, last first.
        self.assertEqual(prompts,
                         ["Read-only design review", "second ask"])
        self.assertEqual([c[0] for c in s.output.calls],
                         ["prompt", "text", "meta"] * 2)
        self.assertIn("Second answer.", s.output.calls[4][1])

    def test_paint_returns_false_without_a_rollout(self):
        class _Out(object):
            conversations = []
            current = None

            def prompt(self, *a, **k):
                raise AssertionError("must not paint")

            def text(self, *a, **k):
                raise AssertionError("must not paint")

            def meta(self, *a, **k):
                raise AssertionError("must not paint")

        class _S(object):
            resume_id = SID
            session_id = SID
            fork = False
            quick_mode = False
            backend = "codex"
            cwd = "/proj/eb"
            output = None
            on_init = []

            def _cwd(self):
                return "/proj/eb"

            def _find_jsonl_path(self):
                return ""

        s = _S()
        s.output = _Out()
        self.assertFalse(paint_resume_preview(s))


if __name__ == "__main__":
    unittest.main()
