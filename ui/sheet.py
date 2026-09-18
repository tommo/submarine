"""Output sheet lifecycle: scratch view, syntax/theme, title, buffer writes."""
from __future__ import annotations

import re
from typing import Any, Optional

from plat.constants import (
    BACKEND_ABBREV,
    STATUS_ACTIVE_WAITING,
    STATUS_ACTIVE_WORKING,
    STATUS_ERROR_HALT,
    STATUS_IDLE,
    STATUS_INACTIVE_WAITING,
    STATUS_INACTIVE_WORKING,
    STATUS_QUESTION,
    STATUS_SLEEPING,
    STATUS_WAKE,
)

from . import keys
from .models import strip_title_decoration
from .session_api import abbrev_for, backend_theme, get_session_for_view

try:
    import sublime
except ImportError:  # tests / non-ST
    sublime = None  # type: ignore


_TITLE_ICON_RE = re.compile(r'^(?:[◉◇•○◐◓◑◒❓⏸↻⚠✘❌!❗⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏*]\s*)+')
TAB_NAME_MAX = 40  # session name only; status + `GR> ` sit in front of this


class _HeadlessRegion(object):
    """Minimal Region stand-in when tests run without the Sublime runtime."""

    def __init__(self, a, b=None):
        self.a = a
        self.b = a if b is None else b

    def begin(self):
        return min(self.a, self.b)

    def end(self):
        return max(self.a, self.b)


def format_tab_title(base: str, prefix: str, abbrev: str = "",
                     max_name: int = TAB_NAME_MAX) -> str:
    """Status + optional `GR> ` + truncated session name.

    Truncate the name *before* adding the backend prefix so `GR> ` is not
    counted against the title budget.
    """
    name = (base or "Submarine").strip() or "Submarine"
    if max_name > 0 and len(name) > max_name:
        name = name[: max_name - 1] + "…"
    if abbrev:
        name = "%s> %s" % (abbrev, name)
    return "%s%s" % (prefix or "", name)


