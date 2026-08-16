"""Submit, permissions, questions, tasks fold, paste, links, retain, edits."""
from __future__ import annotations

import os

import sublime
import sublime_plugin

from features.context import format_line_range
from main import SETTINGS_FILE, get_active_session, get_session_for_view
from ui import keys
from ui.command_parser import CommandParser
from ui.geometry import draft_select_range, history_select_range


QUICK_PROMPTS = {
    "refresh": (
        "Re-read docs/agent/knowledge_index.md and the relevant guide "
        "for the current task. Then continue."
    ),
    "retry": (
        "That didn't work. Read the error carefully and try again "
        "with a different approach."
    ),
    "continue": "Continue.",
}


def _caret_outside_composer(view, output):
    if not output or not output.is_input_mode():
        return False
    if getattr(output, "_question_input_mode", False):
        return False
    input_start = getattr(output, "_input_start", None)
    if input_start is None:
        return False
    for region in view.sel():
        if region.begin() < input_start or region.end() < input_start:
            return True
    return False


def _enter_draft(session):
    if hasattr(session, "_enter_input_with_draft"):
        session._enter_input_with_draft()
    else:
        session.output.enter_input_mode()


def _ctx_add_path(session, path):
    if hasattr(session, "add_context_path"):
        session.add_context_path(path)
    elif getattr(session, "context", None) is not None:
        session.context.add_path(path)


def _ctx_add_image(session, data, mime):
    if hasattr(session, "add_context_image"):
        session.add_context_image(data, mime)
    elif getattr(session, "context", None) is not None:
        session.context.add_image(data, mime)


def _ctx_add_selection(session, label, text):
    if hasattr(session, "add_context_selection"):
        session.add_context_selection(label, text)
    elif getattr(session, "context", None) is not None:
        session.context.add_selection(label, text)


def _pending_context(session):
    ctx = getattr(session, "context", None)
    if ctx is not None:
        return list(ctx.items)
    return list(getattr(session, "pending_context", None) or [])


def _render_current(output):
    fn = getattr(output, "_render_current", None)
    if callable(fn):
        fn()
        return
    renderer = getattr(output, "renderer", None)
    if renderer is not None and hasattr(renderer, "_render_current"):
        renderer._render_current()


class SubmarineToggleSubmitModeCommand(sublime_plugin.ApplicationCommand):
    def run(self):
        s = sublime.load_settings(SETTINGS_FILE)
        cur = bool(s.get("submit_with_modifier", False))
        s.set("submit_with_modifier", not cur)
        sublime.save_settings(SETTINGS_FILE)
        mode = "Cmd/Ctrl+Enter" if not cur else "Enter"
        sublime.status_message("Submarine: submit with %s" % mode)

    def is_checked(self):
        return bool(sublime.load_settings(SETTINGS_FILE).get("submit_with_modifier", False))


