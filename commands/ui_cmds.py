"""Clear/copy/usage/search/history/reset-input/MCP/permission-mode/devtools."""
from __future__ import annotations

import json
import os
import threading
import time

import sublime
import sublime_plugin

from core.records import load_saved_sessions
from main import SETTINGS_FILE, create_session, get_active_session
def _pending_items(session):
    ctx = getattr(session, "context", None)
    if ctx is not None:
        return list(ctx.items)
    return list(getattr(session, "pending_context", None) or [])


def _refresh_after_clear(session):
    if hasattr(session, "_update_status_bar"):
        try:
            session._update_status_bar()
        except Exception:
            pass
    items = _pending_items(session)
    if items:
        try:
            session.output.set_pending_context(items)
        except Exception:
            pass


class SubmarineClearCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if s:
            s.output.clear()
            _refresh_after_clear(s)


class SubmarineClearKeepLastCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if s:
            s.output.clear_keep_last()
            _refresh_after_clear(s)


class SubmarineCopyCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if s and s.output.view and s.output.view.is_valid():
            content = s.output.view.substr(sublime.Region(0, s.output.view.size()))
            sublime.set_clipboard(content)
            sublime.status_message("Conversation copied to clipboard")


class SubmarineUsageCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        current_usage = []
        if s:
            current_usage = [
                "## Current Session: %s" % s.name,
                "",
                "Queries: %s" % s.query_count,
                "Total Cost: $%.4f" % s.total_cost,
                "",
            ]
        sessions = load_saved_sessions()
        total_cost = sum(sess.get("total_cost", 0) for sess in sessions)
        total_queries = sum(sess.get("query_count", 0) for sess in sessions)
        lines = [
            "# API Usage Statistics",
            "",
            "Total (All Sessions): $%.4f (%s queries)" % (total_cost, total_queries),
            "",
        ]
        if current_usage:
            lines.extend(current_usage)
        if sessions:
            lines.extend(["## Recent Sessions", ""])
            for sess in sessions[:10]:
                name = sess.get("name", "Untitled")
                cost = sess.get("total_cost", 0)
                queries = sess.get("query_count", 0)
                lines.append("- %s: $%.4f (%s queries)" % (name, cost, queries))
        content = "\n".join(lines)
        panel = self.window.create_output_panel("submarine_usage")
        panel.set_read_only(False)
        panel.run_command("append", {"characters": content})
        panel.set_read_only(True)
        panel.settings().set("word_wrap", False)
        panel.settings().set("gutter", False)
        self.window.run_command("show_panel", {"panel": "output.submarine_usage"})


class SubmarineSearchSessionsCommand(sublime_plugin.WindowCommand):
    def run(self):
        self.window.show_input_panel("Search sessions:", "", self._on_done, None, None)

    def _on_done(self, query):
        if not query.strip():
            return
        q = query.lower()

        def search():
            saved = {
                s["session_id"]: s.get("name", "")
                for s in load_saved_sessions() if s.get("session_id")
            }
            projects_dir = os.path.expanduser("~/.claude/projects")
            results = []
            if not os.path.isdir(projects_dir):
                sublime.set_timeout(
                    lambda: sublime.status_message("No sessions matching '%s'" % query), 0)
                return
            for proj_key in os.listdir(projects_dir):
                proj_path = os.path.join(projects_dir, proj_key)
                if not os.path.isdir(proj_path):
                    continue
                for fname in os.listdir(proj_path):
                    if not fname.endswith(".jsonl"):
                        continue
                    fpath = os.path.join(proj_path, fname)
                    sid = fname[:-6]
                    saved_name = saved.get(sid, "")
                    jsonl_title = None
                    try:
                        with open(fpath, "r") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                entry = json.loads(line)
                                if entry.get("type") == "custom-title":
                                    jsonl_title = entry.get("title", "")
                                    break
                                if entry.get("type") == "user" and not entry.get("isSidechain"):
                                    msg = entry.get("message", {})
                                    content = msg.get("content", [])
                                    if isinstance(content, list):
                                        has_tool_result = any(
                                            isinstance(b, dict) and b.get("type") == "tool_result"
                                            for b in content)
                                        if has_tool_result:
                                            continue
                                        for b in content:
                                            if isinstance(b, dict) and b.get("type") == "text":
                                                t = b.get("text", "")
                                                if t and not t.startswith("[Request interrupted"):
                                                    jsonl_title = t[:80]
                                                    break
                                    elif isinstance(content, str) and not content.startswith("[Request interrupted"):
                                        jsonl_title = content[:80]
                                    if jsonl_title:
                                        break
                    except Exception:
                        continue
                    searchable = ("%s %s" % (saved_name, jsonl_title or "")).lower()
                    if q not in searchable:
                        continue
                    title = saved_name or jsonl_title or "untitled"
                    mtime = os.path.getmtime(fpath)
                    results.append((sid, title, mtime, proj_key))
            results.sort(key=lambda x: x[2], reverse=True)
            results = results[:50]
            if not results:
                sublime.set_timeout(
                    lambda: sublime.status_message("No sessions matching '%s'" % query), 0)
                return
            items = []
            for sid, title, mtime, proj_key in results:
                ts = time.strftime("%m/%d %H:%M", time.localtime(mtime))
                proj_short = proj_key.rsplit("-", 1)[-1] if "-" in proj_key else proj_key
                items.append([title, "%s | %s | %s..." % (proj_short, ts, sid[:8])])

            def show_panel():
                def on_select(idx):
                    if idx < 0:
                        return
                    sid = results[idx][0]
                    title = results[idx][1]
                    saved_backend = "claude"
                    for rec in load_saved_sessions():
                        if rec.get("session_id") == sid:
                            saved_backend = rec.get("backend", "claude")
                            break
                    s = create_session(
                        self.window, resume_id=sid, fork=True, backend=saved_backend)
                    from core.session import fork_session_title
                    s.name = fork_session_title(title)
                    s.output.set_name(s.name)

                self.window.show_quick_panel(items, on_select)

            sublime.set_timeout(show_panel, 0)

        threading.Thread(target=search, daemon=True).start()


