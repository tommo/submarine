"""SubmarineOutputView — OutputPort + ChromePort façade.

core/session.py talks to this object. core/ports.py does not exist yet
(P2); the surface below matches the P2 protocol list plus the extra
OutputView methods Session already calls.

Viewless convention (Worker C / HostView):
  Each of SubmarineOutputView, TurnRenderer, ModalUI, Composer, OutputSheet
  exposes `_has_view()` → bool (a live ST view is bound and `is_valid()`).
  Buffer writes, named regions, phantoms, and scrolling run only inside
  `if self._has_view()`. Session/render state (`conversations`, `current`,
  pending modals, captured draft) always updates. `Conversation.region`
  and modal `.region` tuples are None while detached; `repaint_from_state()`
  recomputes them; `surface_restore()` re-enters composer / modals / scroll.
"""
from __future__ import annotations

import os
from typing import Any, Callable, List, Optional

from . import keys
from .composer import Composer
from .format_helpers import FormatHelpers
from .formatters import is_image_path, is_video_path, media_display_path
from .modals import ModalUI
from .renderer import TurnRenderer
from .session_api import get_session_for_view
from .sheet import OutputSheet
from .tools import is_host_control_tool as _is_host_control

try:
    import sublime
except ImportError:
    sublime = None  # type: ignore