class SubmarineSubmitInputCommand(sublime_plugin.TextCommand):
    def run(self, edit, send_now=False):
        s = get_session_for_view(self.view)
        if not s:
            return
        if s.is_sleeping:
            s.wake()
            return
        if s.output.submit_question_input():
            return
        if not s.output.is_input_mode():
            if not s.working and getattr(s, "_composer_allowed", True) and not s.is_sleeping:
                try:
                    _enter_draft(s)
                except Exception:
                    pass
            return
        if _caret_outside_composer(self.view, s.output):
            try:
                s.output.set_caret_owner("draft")
                s.output.park_composer_caret("end")
                s.output.scroll_composer_chrome(force=True)
            except Exception:
                pass
            return
        text = s.output.get_input_text().strip()
        if text.upper() == "RESTART NEW":
            s.output.exit_input_mode(keep_text=False)
            s.draft_prompt = ""
            s.window.run_command("submarine_restart_new")
            return
        if not text:
            if send_now and s.working and s._queued_prompts:
                s.send_now("")
            return
        s.is_looping = False
        s.next_wake_at = None
        cmd = CommandParser.parse(text)
        if cmd:
            s.output.exit_input_mode(keep_text=False)
            s.draft_prompt = ""
            self._handle_command(s, cmd)
            return
        if s.working:
            if send_now:
                s.send_now(text)
            else:
                s.queue_prompt(text)
            try:
                if s.output.is_input_mode() and s.output.view:
                    s.output.set_composer_text("")
                    s.draft_prompt = ""
                    try:
                        if hasattr(s, "_update_queue_phantom"):
                            s._update_queue_phantom()
                        elif s.output:
                            s.output.queue_chips(list(s._queued_prompts or []))
                    except Exception:
                        pass
                    s.output.focus_composer(force_show=True, park_at_end=True)

                    def _refocus_after_queue():
                        if not s.output or not s.output.is_input_mode():
                            return
                        s.output.focus_composer(force_show=True)

                    sublime.set_timeout(_refocus_after_queue, 30)
                    sublime.set_timeout(_refocus_after_queue, 120)
                    return
                s.draft_prompt = ""
            except Exception as e:
                print("[Submarine] queue clear input: %s" % e)
                s.output.exit_input_mode(keep_text=False)
                s.draft_prompt = ""
                s._input_mode_entered = False

                def _rearm_after_queue():
                    if s.output and not s.output.is_input_mode():
                        _enter_draft(s)
                    if s.output and s.output.is_input_mode():
                        s.output.focus_composer(force_show=True)

                sublime.set_timeout(_rearm_after_queue, 30)
            try:
                if hasattr(s, "_update_queue_phantom"):
                    s._update_queue_phantom()
            except Exception:
                pass
            return

        s.output.exit_input_mode(keep_text=False)
        s.draft_prompt = ""
        s._input_mode_entered = False

        if getattr(s, "quick_mode", False):
            try:
                from features import quick as qa
                host = qa.get_host(s.window) if s.window else None
                if host and host.submit_prompt(s, text):
                    def _rearm_q():
                        if not s.output or s.output.is_input_mode():
                            return
                        cur = host.active_session or s
                        if getattr(cur, "_quick_pending_prompt", None):
                            return
                        cur._input_mode_entered = False
                        _enter_draft(cur)
                    sublime.set_timeout(_rearm_q, 30)
                    return
            except Exception as e:
                print("[Submarine] quick submit: %s" % e)

        s.query(text)

        def _rearm():
            if not s.output or s.output.is_input_mode():
                return
            s._input_mode_entered = False
            _enter_draft(s)

        sublime.set_timeout(_rearm, 30)

    def _handle_command(self, session, cmd):
        if cmd.name == "clear":
            session.clear_conversation()
        elif cmd.name in ("restart", "restart-new", "restart_new"):
            args = (getattr(cmd, "args", None) or "").strip().lower()
            if cmd.name == "restart" and args and args not in ("new",):
                session.query(cmd.raw)
                return
            session.clear_conversation()
        elif cmd.name == "compact":
            session._compacting = True
            session.current_tool = "compact…"
            session.query("/compact", display_prompt="/compact")
        elif cmd.name == "context":
            pending = _pending_context(session)
            if not pending:
                session.output.text("\n*No pending context.*\n")
            else:
                lines = ["\n*Pending context:*"]
                for item in pending:
                    lines.append("  📎 %s" % getattr(item, "name", item))
                lines.append("")
                session.output.text("\n".join(lines))
            session.output.enter_input_mode()
        elif cmd.name == "goal":
            if hasattr(session, "handle_goal_command"):
                session.handle_goal_command(cmd.args)
            else:
                from features.goals.loop import handle_goal_command
                handle_goal_command(session, cmd.args)
        else:
            session.query(cmd.raw)


class SubmarineSendNowCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        s = get_session_for_view(self.view)
        if not s or not s.output:
            return
        self.view.run_command("submarine_submit_input", {"send_now": True})


class SubmarineGoalStatusCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if s and hasattr(s, "handle_goal_command"):
            s.handle_goal_command("status")


class SubmarineGoalPauseCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if s and hasattr(s, "handle_goal_command"):
            s.handle_goal_command("pause")


class SubmarineGoalResumeCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if s and hasattr(s, "handle_goal_command"):
            s.handle_goal_command("resume")


class SubmarineGoalClearCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if s and hasattr(s, "handle_goal_command"):
            s.handle_goal_command("clear")


class SubmarineInsertCommand(sublime_plugin.TextCommand):
    def run(self, edit, pos, text):
        self.view.insert(edit, pos, text)


class SubmarineReplaceCommand(sublime_plugin.TextCommand):
    def run(self, edit, start, end, text):
        self.view.replace(edit, sublime.Region(start, end), text)


