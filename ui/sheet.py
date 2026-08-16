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


class OutputSheet:
    """View lifecycle + named-region helpers + buffer mutations."""

    def __init__(self, owner, window):
        self.owner = owner
        self.window = window
        self.view = None
        self._name = "Submarine"
        self._panel_name = None

    # --- lifecycle ---------------------------------------------------------

    def show(self, focus: bool = True, panel: Optional[str] = None) -> None:
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
        prev = self.window.active_view()
        self.view = self.window.new_file()
        self.view.set_name("Submarine")
        self.view.set_scratch(True)
        self.view.set_read_only(True)
        st = self.view.settings()
        keys.write_setting(st, keys.OUTPUT, True)
        st.set("auto_indent", False)
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
        """Prefix ⏸/❓/⚠/↻/◉/•/◐/○/◇/* + ABBR> + 24-char truncate.

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
            prefix = "* "
        else:
            prefix = STATUS_IDLE + " "
        backend = keys.read_setting(self.view.settings(), keys.BACKEND)
        if backend:
            try:
                abbr = abbrev_for(backend)
            except Exception:
                abbr = BACKEND_ABBREV.get(backend, backend[:2].upper())
            if abbr:
                name = "%s> %s" % (abbr, name)
        if len(name) > 24:
            name = name[:23] + "…"
        full = "%s%s" % (prefix, name)
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
        if not self._valid():
            return 0
        self.view.set_read_only(False)
        if pos is None:
            pos = self.view.size()
        self.view.run_command(keys.CMD_INSERT, {"pos": pos, "text": text})
        self.finish_buffer_edit()
        return pos + len(text)

    def replace(self, start: int, end: int, text: str) -> int:
        if not self._valid():
            return end
        self.view.set_read_only(False)
        self.view.run_command(keys.CMD_REPLACE, {
            "start": start, "end": end, "text": text,
        })
        self.finish_buffer_edit()
        return start + len(text)

    def clear_all(self) -> None:
        if not self._valid():
            return
        self.view.set_read_only(False)
        self.view.run_command(keys.CMD_CLEAR_ALL)
        self.view.set_read_only(True)

    # --- regions -----------------------------------------------------------

    def set_hidden_region(self, key: str, start: int, end: int) -> None:
        if not self._valid() or sublime is None:
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
        view = self.view
        pin = {
            "sels": [],
            "preserve_sel": True,
            "vx": 0.0, "vy": 0.0,
            "anchor_pt": 0,
            "anchor_off": 0,
        }
        if not self._valid():
            return pin
        try:
            vx, vy = view.viewport_position()
            pin["vx"], pin["vy"] = float(vx), float(vy)
        except Exception:
            pass
        try:
            vis = view.visible_region()
            pin["anchor_pt"] = vis.begin()
            pin["anchor_off"] = 0
        except Exception:
            pass
        try:
            pin["sels"] = [(r.begin(), r.end()) for r in view.sel()]
        except Exception:
            pass
        return pin

    def restore_view_state(self, pin: dict) -> None:
        if not self._valid() or not pin:
            return
        view = self.view
        try:
            if pin.get("preserve_sel") and pin.get("sels"):
                if sublime is not None:
                    view.sel().clear()
                    for a, b in pin["sels"]:
                        view.sel().add(sublime.Region(int(a), int(b)))
            if pin.get("composer_caret") is not None and sublime is not None:
                pos = int(pin["composer_caret"])
                view.sel().clear()
                view.sel().add(sublime.Region(pos, pos))
        except Exception:
            pass
        try:
            view.set_viewport_position(
                (float(pin.get("vx", 0)), float(pin.get("vy", 0))), False)
        except Exception:
            pass

    def schedule_viewport_restore(self, pin: dict) -> None:
        if sublime is None or not pin:
            return
        view = self.view

        def _pass(p=pin, v=view):
            if v and v.is_valid():
                try:
                    v.set_viewport_position(
                        (float(p.get("vx", 0)), float(p.get("vy", 0))), False)
                except Exception:
                    pass

        sublime.set_timeout(_pass, 0)
        sublime.set_timeout(_pass, 16)

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
