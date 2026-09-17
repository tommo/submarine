"""Sublime event listeners: close intercept, copy tracking, composer gate, restore."""
from __future__ import annotations

import os
import re
import time

try:
    import sublime
    import sublime_plugin
except ImportError:  # unit tests
    sublime = None  # type: ignore
    sublime_plugin = None  # type: ignore

    class _EL(object):
        pass

    class _VEL(object):
        @classmethod
        def is_applicable(cls, settings):
            return False

    class _NS(object):
        EventListener = _EL
        ViewEventListener = _VEL

    sublime_plugin = _NS()  # type: ignore

from . import keys
from .composer import Composer
from .context_menu import ContextMenuHandler, ContextParser
from .geometry import classify_regions, wholly_in_draft
from .models import strip_title_decoration
from .session_api import (
    close_or_detach_session,
    create_session,
    detach_session,
    get_session_by_agent_id,
    get_session_for_view,
    in_startup_quiet,
    keep_running_on_close,
    load_saved_sessions,
    new_agent_id,
    register_session,
    remember_active_session,
    sessions_for_window,
    sessions_map,
    unregister_view,
)


def _normalize_session_name(name):
    if not name:
        return ""
    return " ".join(str(name).replace("\r", "\n").split())


def _session_names_match(saved_name, tab_name):
    a = _normalize_session_name(saved_name)
    b = _normalize_session_name(tab_name)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(b) >= 8 and a.startswith(b):
        return True
    if len(a) >= 8 and b.startswith(a):
        return True
    a0 = str(saved_name or "").split("\n", 1)[0].strip()
    b0 = str(tab_name or "").split("\n", 1)[0].strip()
    a0n, b0n = _normalize_session_name(a0), _normalize_session_name(b0)
    if a0n and b0n and len(min(a0n, b0n, key=len)) >= 8:
        if a0n == b0n or a0n.startswith(b0n) or b0n.startswith(a0n):
            return True
    return False


def _find_saved_session_for_view(view, saved_sessions):
    """Prefer submarine_session_id; fall back to legacy claude_session_id."""
    if not view:
        return None
    settings = view.settings()
    view_sid = (keys.read_setting(settings, keys.SESSION_ID) or "").strip()
    view_backend = (keys.read_setting(settings, keys.BACKEND) or "").strip() or None
    if view_sid:
        for saved in saved_sessions:
            if saved.get("session_id") == view_sid:
                return saved
        return {
            "session_id": view_sid,
            "backend": view_backend or "claude",
            "name": None,
        }
    name = view.name() or ""
    if name.endswith("…") or name.endswith("..."):
        name = name[:-1] if name.endswith("…") else name[:-3]
    try:
        name = strip_title_decoration(name)
    except Exception:
        pass
    name = name.strip()
    if name and name not in ("Submarine", "Claude"):
        for saved in saved_sessions:
            if not saved.get("session_id"):
                continue
            if view_backend and saved.get("backend") and saved.get("backend") != view_backend:
                continue
            if _session_names_match(saved.get("name") or "", name):
                return saved
        if view_backend:
            for saved in saved_sessions:
                if not saved.get("session_id"):
                    continue
                if _session_names_match(saved.get("name") or "", name):
                    return saved
    try:
        content = view.substr(sublime.Region(0, min(800, view.size())))
        m = re.search(r"◎ (.+?) ▶", content)
        if m:
            first_prompt = m.group(1).strip()
            fp_norm = _normalize_session_name(first_prompt)
            for saved in saved_sessions:
                if not saved.get("session_id") or not saved.get("query_count", 0):
                    continue
                if view_backend and saved.get("backend") and saved.get("backend") != view_backend:
                    continue
                sp = saved.get("first_prompt") or ""
                sn = saved.get("name") or ""
                if sp and _normalize_session_name(sp) == fp_norm:
                    return saved
                if sn and _session_names_match(sn, first_prompt):
                    return saved
    except Exception:
        pass
    return None


