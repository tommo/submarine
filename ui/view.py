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

import html as _html
import os
from typing import Any, Callable, List, Optional, Sequence

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


def _keyed_phantom_set(view, key):
    """One long-lived PhantomSet per (view, key) so update() replaces, not stacks."""
    if sublime is None or not view:
        return None
    try:
        if not view.is_valid() or not key:
            return None
    except Exception:
        return None
    reg = getattr(sublime, "_submarine_phantom_registry", None)
    if not isinstance(reg, dict):
        reg = {}
        sublime._submarine_phantom_registry = reg
    k = (view.id(), key)
    ps = reg.get(k)
    if ps is not None:
        return ps
    try:
        ps = sublime.PhantomSet(view, key)
    except Exception:
        return None
    reg[k] = ps
    return ps


def sleep_banner_anchor(content, peel=None):
    """Start of the last non-empty line. LAYOUT_BLOCK hangs under that line
    (the paused hint). Visibility is `_scroll_sleep_banner_into_view`.
    """
    text = content or ""
    stripped = text.rstrip("\n")
    if not stripped:
        return 0
    last_nl = stripped.rfind("\n")
    return 0 if last_nl < 0 else last_nl + 1


def format_sleep_banner_html(text: str, color: str = "#ffcc66",
                             width_px: int = 400) -> str:
    """Paused banner. Minihtml phantoms ignore CSS margin — use padding +
    explicit-height spacers so the inset actually shows."""
    body = _html.escape(text or "")
    w = max(80, int(width_px or 400))
    fill = "color-mix(in srgb, %s 18%%, transparent)" % color
    gap = (
        '<div style="padding:0;margin:0;line-height:1;height:10px;'
        'font-size:1px;">&nbsp;</div>'
    )
    rule = (
        '<div style="padding:0;margin:0;line-height:1;height:2px;'
        'font-size:1px;max-width:%dpx;background-color:%s;">&nbsp;</div>'
        % (w, color)
    )
    return (
        '<body id="submarine-overlay" style="margin:0;padding:12px 10px 14px 10px;'
        'max-width:%dpx;">'
        '%s%s%s'
        '<div style="padding:8px 14px;font-size:13px;font-weight:bold;'
        'letter-spacing:0.02em;color:%s;background-color:%s;'
        'border-left:3px solid %s;">%s</div>'
        '%s'
        '</body>'
        % (w, gap, rule, gap, color, fill, color, body, gap)
    )