class SubmarineReplaceContentCommand(sublime_plugin.TextCommand):
    def run(self, edit, content):
        self.view.replace(edit, sublime.Region(0, self.view.size()), content)


class SubmarineClearAllCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        self.view.erase(edit, sublime.Region(0, self.view.size()))


class SubmarineUndoClearCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s and s.output:
            s.output.undo_clear()


class NoopCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        pass


class SubmarineToggleTasksFoldCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        expanded = keys.read_setting(view.settings(), keys.TASKS_EXPANDED, False)
        keys.write_setting(view.settings(), keys.TASKS_EXPANDED, not expanded)
        s = get_session_for_view(view)
        if not s or not s.output:
            return
        if s.output.is_input_mode():
            s.output.refresh_preserving_input()
        else:
            _render_current(s.output)

    def is_enabled(self):
        return keys.is_output_view(self.view)


class SubmarineSelectDraftCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        s = get_session_for_view(self.view)
        if not s or not s.output or not s.output.is_input_mode():
            self.view.run_command("select_all")
            return
        start = s.output._input_start
        end = self.view.size()
        if start is None:
            start = end
        a, b = draft_select_range(start, end)
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(a, b))
        try:
            self.view.show(sublime.Region(a, b))
        except Exception:
            pass


class SubmarineSelectHistoryCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        s = get_session_for_view(self.view)
        if not s or not s.output or not s.output.is_input_mode():
            self.view.run_command("select_all")
            return
        start = s.output._input_start
        if start is None:
            start = self.view.size()
        a, b = history_select_range(start, self.view.size())
        self.view.sel().clear()
        if b > a:
            self.view.sel().add(sublime.Region(a, b))
        else:
            self.view.sel().add(sublime.Region(0, 0))


class SubmarineInsertNewlineCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        s = get_session_for_view(self.view)
        if not s or not s.output.is_input_mode():
            return
        if _caret_outside_composer(self.view, s.output):
            try:
                s.output.set_caret_owner("draft")
                s.output.park_composer_caret("end")
                s.output.scroll_composer_chrome(force=True)
            except Exception:
                pass
            return
        for region in self.view.sel():
            if s.output.is_in_input_region(region.begin()):
                self.view.insert(edit, region.begin(), "\n")


class SubmarinePermissionAllowCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s:
            if not s.output.handle_plan_key("y"):
                s.output.handle_permission_key("y")


class SubmarinePermissionDenyCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s:
            if not s.output.handle_plan_key("n"):
                s.output.handle_permission_key("n")


class SubmarinePermissionAllowSessionCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s:
            s.output.handle_permission_key("s")


class SubmarinePermissionAllowAllCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s:
            s.output.handle_permission_key("a")


class SubmarineViewPlanCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s:
            s.output.handle_plan_key("v")


class SubmarineQuestionKeyCommand(sublime_plugin.TextCommand):
    def run(self, edit, key=""):
        s = get_session_for_view(self.view)
        if s:
            s.output.handle_question_key(key)


class SubmarineUndoMessageCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s:
            s.undo_message()


class SubmarineQuickPromptCommand(sublime_plugin.TextCommand):
    def run(self, edit, key):
        s = get_session_for_view(self.view)
        if not s:
            return
        prompt = QUICK_PROMPTS.get(key)
        if prompt and s.initialized and not s.working:
            s.query(prompt)