def settle_active_output_view(window) -> None:
    if not window:
        return
    view = window.active_view()
    if not view or not keys.is_output_view(view):
        return
    s = get_session_for_view(view)
    if not s:
        SubmarineOutputEventListener(view)._restore_session(window, paint=True)
        s = get_session_for_view(view)
    if not s:
        return
    if hasattr(s, "_update_status_bar"):
        s._update_status_bar()
    s.output.set_name(getattr(s, "display_name", None) or s.name or "Submarine")
    if getattr(s, "is_sleeping", False):
        if hasattr(s, "_apply_sleep_ui"):
            s._apply_sleep_ui()
    elif getattr(s, "initialized", False) and not getattr(s, "working", False) and not s.output.is_input_mode():
        if hasattr(s, "_enter_input_with_draft"):
            s._enter_input_with_draft()


def settle_startup_output_views() -> None:
    if sublime is None:
        return
    for w in sublime.windows():
        active = w.active_view()
        active_id = active.id() if active else None
        for view in w.views():
            if not keys.is_output_view(view):
                continue
            if keys.read_setting(w.settings(), keys.CREATING_SESSION):
                continue
            if keys.read_setting(view.settings(), keys.QUICK):
                continue
            keys.write_setting(view.settings(), keys.INPUT_MODE, False)
            Composer.strip_composer_tail(view)
            is_focused = view.id() == active_id
            if get_session_for_view(view):
                s = get_session_for_view(view)
                if s and getattr(s, "is_sleeping", False) and hasattr(s, "_apply_sleep_ui"):
                    s._apply_sleep_ui()
                continue
            SubmarineOutputEventListener(view)._restore_session(w, paint=is_focused)
        settle_active_output_view(w)


def _forget_host_view(view) -> None:
    try:
        from ui.host import HostView
        win = None
        try:
            win = view.window()
        except Exception:
            win = None
        if win is not None:
            HostView.for_window(win).forget_view(view)
        else:
            HostView.forget_view_id(view.id())
    except Exception:
        pass


# Back-compat names used by old call sites
settle_active_claude_view = settle_active_output_view
settle_startup_claude_views = settle_startup_output_views

_last_copy_meta = None


