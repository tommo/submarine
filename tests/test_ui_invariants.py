"""Pinnable UI invariants that do not need a Sublime host."""
from __future__ import annotations

import unittest

from ui.models import (
    HISTORY_CAP,
    Conversation,
    GoalState,
    TodoItem,
    ToolCall,
    _goal_is_open,
    strip_title_decoration,
)
from ui.sheet import format_tab_title
from ui.render_policy import (
    cap_history,
    format_user_prompt_block,
    should_incremental_append,
    tasks_fold_rows,
)
from ui.tools import is_host_control_tool, may_background


class TestHostControl(unittest.TestCase):
    def test_quick_done_never_renders(self):
        for name in (
            "quick_done",
            "mcp__sublime__quick_done",
            "mcp__submarine__quick_done",
            "sublime__quick_done",
            "submarine__quick_done",
            "use_tool_quick_done",
        ):
            self.assertTrue(is_host_control_tool(name), name)
        self.assertFalse(is_host_control_tool("Bash"))
        self.assertFalse(is_host_control_tool("Read"))


class TestBackgroundGate(unittest.TestCase):
    def test_only_shell_workflow_may_be_bg(self):
        for name in ("Bash", "Shell", "execute", "run_terminal_command", "Workflow"):
            self.assertTrue(may_background(name), name)
        for name in ("Read", "TaskGet", "Edit", "Write", "Grep"):
            self.assertFalse(may_background(name), name)


class TestHistoryCap(unittest.TestCase):
    def test_cap_is_20(self):
        self.assertEqual(HISTORY_CAP, 20)
        convs = [Conversation(prompt=str(i)) for i in range(25)]
        kept, dropped = cap_history(convs)
        self.assertEqual(dropped, 5)
        self.assertEqual(len(kept), 20)
        self.assertEqual(kept[0].prompt, "5")
        self.assertEqual(kept[-1].prompt, "24")


class TestIncrementalAppend(unittest.TestCase):
    def test_text_growth_appends(self):
        events = ["hello"]
        delta = should_incremental_append(1, "hello", ["hello world"], False)
        self.assertEqual(delta, " world")

    def test_new_str_event_appends(self):
        events = ["hello", " more"]
        delta = should_incremental_append(1, "hello", events, False)
        self.assertEqual(delta, " more")

    def test_structural_forces_rewrite(self):
        events = ["hello world"]
        self.assertIsNone(
            should_incremental_append(1, "hello", events, True))

    def test_tool_event_forces_rewrite(self):
        events = ["hello", ToolCall(name="Bash", tool_input={})]
        self.assertIsNone(
            should_incremental_append(1, "hello", events, False))

    def test_empty_delta_is_none(self):
        self.assertIsNone(
            should_incremental_append(1, "hello", ["hello"], False))


class TestUserPromptBlock(unittest.TestCase):
    def test_single_line(self):
        self.assertEqual(
            format_user_prompt_block("hello", False, "📎 "),
            "◎ hello ▶\n")

    def test_continuation_indent_and_context(self):
        self.assertEqual(
            format_user_prompt_block("a\nb", True, "📎 "),
            "◎ a\n  b ▶\n  📎 \n")


class TestGoalTasksStripped(unittest.TestCase):
    def test_goal_open_statuses(self):
        self.assertTrue(_goal_is_open(GoalState(status="active")))
        self.assertFalse(_goal_is_open(GoalState(status="complete")))
        self.assertFalse(_goal_is_open(None))

    def test_tasks_fold_cap(self):
        todos = [
            TodoItem(content="a", status="in_progress"),
            TodoItem(content="b", status="pending"),
            TodoItem(content="c", status="pending"),
            TodoItem(content="d", status="pending"),
            TodoItem(content="e", status="pending"),
        ]
        show, hidden = tasks_fold_rows(todos, expanded=False)
        # 1 active + (3-1)=2 pending
        self.assertEqual(len(show), 3)
        self.assertEqual(hidden, 2)
        show_all, hidden_all = tasks_fold_rows(todos, expanded=True)
        self.assertEqual(len(show_all), 5)
        self.assertEqual(hidden_all, 0)


class TestTitleStrip(unittest.TestCase):
    def test_strips_icons_and_abbrev(self):
        self.assertEqual(strip_title_decoration("◇ hello"), "hello")
        self.assertEqual(strip_title_decoration("⏸ GR> hello"), "hello")
        self.assertEqual(strip_title_decoration("* KM> hello"), "hello")
        self.assertEqual(strip_title_decoration("! GR> hello"), "hello")
        self.assertEqual(strip_title_decoration("Claude: old"), "old")
        self.assertEqual(strip_title_decoration("Submarine: new"), "new")

    def test_tab_title_truncates_name_not_prefix(self):
        long_name = "what solution can we use for adding audio midi processing"
        full = format_tab_title(long_name, "◇ ", "GR", max_name=40)
        self.assertTrue(full.startswith("◇ GR> "))
        self.assertIn("what solution can we use for adding", full)
        self.assertTrue(full.endswith("…"))
        self.assertGreater(len(full), 24)
        short = format_tab_title("hello", "⏸ ", "GR")
        self.assertEqual(short, "⏸ GR> hello")


class TestReplayUserContract(unittest.TestCase):
    def test_replay_user_is_not_prompt(self):
        """resume replay must never call prompt(); that's the event router's job.

        Pin the OutputPort surface so a future Session cannot 'helpfully'
        replay by calling prompt() from this layer.
        """
        from ui.view import SubmarineOutputView
        self.assertTrue(hasattr(SubmarineOutputView, "prompt"))
        # The view itself has no replay_user handler — replay is a no-op
        # at the event-router (core) layer.
        self.assertFalse(hasattr(SubmarineOutputView, "replay_user"))
        self.assertFalse(hasattr(SubmarineOutputView, "on_replay_user"))


class TestFormatterAliases(unittest.TestCase):
    def test_submarine_and_sublime_aliases(self):
        from ui.formatters import TOOL_FORMATTERS, SUBLIME_MCP_FORMATTERS
        self.assertIn("find_file", SUBLIME_MCP_FORMATTERS)
        self.assertIn("mcp__submarine__find_file", TOOL_FORMATTERS)
        self.assertIn("mcp__sublime__find_file", TOOL_FORMATTERS)
        self.assertNotIn("terminal_run", SUBLIME_MCP_FORMATTERS)
        self.assertNotIn("order", SUBLIME_MCP_FORMATTERS)
        self.assertNotIn("chatroom", SUBLIME_MCP_FORMATTERS)
        self.assertNotIn("list_personas", SUBLIME_MCP_FORMATTERS)
        self.assertNotIn("set_timer", SUBLIME_MCP_FORMATTERS)
        self.assertNotIn("subscribe", SUBLIME_MCP_FORMATTERS)


class TestMetaSyncFlag(unittest.TestCase):
    def test_meta_clears_pending_debounce(self):
        """meta() must flush synchronously and clear _render_pending."""
        from tests.stubs import FakeWindow
        from ui.view import SubmarineOutputView
        w = FakeWindow()
        out = SubmarineOutputView(w)
        out.renderer.current = Conversation(prompt="hi", working=True)
        out.renderer._render_pending = True
        out.renderer.meta(1.2)
        self.assertFalse(out.renderer._render_pending)
        self.assertTrue(out.renderer.current.has_meta)
        self.assertFalse(out.renderer.current.working)


if __name__ == "__main__":
    unittest.main()