class SubmarineManageAutoAllowedToolsCommand(sublime_plugin.WindowCommand):
    def run(self):
        folders = self.window.folders()
        if not folders:
            sublime.error_message("No project folder open")
            return
        project_dir = folders[0]
        settings_dir = os.path.join(project_dir, ".claude")
        settings_path = os.path.join(settings_dir, "settings.json")
        settings = {}
        if os.path.exists(settings_path):
            try:
                import json
                with open(settings_path, "r") as f:
                    settings = json.load(f)
            except Exception as e:
                print("[Submarine] Error loading settings: %s" % e)
        auto_allowed = settings.get("autoAllowedMcpTools", [])
        options = [("add", None, "➕ Add new pattern", "Add a new MCP tool pattern to auto-allow")]
        for i, pattern in enumerate(auto_allowed):
            options.append(("remove", i, "❌ Remove: %s" % pattern, "Click to remove this pattern"))
        if not auto_allowed:
            options.append(("info", None, "ℹ️  No patterns configured", "Add patterns to auto-allow MCP tools"))
        items = [[opt[2], opt[3]] for opt in options]

        def on_select(idx):
            if idx < 0:
                return
            action, data, _, _ = options[idx]
            if action == "add":
                self._show_add_pattern_input(settings_path, settings, auto_allowed)
            elif action == "remove":
                self._remove_pattern(settings_path, settings, auto_allowed, data)

        self.window.show_quick_panel(items, on_select)

    def _show_add_pattern_input(self, settings_path, settings, auto_allowed):
        common_patterns = [
            "mcp__*__*",
            "mcp__plugin_*",
            "Bash(git:*)",
            "Bash(ls:*)",
            "Bash(cat:*)",
            "Bash(python:*)",
            "Bash(npm:*)",
            "Read",
            "Write",
        ]
        items = [["✏️ Enter custom pattern", "Type your own pattern"]]
        for pattern in common_patterns:
            items.append(["Add: %s" % pattern, "Common pattern"])

        def on_select_pattern(idx):
            if idx < 0:
                return
            if idx == 0:
                self.window.show_input_panel(
                    "Enter MCP tool pattern (supports wildcards like mcp__*__):",
                    "",
                    lambda pattern: self._add_pattern(
                        settings_path, settings, auto_allowed, pattern),
                    None, None)
            else:
                self._add_pattern(
                    settings_path, settings, auto_allowed, common_patterns[idx - 1])

        self.window.show_quick_panel(items, on_select_pattern)

    def _add_pattern(self, settings_path, settings, auto_allowed, pattern):
        import json
        if not pattern or not pattern.strip():
            return
        pattern = pattern.strip()
        if pattern in auto_allowed:
            sublime.status_message("Pattern already exists: %s" % pattern)
            return
        auto_allowed.append(pattern)
        settings["autoAllowedMcpTools"] = auto_allowed
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        try:
            with open(settings_path, "w") as f:
                json.dump(settings, f, indent=2)
            sublime.status_message("Added auto-allow pattern: %s" % pattern)
        except Exception as e:
            sublime.error_message("Failed to save settings: %s" % e)

    def _remove_pattern(self, settings_path, settings, auto_allowed, index):
        import json
        if 0 <= index < len(auto_allowed):
            pattern = auto_allowed.pop(index)
            settings["autoAllowedMcpTools"] = auto_allowed
            try:
                with open(settings_path, "w") as f:
                    json.dump(settings, f, indent=2)
                sublime.status_message("Removed auto-allow pattern: %s" % pattern)
            except Exception as e:
                sublime.error_message("Failed to save settings: %s" % e)


class SubmarinePasteImageCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        session = get_session_for_view(self.view)
        if not session:
            sublime.status_message("No active Submarine session")
            return
        image_data, mime_type, file_paths_from_clip = self._get_clipboard_image()

        def _ensure_input():
            try:
                if not session.output:
                    return
                if not session.output.is_input_mode():
                    session.output.enter_input_mode()
                else:
                    session.output.focus_composer(force_show=True, steal_focus=True)
            except Exception:
                pass

        if file_paths_from_clip:
            valid_paths = [p for p in file_paths_from_clip if os.path.exists(p)]
            if valid_paths:
                for p in valid_paths:
                    _ctx_add_path(session, p)
                _ensure_input()
                sublime.status_message("Added %s path(s) to context" % len(valid_paths))
                return
        if image_data:
            _ctx_add_image(session, image_data, mime_type)
            _ensure_input()
            sublime.status_message(
                "Image added to context (%s bytes) — send prompt to attach"
                % len(image_data))
            return
        text = sublime.get_clipboard()
        if text:
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            path_lines = [line for line in lines if os.path.isfile(line) or os.path.isdir(line)]
            if path_lines and len(path_lines) == len(lines):
                for p in path_lines:
                    _ctx_add_path(session, p)
                _ensure_input()
                sublime.status_message("Added %s path(s) to context" % len(path_lines))
                return
            if self._try_paste_as_context(session, text):
                return
            self.view.run_command("insert", {"characters": text})

    def _try_paste_as_context(self, session, text):
        try:
            from ui.listeners import _last_copy_meta
        except Exception:
            return False
        if not _last_copy_meta:
            return False
        if _last_copy_meta.get("text") != text:
            return False
        path = _last_copy_meta.get("file")
        regions = _last_copy_meta.get("regions") or []
        region_str = ",".join(format_line_range(start, end) for start, end in regions)
        label = "%s:%s" % (path, region_str)
        _ctx_add_selection(session, label, text)
        sublime.status_message(
            "Pasted as context: %s:%s" % (os.path.basename(path or ""), region_str))
        return True

    def _get_clipboard_image(self):
        import platform
        import subprocess
        import base64
        try:
            pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            helpers_dir = os.path.join(pkg_root, "helpers")
            system = platform.system()
            if system == "Darwin":
                helper = os.path.join(helpers_dir, "clipboard_image.js")
                cmd = ["osascript", "-l", "JavaScript", helper]
            elif system == "Linux":
                helper = os.path.join(helpers_dir, "clipboard_image_linux.sh")
                cmd = ["bash", helper]
            elif system == "Windows":
                helper = os.path.join(helpers_dir, "clipboard_image_windows.ps1")
                cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", helper]
            else:
                return None, None, None
            if not os.path.isfile(helper):
                print("[Submarine] clipboard helper missing: %s" % helper)
                return None, None, None
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode != 0 and result.stderr:
                print("[Submarine] clipboard helper stderr: %s" % result.stderr[:300])
            output = (result.stdout or "").strip()
            if not output:
                return None, None, None
            if output.startswith("file_paths"):
                paths = [p.strip() for p in output.split("\n")[1:] if p.strip()]
                return None, None, paths
            if output.startswith("image/"):
                lines = output.split("\n")
                mime_type = lines[0].strip()
                b64_data = "".join(lines[1:]).strip()
                if b64_data:
                    return base64.b64decode(b64_data), mime_type, None
            return None, None, None
        except Exception as e:
            print("[Submarine] Clipboard error: %s" % e)
            return None, None, None