def format_queue_phantom_html(prompts: Sequence[str],
                              send_now_hint: str = "Ctrl+↵ send now") -> str:
    """Composer chrome *above* ◎: optional queue chips + a hairline split.

    Layout (top → bottom, phantoms only):
      ⏳ queued msg  ↵ ×   (only when queued)
      ─ hairline ─
      ◎ input…
    """
    rows = []
    q = [p for p in (prompts or []) if p]
    if q:
        rows.append(
            '<div style="margin:0 0 2px 0;font-size:10px;'
            'color:color(var(--foreground) alpha(0.4));">'
            'queue · '
            '<a href="send_now" style="color:var(--orangish);'
            'text-decoration:none;" title="Cancel turn and send top now">'
            'send now</a>'
            '</div>'
        )
        for i, msg in enumerate(q):
            short = str(msg).replace("\n", " ").strip()
            if len(short) > 72:
                short = short[:72] + "…"
            safe = _html.escape(short)
            rows.append(
                '<div style="margin:1px 0;padding:1px 6px;'
                'background-color:color(var(--foreground) alpha(0.06));'
                'color:var(--bluish);font-size:11px;">'
                '⏳ %s'
                '&nbsp;<a href="send:%d" style="color:var(--orangish);'
                'text-decoration:none;" title="send now">↵</a>'
                '&nbsp;<a href="drop:%d" style="color:var(--redish);'
                'text-decoration:none;" title="remove">×</a>'
                '</div>' % (safe, i, i)
            )
        rows.append(
            '<div style="margin:2px 0 1px 0;font-size:10px;'
            'color:color(var(--foreground) alpha(0.35));">%s</div>'
            % _html.escape(send_now_hint or "")
        )
    rows.append(
        '<div style="margin:0;padding:0;line-height:1;'
        'font-size:1px;height:0;border-top:1px solid '
        'color(var(--foreground) alpha(0.14));">&nbsp;</div>'
    )
    return (
        '<body id="submarine-queue" style="margin:0;padding:0;">'
        '%s</body>' % "".join(rows)
    )


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

    def mark_dirty(self) -> None:
        """Bump the session dirty counter (chrome / queue / modal)."""
        self.renderer.mark_chrome_dirty()

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
        r._artifact_phantom_set = None

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
        full_text, transcript_end = self._buffer_snapshot_text()
        surface = {
            "draft": draft,
            "input_mode": input_mode,
            "scroll": scroll,
            "caret": caret,
            "tasks_expanded": tasks_expanded,
            "modals": self.modals.descriptors(),
            "buffer_text": full_text,
            "transcript_end": transcript_end,
            "dirty": int(self.renderer._dirty),
            "input_start": int(c._input_start or 0) if input_mode else 0,
            "input_area_start": int(getattr(c, "_input_area_start", 0) or 0)
            if input_mode else 0,
        }
        prev = self._surface or {}
        # If the composer was already peeled (HostView/QuickHost exit input
        # before unbinding), keep the last snapshot's draft/caret/input_mode.
        if not surface["input_mode"] and prev.get("input_mode"):
            surface["input_mode"] = True
            surface["draft"] = prev.get("draft") or surface["draft"]
            surface["caret"] = prev.get("caret", surface["caret"])
            if prev.get("input_start"):
                surface["input_start"] = prev.get("input_start")
            if prev.get("input_area_start"):
                surface["input_area_start"] = prev.get("input_area_start")
        self._surface = surface
        try:
            self.renderer.snapshot_detach()
        except Exception:
            pass
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

    def restore_buffer_snapshot(self, text: str) -> None:
        """Replace the bound buffer with a detach snapshot. One replace."""
        view = self.view
        if not view:
            return
        try:
            end = view.size()
        except Exception:
            end = 0
        self.sheet.replace(0, end, text or "")

    def fast_paint(self, surface: dict) -> str:
        """Attach paint: clean snapshot, incremental catch-up, or full repaint."""
        try:
            from plat.log import log_plugin
        except ImportError:
            def log_plugin(message):  # type: ignore
                print("[Submarine] %s" % message)

        surface = dict(surface or {})
        text = surface.get("buffer_text")
        journal = list(self.renderer._journal or [])
        if not isinstance(text, str) or not self._has_view():
            if self.conversations or self.current or journal:
                log_plugin("swap fallback: missing snapshot")
            self.repaint_from_state()
            return "fallback"

        snap_dirty = surface.get("dirty")
        try:
            current_dirty = int(self.renderer._dirty)
        except Exception:
            current_dirty = None
        clean = (
            snap_dirty == current_dirty
            and not journal
        )

        if clean:
            self.restore_buffer_snapshot(text)
            self.renderer.restore_detach_projection()
            self.renderer.stamp_live_regions()
            try:
                self.modals.restore_stashed_regions()
            except Exception:
                pass
            self._rehydrate_surface(surface)
            return "clean"

        # Instant old content, then catch-up on the transcript span.
        end = surface.get("transcript_end")
        if isinstance(end, int) and 0 <= end <= len(text):
            base = text[:end]
        else:
            base = text
        self.restore_buffer_snapshot(base)

        if journal:
            ok = self.renderer.catch_up_events(journal)
            if not ok:
                log_plugin("swap fallback: journal cannot cover gap")
                self.repaint_from_state()
                return "fallback"
            self.renderer.stamp_live_regions()
            return "dirty"

        if not self.renderer.state_matches_detach_snap():
            log_plugin("swap fallback: snapshot cannot cover gap")
            self.repaint_from_state()
            return "fallback"

        self.renderer.restore_detach_projection()
        self.renderer.stamp_live_regions()
        try:
            self.modals.restore_stashed_regions()
        except Exception:
            pass
        return "dirty"

    def _rehydrate_surface(self, surface: dict) -> None:
        """Restore composer/modal offsets after an exact buffer restore."""
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
        want_input = bool(surface.get("input_mode"))
        draft = surface.get("draft") or ""
        if want_input:
            c = self.composer
            c._detached_draft = draft
            c._input_mode = True
            try:
                c._input_start = max(0, int(surface.get("input_start") or 0))
            except Exception:
                c._input_start = 0
            try:
                c._input_area_start = max(
                    0, int(surface.get("input_area_start") or c._input_start))
            except Exception:
                c._input_area_start = c._input_start
            try:
                keys.write_setting(view.settings(), keys.INPUT_MODE, True)
            except Exception:
                pass
            caret = surface.get("caret")
            if caret is not None:
                try:
                    c._draft_caret_off = max(0, int(caret))
                    c.restore_draft_caret(force=True)
                except Exception:
                    pass
            try:
                c._update_composer_pad_phantom()
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

    def _buffer_snapshot_text(self):
        """Return (full buffer text, transcript_end before composer/modals)."""
        view = self.view
        if not view:
            return "", 0
        size = 0
        try:
            size = view.size()
        except Exception:
            size = 0
        full = ""
        try:
            if sublime is not None:
                full = view.substr(sublime.Region(0, size))
            else:
                full = view.substr(_span(0, size))
        except Exception:
            try:
                full = view.substr(None)
            except Exception:
                full = ""
        cuts = []
        try:
            peel = self.composer.peel_start()
            if peel is not None:
                cuts.append(int(peel))
        except Exception:
            pass
        try:
            trail = self.modals.trailing_ui_start()
            if trail is not None:
                cuts.append(int(trail))
        except Exception:
            pass
        end = min(cuts) if cuts else len(full)
        if end < 0:
            end = 0
        if end > len(full):
            end = len(full)
        return full, end

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

    def begin_continued(self):
        """Open a live sheet after @done without wiping the last turn."""
        self.renderer.begin_continued()

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

    def artifact_card(self, path, name, bytes=0, summary="", title=None):
        """Append an artifact card. Viewless: records event, chrome when bound."""
        self.renderer.artifact_card(
            path, name, bytes=bytes, summary=summary, title=title)

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

    def render_idle(self, window=None):
        """No session left to show: the branding page (ui/idle.py). Viewless: no-op."""
        try:
            from ui import idle
            return idle.render(self.sheet.view, window or self.sheet.window)
        except Exception:
            return False

    def clear_idle(self):
        """A session is taking the sheet over: drop the idle flag."""
        try:
            from ui import idle
            idle.clear(self.sheet.view)
        except Exception:
            pass

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

    def collapse_empty_composer_tail(self):
        self.composer.collapse_empty_composer_tail()

    def ensure_composer_spare_line(self):
        self.composer.ensure_composer_spare_line()

    def _view_is_focused(self):
        return self.sheet.view_is_focused()

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
        self._set_banner(
            "_sleep_phantom", keys.PHANTOM_SLEEP, text, show, strong=True)

    def connecting_banner(self, show=True):
        """Connecting phantom. Viewless: no-op."""
        self._set_banner(
            "_sleep_phantom", keys.PHANTOM_SLEEP,
            "↻ connecting…" if show else "", show, strong=False)

    def queue_chips(self, prompts=None):
        """Queued-prompt chrome above ◎. Viewless: no-op."""
        self.mark_dirty()
        items = []
        for it in list(prompts or []):
            if isinstance(it, dict):
                items.append(str(it.get("text") or it.get("name") or ""))
            else:
                items.append(str(it))
        self._paint_queue_phantom(items)

    def clear_queue_phantom(self):
        """Drop queue chips + hairline even if ◎ is open."""
        self._clear_queue_phantom_set()

    def _clear_queue_phantom_set(self):
        view = self.view
        ps = getattr(self, "_queue_phantom", None)
        if ps is not None:
            try:
                ps.update([])
            except Exception:
                pass
        if view and hasattr(view, "erase_phantoms"):
            try:
                view.erase_phantoms(keys.PHANTOM_QUEUE)
                view.erase_phantoms("claude_queue")
            except Exception:
                pass

    def _paint_queue_phantom(self, prompts):
        """Park queue chips + hairline on the line *above* ◎ (LAYOUT_BLOCK)."""
        view = self.view
        if not self._has_view() or sublime is None:
            return
        c = self.composer
        in_input = bool(c.is_input_mode())
        sleeping = bool(keys.read_setting(view.settings(), keys.SLEEPING))
        if (
            not in_input
            or getattr(c, "_question_input_mode", False)
            or sleeping
            or (self.has_turn_modal_ui() and not in_input)
        ):
            self._clear_queue_phantom_set()
            return
        peel = getattr(c, "_input_area_start", None)
        if peel is None or peel < 0:
            peel = getattr(c, "_input_start", None)
        if peel is None or peel < 0:
            peel = view.size()
        peel = min(max(0, int(peel)), view.size())
        if peel <= 0:
            self._clear_queue_phantom_set()
            return
        pt = peel - 1
        try:
            hint = (
                "⌘↵ send now"
                if sublime.platform() == "osx"
                else "Ctrl+↵ send now"
            )
        except Exception:
            hint = "Ctrl+↵ send now"
        html = format_queue_phantom_html(prompts, hint)
        ps = _keyed_phantom_set(view, keys.PHANTOM_QUEUE)
        if not ps:
            return
        self._queue_phantom = ps
        # Chips need a real row (LAYOUT_BLOCK). The empty hairline must not:
        # BLOCK above ◎ is a full text row, and clearing it on submit is what
        # yanked ◎ hello ▶ up one line from where the user typed.
        has_rows = any((p or "").strip() for p in prompts)
        if has_rows:
            layout = sublime.LAYOUT_BLOCK
        else:
            layout = getattr(sublime, "LAYOUT_BELOW", None) or sublime.LAYOUT_INLINE
        try:
            ps.update([sublime.Phantom(
                sublime.Region(pt, pt),
                html,
                layout,
                on_navigate=self._on_queue_phantom_navigate,
            )])
        except Exception:
            self._clear_queue_phantom_set()

    def _on_queue_phantom_navigate(self, href):
        sess = get_session_for_view(self.view)
        if sess is None:
            return
        fn = getattr(sess, "_on_queue_phantom_navigate", None)
        if callable(fn):
            fn(href)

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

    def _set_banner(self, attr, name, text, on, strong=False):
        view = self.view
        if not self._has_view() or sublime is None:
            return
        ps = _keyed_phantom_set(view, name) or getattr(self, attr, None)
        if ps is None:
            try:
                ps = sublime.PhantomSet(view, name)
            except Exception:
                return
        setattr(self, attr, ps)
        if not on or not text:
            try:
                if hasattr(view, "erase_phantoms"):
                    view.erase_phantoms(name)
                ps.update([])
            except Exception:
                pass
            return
        if strong:
            try:
                vw = int(float(view.viewport_extent()[0])) - 8
                width = max(80, vw)
            except Exception:
                width = 400
            html = format_sleep_banner_html(text, width_px=width)
        else:
            html = (
                '<body id="submarine-chrome" style="margin:0;padding:2px 8px;'
                'font-size:11px;color:color(var(--foreground) alpha(0.7))">'
                "%s</body>" % _html.escape(text)
            )
        content = ""
        try:
            content = view.substr(sublime.Region(0, view.size()))
        except Exception:
            content = ""
        peel = None
        try:
            c = getattr(self, "composer", None)
            if c is not None and c.is_input_mode():
                peel = c.peel_start()
        except Exception:
            peel = None
        pt = sleep_banner_anchor(content, peel)
        try:
            if hasattr(view, "erase_phantoms"):
                view.erase_phantoms(name)
            ps.update([])
            ps.update([sublime.Phantom(
                sublime.Region(pt, pt), html, sublime.LAYOUT_BLOCK)])
        except Exception:
            pass
        if strong:
            self._scroll_sleep_banner_into_view(view)

    def _scroll_sleep_banner_into_view(self, view):
        """LAYOUT_BLOCK height is not in the buffer. Follow-tail stops at the
        caret, so the paused hint sits off-screen unless we scroll layout."""
        def _go():
            if not view:
                return
            try:
                if not view.is_valid():
                    return
            except Exception:
                return
            try:
                _w, h = view.layout_extent()
                vh = float(view.viewport_extent()[1] or 0)
                vx = float(view.viewport_position()[0] or 0)
                view.set_viewport_position(
                    (vx, max(0.0, float(h) - vh)), False)
            except Exception:
                try:
                    view.show(max(0, view.size() - 1))
                except Exception:
                    pass

        if sublime is not None:
            sublime.set_timeout(_go, 0)
            sublime.set_timeout(_go, 40)
            sublime.set_timeout(_go, 160)
        else:
            _go()

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

    def _handle_artifact_href(self, href, fallback_path=""):
        """Card [open] / [path] hrefs. open in ST; path copies the absolute path."""
        href = href or ""
        path = fallback_path or ""
        if href.startswith("artifact-open:"):
            path = href[len("artifact-open:"):] or fallback_path
            self._open_artifact_path(path)
            return
        if href.startswith("artifact-path:"):
            path = href[len("artifact-path:"):] or fallback_path
            self._copy_artifact_path(path)
            return
        if href.startswith("open:") or href == "open":
            self._open_artifact_path(path or href[5:])
            return
        if href.startswith("path:") or href == "path":
            self._copy_artifact_path(path or href[5:])

    def _open_artifact_path(self, path):
        path = os.path.expanduser(path or "")
        if not path:
            return
        window = self.view.window() if self.view else self.window
        if not window:
            return
        try:
            from core.placement import open_file_in_last_session_split
            open_file_in_last_session_split(window, path)
        except Exception:
            try:
                window.open_file(path)
            except Exception:
                pass

    def _copy_artifact_path(self, path):
        path = os.path.expanduser(path or "")
        if not path:
            return
        if sublime is not None:
            try:
                sublime.set_clipboard(path)
                sublime.status_message("Copied path: %s" % path)
            except Exception:
                pass

    @staticmethod
    def _modifier_held_for_remove():
        # type: () -> bool
        """True if Ctrl or primary (Cmd on macOS) is held during a chip click.

        Ported from sublime-claude: Phantom `on_navigate` gets no modifier
        state, so the live keyboard flags are read directly. Anywhere else
        (Linux, or a failed lookup) reports False and the click just opens.
        """
        import sys
        try:
            if sys.platform == "darwin":
                from ctypes import CDLL, c_uint64, util
                lib = CDLL(util.find_library("ApplicationServices"))
                # kCGEventSourceStateHIDSystemState = 1
                lib.CGEventSourceFlagsState.argtypes = [c_uint64]
                lib.CGEventSourceFlagsState.restype = c_uint64
                flags = lib.CGEventSourceFlagsState(1)
                k_control, k_command = 0x00040000, 0x00100000
                return bool(flags & (k_control | k_command))
            if sys.platform == "win32":
                import ctypes
                return bool(ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000)
        except Exception:
            pass
        return False

    def _open_context_ref(self, ref):
        """Follow one chip ref: editor for code (with its line), reveal else."""
        from features.context import chip_ref, first_line_of_range

        ref = chip_ref(ref)
        path, name = ref["path"], ref["name"]
        if not path:
            self._status("No path for %s" % name)
            return
        if ref["action"] == "open":
            self._open_path(path, line=first_line_of_range(ref["line_range"]))
        else:
            self._reveal_path(path)

    def _status(self, message):
        if sublime is not None:
            try:
                sublime.status_message(message)
            except Exception:
                pass

    def _context_session(self):
        """Session that owns the 📎 chips: view binding first, quick host next.

        A quick/multi-slot view's registry binding lags its chip repaint, and
        losing the session there would leave the chips inert.
        """
        session = get_session_for_view(self.view)
        if session is not None:
            return session
        window = self.view.window() if self.view is not None else None
        if window is None:
            return None
        try:
            from features.quick import get_host
            host = get_host(window)
        except Exception:
            return None
        return getattr(host, "active_session", None) if host is not None else None

    @staticmethod
    def _context_manager(session):
        """The session's pending-context manager, or None when unwired."""
        return getattr(session, "context", None)

    def _handle_context_href(self, href):
        """Clickable 📎 chips: open:N (pending), turn:N (frozen), clear.

        Modifier-click on a pending chip removes it; the trailing `clear`
        link drops the whole queue.
        """
        from features.context import chip_ref

        if not href:
            return
        if href == "clear":
            ctx = self._context_manager(self._context_session())
            if ctx is not None:
                ctx.clear()
                self._status("Context cleared")
            return
        kind, _, rest = href.partition(":")
        try:
            idx = int(rest)
        except (TypeError, ValueError):
            return
        if kind in ("open", "item"):
            ctx = self._context_manager(self._context_session())
            items = list(getattr(ctx, "items", None) or [])
            if not 0 <= idx < len(items):
                return
            item = items[idx]
            if self._modifier_held_for_remove():
                if ctx.remove_at(idx):
                    self._status("Removed context: %s" % chip_ref(item)["name"])
                return
            self._open_context_ref(item)
            return
        if kind in ("turn", "tref") and self.current:
            refs = list(getattr(self.current, "context_refs", None) or [])
            if 0 <= idx < len(refs) and isinstance(refs[idx], dict):
                self._open_context_ref(refs[idx])

    def _format_tool_detail(self, tool):
        from .formatters import format_tool_detail
        return format_tool_detail(self, tool)


def _span(a, b):
    if sublime is not None:
        return sublime.Region(a, b)
    return type("R", (), {
        "a": a, "b": b,
        "begin": lambda self: min(self.a, self.b),
        "end": lambda self: max(self.a, self.b),
    })()


# Back-compat alias used by tests / restore helpers
OutputView = SubmarineOutputView