class SubmarineViewHistoryCommand(sublime_plugin.WindowCommand):
    def run(self):
        sessions = load_saved_sessions()
        if not sessions:
            sublime.status_message("No saved sessions")
            return
        items = []
        for s in sessions:
            name = (s.get("name") or "Unnamed")[:40]
            sid = (s.get("session_id") or "")[:8]
            cost = s.get("total_cost") or 0
            queries = s.get("query_count") or 0
            project = os.path.basename(s.get("project") or "")
            items.append([
                name,
                "%s | %s queries | $%.2f | %s..." % (project, queries, cost, sid),
            ])

        def on_select(idx):
            if idx < 0:
                return
            self._show_history(sessions[idx])

        self.window.show_quick_panel(items, on_select)

    def _show_history(self, session):
        sid = session.get("session_id", "")
        project = session.get("project", "")
        backend = session.get("backend", "claude") or "claude"
        history_file = None
        try:
            from features.resume import find_session_jsonl
            history_file = find_session_jsonl(sid, backend, project)
        except Exception:
            history_file = None
        if not history_file:
            project_key = project.replace("/", "-").lstrip("-")
            history_file = os.path.expanduser(
                "~/.claude/projects/%s/%s.jsonl" % (project_key, sid))
        if not history_file or not os.path.exists(history_file):
            sublime.status_message("History file not found: %s" % history_file)
            return
        messages = []
        with open(history_file, "r") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if d.get("type") == "user":
                        msg = d.get("message", {})
                        content = msg.get("content", [])
                        if isinstance(content, str):
                            messages.append(content)
                        elif isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    text = c.get("text", "")
                                    if text and not text.startswith("[Request interrupted"):
                                        messages.append(text)
                    elif d.get("type") == "user_message" and d.get("text"):
                        messages.append(d.get("text"))
                except Exception:
                    pass
        view = self.window.new_file()
        view.set_name("History: %s" % (session.get("name") or sid[:8]))
        view.set_scratch(True)
        view.assign_syntax("Packages/Markdown/Markdown.sublime-syntax")
        output = "# Session: %s\n" % session.get("name", "Unnamed")
        output += "**ID:** %s\n" % sid
        output += "**Project:** %s\n" % project
        output += "**Queries:** %s | **Cost:** $%.2f\n\n" % (
            session.get("query_count", 0), session.get("total_cost", 0))
        output += "---\n\n"
        for i, msg in enumerate(messages, 1):
            output += "## [%s]\n%s\n\n" % (i, msg)
        view.run_command("append", {"characters": output})


class SubmarineResetInputCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if s:
            s.output.reset_input_mode(reenter=True)
            sublime.status_message("Input mode reset")


class SubmarineAddMcpCommand(sublime_plugin.WindowCommand):
    def run(self):
        folders = self.window.folders()
        if not folders:
            sublime.status_message("No project folder open")
            return
        project_root = folders[0]
        claude_dir = os.path.join(project_root, ".claude")
        settings_path = os.path.join(claude_dir, "settings.json")
        tools_dir = os.path.join(claude_dir, "sublime_tools")
        os.makedirs(claude_dir, exist_ok=True)
        os.makedirs(tools_dir, exist_ok=True)
        plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        mcp_server = os.path.join(plugin_dir, "mcp", "server.py")
        settings = {}
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r") as f:
                    settings = json.load(f)
            except Exception:
                pass
        if "mcpServers" not in settings:
            settings["mcpServers"] = {}
        settings["mcpServers"]["sublime"] = {
            "command": "python3",
            "args": [mcp_server],
        }
        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)
        example_tool = os.path.join(tools_dir, "example.py")
        if not os.path.exists(example_tool):
            with open(example_tool, "w") as f:
                f.write(
                    "# Example sublime tool\n"
                    "# Run with: sublime_eval(tool=\"example\")\n\n"
                    "window = sublime.active_window()\n"
                    "view = window.active_view()\n\n"
                    "return {\n"
                    "    \"file\": view.file_name() if view else None,\n"
                    "    \"selection\": view.substr(view.sel()[0]) if view and view.sel() else None,\n"
                    "    \"cursor\": view.rowcol(view.sel()[0].begin()) if view and view.sel() else None,\n"
                    "}\n"
                )
        sublime.status_message("MCP config added to %s" % claude_dir)
        self.window.open_file(settings_path)