class SubmarineOpenLinkCommand(sublime_plugin.TextCommand):
    def run(self, edit, event=None):
        import re
        import webbrowser
        if event:
            pt = self.view.window_to_text((event["x"], event["y"]))
        else:
            sel = self.view.sel()
            if not sel:
                return
            pt = sel[0].begin()
        line_region = self.view.line(pt)
        line = self.view.substr(line_region)
        col = pt - line_region.begin()
        if "super+click to expand" in line or "super+click to collapse" in line:
            self.view.run_command("submarine_toggle_tasks_fold")
            return
        if line.strip().startswith("◎ goal ·") and keys.is_output_view(self.view):
            self.view.run_command("submarine_toggle_tasks_fold")
            return
        if (line.strip().startswith("… +") and "more" in line
                and keys.is_output_view(self.view)):
            self.view.run_command("submarine_toggle_tasks_fold")
            return
        media_path = self._media_path_from_line(line)
        if media_path:
            session = get_session_for_view(self.view)
            if session and session.output and hasattr(session.output, "show_media_popup"):
                session.output.show_media_popup(media_path, location=pt)
                return
        url_pattern = r'https?://[^\s\]\)>\'"]+|file://[^\s\]\)>\'"]+'
        for match in re.finditer(url_pattern, line):
            if match.start() <= col <= match.end():
                webbrowser.open(match.group())
                return
        path_pattern = r'(?:[/.]|[a-zA-Z]:)[^\s:,\]\)\}>\'\"]+(?::\d+)?'
        for match in re.finditer(path_pattern, line):
            if match.start() <= col <= match.end():
                path_with_line = match.group()
                line_num = None
                if ":" in path_with_line:
                    parts = path_with_line.rsplit(":", 1)
                    if parts[1].isdigit():
                        path_with_line = parts[0]
                        line_num = int(parts[1])
                if path_with_line.startswith(("images/", "videos/")) or (
                        path_with_line.lower().endswith(
                            (".png", ".jpg", ".jpeg", ".webp", ".gif",
                             ".mp4", ".webm", ".mov"))):
                    resolved = path_with_line if os.path.isfile(path_with_line) \
                        else self._resolve_media_short_path(path_with_line)
                    if resolved and os.path.isfile(resolved):
                        session = get_session_for_view(self.view)
                        if session and session.output and hasattr(session.output, "show_media_popup"):
                            session.output.show_media_popup(resolved, location=pt)
                            return
                        return
                if os.path.isfile(path_with_line):
                    window = self.view.window()
                    if window:
                        if line_num:
                            window.open_file(
                                "%s:%s" % (path_with_line, line_num),
                                sublime.ENCODED_POSITION)
                        else:
                            window.open_file(path_with_line)
                    return
        sublime.status_message("No link or file path found at cursor")

    def _media_path_from_line(self, line):
        import re
        from ui.formatters import MEDIA_TOOLS, extract_media_path
        for name in MEDIA_TOOLS:
            if name in line and ("→" in line or "preview" in line):
                m = re.search(
                    r'→\s*((?:images|videos)/[^\s]+|\S+\.(?:png|jpe?g|webp|gif|mp4|webm|mov))',
                    line, re.I)
                short = m.group(1) if m else None
                if short:
                    resolved = self._resolve_media_short_path(short)
                    if resolved:
                        return resolved
                session = get_session_for_view(self.view)
                if not session or not session.output:
                    return None
                out = session.output
                convs = list(out.conversations)
                if out.current is not None:
                    convs.append(out.current)
                for conv in reversed(convs):
                    for event in reversed(getattr(conv, "events", []) or []):
                        if getattr(event, "name", "") != name:
                            continue
                        inp = getattr(event, "tool_input", None) or {}
                        path = inp.get("_media_path") or extract_media_path(
                            getattr(event, "result", None), inp)
                        if path and os.path.isfile(path):
                            return path
        return None

    def _resolve_media_short_path(self, short):
        from ui.formatters import MEDIA_TOOLS, media_display_path, extract_media_path
        session = get_session_for_view(self.view)
        if not session or not session.output:
            return None
        out = session.output
        convs = list(out.conversations)
        if out.current is not None:
            convs.append(out.current)
        for conv in reversed(convs):
            for event in reversed(getattr(conv, "events", []) or []):
                name = getattr(event, "name", "")
                if name not in MEDIA_TOOLS:
                    continue
                inp = getattr(event, "tool_input", None) or {}
                path = inp.get("_media_path") or extract_media_path(
                    getattr(event, "result", None), inp)
                if not path:
                    continue
                disp = media_display_path(path)
                base = os.path.basename(path)
                if short in (disp, base, path) or path.endswith("/" + short):
                    return path
        return None

    def want_event(self):
        return True