class OutputSheet:
    """View lifecycle + named-region helpers + buffer mutations.

    Viewless convention: `_has_view()` is False when detached. `write` /
    `replace` / region / viewport helpers no-op. `show()` is the bind path
    and still creates a sheet when none exists — detached event handlers
    must not call it.
    """

    def __init__(self, owner, window):
        self.owner = owner
        self.window = window
        self.view = None
        self._name = "Submarine"
        self._panel_name = None
        self._pending_vp_pin = None

    def _has_view(self) -> bool:
        return self._valid()

    # --- lifecycle ---------------------------------------------------------

    def show(self, focus: bool = True, panel: Optional[str] = None,
             create: bool = False) -> None:
        if panel:
            self._panel_name = panel
        panel_name = self._panel_name
        if panel_name:
            self._show_panel(panel_name, focus=focus)
            return
        if self.view and self._valid():
            if focus:
                self.window.focus_view(self.view)
            return
        if not create:
            try:
                from ui.host import is_single_mode
                if is_single_mode():
                    return
            except Exception:
                pass
        prev = self.window.active_view()
        self.view = self.window.new_file()
        self.view.set_name("Submarine")
        self.view.set_scratch(True)
        self.view.set_read_only(True)
        st = self.view.settings()
        keys.write_setting(st, keys.OUTPUT, True)
        st.set("auto_indent", False)
        # A new sheet lands at the end of the active group; put it back where
        # this window last had its session sheet.
        try:
            from core.placement import apply_session_tab
            apply_session_tab(self.window, self.view)
        except Exception as e:
            print("[Submarine] session tab: %s" % e)
        self.apply_output_settings()
        if sublime is not None:
            sublime.load_settings(keys.OUTPUT_SETTINGS).add_on_change(
                "submarine_output_%s" % self.view.id(), self.apply_output_settings
            )
        try:
            self.view.assign_syntax(keys.SYNTAX_PATH)
            if not keys.read_setting(st, keys.QUICK):
                backend = keys.read_setting(st, keys.BACKEND) or "claude"
                theme = backend_theme(backend) if backend != "claude" else keys.THEME_DEFAULT
                if keys.read_setting(st, keys.QUICK):
                    theme = keys.THEME_QUICK
                st.set("color_scheme", theme)
        except Exception as e:
            print("[Submarine] Error setting syntax/theme: %s" % e)
        if sublime is not None:
            self.view.sel().clear()
            self.view.sel().add(sublime.Region(0, 0))
        st.set("is_widget", False)
        if focus:
            self.window.focus_view(self.view)
        elif prev and prev.is_valid() and prev.id() != self.view.id():
            self.window.focus_view(prev)
        if self.owner.conversations or self.owner.current:
            try:
                self.owner.repaint_from_state()
            except Exception as e:
                print("[Submarine] repaint_from_state: %s" % e)

    def _show_panel(self, panel_name: str, focus: bool = True) -> None:
        v = self.window.find_output_panel(panel_name)
        if not v:
            v = self.window.create_output_panel(panel_name)
        self.view = v
        self.view.set_read_only(True)
        st = self.view.settings()
        keys.write_setting(st, keys.OUTPUT, True)
        keys.write_setting(st, keys.QUICK_PANEL, True)
        st.set("auto_indent", False)
        st.set("word_wrap", True)
        st.set("gutter", False)
        st.set("scroll_past_end", False)
        try:
            self.view.assign_syntax(keys.SYNTAX_PATH)
            st.set("color_scheme", keys.THEME_DEFAULT)
        except Exception as e:
            print("[Submarine] panel syntax: %s" % e)
        if focus:
            self.window.run_command("show_panel", {"panel": "output.%s" % panel_name})

    def apply_theme(self, backend: Optional[str] = None, quick: bool = False) -> None:
        if not self._valid():
            return
        st = self.view.settings()
        if quick or keys.read_setting(st, keys.QUICK):
            st.set("color_scheme", keys.THEME_QUICK)
            return
        backend = backend or keys.read_setting(st, keys.BACKEND) or "claude"
        if backend and backend != "claude":
            st.set("color_scheme", backend_theme(backend))
        else:
            st.set("color_scheme", keys.THEME_DEFAULT)

    def apply_output_settings(self) -> None:
        if not self.view:
            return
        if sublime is None:
            self.view.settings().set("scroll_past_end", False)
            return
        s = sublime.load_settings(keys.OUTPUT_SETTINGS)
        for key in (
            "font_size", "line_numbers", "gutter", "word_wrap", "margin",
            "draw_indent_guides", "highlight_line", "fold_buttons",
            "fade_fold_buttons",
        ):
            val = s.get(key)
            if val is not None:
                self.view.settings().set(key, val)
        self.view.settings().set("scroll_past_end", False)

    def _valid(self) -> bool:
        return bool(self.view and self.view.is_valid())

    # --- title -------------------------------------------------------------

    def set_name(self, name: str) -> None:
        name = _TITLE_ICON_RE.sub("", name or "") or "Submarine"
        self._name = name
        self.update_title()

    def update_title(self) -> None:
        """Prefix ⏸/❓/⚠/↻/◉/•/◐/○/◇/! + ABBR> + name (40-char truncate).

        MUST stay in sync with models.strip_title_decoration.
        """
        if not self._valid():
            return
        name = getattr(self, "_name", "Submarine")
        window = self.view.window()
        is_active = bool(
            window
            and keys.read_setting(window.settings(), keys.ACTIVE_VIEW) == self.view.id()
        )
        session = get_session_for_view(self.view)
        if session is not None:
            try:
                from ui.session_list import session_title as _session_title
                recovered = _session_title(session)
                if recovered:
                    name = recovered
            except Exception:
                pass
        is_sleeping = session and getattr(session, "is_sleeping", False)
        owner = self.owner
        is_questioning = bool(
            (getattr(owner, "pending_permission", None)
             and owner.pending_permission.callback)
            or (getattr(owner, "pending_question", None)
                and owner.pending_question.callback)
            or (getattr(owner, "pending_plan", None)
                and owner.pending_plan.callback)
        )
        is_working = (
            (session and getattr(session, "working", False))
            or (session and getattr(session, "_compacting", False))
            or (owner.current and owner.current.working)
        )
        turn_phase = (
            getattr(session, "turn_phase", None) if session else None
        ) or "waiting"
        is_error_halt = bool(
            session and getattr(session, "error_halted", False) and not is_working
        )
        import time as _t
        _nxt = getattr(session, "next_wake_at", None) if session else None
        is_looping = bool(_nxt and _nxt > _t.time())
        if is_sleeping:
            prefix = STATUS_SLEEPING + " "
        elif is_questioning:
            prefix = STATUS_QUESTION + " "
        elif is_error_halt:
            prefix = STATUS_ERROR_HALT + " "
        elif is_looping and not is_working:
            prefix = STATUS_WAKE + " "
        elif is_working:
            if turn_phase == "waiting":
                prefix = (STATUS_ACTIVE_WAITING if is_active else STATUS_INACTIVE_WAITING) + " "
            else:
                prefix = (STATUS_ACTIVE_WORKING if is_active else STATUS_INACTIVE_WORKING) + " "
        elif session and getattr(session, "unread", False):
            prefix = "! "
        elif session:
            try:
                ov = getattr(session, "output", None)
                if ov and ov.active_background_tools():
                    prefix = "⚙ "
                else:
                    prefix = STATUS_IDLE + " "
            except Exception:
                prefix = STATUS_IDLE + " "
        else:
            prefix = STATUS_IDLE + " "
        abbr = ""
        backend = keys.read_setting(self.view.settings(), keys.BACKEND)
        if backend:
            try:
                abbr = abbrev_for(backend)
            except Exception:
                abbr = BACKEND_ABBREV.get(backend, backend[:2].upper())
        full = format_tab_title(name, prefix, abbr)
        if self.view.name() != full:
            self.view.set_name(full)

    # --- buffer ------------------------------------------------------------

    def finish_buffer_edit(self) -> None:
        if not self._valid():
            return
        owner = self.owner
        if (getattr(owner, "_input_mode", False)
                or getattr(owner, "_question_input_mode", False)):
            self.view.set_read_only(False)
        else:
            self.view.set_read_only(True)

    def write(self, text: str, pos: Optional[int] = None) -> int:
        """Insert text. Viewless: no-op, returns 0."""
        if not self._has_view():
            return 0
        self.view.set_read_only(False)
        if pos is None:
            pos = self.view.size()
        self.view.run_command(keys.CMD_INSERT, {"pos": pos, "text": text})
        self.finish_buffer_edit()
        return pos + len(text)

    def replace(self, start: int, end: int, text: str) -> int:
        """Replace a span. Viewless: no-op, returns `end`."""
        if not self._has_view():
            return end
        self.view.set_read_only(False)
        self.view.run_command(keys.CMD_REPLACE, {
            "start": start, "end": end, "text": text,
        })
        self.finish_buffer_edit()
        return start + len(text)

    def clear_all(self) -> None:
        """Erase the buffer. Viewless: no-op."""
        if not self._has_view():
            return
        self.view.set_read_only(False)
        self.view.run_command(keys.CMD_CLEAR_ALL)
        self.view.set_read_only(True)

    # --- regions -----------------------------------------------------------

    def set_hidden_region(self, key: str, start: int, end: int) -> None:
        """Stamp a named region. Viewless: no-op."""
        if not self._has_view() or sublime is None:
            return
        flags = getattr(sublime, "HIDDEN", 0)
        self.view.add_regions(key, [sublime.Region(start, end)], "", "", flags)

    def region_span(self, key: str):
        if not self._valid():
            return None
        regs = self.view.get_regions(key)
        if regs and regs[0].size() > 0:
            return (regs[0].begin(), regs[0].end())
        return None

    def erase_region(self, key: str) -> None:
        if self._valid():
            self.view.erase_regions(key)

    # --- viewport ----------------------------------------------------------

    def is_following_tail(self, slack: int = 120) -> bool:
        if not self._valid():
            return True
        size = self.view.size()
        if size <= 0:
            return True
        vis = self.view.visible_region()
        return vis.end() + slack >= size

    def pin_view_state(self) -> dict:
        """Snapshot a *text anchor* at the viewport top + visible selection.

        Pixel-only viewport y jumps when mid-buffer lines change length.
        """
        view = self.view
        pin = {
            "anchor": 0,
            "vis_a": 0,
            "vis_b": 0,
            "y_off": 0.0,
            "x": 0.0,
            "sels": [],
            "size": 0,
            "preserve_sel": True,
        }
        if not self._valid():
            return pin
        try:
            vp = view.viewport_position()
            pin["x"] = float(vp[0])
        except Exception:
            vp = (0.0, 0.0)
        try:
            vis = view.visible_region()
            pin["vis_a"] = vis.begin()
            pin["vis_b"] = vis.end()
        except Exception:
            vis = None
        try:
            pin["size"] = view.size()
        except Exception:
            pass
        try:
            anchor = int(view.layout_to_text(vp))
        except Exception:
            anchor = pin["vis_a"]
        pin["anchor"] = anchor
        try:
            _ax, ay = view.text_to_layout(anchor)
            pin["y_off"] = float(vp[1]) - float(ay)
        except Exception:
            pass
        try:
            pin["sels"] = [(r.a, r.b) if hasattr(r, "a") else (r.begin(), r.end())
                           for r in view.sel()]
        except Exception:
            pass
        return pin

    def restore_view_state(self, pin: dict) -> None:
        """Restore viewport to the pinned text anchor; keep caret out of the tail.

        Sticky-composer carets use pin['composer_caret'] / preserve_sel so
        deferred restores do not yank the caret to viewport-top or draft EOF
        while the user is editing mid-draft during a stream.
        """
        if not self._valid() or not pin:
            return
        view = self.view
        try:
            size = view.size()
        except Exception:
            return
        anchor = max(0, min(int(pin.get("anchor") or 0), max(0, size)))
        vis_a = max(0, min(int(pin.get("vis_a") or anchor), size))
        vis_b = max(vis_a, min(int(pin.get("vis_b") or anchor), size))
        y_off = float(pin.get("y_off") or 0)
        x = float(pin.get("x") or pin.get("vx") or 0)

        def _apply_vp():
            if not view.is_valid():
                return
            try:
                _ax, ay = view.text_to_layout(anchor)
                view.set_viewport_position((x, max(0.0, float(ay) + y_off)), False)
            except Exception:
                try:
                    if sublime is not None:
                        view.show(
                            sublime.Region(anchor, anchor),
                            show_surrounds=False,
                            animate=False,
                            keep_to_left=True,
                        )
                    else:
                        view.set_viewport_position(
                            (x, float(pin.get("vy") or 0)), False)
                except Exception:
                    pass

        def _add_sel(a, b):
            if sublime is not None:
                view.sel().add(sublime.Region(a, b))
            else:
                view.sel().add(_HeadlessRegion(a, b))

        cc = pin.get("composer_caret")
        if cc is not None:
            try:
                pos = max(0, min(int(cc), size))
                view.sel().clear()
                _add_sel(pos, pos)
            except Exception:
                pass
            _apply_vp()
            return

        keep = []
        for a, b in (pin.get("sels") or []):
            lo, hi = (a, b) if a <= b else (b, a)
            if hi >= vis_a and lo <= vis_b:
                keep.append((
                    max(0, min(int(a), size)),
                    max(0, min(int(b), size)),
                ))
        if keep:
            try:
                view.sel().clear()
                for a, b in keep:
                    _add_sel(a, b)
            except Exception:
                pass
        elif pin.get("preserve_sel"):
            pass
        elif pin.get("sels"):
            try:
                view.sel().clear()
                _add_sel(anchor, anchor)
            except Exception:
                pass
        _apply_vp()

    def schedule_viewport_restore(self, pin: dict) -> None:
        """Re-apply pin after ST's post-edit scroll settles (double tick)."""
        if not pin:
            return
        self._pending_vp_pin = pin
        if sublime is None:
            return

        def _pass1():
            p = getattr(self, "_pending_vp_pin", None)
            if p is None:
                return
            self.restore_view_state(p)

            def _pass2():
                p2 = getattr(self, "_pending_vp_pin", None)
                if p2 is None:
                    return
                self.restore_view_state(p2)
                if getattr(self, "_pending_vp_pin", None) is p2:
                    self._pending_vp_pin = None

            sublime.set_timeout(_pass2, 0)

        sublime.set_timeout(_pass1, 0)

    def view_is_focused(self) -> bool:
        if not self._valid():
            return False
        try:
            win = self.view.window()
            if not win:
                return False
            av = win.active_view()
            return bool(av and av.id() == self.view.id())
        except Exception:
            return False