class SubmarineEventListener(sublime_plugin.EventListener):
    def on_post_text_command(self, view, command_name, args):
        global _last_copy_meta
        if command_name not in ("copy", "cut"):
            return
        path = view.file_name()
        if not path or view.is_scratch() or keys.is_output_view(view):
            return
        sel = view.sel()
        if not sel:
            return
        regions = []
        for r in sel:
            row_start = view.rowcol(r.begin())[0] + 1
            row_end = view.rowcol(r.end())[0] + 1
            regions.append((row_start, row_end))
        clip = ""
        try:
            clip = sublime.get_clipboard()
        except Exception:
            pass
        _last_copy_meta = {"file": path, "regions": regions, "text": clip}

    def on_window_command(self, window, command, args):
        if command == "close_window":
            for session in list(sessions_for_window(window)):
                try:
                    session.stop()
                except Exception:
                    pass
                view = None
                try:
                    view = session.output.view if session.output else None
                except Exception:
                    view = None
                if view:
                    try:
                        unregister_view(view.id())
                    except Exception:
                        pass
                aid = getattr(session, "agent_id", None)
                if aid:
                    try:
                        from core.registry import default_registry
                        default_registry.by_agent.pop(aid, None)
                        default_registry.unbind(aid)
                    except Exception:
                        pass

        if command in ("close", "close_file", "close_by_index"):
            view = window.active_view()
            if command == "close_by_index" and args:
                group = args.get("group", 0)
                index = args.get("index", 0)
                views = window.views_in_group(group)
                if index < len(views):
                    view = views[index]
            if view and keys.is_output_view(view):
                if keys.read_setting(view.settings(), keys.QUICK_SOFT_CLOSE):
                    return None
                session = get_session_for_view(view)
                if session and (getattr(session, "initialized", False)
                                or getattr(session, "is_sleeping", False)):
                    def _ask():
                        s = get_session_for_view(view)
                        if not s or not (getattr(s, "initialized", False)
                                         or getattr(s, "is_sleeping", False)):
                            view.close()
                            return
                        if keep_running_on_close(s):
                            close_or_detach_session(s, view)
                            try:
                                view.close()
                            except Exception:
                                pass
                            if sublime is not None:
                                sublime.status_message(
                                    "Submarine: session still running (open from Sessions)")
                            return
                        if sublime is not None and sublime.ok_cancel_dialog(
                                "Close this Submarine session?", "Close"):
                            close_or_detach_session(s, view)
                            try:
                                view.close()
                            except Exception:
                                pass
                    if sublime is not None:
                        sublime.set_timeout(_ask, 0)
                    return ("noop",)

    def on_activated(self, view):
        window = view.window()
        if not window:
            return
        if keys.is_output_view(view):
            return
        session_view_id = keys.read_setting(
            window.settings(), keys.PENDING_CONTEXT_SESSION)
        if not session_view_id:
            return
        pending_time = keys.read_setting(
            window.settings(), keys.PENDING_CONTEXT_TIME, 0) or 0
        if time.time() - pending_time < 0.3:
            return
        keys.erase_setting(window.settings(), keys.PENDING_CONTEXT_SESSION)
        keys.erase_setting(window.settings(), keys.PENDING_CONTEXT_TIME)
        session = sessions_map().get(session_view_id)
        if not session:
            return
        path = view.file_name()
        if path:
            content = view.substr(sublime.Region(0, view.size()))
            if hasattr(session, "add_context_file"):
                session.add_context_file(path, content)
            if sublime is not None:
                sublime.status_message("Added context: %s" % path.split("/")[-1])

            def refocus():
                session.output.show(focus=True)
                if not session.output.is_input_mode():
                    session.output.enter_input_mode()
                if getattr(session, "draft_prompt", None) and session.output.is_input_mode():
                    session.output.set_composer_text(session.draft_prompt)
                if session.output.is_input_mode():
                    session.output.focus_composer(force_show=True, steal_focus=True)

            if sublime is not None:
                sublime.set_timeout(refocus, 100)

    def on_post_save(self, view):
        fname = view.file_name() or ""
        if os.path.basename(fname) in (
                "SubmarineOutput.sublime-settings", "ClaudeOutput.sublime-settings"):
            for session in sessions_map().values():
                if session.output:
                    session.output._apply_output_settings()
        if fname:
            try:
                from core.artifacts import handle_external_save
                handle_external_save(fname)
            except Exception:
                pass

    def on_pre_close(self, view):
        task_id = view.settings().get("submarine_workflow_view") or view.settings().get(
            "claude_workflow_view")
        if not task_id:
            return
        parent_id = view.settings().get("submarine_workflow_parent") or view.settings().get(
            "claude_workflow_parent")
        win = view.window()
        sess = sessions_map().get(parent_id)
        if sess:
            if hasattr(sess, "_workflow_views"):
                sess._workflow_views.pop(task_id, None)
            if hasattr(sess, "_workflow_view_ps"):
                sess._workflow_view_ps.pop(task_id, None)
        if parent_id is not None and win:
            def refocus():
                pv = next((v for v in win.views() if v.id() == parent_id), None)
                if pv:
                    win.focus_view(pv)
            if sublime is not None:
                sublime.set_timeout(refocus, 0)

    def on_close(self, view):
        if sublime is not None:
            sublime.load_settings(keys.OUTPUT_SETTINGS).clear_on_change(
                "submarine_output_%s" % view.id()
            )
            try:
                sublime.load_settings("ClaudeOutput.sublime-settings").clear_on_change(
                    "claude_output_%s" % view.id()
                )
            except Exception:
                pass
        is_host = False
        try:
            is_host = bool(keys.read_setting(view.settings(), keys.HOST))
        except Exception:
            is_host = False
        if keys.read_setting(view.settings(), keys.QUICK_SOFT_CLOSE):
            unregister_view(view.id())
            if is_host:
                _forget_host_view(view)
            return
        if keys.read_setting(view.settings(), keys.SOFT_CLOSE):
            unregister_view(view.id())
            if is_host:
                _forget_host_view(view)
            return
        session = get_session_for_view(view)
        if session:
            if keep_running_on_close(session):
                detach_session(session)
                if is_host:
                    _forget_host_view(view)
                return
            try:
                session.stop()
            except Exception:
                pass
            unregister_view(view.id())
        if is_host:
            _forget_host_view(view)