def _retain_path(session):
    if hasattr(session, "_get_retain_path"):
        try:
            p = session._get_retain_path()
            if p:
                return p
        except Exception:
            pass
    cwd = getattr(session, "cwd", "") or ""
    sid = getattr(session, "session_id", None) or "pending"
    if cwd:
        return os.path.join(cwd, ".claude", "retain-%s.md" % sid[:12])
    return os.path.expanduser("~/.submarine/retain-%s.md" % sid[:12])


def _retain_read(session):
    if hasattr(session, "retain") and callable(session.retain):
        try:
            return session.retain() or ""
        except Exception:
            pass
    path = _retain_path(session)
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""
    return ""


def _retain_clear(session):
    if hasattr(session, "clear_retain"):
        try:
            session.clear_retain()
            return
        except Exception:
            pass
    path = _retain_path(session)
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except Exception:
            pass


class SubmarineRetainCommand(sublime_plugin.WindowCommand):
    def run(self, action="view"):
        session = get_active_session(self.window)
        if not session:
            sublime.status_message("No active session")
            return
        if action == "view":
            content = _retain_read(session)
            if content:
                panel = self.window.create_output_panel("submarine_retain")
                panel.run_command(
                    "append",
                    {"characters": "# Session Retain Content\n\n%s" % content})
                self.window.run_command("show_panel", {"panel": "output.submarine_retain"})
            else:
                sublime.status_message("Retain file is empty")
        elif action == "edit":
            path = _retain_path(session)
            if path:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                if not os.path.exists(path):
                    with open(path, "w") as f:
                        f.write("")
                self.window.open_file(path)
            else:
                sublime.status_message("Session not initialized yet")
        elif action == "clear":
            _retain_clear(session)
            sublime.status_message("Retain content cleared")


class SubmarineProjectRetainCommand(sublime_plugin.WindowCommand):
    def run(self):
        folders = self.window.folders()
        if not folders:
            sublime.status_message("No project folder open")
            return
        retain_path = os.path.join(folders[0], ".claude", "RETAIN.md")
        os.makedirs(os.path.dirname(retain_path), exist_ok=True)
        if not os.path.exists(retain_path):
            with open(retain_path, "w") as f:
                f.write("")
        self.window.open_file(retain_path)
