"""Sticky ◎ composer at EOF. All caret policy delegates to geometry.py."""
from __future__ import annotations

from typing import Optional, Tuple

from plat.constants import BACKGROUND_PREFIX, CONTEXT_PREFIX, INPUT_MARKER

from . import keys
from .geometry import OWNER_DRAFT, OWNER_HISTORY, stream_treat_as_composing
from .session_api import get_session_for_view
from .tools import format_tool_detail

try:
    import sublime
except ImportError:
    sublime = None  # type: ignore


class Composer:
    """Sticky ◎ at EOF + pad phantom + pending-context 📎 + caret ownership.

    Viewless convention: `_has_view()` is False when detached. `_input_start`
    / `_input_area_start` are buffer offsets — `detach()` peels the draft
    into `_detached_draft` and drops them. Buffer writes, phantoms, and
    scrolling no-op while viewless; `surface_restore` re-enters input mode.
    """

    def __init__(self, owner):
        self.owner = owner
        self._input_mode = False
        self._input_start = 0
        self._input_area_start = 0
        self._input_marker = INPUT_MARKER
        self._draft_caret_off = None  # type: Optional[int]
        self._caret_owner = OWNER_DRAFT
        self._pending_caret_token = 0
        self._pending_context_region = (0, 0)
        self._question_input_mode = False
        self._question_input_start = None
        self._pad_phantom_set = None
        self._context_phantom_set = None
        self._detached_draft = ""
        self._submit_pin = None  # type: Optional[dict]

    def _has_view(self) -> bool:
        view = self.owner.view
        if not view:
            return False
        try:
            return bool(view.is_valid())
        except Exception:
            return True

    def detach(self) -> None:
        """Capture draft text and drop buffer offsets. Live flag goes idle."""
        draft = ""
        if self._has_view() and self._input_mode:
            try:
                draft = self.get_input_text()
            except Exception:
                draft = self._detached_draft or ""
        elif self._detached_draft:
            draft = self._detached_draft
        self._detached_draft = draft or ""
        self._input_mode = False
        self._input_start = 0
        self._input_area_start = 0
        self._question_input_mode = False
        self._question_input_start = None
        self._pending_context_region = (0, 0)
        self._pad_phantom_set = None
        self._context_phantom_set = None

    # --- public flags ------------------------------------------------------

    @property
    def input_mode(self) -> bool:
        return self._input_mode

    def is_input_mode(self) -> bool:
        return self._input_mode

    def is_in_input_region(self, point: int) -> bool:
        """True if point is in the composer tail. Viewless: False."""
        if not self._input_mode or not self._has_view():
            return False
        return point >= self._input_start

    def get_input_text(self) -> str:
        """Composer draft. Viewless: last detached draft if still in input mode."""
        view = self.owner.view
        if not self._input_mode:
            return self._detached_draft if not view else ""
        if not view:
            return self._detached_draft or ""
        return view.substr(_region(self._input_start, view.size()))

    def draft_end(self) -> int:
        view = self.owner.view
        if not view or not self._input_mode:
            return 0
        return view.size()

    def caret_owner(self) -> str:
        o = getattr(self, "_caret_owner", None) or OWNER_DRAFT
        return o if o in (OWNER_DRAFT, OWNER_HISTORY) else OWNER_DRAFT

    def set_caret_owner(self, owner: str) -> None:
        if owner in (OWNER_DRAFT, OWNER_HISTORY):
            self._caret_owner = owner

    def has_turn_modal_ui(self) -> bool:
        if self._question_input_mode:
            return True
        o = self.owner
        if o.pending_question and o.pending_question.callback:
            return True
        if o.pending_permission and o.pending_permission.callback:
            return True
        if o.pending_plan and o.pending_plan.callback:
            return True
        return False

    # --- enter / exit ------------------------------------------------------

    def hide_composer_for_modal(self) -> None:
        """Peel composer before a modal. Viewless: drop input-mode flag, keep draft."""
        if not self._has_view():
            if self._input_mode:
                self._detached_draft = self._detached_draft or ""
                self._input_mode = False
            return
        view = self.owner.view
        if not view or not view.is_valid():
            return
        if self._input_mode:
            try:
                session = get_session_for_view(view)
                draft = self.get_input_text()
                if session is not None and draft is not None:
                    session.draft_prompt = draft
            except Exception:
                pass
            self.exit_input_mode(keep_text=False)
        try:
            session = get_session_for_view(view)
            if session:
                self._clear_session_queue(session)
        except Exception:
            pass
        keys.write_setting(view.settings(), keys.INPUT_MODE, False)

    def enter_input_mode(self) -> None:
        """Open the ◎ composer. Viewless: no-op (restored via surface_restore)."""
        view = self.owner.view
        if not view or not view.is_valid():
            return
        if self._input_mode:
            return
        if keys.read_setting(view.settings(), keys.SLEEPING):
            return
        session = get_session_for_view(view)
        if session is not None:
            if getattr(session, "is_sleeping", False):
                return
            if not getattr(session, "_composer_allowed", True):
                return
        if self.has_turn_modal_ui():
            return
        if self.owner.current and self.owner.current.working and session and not session.working:
            self.owner.current.working = False
        if self.owner._render_pending:
            if keys.read_setting(view.settings(), keys.SLEEPING):
                return
            if session is not None and not getattr(session, "_composer_allowed", True):
                return
            if sublime is not None:
                sublime.set_timeout(self.enter_input_mode, 20)
            return

        has_pending_context = session and getattr(session, "pending_context", None)
        content = view.substr(_region(0, view.size()))
        if content:
            lines = content.split("\n")
            cleanup_start = -1
            for i in range(len(lines) - 1, max(-1, len(lines) - 6), -1):
                line = lines[i]
                is_input_marker = line.startswith(self._input_marker) and " ▶" not in line
                is_context_line = line.startswith(CONTEXT_PREFIX) and not has_pending_context
                is_bg_hint = line.strip().startswith((BACKGROUND_PREFIX, "✔ ", "✘ "))
                is_sep = bool(line.strip()) and set(line.strip()) <= set("┄─━-")
                if is_input_marker or is_context_line or is_bg_hint or is_sep:
                    cleanup_start = len("\n".join(lines[:i]))
                    if i > 0:
                        cleanup_start += 1
                    continue
                elif line.strip():
                    break
            if cleanup_start >= 0 and cleanup_start < view.size():
                view.set_read_only(False)
                view.run_command(keys.CMD_REPLACE, {
                    "start": cleanup_start, "end": view.size(), "text": "",
                })
                self._pending_context_region = (0, 0)

        view.set_read_only(False)
        if self._pending_context_region[1] > self._pending_context_region[0]:
            self.owner._replace(
                self._pending_context_region[0],
                self._pending_context_region[1], "")
            self._pending_context_region = (0, 0)
            view.set_read_only(False)

        if view.size() == 0:
            view.run_command("append", {"characters": "\n"})
        elif view.substr(view.size() - 1) != "\n":
            view.run_command("append", {"characters": "\n"})
        self._input_area_start = view.size()
        self._append_composer_prefix()
        view.run_command("append", {"characters": self._input_marker})
        self._input_start = view.size()
        self._input_mode = True
        self._detached_draft = ""
        keys.write_setting(view.settings(), keys.INPUT_MODE, True)
        self._caret_owner = OWNER_DRAFT
        self._draft_caret_off = 0
        if sublime is not None:
            view.sel().clear()
            view.sel().add(sublime.Region(self._input_start, self._input_start))
        self.collapse_empty_composer_tail()
        self._update_composer_pad_phantom()

        items = list(session.pending_context) if session and getattr(session, "pending_context", None) else []
        if sublime is not None:
            sublime.set_timeout(lambda it=items: self._refresh_context_phantoms(it), 10)
        if session:
            for meth in ("_update_permission_banner", "_update_wakeup_banner"):
                fn = getattr(session, meth, None)
                if callable(fn):
                    try:
                        fn(show=True)
                    except TypeError:
                        try:
                            fn()
                        except Exception:
                            pass
                    except Exception:
                        pass
            try:
                if hasattr(session, "_update_queue_phantom"):
                    session._update_queue_phantom()
            except Exception:
                pass
        if self._submit_pin:
            self.focus(force_show=False, steal_focus=False, preserve_caret=True)
            self.restore_submit_pin()
            self._submit_pin = None
        else:
            self.focus(force_show=True, steal_focus=False, preserve_caret=True)

    def exit_input_mode(self, keep_text: bool = False) -> str:
        """Close the composer. Viewless: clears the input-mode flag, returns draft."""
        view = self.owner.view
        if not view or not self._input_mode:
            draft = self._detached_draft if self._input_mode else ""
            self._input_mode = False
            return draft
        session = get_session_for_view(view)
        if session:
            for meth in ("_update_permission_banner", "_update_wakeup_banner"):
                fn = getattr(session, meth, None)
                if callable(fn):
                    try:
                        fn(show=False)
                    except Exception:
                        pass
            self._clear_session_queue(session)
        input_text = self.get_input_text()
        if not keep_text:
            start = getattr(self, "_input_area_start", self._input_start - len(self._input_marker))
            if start > 0:
                start -= 1
            view.run_command(keys.CMD_REPLACE, {
                "start": max(0, start), "end": view.size(), "text": "",
            })
        self._input_mode = False
        keys.write_setting(view.settings(), keys.INPUT_MODE, False)
        view.set_read_only(True)
        self._pending_context_region = (0, 0)
        self._refresh_context_phantoms([])
        self._update_composer_pad_phantom()
        if session and getattr(session, "_wakeup_armed", None) and session._wakeup_armed():
            try:
                session._update_wakeup_banner(show=True)
            except Exception:
                pass
        return input_text

    def pin_submit_line(self, pt: int) -> None:
        """Remember where the ◎ user line sits in the viewport (pre-submit)."""
        view = self.owner.view
        if not view:
            self._submit_pin = None
            return
        try:
            y = float(view.text_to_layout(int(pt))[1])
            vx, vy = view.viewport_position()
            vx, vy = float(vx), float(vy)
        except Exception:
            self._submit_pin = None
            return
        self._submit_pin = {
            "pt": int(pt),
            "y": y,
            "vx": vx,
            "vy": vy,
            "y_in_view": y - vy,
        }

    def restore_submit_pin(self) -> bool:
        """Keep the ◎ user line at the same viewport Y as pin_submit_line."""
        pin = self._submit_pin
        view = self.owner.view
        if not pin or not view:
            return False
        try:
            pt = max(0, min(int(pin.get("pt") or 0), view.size()))
            y = float(view.text_to_layout(pt)[1])
            target_vy = y - float(pin.get("y_in_view") or 0)
            view.set_viewport_position(
                (float(pin.get("vx") or 0), max(0.0, target_vy)), False)
            return True
        except Exception:
            return False

    def promote_to_prompt(self, text: str, has_context: bool = False
                          ) -> Optional[Tuple[int, int]]:
        """Turn the sticky ◎ strip into the frozen ◎ prompt ▶ line in place.

        One buffer replace from the input-area peel to EOF — the user message
        does not vanish and reappear, so its layout Y stays put. Returns the
        conversation region (including the preceding newline when there is
        one) or None if the composer was not open.
        """
        from .render_policy import format_user_prompt_block

        view = self.owner.view
        if not view or not self._input_mode:
            return None
        marker_len = len(self._input_marker)
        marker_start = int(self._input_start or 0) - marker_len
        if marker_start < 0:
            return None
        try:
            if view.substr(_region(marker_start, marker_start + marker_len)) != self._input_marker:
                return None
        except Exception:
            return None

        self.pin_submit_line(marker_start)
        peel = self.peel_start()
        if peel is None:
            peel = marker_start
        start = peel
        if start > 0:
            try:
                if view.substr(_region(start - 1, start)) == "\n":
                    start -= 1
            except Exception:
                pass
        body = format_user_prompt_block(text, has_context, CONTEXT_PREFIX)
        if start < peel:
            body = "\n" + body
        view.set_read_only(False)
        view.run_command(keys.CMD_REPLACE, {
            "start": max(0, start), "end": view.size(), "text": body,
        })
        session = get_session_for_view(view)
        if session:
            for meth in ("_update_permission_banner", "_update_wakeup_banner"):
                fn = getattr(session, meth, None)
                if callable(fn):
                    try:
                        fn(show=False)
                    except Exception:
                        pass
            self._clear_session_queue(session)
        self._input_mode = False
        self._input_start = 0
        self._input_area_start = 0
        self._detached_draft = ""
        keys.write_setting(view.settings(), keys.INPUT_MODE, False)
        view.set_read_only(True)
        self._pending_context_region = (0, 0)
        self._refresh_context_phantoms([])
        self._update_composer_pad_phantom()
        # ◎ in the written block: after optional leading newline
        new_pt = start + (1 if start < peel else 0)
        if self._submit_pin is not None:
            self._submit_pin["pt"] = new_pt
        self.restore_submit_pin()
        return (start, start + len(body))

    def reset_input_mode(self, reenter: bool = False) -> None:
        """Strip composer chrome. Viewless: drop input-mode flag and offsets."""
        view = self.owner.view
        if not view:
            self._input_mode = False
            self._input_start = 0
            self._input_area_start = 0
            return
        try:
            if self._pad_phantom_set is not None:
                self._pad_phantom_set.update([])
            if view.is_valid() and hasattr(view, "erase_phantoms"):
                view.erase_phantoms(keys.PHANTOM_PAD)
                view.erase_phantoms("claude_composer_pad")
        except Exception:
            pass
        try:
            session = get_session_for_view(view)
            self._clear_session_queue(session)
        except Exception:
            pass
        Composer.strip_composer_tail(view)
        Composer.collapse_trailing_blank_lines(view, keep=1)
        self._input_mode = False
        self._input_start = 0
        self._input_area_start = 0
        keys.write_setting(view.settings(), keys.INPUT_MODE, False)
        view.set_read_only(True)
        self._pending_context_region = (0, 0)
        if reenter and not keys.read_setting(view.settings(), keys.SLEEPING):
            if sublime is not None:
                sublime.set_timeout(self.enter_input_mode, 10)

    def set_composer_text(self, text: str) -> None:
        """Replace draft body. Viewless: stashes text on `_detached_draft`."""
        view = self.owner.view
        if not view or not view.is_valid() or not self._input_mode:
            if self._input_mode or not view:
                self._detached_draft = text or ""
            return
        body = text or ""
        if not body.strip():
            body = ""
        view.set_read_only(False)
        view.run_command(keys.CMD_REPLACE, {
            "start": self._input_start, "end": view.size(), "text": body,
        })
        caret = self._input_start + len(body)
        if sublime is not None:
            view.sel().clear()
            view.sel().add(sublime.Region(caret, caret))
        self._caret_owner = OWNER_DRAFT
        self._draft_caret_off = len(body)
        self.collapse_empty_composer_tail()
        self._update_composer_pad_phantom()

    # --- caret / scroll ----------------------------------------------------

    def note_draft_caret(self) -> None:
        view = self.owner.view
        if not view or not view.is_valid() or not self._input_mode:
            return
        if self._question_input_mode:
            return
        try:
            start = int(self._input_start or 0)
            size = view.size()
            sel = view.sel()
            if not sel:
                return
            b = sel[0].begin()
            if start <= b <= size:
                self._draft_caret_off = b - start
                self._caret_owner = OWNER_DRAFT
        except Exception:
            pass

    def restore_draft_caret(self, force: bool = False) -> bool:
        view = self.owner.view
        if not view or not view.is_valid() or not self._input_mode:
            return False
        if not force and self.caret_owner() == OWNER_HISTORY:
            return False
        off = self._draft_caret_off
        if off is None:
            return False
        try:
            start = int(self._input_start or 0)
            size = view.size()
            if not force:
                sel = view.sel()
                if sel and sel[0].begin() < start:
                    return False
            max_off = max(0, size - start)
            pos = start + max(0, min(int(off), max_off))
            if sublime is not None:
                view.sel().clear()
                view.sel().add(sublime.Region(pos, pos))
            return True
        except Exception:
            return False

    def park(self, mode: str = "end") -> None:
        view = self.owner.view
        if not view or not view.is_valid() or not self._input_mode:
            return
        if self._question_input_mode:
            return
        try:
            view.set_read_only(False)
            start = int(self._input_start or 0)
            size = view.size()
            pos = start if mode == "start" else size
            if sublime is not None:
                view.sel().clear()
                view.sel().add(sublime.Region(pos, pos))
            self._draft_caret_off = max(0, pos - start)
            self._caret_owner = OWNER_DRAFT
        except Exception:
            pass

    park_composer_caret = park

    def focus(
            self, force_show: bool = True, steal_focus: bool = False,
            preserve_caret: bool = True, park_at_end: bool = False) -> None:
        view = self.owner.view
        if not view or not view.is_valid() or not self._input_mode:
            return
        try:
            if steal_focus:
                win = view.window()
                if win:
                    win.focus_view(view)
            view.set_read_only(False)
            if self._question_input_mode:
                self._update_composer_pad_phantom()
                caret = view.size()
                if park_at_end or not preserve_caret:
                    if sublime is not None:
                        view.sel().clear()
                        view.sel().add(sublime.Region(caret, caret))
                if force_show and self.owner.sheet.view_is_focused():
                    try:
                        view.show(view.sel()[0].begin() if view.sel() else caret)
                    except Exception:
                        pass
                return
            explicit_park = bool(park_at_end or not preserve_caret)
            history = (not explicit_park) and self.caret_owner() == OWNER_HISTORY
            if history:
                return
            saved_off = None
            try:
                start = int(self._input_start or 0)
                size0 = view.size()
                sel = view.sel()
                if sel:
                    b = sel[0].begin()
                    if start <= b <= size0:
                        saved_off = b - start
            except Exception:
                saved_off = None
            self.collapse_empty_composer_tail()
            self._update_composer_pad_phantom()
            start = int(self._input_start or 0)
            size = view.size()
            if explicit_park:
                if sublime is not None:
                    view.sel().clear()
                    view.sel().add(sublime.Region(size, size))
                self._draft_caret_off = max(0, size - start)
                self._caret_owner = OWNER_DRAFT
            else:
                restored = False
                try:
                    sel = view.sel()
                    if sel:
                        b = sel[0].begin()
                        if start <= b <= size:
                            restored = True
                            self._draft_caret_off = b - start
                            self._caret_owner = OWNER_DRAFT
                except Exception:
                    pass
                if not restored and saved_off is not None and sublime is not None:
                    pos = min(start + max(0, saved_off), size)
                    view.sel().clear()
                    view.sel().add(sublime.Region(pos, pos))
                    self._draft_caret_off = saved_off
                    self._caret_owner = OWNER_DRAFT
                elif not restored:
                    if self.caret_owner() == OWNER_DRAFT:
                        self.restore_draft_caret()
            if force_show:
                self._scroll_layout_to_bottom(
                    force=True, reapply_caret=(self.caret_owner() == OWNER_DRAFT))
                if self.caret_owner() == OWNER_DRAFT:
                    self.restore_draft_caret()
                    # One coalesced deferred reapply (not stacked 16+80 thrash)
                    if self.owner.sheet.view_is_focused() and sublime is not None:
                        tok = int(getattr(self, "_pending_caret_token", 0) or 0) + 1
                        self._pending_caret_token = tok

                        def _once(t=tok):
                            if getattr(self, "_pending_caret_token", 0) != t:
                                return
                            if not self.owner.sheet.view_is_focused():
                                return
                            if self.caret_owner() != OWNER_DRAFT:
                                return
                            self._scroll_layout_to_bottom(
                                force=True, reapply_caret=True)
                            self.restore_draft_caret()

                        sublime.set_timeout(_once, 30)
        except Exception:
            pass

    focus_composer = focus

    def scroll_composer_chrome(self, force: bool = False) -> None:
        self._scroll_layout_to_bottom(force=force, reapply_caret=False)

    def _draft_caret_is_mid(self) -> bool:
        view = self.owner.view
        if not view or not self._input_mode:
            return False
        try:
            start = int(self._input_start or 0)
            size = view.size()
            sel = view.sel()
            if not sel:
                off = self._draft_caret_off
                return off is not None and off < max(0, size - start)
            b = sel[0].begin()
            if b < start or b > size:
                return False
            return b < size
        except Exception:
            return False

    def _scroll_layout_to_bottom(self, force: bool = False, reapply_caret: bool = True) -> None:
        view = self.owner.view
        if not view or not view.is_valid():
            return
        history = self.caret_owner() == OWNER_HISTORY
        mid = (not history) and self._draft_caret_is_mid()
        if self._input_mode and mid:
            self.note_draft_caret()
        try:
            end = view.size()
            y = float(view.text_to_layout(end)[1]) if end > 0 else 0.0
            line_h = float(view.line_height() or 16.0)
            y_bottom = y + line_h + 6.0
            try:
                layout_h = float(view.layout_extent()[1])
                if y_bottom <= layout_h <= y_bottom + line_h * 2.5:
                    y_bottom = layout_h
            except Exception:
                pass
            vh = float(view.viewport_extent()[1])
            vx, vy = view.viewport_position()
            vx, vy = float(vx), float(vy)
            target_y = max(0.0, y_bottom - vh)
            if not force and abs(vy - target_y) < max(2.0, line_h * 0.35):
                if reapply_caret and mid and not history:
                    self.restore_draft_caret()
                return
            view.set_viewport_position((vx, target_y), False)
            if reapply_caret and not history:
                self.restore_draft_caret()
                return
            if history or self._input_mode:
                return
            if not self.owner.sheet.view_is_focused():
                return
            if sublime is not None and (force or abs(float(view.viewport_position()[1]) - target_y) > line_h * 0.5):
                try:
                    view.show(sublime.Region(end, end), False)
                except Exception:
                    pass
                view.set_viewport_position((vx, target_y), False)
        except Exception:
            if reapply_caret and mid and not history:
                self.restore_draft_caret()

    def scroll_to_end(self, force: bool = False) -> None:
        view = self.owner.view
        if not view or not view.is_valid():
            return
        if self._input_mode:
            hist = self.caret_owner() == OWNER_HISTORY
            self._scroll_layout_to_bottom(
                force=bool(force), reapply_caret=not hist)
            return
        if not force and not self.owner.sheet.is_following_tail():
            return
        try:
            end = view.size()
            if end <= 0:
                return
            _x, y = view.text_to_layout(end)
            vh = float(view.viewport_extent()[1])
            vx, _vy = view.viewport_position()
            target_y = max(0.0, float(y) + float(view.line_height() or 16) - vh)
            view.set_viewport_position((float(vx), target_y), False)
        except Exception:
            pass

    # --- peel / pad / prefix -----------------------------------------------

    def peel_start(self) -> Optional[int]:
        view = self.owner.view
        if not self._input_mode or not view:
            return None
        peel = getattr(self, "_input_area_start", None)
        if peel is None and self._input_start:
            peel = max(0, self._input_start - len(self._input_marker))
        if peel is None or peel < 0:
            return None
        return min(peel, view.size())

    def input_marker_intact(self) -> bool:
        view = self.owner.view
        if not view or not self._input_mode:
            return False
        ms = self._input_start - len(self._input_marker)
        if ms < 0 or self._input_start > view.size():
            return False
        return view.substr(_region(ms, self._input_start)) == self._input_marker

    def shift_anchors(self, delta: int) -> None:
        if delta == 0:
            return
        if getattr(self, "_input_area_start", None) is not None:
            self._input_area_start = max(0, self._input_area_start + delta)
        if self._input_start is not None:
            self._input_start = max(0, self._input_start + delta)
        pca, pcb = self._pending_context_region
        if pcb > pca:
            self._pending_context_region = (max(0, pca + delta), max(0, pcb + delta))

    def _append_composer_prefix(self) -> None:
        view = self.owner.view
        if not view:
            return
        for bt in self.owner.active_background_tools():
            detail = format_tool_detail(self.owner, bt)
            view.run_command("append", {
                "characters": "  %s%s%s\n" % (BACKGROUND_PREFIX, bt.name, detail),
            })
        session = get_session_for_view(view)
        if session and getattr(session, "pending_context", None):
            ctx_start = view.size()
            view.run_command("append", {"characters": "%s\n" % CONTEXT_PREFIX})
            self._pending_context_region = (ctx_start, view.size())
        else:
            self._pending_context_region = (0, 0)

    def _composer_prefix_text(self) -> str:
        parts = []
        for bt in self.owner.active_background_tools():
            detail = format_tool_detail(self.owner, bt)
            parts.append("  %s%s%s\n" % (BACKGROUND_PREFIX, bt.name, detail))
        view = self.owner.view
        session = get_session_for_view(view) if view else None
        if session and getattr(session, "pending_context", None):
            parts.append("%s\n" % CONTEXT_PREFIX)
        return "".join(parts)

    def refresh_background_hints(self) -> None:
        view = self.owner.view
        if not view or not view.is_valid() or not self._input_mode:
            return
        if not self.input_marker_intact():
            return
        peel = self.peel_start()
        if peel is None:
            return
        marker_start = self._input_start - len(self._input_marker)
        if marker_start < peel or marker_start > view.size():
            return
        draft = self.get_input_text()
        caret_off = 0
        try:
            sel = view.sel()
            if sel and self._input_start is not None:
                b = sel[0].begin()
                if b >= self._input_start:
                    caret_off = max(0, min(b - self._input_start, len(draft)))
        except Exception:
            caret_off = int(self._draft_caret_off or 0)
        new_prefix = self._composer_prefix_text()
        old = view.substr(_region(peel, marker_start))
        if old == new_prefix:
            return
        was_ro = view.is_read_only()
        view.set_read_only(False)
        try:
            view.run_command(keys.CMD_REPLACE, {
                "start": peel, "end": marker_start, "text": new_prefix,
            })
        finally:
            if was_ro and not self._input_mode:
                view.set_read_only(True)
        delta = len(new_prefix) - (marker_start - peel)
        self._input_area_start = peel
        self._input_start = marker_start + delta + len(self._input_marker)
        if new_prefix.endswith("%s\n" % CONTEXT_PREFIX):
            ctx_end = peel + len(new_prefix)
            ctx_start = ctx_end - len("%s\n" % CONTEXT_PREFIX)
            self._pending_context_region = (ctx_start, ctx_end)
        else:
            self._pending_context_region = (0, 0)
        pos = min(self._input_start + caret_off, view.size())
        try:
            if sublime is not None:
                view.sel().clear()
                view.sel().add(sublime.Region(pos, pos))
            self._draft_caret_off = caret_off
        except Exception:
            pass
        view.set_read_only(False)
        try:
            self._update_composer_pad_phantom()
        except Exception:
            pass
        try:
            session = get_session_for_view(view)
            items = list(session.pending_context) if session and getattr(session, "pending_context", None) else []
            if sublime is not None:
                sublime.set_timeout(lambda it=items: self._refresh_context_phantoms(it), 10)
            if session and hasattr(session, "_update_queue_phantom") and sublime is not None:
                sublime.set_timeout(session._update_queue_phantom, 15)
        except Exception:
            pass

    def ensure_composer_spare_line(self) -> None:
        """Re-pin hairline under ◎ (no blank buffer rows)."""
        self.collapse_empty_composer_tail()
        self._update_composer_pad_phantom()

    def _composer_pad_width_px(self) -> int:
        """Viewport width in px for pad — never wider than the pane."""
        view = self.owner.view
        try:
            vw = float(view.viewport_extent()[0])
            return max(80, int(vw) - 8)
        except Exception:
            return 400

    def collapse_empty_composer_tail(self) -> None:
        view = self.owner.view
        if not view or not view.is_valid() or not self._input_mode:
            return
        if self._question_input_mode:
            return
        try:
            start = int(self._input_start or 0)
            end = view.size()
            if end <= start:
                return
            raw = view.substr(_region(start, end))
            if raw.strip():
                return
            view.set_read_only(False)
            view.run_command(keys.CMD_REPLACE, {
                "start": start, "end": end, "text": "",
            })
            view.set_read_only(False)
            if sublime is not None:
                view.sel().clear()
                view.sel().add(sublime.Region(start, start))
        except Exception:
            pass

    @staticmethod
    def collapse_trailing_blank_lines(view, keep: int = 1) -> bool:
        if not view or not view.is_valid():
            return False
        try:
            keep = max(0, min(int(keep), 3))
            size = view.size()
            if size <= 0:
                return False
            content = view.substr(_region(0, size))
            stripped = content.rstrip("\n")
            if not stripped:
                return False
            target_tail = "\n" * keep
            new_size = len(stripped) + len(target_tail)
            if new_size == size and content.endswith(target_tail):
                return False
            view.set_read_only(False)
            view.run_command(keys.CMD_REPLACE, {
                "start": len(stripped), "end": size, "text": target_tail,
            })
            view.set_read_only(True)
            return True
        except Exception as e:
            print("[Submarine] collapse_trailing_blank_lines: %s" % e)
            return False

    def _composer_last_line_pt(self) -> int:
        view = self.owner.view
        end = view.size()
        if end <= 0:
            return 0
        try:
            start = int(getattr(self, "_input_start", 0) or 0)
            start = max(0, min(start, end))
            last = view.line(end)
            if last.begin() >= start:
                return last.begin()
            return max(start - 1, 0) if start > 0 else 0
        except Exception:
            return max(0, end - 1)

    def _update_composer_pad_phantom(self) -> None:
        view = self.owner.view
        if not view or not view.is_valid() or sublime is None:
            return
        try:
            if not self._input_mode or self._question_input_mode:
                if self._pad_phantom_set is not None:
                    self._pad_phantom_set.update([])
                return
            if self._pad_phantom_set is None:
                self._pad_phantom_set = sublime.PhantomSet(view, keys.PHANTOM_PAD)
            pt = self._composer_last_line_pt()
            w = self._composer_pad_width_px()
            html = (
                '<body id="submarine-pad" style="margin:0;padding:0;">'
                '<div style="margin:2px 0 0 0;padding:0;line-height:1;'
                'font-size:1px;height:0;max-width:%dpx;width:%dpx;'
                'border-top:1px solid '
                'color(var(--foreground) alpha(0.12));">&nbsp;</div></body>'
                % (w, w)
            )
            layout = getattr(sublime, "LAYOUT_BELOW", None) or sublime.LAYOUT_INLINE
            self._pad_phantom_set.update([
                sublime.Phantom(
                    sublime.Region(pt, pt), html, layout,
                    on_navigate=self._on_pad_navigate,
                )
            ])
        except Exception as e:
            print("[Submarine] composer pad: %s" % e)

    def _on_pad_navigate(self, href: str) -> None:
        try:
            view = self.owner.view
            if self._input_mode and view and view.is_valid():
                view.set_read_only(False)
                self.set_caret_owner(OWNER_DRAFT)
                self.park("end")
                win = view.window()
                if win:
                    win.focus_view(view)
                self.scroll_composer_chrome(force=True)
        except Exception as e:
            print("[Submarine] pad click: %s" % e)

    # --- pending context ---------------------------------------------------

    def set_pending_context(self, context_items: list) -> None:
        """Show 📎 chips. Viewless: no-op."""
        view = self.owner.view
        if not view or not view.is_valid():
            return
        if self._input_mode:
            input_text = self.get_input_text()
            session = get_session_for_view(view)
            if session:
                session.draft_prompt = ""
            self.exit_input_mode(keep_text=False)
            self.enter_input_mode()
            if input_text:
                view.run_command("append", {"characters": input_text})
                if sublime is not None:
                    end = view.size()
                    view.sel().clear()
                    view.sel().add(sublime.Region(end, end))
            return
        start, end = self._pending_context_region
        if end > start:
            self.owner._replace(start, end, "")
        if not context_items:
            self._pending_context_region = (0, 0)
            self._refresh_context_phantoms([])
            return
        text = "\n%s\n" % CONTEXT_PREFIX
        start = view.size()
        end = self.owner._write(text)
        self._pending_context_region = (start, end)
        self.scroll_to_end()
        if sublime is not None:
            sublime.set_timeout(
                lambda items=list(context_items): self._refresh_context_phantoms(items),
                10)

    def _refresh_context_phantoms(self, context_items: list) -> None:
        from features.context import chip_ref

        view = self.owner.view
        if not view or not view.is_valid() or sublime is None:
            return
        try:
            if (self._context_phantom_set is None
                    or getattr(self, "_context_phantom_view_id", None) != view.id()):
                self._context_phantom_set = sublime.PhantomSet(view, keys.PHANTOM_CONTEXT)
                self._context_phantom_view_id = view.id()
        except Exception:
            return
        if not context_items:
            try:
                self._context_phantom_set.update([])
            except Exception:
                pass
            return
        start, end = self._pending_context_region
        if end <= start:
            return
        # Find 📎 in the pending-context span
        content = view.substr(_region(start, end))
        idx = content.find(CONTEXT_PREFIX.strip())
        if idx < 0:
            return
        pt = start + idx + len(CONTEXT_PREFIX.strip())
        chips = []
        for i, item in enumerate(context_items):
            ref = chip_ref(item)
            what = "open" if ref["action"] == "open" else "reveal"
            tip = "click to %s; ctrl/cmd+click to remove" % what
            chips.append(
                '<a href="open:%d" title="%s">%s</a>'
                % (i, _html_escape(tip), _html_escape(str(ref["name"])))
            )
        chips.append(
            '<a href="clear" title="clear all context" '
            'style="color:color(var(--foreground) alpha(0.55))">clear</a>'
        )
        html = (
            '<body id="submarine-ctx" style="margin:0;padding:0 0 0 6px;'
            'font-size:11px;">%s</body>' % " · ".join(chips)
        )

        def _nav(href, items=list(context_items)):
            self.owner._handle_context_href(href)

        try:
            self._context_phantom_set.update([
                sublime.Phantom(sublime.Region(pt, pt), html, sublime.LAYOUT_INLINE, _nav)
            ])
        except Exception:
            pass

    # --- static restore helper --------------------------------------------

    @staticmethod
    def strip_composer_tail(view) -> bool:
        if not view or not view.is_valid():
            return False
        try:
            keys.write_setting(view.settings(), keys.INPUT_MODE, False)
        except Exception:
            pass
        content = view.substr(_region(0, view.size()))
        if not content:
            return False
        lines = content.split("\n")
        cleanup_start = -1
        marker = INPUT_MARKER
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            stripped = line.strip()
            is_input_marker = (
                (line.startswith(marker)
                 or stripped.startswith("◎")
                 or stripped.startswith("\u25ce"))
                and " ▶" not in line
            )
            is_context_line = line.startswith(CONTEXT_PREFIX)
            is_bg_hint = stripped.startswith((BACKGROUND_PREFIX, "✔ ", "✘ "))
            if is_input_marker or is_context_line or is_bg_hint:
                cleanup_start = len("\n".join(lines[:i]))
                if i > 0:
                    cleanup_start += 1
                continue
            elif stripped:
                break
        if cleanup_start < 0 or cleanup_start >= view.size():
            return False
        try:
            view.set_read_only(False)
            view.run_command(keys.CMD_REPLACE, {
                "start": cleanup_start, "end": view.size(), "text": "",
            })
            view.set_read_only(True)
            return True
        except Exception as e:
            print("[Submarine] strip_composer_tail: %s" % e)
            return False

    @staticmethod
    def _clear_session_queue(session) -> None:
        if not session:
            return
        if hasattr(session, "_clear_queue_phantom"):
            try:
                session._clear_queue_phantom()
                return
            except Exception:
                pass
        ps = getattr(session, "_queue_phantom_set", None)
        if ps:
            try:
                ps.update([])
            except Exception:
                pass


def _region(a, b):
    if sublime is not None:
        return sublime.Region(a, b)
    return type("R", (), {"a": a, "b": b})()


def _html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