class SubmarineOutputEventListener(sublime_plugin.ViewEventListener):
    """Mutation gate + restore-on-activate for output sheets."""

    @classmethod
    def is_applicable(cls, settings):
        return bool(
            settings.get(keys.OUTPUT, False) or settings.get("claude_output", False)
        )

    def on_activated(self):
        window = self.view.window()
        if not window:
            return
        if in_startup_quiet():
            return
        active = window.active_view()
        is_real_active = bool(active and active.id() == self.view.id())
        if not is_real_active:
            return
        old_agent = keys.read_setting(window.settings(), keys.ACTIVE_AGENT)
        old_view_id = None
        if not old_agent:
            old_view_id = keys.read_setting(window.settings(), keys.ACTIVE_VIEW)
        remember_active_session(window, self.view)
        s = get_session_for_view(self.view)
        if not s:
            if keys.read_setting(window.settings(), keys.CREATING_SESSION):
                return
            self._restore_session(window, paint=True)
            s = get_session_for_view(self.view)
            if not s:
                return
        if hasattr(s, "_set_unread"):
            s._set_unread(False)
        if hasattr(s, "_update_status_bar"):
            s._update_status_bar()
        s.output.set_name(getattr(s, "display_name", None) or s.name or "Submarine")
        if getattr(s, "is_sleeping", False):
            if hasattr(s, "_apply_sleep_ui"):
                s._apply_sleep_ui()
        elif getattr(s, "initialized", False) and not s.output.is_input_mode():
            if hasattr(s, "_enter_input_with_draft"):
                s._enter_input_with_draft()
        elif s.output.is_input_mode():
            input_start = s.output._input_start
            sel = self.view.sel()
            if len(sel) == 0 and sublime is not None:
                self.view.sel().clear()
                self.view.sel().add(sublime.Region(input_start, input_start))
        old_session = None
        if old_agent:
            old_session = get_session_by_agent_id(old_agent)
        elif old_view_id and old_view_id != self.view.id():
            old_session = sessions_map().get(old_view_id)
        if old_session is not None and old_session is not s:
            try:
                old_session.output.set_name(
                    getattr(old_session, "display_name", None) or old_session.name)
            except Exception:
                pass

    def _restore_session(self, window, paint=True):
        if self.view and keys.read_setting(self.view.settings(), keys.QUICK):
            return
        view = self.view
        if get_session_for_view(view):
            return
        if keys.read_setting(view.settings(), keys.RECONNECTING):
            return
        keys.write_setting(view.settings(), keys.RECONNECTING, True)
        try:
            saved_sessions = load_saved_sessions()
            matched = _find_saved_session_for_view(view, saved_sessions)
            resume_id = (matched or {}).get("session_id") if matched else None
            session_name = (matched or {}).get("name") if matched else None
            resume_session_at = (matched or {}).get("resume_session_at") if matched else None
            saved_backend = (
                keys.read_setting(view.settings(), keys.BACKEND)
                or (matched or {}).get("backend")
                or "claude"
            )
            if not session_name:
                raw = view.name() or ""
                if raw.endswith("…"):
                    raw = raw[:-1]
                session_name = strip_title_decoration(raw) or None
            session = create_session(window, resume_id=resume_id, backend=saved_backend)
            if session is None:
                # core/ not wired yet — sleep-chrome the leftover buffer only
                keys.write_setting(view.settings(), keys.SLEEPING, True)
                keys.write_setting(view.settings(), keys.INPUT_MODE, False)
                Composer.strip_composer_tail(view)
                return
            session.name = session_name
            saved_model = (
                (matched or {}).get("model")
                or keys.read_setting(view.settings(), keys.MODEL)
            )
            if saved_model:
                session.model = saved_model
                keys.write_setting(view.settings(), keys.MODEL, saved_model)
            if matched:
                try:
                    session.last_activity = float(
                        matched.get("last_activity") or session.last_activity)
                    session.last_access = float(
                        matched.get("last_access")
                        or matched.get("last_activity")
                        or session.last_access)
                except (TypeError, ValueError):
                    pass
                try:
                    saved_q = int(matched.get("query_count") or 0)
                except (TypeError, ValueError):
                    saved_q = 0
                if saved_q > int(getattr(session, "query_count", 0) or 0):
                    session.query_count = saved_q
                fp = matched.get("first_prompt")
                if fp:
                    session.first_prompt = fp
            session.output.view = view
            session.draft_prompt = ""
            session._composer_allowed = False
            keys.write_setting(view.settings(), keys.SLEEPING, True)
            keys.write_setting(view.settings(), keys.INPUT_MODE, False)
            keys.write_setting(view.settings(), keys.BACKEND, saved_backend)
            if resume_session_at:
                session._pending_resume_at = resume_session_at
            aid = (
                keys.read_setting(view.settings(), keys.AGENT_ID)
                or (matched or {}).get("agent_id")
                or None
            )
            if not aid:
                aid = new_agent_id()
            session.agent_id = aid
            session.subsession_id = (
                keys.read_setting(view.settings(), keys.SUBSESSION_ID)
                or (matched or {}).get("subsession_id")
                or None
            )
            session.parent_agent_id = (
                keys.read_setting(view.settings(), keys.PARENT_AGENT_ID)
                or (matched or {}).get("parent_agent_id")
                or None
            )
            if resume_id:
                session.session_id = resume_id
            try:
                if hasattr(session, "_persist_view_identity"):
                    session._persist_view_identity()
            except Exception:
                pass
            register_session(session)
            try:
                from ui.host import claim_host_for_restore
                claim_host_for_restore(window, session, view)
            except Exception:
                pass
            if keys.read_setting(view.settings(), keys.QUICK):
                view.settings().set("color_scheme", keys.THEME_QUICK)
            else:
                from .session_api import backend_theme
                if saved_backend and saved_backend != "claude":
                    view.settings().set("color_scheme", backend_theme(saved_backend))
            session.output.reset_active_states(soft=True)
            Composer.strip_composer_tail(view)
            session.output.clear_phantoms()
            if hasattr(session, "reset_phantoms_for_new_view"):
                session.reset_phantoms_for_new_view()
            if resume_id and hasattr(session, "_apply_sleep_ui"):
                session._apply_sleep_ui()
            else:
                session.output.set_name(
                    getattr(session, "display_name", None) or session.name)
                if paint:
                    session.output.show_sleep_banner(
                        "⏸ No session to resume — Restart Session or close", on=True)
        finally:
            keys.erase_setting(view.settings(), keys.RECONNECTING)

    _INPUT_EDIT_COMMANDS = frozenset({
        "insert", "insert_snippet", "insert_best_completion",
        "left_delete", "right_delete", "delete_word", "delete_to_mark",
        "delete_to_bol", "delete_to_eol", "delete_line", "delete_by_type",
        "cut", "paste", "paste_and_indent", "paste_from_history",
        "swap_line_up", "swap_line_down", "join_lines", "duplicate_line",
        "permute_lines", "permute_selection", "sort_lines", "wrap_lines",
        "indent", "unindent", "reindent", "complete_under",
        "commit_completion", "auto_complete", "replace_completion_with_next_completion",
        "yank", "run_macro_file", "run_macro",
        "upper_case", "lower_case", "title_case", "swap_case",
        "transpose",
    })
    _INPUT_UNDO_COMMANDS = frozenset({"undo", "soft_undo", "redo", "soft_redo"})

    def _sel_regions(self):
        return [(r.begin(), r.end()) for r in self.view.sel()]

    def _geometry(self, session):
        if not session.output.is_input_mode():
            return "none"
        start = session.output._input_start
        if start is None:
            return "none"
        return classify_regions(self._sel_regions(), start, self.view.size())

    def _wholly_in_draft(self, session):
        if not session.output.is_input_mode():
            return False
        start = session.output._input_start
        if start is None:
            return False
        return wholly_in_draft(self._sel_regions(), start, self.view.size())

    def on_text_command(self, command_name, args):
        s = get_session_for_view(self.view)
        if not s:
            return None
        if not s.output.is_input_mode():
            if command_name in ("paste", "paste_and_indent"):
                return ("submarine_paste_image", {})
            return None
        geom = self._geometry(s)
        in_draft = geom == "draft"
        if command_name == "drag_select":
            args = args or {}
            if sublime is not None:
                sublime.set_timeout(
                    lambda a=dict(args), sess=s: self._handle_composer_click(sess, a),
                    0,
                )
            return None
        if command_name == "select_all":
            if in_draft:
                return ("submarine_select_draft", {})
            return ("submarine_select_history", {})
        if command_name in self._INPUT_UNDO_COMMANDS:
            if not in_draft:
                return ("noop", {})
            if command_name == "redo":
                return ("soft_redo", {})
            if command_name == "undo":
                return ("soft_undo", {})
            return None
        if command_name.startswith("submarine_") or command_name.startswith("claude_"):
            return None
        if command_name in ("paste", "paste_and_indent"):
            if in_draft:
                return ("submarine_paste_image", {})
            return ("noop", {})
        if command_name == "insert" and not in_draft:
            chars = (args or {}).get("characters") or ""
            if chars and not getattr(s.output, "_question_input_mode", False):
                self._guide_insert_to_composer(s, chars)
            return ("noop", {})
        if command_name not in self._INPUT_EDIT_COMMANDS:
            return None
        if not in_draft:
            return ("noop", {})
        if command_name in ("left_delete", "delete_word", "delete_to_bol"):
            start = s.output._input_start
            if start is not None:
                for r in self.view.sel():
                    if r.empty() and r.begin() <= start:
                        return ("noop", {})
                    if not r.empty() and r.begin() < start:
                        return ("noop", {})
        return None

    def _guide_insert_to_composer(self, session, characters):
        view = self.view
        chars = characters or ""
        if not chars or not session or not session.output:
            return

        def _go(v=view, s=session, c=chars):
            try:
                if not v or not v.is_valid():
                    return
                if not s.output or not s.output.is_input_mode():
                    return
                if getattr(s.output, "_question_input_mode", False):
                    return
                v.set_read_only(False)
                s.output.set_caret_owner("draft")
                s.output.park_composer_caret("end")
                s.output.scroll_composer_chrome(force=True)
                v.run_command("insert", {"characters": c})
            except Exception as e:
                print("[Submarine] guide insert: %s" % e)

        if sublime is not None:
            sublime.set_timeout(_go, 0)

    def _handle_composer_click(self, session, args):
        try:
            if not session or not session.output or not session.output.is_input_mode():
                return
            view = self.view
            if not view or not view.is_valid():
                return
            input_start = session.output._input_start
            if input_start is None:
                return
            event = (args or {}).get("event") or {}
            if "y" not in event:
                return
            try:
                layout = view.window_to_layout((event.get("x", 0), event["y"]))
                click_y = float(layout[1])
            except Exception:
                return
            try:
                _lx, input_y = view.text_to_layout(input_start)
            except Exception:
                return
            try:
                _ex, eof_y = view.text_to_layout(view.size())
                line_h = max(view.line_height(), 1.0)
                if click_y > float(eof_y) + line_h * 0.5:
                    session.output.set_caret_owner("draft")
                    session.output.park_composer_caret("end")
                    session.output.scroll_composer_chrome(force=True)
                    return
            except Exception:
                pass
            try:
                if abs(click_y - float(input_y)) < view.line_height():
                    row, col = view.rowcol(input_start)
                    click_pt = view.layout_to_text(layout)
                    crow, ccol = view.rowcol(click_pt)
                    if crow == row and ccol <= 2:
                        session.output.set_caret_owner("draft")
                        view.sel().clear()
                        view.sel().add(sublime.Region(input_start, input_start))
                        session.output.note_draft_caret()
                        return
            except Exception:
                pass
            try:
                click_pt = view.layout_to_text(layout)
                if click_pt < input_start:
                    session.output.set_caret_owner("history")
                else:
                    session.output.set_caret_owner("draft")
                    session.output.note_draft_caret()
            except Exception:
                pass
        except Exception:
            pass

    def on_selection_modified(self):
        s = get_session_for_view(self.view)
        if not s or not s.output.is_input_mode():
            return
        if getattr(s.output, "_sel_guard", False):
            return
        sel = self.view.sel()
        if len(sel) == 0:
            return
        try:
            geom = self._geometry(s)
            if geom == "draft":
                s.output.set_caret_owner("draft")
                s.output.note_draft_caret()
            elif geom in ("history", "crossing"):
                s.output.set_caret_owner("history")
        except Exception:
            pass
        if self.view.is_read_only():
            self.view.set_read_only(False)

    def on_query_context(self, key, operator, operand, match_all):
        def _bool_match(val):
            if sublime is None:
                return val == bool(operand)
            if operator == sublime.OP_EQUAL:
                return val == bool(operand)
            if operator == sublime.OP_NOT_EQUAL:
                return val != bool(operand)
            return None

        geom_keys = (
            keys.CARET_IN_DRAFT, "claude_caret_in_draft",
            keys.SELECTION_IN_HISTORY, "claude_selection_in_history",
            keys.SELECTION_CROSSES_DRAFT, "claude_selection_crosses_draft",
            keys.OUTSIDE_INPUT_AREA, "claude_outside_input_area",
        )
        if key in geom_keys:
            s = get_session_for_view(self.view)
            if not s or not s.output.is_input_mode():
                return _bool_match(False)
            geom = self._geometry(s)
            if key in (keys.CARET_IN_DRAFT, "claude_caret_in_draft"):
                return _bool_match(geom == "draft")
            if key in (keys.SELECTION_IN_HISTORY, "claude_selection_in_history"):
                return _bool_match(geom == "history")
            if key in (keys.SELECTION_CROSSES_DRAFT, "claude_selection_crosses_draft"):
                return _bool_match(geom == "crossing")
            return _bool_match(geom != "draft")
        if key in (keys.SUBMIT_WITH_MODIFIER, "claude_submit_with_modifier"):
            val = False
            if sublime is not None:
                val = bool(sublime.load_settings(keys.PLUGIN_SETTINGS).get(
                    "submit_with_modifier", False))
            return _bool_match(val)
        return None

    _in_soft_undo = False

    def on_modified(self):
        if self._in_soft_undo:
            return
        s = get_session_for_view(self.view)
        if not s or not s.output.is_input_mode():
            return
        command, args, _ = self.view.command_history(0)
        if command == "insert" and args and "characters" in args and len(self.view.sel()) == 1:
            chars = args["characters"]
            input_start = s.output._input_start
            current_cursor = self.view.sel()[0].end()
            insert_pos = max(current_cursor - len(chars), 0)
            if insert_pos < input_start:
                self._in_soft_undo = True
                try:
                    self.view.run_command("soft_undo")
                finally:
                    self._in_soft_undo = False
                if chars and not getattr(s.output, "_question_input_mode", False):
                    self._guide_insert_to_composer(s, chars)
                return
        elif command and not command.startswith("submarine") and not command.startswith("claude"):
            if command in self._INPUT_EDIT_COMMANDS:
                input_start = s.output._input_start
                if any(r.begin() < input_start for r in self.view.sel()):
                    self._in_soft_undo = True
                    try:
                        self.view.run_command("soft_undo")
                    finally:
                        self._in_soft_undo = False
                    return
        if getattr(s.output, "_question_input_mode", False):
            return
        if self._wholly_in_draft(s):
            input_text = s.output.get_input_text()
            s.draft_prompt = "" if not input_text.strip() else input_text
        try:
            s.output._update_composer_pad_phantom()
        except Exception:
            pass
        sel = self.view.sel()
        if sel and len(sel) == 1:
            cursor = sel[0].end()
            content = self.view.substr(sublime.Region(0, self.view.size()))
            trigger = ContextParser.check_trigger(content, cursor)
            if trigger:
                self._show_context_popup(s, cursor)

    def _show_context_popup(self, session, cursor):
        window = self.view.window()
        if not window:
            return
        self.view.run_command(keys.CMD_REPLACE, {
            "start": cursor - 1, "end": cursor, "text": "",
        })
        open_files = []
        for v in window.views():
            if v.file_name() and not keys.is_output_view(v):
                open_files.append((os.path.basename(v.file_name()), v.file_name()))
        has_pending = bool(getattr(session, "pending_context", None))
        pending_count = len(session.pending_context) if has_pending else 0
        menu_items = ContextParser.build_menu(open_files, has_pending, pending_count)

        def on_browse():
            self._show_file_picker(session)

        def on_clear():
            if hasattr(session, "clear_context"):
                session.clear_context()
            if sublime is not None:
                sublime.status_message("Context cleared")

        def on_add_file(path, _content):
            for v in window.views():
                if v.file_name() == path:
                    content = v.substr(sublime.Region(0, v.size()))
                    if hasattr(session, "add_context_file"):
                        session.add_context_file(path, content)
                    break

        handler = ContextMenuHandler(on_browse, on_clear, on_add_file)

        def on_select(idx):
            handler.handle_selection(menu_items, idx)

        window.show_quick_panel(
            ContextParser.format_menu_items(menu_items),
            on_select,
            placeholder="Add context...",
        )

    def _show_file_picker(self, session):
        window = self.view.window()
        if not window:
            return
        keys.write_setting(
            window.settings(), keys.PENDING_CONTEXT_SESSION, session.output.view.id())
        keys.write_setting(window.settings(), keys.PENDING_CONTEXT_TIME, time.time())
        window.run_command("show_overlay", {"overlay": "goto", "show_files": True})