class SubmarineOutputView(FormatHelpers):
    """Conversation surface: sheet + composer + renderer + modals.

    Detached sessions keep this object with `view is None`. OutputPort and
    ChromePort methods still record state; they no-op any buffer/phantom
    mutation. See module docstring for the `_has_view()` convention.
    """

    def __init__(self, window):
        self.window = window
        self.sheet = OutputSheet(self, window)
        self.composer = Composer(self)
        self.renderer = TurnRenderer(self)
        self.modals = ModalUI(self)
        self.modals.load_persisted_auto_allow()
        self._sleep_phantom = None
        self._queue_phantom = None
        self._wakeup_phantom = None
        self._perm_banner_phantom = None
        self._surface = {}
        self._tasks_expanded = False
        self._sel_guard = False

    def _has_view(self) -> bool:
        view = self.sheet.view
        if not view:
            return False
        try:
            return bool(view.is_valid())
        except Exception:
            return True

    # --- view / state aliases (listeners + session poke these) -------------

    @property
    def view(self):
        return self.sheet.view

    @view.setter
    def view(self, v):
        old = self.sheet.view
        if old is not None and v is not old:
            try:
                self.surface_save()
            except Exception:
                pass
            try:
                self.modals.drop_view_chrome()
            except Exception:
                pass
            try:
                self.clear_phantoms()
            except Exception:
                pass
            try:
                self.composer.detach()
            except Exception:
                pass
            try:
                self.renderer.invalidate_regions()
            except Exception:
                pass
        self.sheet.view = v
        if v is not old:
            self._drop_phantom_set_refs()
        if v is not None:
            self.renderer.restore_stashed_regions()

    def _drop_phantom_set_refs(self) -> None:
        """PhantomSets are per-view; drop them so bind rebuilds against the new view."""
        self._sleep_phantom = None
        self._queue_phantom = None
        self._wakeup_phantom = None
        self._perm_banner_phantom = None
        c = self.composer
        c._pad_phantom_set = None
        c._context_phantom_set = None
        r = self.renderer
        r._media_phantom_set = None
        r._turn_context_phantom_set = None

    def surface_save(self) -> dict:
        """Snapshot per-session chrome. Viewless: return last snapshot unchanged."""
        if not self._has_view():
            return dict(self._surface or {})
        c = self.composer
        view = self.view
        input_mode = bool(c.is_input_mode() and not c._question_input_mode)
        draft = ""
        caret = 0
        if input_mode:
            try:
                draft = c.get_input_text() or ""
            except Exception:
                draft = c._detached_draft or ""
            try:
                start = int(c._input_start or 0)
                sel = view.sel()
                if sel:
                    b = sel[0].begin() if hasattr(sel[0], "begin") else getattr(sel[0], "a", start)
                    caret = max(0, int(b) - start)
                elif c._draft_caret_off is not None:
                    caret = int(c._draft_caret_off)
            except Exception:
                caret = int(c._draft_caret_off or 0)
        else:
            draft = c._detached_draft or ""
        scroll = (0.0, 0.0)
        try:
            vp = view.viewport_position()
            if vp and len(vp) >= 2:
                scroll = (float(vp[0]), float(vp[1]))
        except Exception:
            pass
        tasks_expanded = bool(self._tasks_expanded)
        try:
            tasks_expanded = bool(keys.read_setting(
                view.settings(), keys.TASKS_EXPANDED, tasks_expanded))
        except Exception:
            pass
        self._tasks_expanded = tasks_expanded
        self.renderer._tasks_expanded = tasks_expanded
        surface = {
            "draft": draft,
            "input_mode": input_mode,
            "scroll": scroll,
            "caret": caret,
            "tasks_expanded": tasks_expanded,
            "modals": self.modals.descriptors(),
        }
        prev = self._surface or {}
        # If the composer was already peeled (HostView/QuickHost exit input
        # before unbinding), keep the last snapshot's draft/caret/input_mode.
        if not surface["input_mode"] and prev.get("input_mode"):
            surface["input_mode"] = True
            surface["draft"] = prev.get("draft") or surface["draft"]
            surface["caret"] = prev.get("caret", surface["caret"])
        self._surface = surface
        return dict(surface)

    def surface_restore(self, surface: dict) -> None:
        """Re-apply a snapshot. Assumes the view is bound and `repaint_from_state()` ran."""
        surface = dict(surface or {})
        self._surface = dict(surface)
        if not self._has_view():
            if "draft" in surface:
                self.composer._detached_draft = surface.get("draft") or ""
            return
        view = self.view
        tasks_expanded = bool(surface.get("tasks_expanded"))
        self._tasks_expanded = tasks_expanded
        self.renderer._tasks_expanded = tasks_expanded
        try:
            keys.write_setting(view.settings(), keys.TASKS_EXPANDED, tasks_expanded)
        except Exception:
            pass
        try:
            self.modals.rerender_pending()
        except Exception as e:
            print("[Submarine] surface_restore modals: %s" % e)
        want_input = bool(surface.get("input_mode"))
        draft = surface.get("draft") or ""
        idle = not self.composer.has_turn_modal_ui()
        if want_input and idle:
            if not self.composer.is_input_mode():
                self.composer.enter_input_mode()
            if self.composer.is_input_mode() and draft:
                try:
                    self.composer.set_composer_text(draft)
                except Exception:
                    pass
            caret = surface.get("caret")
            if caret is not None and self.composer.is_input_mode():
                try:
                    self.composer._draft_caret_off = max(0, int(caret))
                    self.composer.restore_draft_caret(force=True)
                except Exception:
                    pass
        elif draft:
            self.composer._detached_draft = draft
        scroll = surface.get("scroll")
        if scroll:
            try:
                self.view.set_viewport_position(
                    (float(scroll[0]), float(scroll[1])), False)
            except Exception:
                pass

    @property
    def conversations(self):
        return self.renderer.conversations

    @conversations.setter
    def conversations(self, v):
        self.renderer.conversations = v

    @property
    def current(self):
        return self.renderer.current

    @current.setter
    def current(self, v):
        self.renderer.current = v

    @property
    def pending_permission(self):
        return self.modals.pending_permission

    @pending_permission.setter
    def pending_permission(self, v):
        self.modals.pending_permission = v

    @property
    def pending_plan(self):
        return self.modals.pending_plan

    @pending_plan.setter
    def pending_plan(self, v):
        self.modals.pending_plan = v

    @property
    def pending_question(self):
        return self.modals.pending_question

    @pending_question.setter
    def pending_question(self, v):
        self.modals.pending_question = v

    @property
    def auto_allow_tools(self):
        return self.modals.auto_allow_tools

    @auto_allow_tools.setter
    def auto_allow_tools(self, v):
        self.modals.auto_allow_tools = v

    @property
    def _input_mode(self):
        return self.composer._input_mode

    @_input_mode.setter
    def _input_mode(self, v):
        self.composer._input_mode = v

    @property
    def _input_start(self):
        return self.composer._input_start

    @_input_start.setter
    def _input_start(self, v):
        self.composer._input_start = v

    @property
    def _question_input_mode(self):
        return self.composer._question_input_mode

    @_question_input_mode.setter
    def _question_input_mode(self, v):
        self.composer._question_input_mode = v

    @property
    def _render_pending(self):
        return self.renderer._render_pending

    @_render_pending.setter
    def _render_pending(self, v):
        self.renderer._render_pending = v

    @property
    def _caret_owner(self):
        return self.composer.caret_owner()

    # --- OutputPort --------------------------------------------------------

    def prompt(self, text, context_names=None, context_refs=None):
        """Start a user turn. Viewless: records conversation, no buffer write."""
        self.renderer.prompt(text, context_names, context_refs)

    def tool(self, name, tool_input=None, tool_id=None, background=False):
        """Open a tool row. Viewless: records ToolCall, no buffer write."""
        self.renderer.tool(name, tool_input, tool_id, background)

    def tool_done(self, name, result=None, tool_id=None):
        """Mark a tool done. Viewless: updates ToolCall, no buffer write."""
        self.renderer.tool_done(name, result, tool_id)

    def tool_error(self, name, result=None, tool_id=None):
        """Mark a tool errored. Viewless: updates ToolCall, no buffer write."""
        self.renderer.tool_error(name, result, tool_id)

    def text(self, content):
        """Append assistant text. Viewless: records events, no buffer write."""
        self.renderer.text(content)

    def meta(self, duration, cost=None, usage=None):
        """Finish the live turn. Viewless: records meta, skips buffer flush."""
        self.renderer.meta(duration, cost, usage)

    def interrupted(self, show_banner=True):
        """Mark the turn interrupted. Viewless: records state, no buffer write."""
        self.renderer.interrupted(show_banner)

    def clear_asking_state(self):
        """Drop leftover question / permission / plan UI without callbacks."""
        self.modals.clear_asking_state()
        try:
            if isinstance(getattr(self, "_surface", None), dict):
                self._surface["modals"] = []
        except Exception:
            pass

    def apply_plan_todos(self, entries):
        """Replace live todos. Viewless: records todos, no buffer write."""
        self.renderer.apply_plan_todos(entries)

    def clear(self, keep_supportive=True):
        """Clear the transcript. Viewless: drops conversations, no buffer write."""
        self.renderer.clear(keep_supportive)

    def permission_request(self, pid, tool, tool_input, callback):
        """Queue/show a permission modal. Viewless: records request, no chrome."""
        self.modals.permission_request(pid, tool, tool_input, callback)

    def question_request(self, qid, questions, callback):
        """Show a question modal. Viewless: records request, no chrome."""
        self.modals.question_request(qid, questions, callback)

    def plan_approval_request(self, plan_id, plan_file, allowed_prompts, callback):
        """Show a plan-approval modal. Viewless: records request, no chrome."""
        self.modals.plan_approval_request(plan_id, plan_file, allowed_prompts, callback)

    def advance_spinner(self, frames=None):
        """Advance the working spinner. Viewless: no-op."""
        self.renderer.advance_spinner(frames)

    def set_retry_hint(self, text):
        """Set the live retry hint. Viewless: records hint, no buffer write."""
        self.renderer.set_retry_hint(text)

    def enter_input_mode(self):
        """Open the ◎ composer. Viewless: no-op (restored via surface_restore)."""
        self.composer.enter_input_mode()

    def reset_active_states(self, soft=False):
        """Reset composer + pending tools. Viewless: state only, no buffer patch."""
        self.renderer.reset_active_states(soft)

    # --- extra OutputView API Session already calls ------------------------

    def show(self, focus=True, panel=None, create=False):
        """Ensure a sheet exists. Viewless: creates one (bind path, not a no-op).

        `create=True` forces a new_file even in single mode (host bootstrap
        / tabs restore). Default `create=False` is a no-op in single mode
        when no view is bound, so detached sessions stay headless.
        """
        self.sheet.show(focus=focus, panel=panel, create=create)

    def set_name(self, name):
        """Set the tab base name. Viewless: stored, title write skipped."""
        self.sheet.set_name(name)

    def refresh_title(self):
        self.sheet.update_title()

    def exit_input_mode(self, keep_text=False):
        return self.composer.exit_input_mode(keep_text=keep_text)

    def is_input_mode(self):
        """Composer flag. Viewless: False after detach (draft lives on the surface)."""
        return self.composer.is_input_mode()

    def get_input_text(self):
        return self.composer.get_input_text()

    def set_composer_text(self, text):
        self.composer.set_composer_text(text)

    def is_in_input_region(self, point):
        return self.composer.is_in_input_region(point)

    def has_turn_modal_ui(self):
        return self.composer.has_turn_modal_ui()

    def hide_composer_for_modal(self):
        self.composer.hide_composer_for_modal()

    def focus_composer(self, force_show=False, steal_focus=False,
                       preserve_caret=False, park_at_end=False):
        self.composer.focus(
            force_show=force_show, steal_focus=steal_focus,
            preserve_caret=preserve_caret, park_at_end=park_at_end)

    def park_composer_caret(self, mode="end"):
        self.composer.park(mode)

    def scroll_composer_chrome(self, force=False):
        self.composer.scroll_composer_chrome(force=force)

    def caret_owner(self):
        return self.composer.caret_owner()

    def set_caret_owner(self, owner):
        self.composer.set_caret_owner(owner)

    def note_draft_caret(self):
        self.composer.note_draft_caret()

    def restore_draft_caret(self, force=False):
        return self.composer.restore_draft_caret(force=force)

    def refresh_preserving_input(self):
        self.renderer.refresh_preserving_input()

    def refresh_background_hints(self):
        """Refresh ⚙ hints in the composer prefix. Viewless: no-op."""
        self.composer.refresh_background_hints()

    def reset_input_mode(self, reenter=False):
        self.composer.reset_input_mode(reenter=reenter)

    @staticmethod
    def strip_composer_tail(view):
        return Composer.strip_composer_tail(view)

    @staticmethod
    def collapse_trailing_blank_lines(view, keep=1):
        return Composer.collapse_trailing_blank_lines(view, keep=keep)

    def set_pending_context(self, context_items):
        """Show 📎 chips. Viewless: no-op."""
        self.composer.set_pending_context(context_items)

    def draft_end(self):
        return self.composer.draft_end()

    def find_tool_by_id(self, tool_id):
        """Lookup a tool by id. Viewless: same, state-only."""
        return self.renderer.find_tool_by_id(tool_id)

    def active_background_tools(self):
        """List BACKGROUND tools. Viewless: same, state-only."""
        return self.renderer.active_background_tools()

    def remove_tool(self, target):
        """Drop a tool event. Viewless: mutates events, no buffer patch."""
        self.renderer.remove_tool(target)

    def clear_keep_last(self):
        self.renderer.clear_keep_last()

    def undo_clear(self):
        self.renderer.undo_clear()

    def repaint_from_state(self):
        """Reproject conversations onto the bound view. Viewless: no-op."""
        self.renderer.repaint_from_state()

    def handle_permission_key(self, key):
        return self.modals.handle_permission_key(key)

    def handle_plan_key(self, key):
        return self.modals.handle_plan_key(key)

    def handle_question_key(self, key):
        return self.modals.handle_question_key(key)

    def submit_question_input(self):
        return self.modals.submit_question_input()

    def clear_stale_permission(self, current_pid):
        self.modals.clear_stale_permission(current_pid)

    def clear_all_permissions(self):
        """Dismiss pending permissions. Viewless: drops live requests, no buffer."""
        self.modals.clear_all_permissions()

    @classmethod
    def is_host_control_tool(cls, name):
        return _is_host_control(name)

    # --- ChromePort --------------------------------------------------------

    def sleep_banner(self, show=True, text=""):
        """Sleep phantom. Viewless: no-op."""
        self._set_banner("_sleep_phantom", keys.PHANTOM_SLEEP, text, show)

    def connecting_banner(self, show=True):
        """Connecting phantom. Viewless: no-op."""
        self._set_banner(
            "_sleep_phantom", keys.PHANTOM_SLEEP,
            "↻ connecting…" if show else "", show)

    def queue_chips(self, prompts=None):
        """Queued-prompt chips. Viewless: no-op."""
        items = list(prompts or [])
        if not items:
            self._set_banner("_queue_phantom", keys.PHANTOM_QUEUE, "", False)
            return
        labels = []
        for it in items:
            if isinstance(it, dict):
                labels.append(str(it.get("text") or it.get("name") or ""))
            else:
                labels.append(str(it))
        html = " · ".join(l for l in labels if l)
        try:
            hint = (
                "⌘↵ send now"
                if sublime is not None and sublime.platform() == "osx"
                else "Ctrl+↵ send now"
            )
        except Exception:
            hint = "Ctrl+↵ send now"
        html = (
            '%s<div style="margin:2px 0 1px 0;font-size:10px;'
            'color:color(var(--foreground) alpha(0.35));">%s</div>'
            % (html, hint)
        )
        self._set_banner("_queue_phantom", keys.PHANTOM_QUEUE, html, True)

    def wakeup_banner(self, fire_at=None):
        """Wake countdown phantom. Viewless: no-op."""
        if not fire_at:
            self._set_banner("_wakeup_phantom", keys.PHANTOM_WAKEUP, "", False)
            return
        import time as _t
        try:
            remain = max(0, int(float(fire_at) - _t.time()))
        except (TypeError, ValueError):
            remain = 0
        self._set_banner(
            "_wakeup_phantom", keys.PHANTOM_WAKEUP,
            "↻ wake in %ds" % remain, True)

    def set_status(self, text):
        """Status-bar text. Viewless: no-op."""
        view = self.view
        if not view or not view.is_valid():
            return
        try:
            view.set_status("submarine", text or "")
        except Exception:
            pass

    def refresh_tab_title(self):
        """Rewrite the tab title. Viewless: no-op."""
        self.sheet.update_title()

    def set_unread(self, on):
        """Stamp unread on the bound view. Viewless: skips the view setting."""
        view = self.view
        if view:
            # Tab glyph is derived from session.unread; stamp a view key too.
            try:
                view.settings().set("submarine_unread", bool(on))
            except Exception:
                pass
        elif on:
            self._detached_status("finished")
        self.sheet.update_title()

    def _detached_status(self, what):
        """One-shot status bar. Never steals host focus."""
        if sublime is None:
            return
        name = getattr(self.sheet, "_name", None) or "session"
        try:
            sublime.status_message("Submarine: %s %s" % (name, what))
        except Exception:
            pass

    # Back-compat aliases used before core/ports.py landed
    def show_sleep_banner(self, text="", on=True):
        self.sleep_banner(show=on, text=text)

    def hide_sleep_banner(self):
        self.sleep_banner(show=False)

    def show_queue_chips(self, items=None):
        self.queue_chips(items)

    def clear_queue_chips(self):
        self.queue_chips([])

    def show_wakeup_banner(self, text="", on=True):
        if not on:
            self.wakeup_banner(None)
            return
        self._set_banner("_wakeup_phantom", keys.PHANTOM_WAKEUP, text, True)

    def hide_wakeup_banner(self):
        self.wakeup_banner(None)

    def set_status_bar(self, text):
        self.set_status(text)

    def clear_phantoms(self, names=None):
        """Erase chrome phantoms. Viewless: drop PhantomSet refs, no view API."""
        pairs = [
            ("_sleep_phantom", keys.PHANTOM_SLEEP),
            ("_queue_phantom", keys.PHANTOM_QUEUE),
            ("_wakeup_phantom", keys.PHANTOM_WAKEUP),
            ("_perm_banner_phantom", keys.PHANTOM_PERM_BANNER),
        ]
        if names:
            want = set(names)
            pairs = [p for p in pairs if p[1] in want or p[1].replace("submarine_", "claude_") in want]
        for attr, name in pairs:
            ps = getattr(self, attr, None)
            if ps is not None:
                try:
                    ps.update([])
                except Exception:
                    pass
            view = self.view
            if view and hasattr(view, "erase_phantoms"):
                try:
                    view.erase_phantoms(name)
                    view.erase_phantoms(name.replace("submarine_", "claude_"))
                except Exception:
                    pass
        if names:
            return
        try:
            if self.composer._pad_phantom_set is not None:
                self.composer._pad_phantom_set.update([])
            if self.renderer._media_phantom_set is not None:
                self.renderer._media_phantom_set.update([])
        except Exception:
            pass
        if not self._has_view():
            self._drop_phantom_set_refs()

    def _set_banner(self, attr, name, text, on):
        view = self.view
        if not self._has_view() or sublime is None:
            return
        ps = getattr(self, attr, None)
        if ps is None:
            try:
                ps = sublime.PhantomSet(view, name)
                setattr(self, attr, ps)
            except Exception:
                return
        if not on or not text:
            try:
                ps.update([])
            except Exception:
                pass
            return
        html = (
            '<body id="submarine-chrome" style="margin:0;padding:2px 8px;'
            'font-size:11px;color:color(var(--foreground) alpha(0.7))">'
            "%s</body>" % text
        )
        pt = max(0, view.size() - 1)
        try:
            ps.update([sublime.Phantom(
                sublime.Region(pt, pt), html, sublime.LAYOUT_BLOCK)])
        except Exception:
            pass

    # --- persist stamps (PersistPort-shaped helpers on the view) -----------

    def stamp(self, key, value):
        """Write a view setting. Viewless: no-op."""
        if self.view:
            keys.write_setting(self.view.settings(), key, value)

    def read_stamp(self, key, default=None):
        """Read a view setting. Viewless: returns default."""
        if not self.view:
            return default
        return keys.read_setting(self.view.settings(), key, default)

    def clear_stamp(self, key):
        """Erase a view setting. Viewless: no-op."""
        if self.view:
            keys.erase_setting(self.view.settings(), key)

    # --- buffer helpers used by children -----------------------------------

    def _write(self, text, pos=None):
        return self.sheet.write(text, pos)

    def _replace(self, start, end, text):
        return self.sheet.replace(start, end, text)

    def _apply_output_settings(self):
        self.sheet.apply_output_settings()

    def _update_title(self):
        self.sheet.update_title()

    def _move_cursor_to_end(self):
        view = self.view
        if not view or not view.is_valid():
            return
        if (self.composer.is_input_mode() and self.caret_owner() == "history"
                and not self.composer._question_input_mode
                and not self.has_turn_modal_ui()):
            return
        end = view.size()
        if sublime is not None:
            view.sel().clear()
            view.sel().add(sublime.Region(end, end))
        if self.composer.is_input_mode() and not self.composer._question_input_mode:
            self.composer.set_caret_owner("draft")
            try:
                start = int(self.composer._input_start or 0)
                self.composer._draft_caret_off = max(0, end - start)
            except Exception:
                pass
            self.composer.scroll_composer_chrome(force=True)
        else:
            view.show(end)

    def _update_composer_pad_phantom(self):
        self.composer._update_composer_pad_phantom()

    # --- media / context hrefs ---------------------------------------------

    def show_media_popup(self, path, location=-1):
        view = self.view
        if not view or not view.is_valid() or not path or sublime is None:
            return
        path = os.path.expanduser(path)
        self._sync_edit_target_from_preview(path, as_context=False, announce=False)
        html = self._media_popup_html(path)
        if not html:
            sublime.status_message("Media: %s" % path)
            return
        if location < 0:
            location = self.renderer._media_anchor.get(path, -1)
        if location < 0:
            disp = media_display_path(path)
            content = view.substr(sublime.Region(0, view.size()))
            if disp:
                idx = content.rfind(disp)
                if idx >= 0:
                    location = idx
            if location < 0:
                sel = view.sel()
                location = sel[0].begin() if sel else 0
        try:
            view.show(location)
        except Exception:
            pass
        try:
            view.show_popup(
                html,
                flags=sublime.HIDE_ON_MOUSE_MOVE_AWAY,
                location=location,
                max_width=560, max_height=480,
                on_navigate=lambda href, _p=path, _l=location: self._handle_media_href(
                    href, _p, location=_l),
            )
        except Exception as e:
            print("[Submarine] media popup: %s" % e)

    def _media_popup_html(self, path):
        import html as _html
        disp = media_display_path(path) or os.path.basename(path) or path
        style = (
            "margin:0;padding:8px 10px;background-color:var(--background);"
            "color:var(--foreground);font-size:12px;"
        )
        links = (
            '<a href="reveal:%s">reveal</a> · '
            '<a href="edit:%s">use as edit target</a> · '
            '<a href="dismiss:">dismiss</a><br>'
            '<span style="color:color(var(--foreground) alpha(0.55))">%s</span>'
            % (_html.escape(path), _html.escape(path), _html.escape(disp))
        )
        if is_video_path(path):
            return (
                '<body id="submarine-media-popup" style="%s">'
                '<div style="margin-bottom:6px">🎬 video (no inline preview)</div>'
                "%s</body>" % (style, links)
            )
        return '<body id="submarine-media-popup" style="%s">%s</body>' % (style, links)

    def _sync_edit_target_from_preview(self, path, as_context=True, announce=True, line=None):
        if not path:
            return
        try:
            session = get_session_for_view(self.view)
            if session and hasattr(session, "set_edit_target"):
                session.set_edit_target(
                    path, as_context=as_context, line=line, announce=announce)
                return
        except Exception as e:
            print("[Submarine] sync edit target: %s" % e)
        if announce and sublime is not None:
            sublime.status_message("Edit target: %s" % os.path.basename(path))

    def _handle_media_href(self, href, fallback_path="", location=-1):
        if href == "dismiss:" or (href or "").startswith("dismiss"):
            try:
                self.view.hide_popup()
            except Exception:
                pass
            return
        path = fallback_path or ""
        if href.startswith("popup:"):
            path = href[6:] or fallback_path
            loc = location if location >= 0 else self.renderer._media_anchor.get(
                os.path.expanduser(path), -1)
            self.show_media_popup(path, location=loc)
            return
        if href.startswith("edit:"):
            path = os.path.expanduser(href[5:] or fallback_path)
            self._sync_edit_target_from_preview(path, as_context=True, announce=True)
            try:
                self.view.hide_popup()
            except Exception:
                pass
            return
        if href.startswith("reveal:"):
            path = href[7:]
        path = os.path.expanduser(path)
        if path:
            self._reveal_path(path)

    def _reveal_path(self, path):
        import subprocess
        path = os.path.expanduser(path or "")
        if not path or sublime is None:
            return
        try:
            if sublime.platform() == "osx":
                subprocess.Popen(["open", "-R", path])
            elif sublime.platform() == "windows":
                subprocess.Popen(["explorer", "/select,", path])
            else:
                target = path if os.path.isdir(path) else (os.path.dirname(path) or ".")
                subprocess.Popen(["xdg-open", target])
            sublime.status_message("Revealed %s" % os.path.basename(path))
        except Exception as e:
            sublime.status_message("Reveal failed: %s" % e)

    def _open_path(self, path, line=None):
        path = os.path.expanduser(path or "")
        if not path:
            return
        path = os.path.abspath(path)
        window = self.view.window() if self.view else None
        if not window:
            return
        if os.path.isdir(path):
            self._reveal_path(path)
            return
        self._sync_edit_target_from_preview(
            path, as_context=is_image_path(path), announce=False, line=line)
        target = None
        try:
            for v in window.views():
                fn = v.file_name()
                if fn and os.path.abspath(fn) == path:
                    target = v
                    break
        except Exception:
            pass
        if target is None:
            flags = 0
            if line:
                target = window.open_file(
                    "%s:%d" % (path, int(line)),
                    getattr(sublime, "ENCODED_POSITION", 1) if sublime else 1)
            else:
                target = window.open_file(path)
        else:
            window.focus_view(target)
            if line and sublime is not None:
                pt = target.text_point(max(0, int(line) - 1), 0)
                target.sel().clear()
                target.sel().add(sublime.Region(pt, pt))
                target.show(pt)

    def _handle_context_href(self, href):
        """Clickable 📎 chips: open:N (pending) or turn:N (frozen)."""
        if not href:
            return
        session = get_session_for_view(self.view)
        kind, _, rest = href.partition(":")
        try:
            idx = int(rest)
        except (TypeError, ValueError):
            idx = -1
        if kind == "open" and session and getattr(session, "pending_context", None):
            items = list(session.pending_context)
            if 0 <= idx < len(items):
                item = items[idx]
                path = getattr(item, "path", None) or (
                    item.get("path") if isinstance(item, dict) else None)
                if path:
                    # Modifier-click removes (best-effort: always open; commands layer
                    # can bind a dedicated remove).
                    self._open_path(path)
            return
        if kind == "turn" and self.current:
            refs = list(getattr(self.current, "context_refs", None) or [])
            if 0 <= idx < len(refs):
                path = refs[idx].get("path") or ""
                lr = refs[idx].get("line_range") or ""
                line = None
                if lr:
                    try:
                        line = int(str(lr).lstrip("L").split("-")[0])
                    except Exception:
                        line = None
                if path:
                    self._open_path(path, line=line)

    def _format_tool_detail(self, tool):
        from .formatters import format_tool_detail
        return format_tool_detail(self, tool)


# Back-compat alias used by tests / restore helpers
OutputView = SubmarineOutputView