class SubmarineTogglePermissionModeCommand(sublime_plugin.WindowCommand):
    MODES = ["default", "acceptEdits", "plan", "bypassPermissions"]
    MODE_LABELS = {
        "default": "Default (prompt for all)",
        "acceptEdits": "Accept Edits (auto-approve file ops)",
        "plan": "Plan (plan mode, approve before implement)",
        "bypassPermissions": "Bypass (allow ALL - use with caution)",
    }

    def run(self):
        settings = sublime.load_settings(SETTINGS_FILE)
        current = settings.get("permission_mode", "default")
        items = []
        current_idx = 0
        for i, mode in enumerate(self.MODES):
            label = self.MODE_LABELS[mode]
            if mode == current:
                label = "● %s" % label
                current_idx = i
            else:
                label = "  %s" % label
            items.append(label)

        def on_select(idx):
            if idx < 0:
                return
            new_mode = self.MODES[idx]
            settings.set("permission_mode", new_mode)
            sublime.save_settings(SETTINGS_FILE)
            sublime.status_message("Submarine: permission mode = %s" % new_mode)
            s = get_active_session(self.window)
            if s:
                s.permission_mode = new_mode
                if s.client:
                    s.client.send("set_permission_mode", {"mode": new_mode})
                if hasattr(s, "_update_permission_banner"):
                    s._update_permission_banner(show=True)

        self.window.show_quick_panel(items, on_select, selected_index=current_idx)


def _devtools():
    from features.devtools import server as devtools
    return devtools


def _dump_json(window, data, name, status):
    text = json.dumps(data, indent=2, default=str)
    sublime.set_clipboard(text)
    v = window.new_file()
    v.set_name(name)
    v.set_scratch(True)
    v.set_syntax_file("Packages/JavaScript/JSON.sublime-syntax")
    v.run_command("append", {"characters": text})
    sublime.status_message(status)


class SubmarineDevtoolsSnapshotCommand(sublime_plugin.WindowCommand):
    def run(self, agent_id=None):
        view_id = None
        if agent_id is None:
            s = get_active_session(self.window)
            if s:
                agent_id = getattr(s, "agent_id", None)
                if s.output and s.output.view:
                    view_id = s.output.view.id()
        else:
            from core.registry import default_registry
            s = default_registry.by_agent_id(agent_id)
            if s is not None:
                view_id = default_registry.bound_view_id(s)
        data = _devtools().snapshot(view_id)
        _dump_json(
            self.window, data, "Submarine Devtools Snapshot",
            "Submarine devtools: snapshot copied + opened")


class SubmarineDevtoolsSessionsCommand(sublime_plugin.WindowCommand):
    def run(self):
        data = _devtools().sessions_dump()
        _dump_json(
            self.window, data, "Submarine Devtools Sessions",
            "Submarine devtools: %s session(s)" % data.get("count", 0))


class SubmarineDevtoolsComposerCommand(sublime_plugin.WindowCommand):
    def run(self, agent_id=None):
        view_id = None
        if agent_id is None:
            s = get_active_session(self.window)
            if s:
                agent_id = getattr(s, "agent_id", None)
                if s.output and s.output.view:
                    view_id = s.output.view.id()
        else:
            from core.registry import default_registry
            s = default_registry.by_agent_id(agent_id)
            if s is not None:
                view_id = default_registry.bound_view_id(s)
        data = _devtools().composer_dump(view_id)
        _dump_json(
            self.window, data, "Submarine Devtools Composer",
            "Submarine devtools: composer dump")


class SubmarineDevtoolsLogCommand(sublime_plugin.WindowCommand):
    def run(self, tail=120):
        data = _devtools().log_tail(tail=tail)
        text = json.dumps(data, indent=2, default=str)
        v = self.window.new_file()
        v.set_name("Submarine Devtools Log")
        v.set_scratch(True)
        v.set_syntax_file("Packages/JavaScript/JSON.sublime-syntax")
        v.run_command("append", {"characters": text})
        sublime.status_message("Submarine devtools log → %s" % data.get("log_path"))


class SubmarineDevtoolsReloadCommand(sublime_plugin.WindowCommand):
    def run(self, mode="soft"):
        r = _devtools().reload_plugin(mode=mode or "soft")
        sublime.status_message("Submarine: reload scheduled (%s)" % r.get("mode", mode))
        print("[Submarine] reload scheduled: %s" % r)
