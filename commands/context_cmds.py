"""Add file/selection/folder context; query selection/file; clear queue."""
from __future__ import annotations

import os

import sublime
import sublime_plugin

from features.context import format_line_range
from main import create_session, get_active_session
from ui import keys


def _selection_query(prompt, fname, selection):
    return "%s\n\nSelection from %s:\n```\n%s\n```" % (prompt, fname, selection)


def _file_query(prompt, fname, content):
    return "%s\n\nFile: %s\n```\n%s\n```" % (prompt, fname, content)


def _add_file(session, path, content):
    if hasattr(session, "add_context_file"):
        session.add_context_file(path, content)
    elif getattr(session, "context", None) is not None:
        session.context.add_file(path, content)


def _add_selection(session, label, content):
    if hasattr(session, "add_context_selection"):
        session.add_context_selection(label, content)
    elif getattr(session, "context", None) is not None:
        session.context.add_selection(label, content)


def _add_folder(session, folder):
    if hasattr(session, "add_context_folder"):
        session.add_context_folder(folder)
    elif getattr(session, "context", None) is not None:
        session.context.add_folder(folder)


def _clear_context(session):
    if hasattr(session, "clear_context"):
        session.clear_context()
    elif getattr(session, "context", None) is not None:
        session.context.clear()


def _schedule_query(session, prompt):
    session.output.show()
    if session.initialized:
        session.query(prompt)
    else:
        sublime.set_timeout(lambda: session.query(prompt), 500)


class SubmarineQuerySelectionCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        sel = self.view.sel()
        if not sel or sel[0].empty():
            return
        text = self.view.substr(sel[0])
        fname = self.view.file_name() or "untitled"
        self.view.window().show_input_panel(
            "Ask about selection:", "",
            lambda p: self._done(p, text, fname), None, None)

    def _done(self, prompt, selection, fname):
        if not prompt.strip():
            return
        window = self.view.window()
        s = get_active_session(window)
        if not s:
            s = create_session(window)
        _schedule_query(s, _selection_query(prompt, fname, selection))


class SubmarineQueryFileCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.active_view()
        if not view or not view.file_name():
            sublime.status_message("No file to send")
            return
        s = get_active_session(self.window)
        if not s:
            s = create_session(self.window)
        content = view.substr(sublime.Region(0, view.size()))
        fname = view.file_name()
        self.window.show_input_panel(
            "Ask about file:", "",
            lambda p: self._done(p, content, fname), None, None)

    def _done(self, prompt, content, fname):
        if not prompt.strip():
            return
        s = get_active_session(self.window)
        if not s:
            return
        _schedule_query(s, _file_query(prompt, fname, content))


class SubmarineAddFileCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.active_view()
        if not view or not view.file_name():
            sublime.status_message("No file to add")
            return
        s = get_active_session(self.window)
        if not s:
            sublime.status_message("No active session. Use 'Submarine: New Session' first.")
            return
        content = view.substr(sublime.Region(0, view.size()))
        _add_file(s, view.file_name(), content)
        sublime.status_message("Added: %s" % os.path.basename(view.file_name()))


class SubmarineAddSelectionCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.active_view()
        if not view:
            sublime.status_message("No active view")
            return
        sel = view.sel()
        if not sel or sel[0].empty():
            sublime.status_message("No selection")
            return
        s = get_active_session(self.window)
        if not s:
            sublime.status_message("No active session. Use 'Submarine: New Session' first.")
            return
        content = view.substr(sel[0])
        path = view.file_name() or "untitled"
        r0 = view.rowcol(sel[0].begin())[0] + 1
        r1 = view.rowcol(sel[0].end())[0] + 1
        label = "%s:%s" % (path, format_line_range(r0, r1))
        _add_selection(s, label, content)
        base = os.path.basename(path)
        sublime.status_message(
            "Added selection from: %s:%s" % (base, format_line_range(r0, r1)))


class SubmarineAddOpenFilesCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if not s:
            sublime.status_message("No active session. Use 'Submarine: New Session' first.")
            return
        count = 0
        for view in self.window.views():
            if view.file_name() and not keys.is_output_view(view):
                content = view.substr(sublime.Region(0, view.size()))
                _add_file(s, view.file_name(), content)
                count += 1
        sublime.status_message("Added %s files" % count)


class SubmarineAddFolderCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.active_view()
        if not view or not view.file_name():
            sublime.status_message("No file open")
            return
        s = get_active_session(self.window)
        if not s:
            sublime.status_message("No active session. Use 'Submarine: New Session' first.")
            return
        folder = os.path.dirname(view.file_name())
        _add_folder(s, folder)
        sublime.status_message("Added folder: %s/" % os.path.basename(folder))


class SubmarineClearContextCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if s:
            _clear_context(s)
            sublime.status_message("Context cleared")
