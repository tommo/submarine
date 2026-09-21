"""TurnRenderer: conversations + current, incremental-append projection."""
from __future__ import annotations

import os
import re
from typing import List, Optional

from plat.constants import (
    CONTEXT_PREFIX,
    SPINNER_FRAMES,
    SPINNER_RESPONDING,
    SPINNER_WAITING,
)

from . import keys
from .geometry import should_pin_view_state, stream_treat_as_composing
from .models import (
    ArtifactCard,
    BACKGROUND,
    Conversation,
    DONE,
    ERROR,
    GoalState,
    HISTORY_CAP,
    PENDING,
    ToolCall,
    _goal_is_open,
    _open_todos,
    _todo_is_active,
    format_goal_strip_line,
)
from .render_policy import (
    cap_history,
    format_user_prompt_block,
    should_incremental_append,
    tasks_fold_rows,
    text_events_joined,
)
from .session_api import get_session_for_view
from .tools import (
    SYMBOLS,
    apply_tool_side_effects,
    format_tool_row,
    is_host_control_tool,
    may_background,
    same_ask_payload,
    same_modal_tool,
    stash_media_path,
    sync_todos_from_task_result,
)

try:
    import sublime
except ImportError:
    sublime = None  # type: ignore


class TurnRenderer:
    """Owns conversations (cap 20) + current and the buffer projection.

    Viewless convention: `_has_view()` is False when detached. Public turn
    methods always update `conversations` / `current` / projection flags;
    buffer writes, named regions, phantoms, and scrolling run only while
    bound. `Conversation.region` is None while detached (stale offsets are
    never read); `repaint_from_state()` recomputes them on attach.
    """

    def __init__(self, owner):
        self.owner = owner
        self.conversations = []  # type: List[Conversation]
        self.current = None  # type: Optional[Conversation]
        self._render_pending = False
        self._auto_scroll = True
        self._spinner_frame = 0
        self._spinner_frames = SPINNER_FRAMES
        self._retry_hint = None  # type: Optional[str]
        self._cleared_content = None  # type: Optional[str]
        self._struct_dirty = False
        self._proj_event_count = 0
        self._proj_joined_text = ""
        self._proj_events_end = None  # type: Optional[int]
        self._media_phantom_set = None
        self._media_uri_cache = {}
        self._media_anchor = {}
        self._turn_context_phantom_set = None
        self._artifact_phantom_set = None
        self._tasks_expanded = False
        self._region_stash = None  # type: Optional[tuple]
        self._dirty = 0
        self._journal = []  # type: list
        self._detach_snap = None  # type: Optional[dict]
        self._replaying = False
        self._last_catch_up_n = 0
        self._MEDIA_SOURCE_MAX_BYTES = 8_000_000
        self._MEDIA_PHANTOM_MAX_W = 96
        self._MEDIA_POPUP_MAX_W = 360
        self._MINIHTML_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif")

    def _has_view(self) -> bool:
        view = self.owner.view
        if not view:
            return False
        try:
            return bool(view.is_valid())
        except Exception:
            return True

    def invalidate_regions(self) -> None:
        """Drop buffer offsets so detached paths cannot read stale spans.

        Stashes the previous tuples for same-view rebind (QuickHost); a
        full `repaint_from_state()` overwrites them after a host swap.
        """
        conv_regs = []
        for conv in self.conversations:
            conv_regs.append(conv.region)
            conv.region = None
        cur = self.current.region if self.current is not None else None
        if self.current is not None:
            self.current.region = None
        self._region_stash = (conv_regs, cur)
        self._proj_events_end = None

    def restore_stashed_regions(self) -> None:
        stash = self._region_stash
        if not stash:
            return
        conv_regs, cur = stash
        for conv, reg in zip(self.conversations, conv_regs):
            if conv.region is None:
                conv.region = reg
        if self.current is not None and self.current.region is None:
            self.current.region = cur
        self._region_stash = None

    def mark_chrome_dirty(self) -> None:
        """Bump the detach dirty counter without journaling (modals / queue)."""
        self._dirty += 1

    def snapshot_detach(self) -> None:
        """Snapshot renderer state at detach so a dirty rebind can rewind."""
        self._detach_snap = {
            "conversations": [_clone_conv(c) for c in self.conversations],
            "current": _clone_conv(self.current),
            "proj_event_count": self._proj_event_count,
            "proj_joined_text": self._proj_joined_text,
            "proj_events_end": self._proj_events_end,
            "struct_dirty": self._struct_dirty,
            "retry_hint": self._retry_hint,
            "spinner_frame": self._spinner_frame,
            "dirty": self._dirty,
        }
        self._journal = []

    def restore_detach_projection(self) -> None:
        """Re-apply projection cursors saved at detach (clean-swap helper)."""
        cp = self._detach_snap
        if not cp:
            return
        self._proj_event_count = cp["proj_event_count"]
        self._proj_joined_text = cp["proj_joined_text"]
        self._proj_events_end = cp["proj_events_end"]
        self._struct_dirty = cp["struct_dirty"]
        self._retry_hint = cp.get("retry_hint")
        self._spinner_frame = cp.get("spinner_frame", self._spinner_frame)

    def stamp_live_regions(self) -> None:
        """Re-stamp CONV_REGION from current.region after a snapshot restore."""
        if not self.current or not self.current.region:
            return
        start, end = self.current.region
        self.owner.sheet.set_hidden_region(keys.CONV_REGION, start, end)

    def state_matches_detach_snap(self) -> bool:
        """True when conversations/current still match the detach snapshot."""
        cp = self._detach_snap
        if not cp:
            return False
        if len(self.conversations) != len(cp["conversations"]):
            return False
        live_cur, snap_cur = self.current, cp["current"]
        if (live_cur is None) != (snap_cur is None):
            return False
        if live_cur is None:
            return True
        if live_cur.prompt != snap_cur.prompt:
            return False
        if len(live_cur.events) != len(snap_cur.events):
            return False
        if bool(live_cur.has_meta) != bool(snap_cur.has_meta):
            return False
        if text_events_joined(live_cur.events) != text_events_joined(snap_cur.events):
            return False
        return True

    def catch_up_events(self, entries) -> bool:
        """Replay journaled events through the bound incremental path.

        Rewinds to the detach snapshot, then re-invokes the original
        renderer methods so live rendering (not `repaint_from_state`)
        paints the gap. Returns False when the journal cannot cover it.
        """
        entries = list(entries or [])
        if not entries:
            self._last_catch_up_n = 0
            return True
        if self._detach_snap is None:
            return False
        for item in entries:
            if not item or item[0] not in _REPLAYABLE:
                return False
        live_convs = self.conversations
        live_cur = self.current
        live_proj = (
            self._proj_event_count,
            self._proj_joined_text,
            self._proj_events_end,
            self._struct_dirty,
            self._retry_hint,
            self._spinner_frame,
        )
        self._last_catch_up_n = 0
        try:
            self._restore_detach_snap()
            self._replaying = True
            self._render_pending = False
            for kind, args, kwargs in entries:
                fn = getattr(self, kind)
                fn(*args, **(kwargs or {}))
                self._last_catch_up_n += 1
            self._journal = []
            return True
        except Exception:
            self.conversations = live_convs
            self.current = live_cur
            (self._proj_event_count, self._proj_joined_text,
             self._proj_events_end, self._struct_dirty,
             self._retry_hint, self._spinner_frame) = live_proj
            self._last_catch_up_n = 0
            return False
        finally:
            self._replaying = False

    def _restore_detach_snap(self) -> None:
        """Replace live conversations/current with clones of the detach snapshot."""
        cp = self._detach_snap or {}
        self.conversations = [_clone_conv(c) for c in cp.get("conversations") or []]
        self.current = _clone_conv(cp.get("current"))
        self.restore_detach_projection()
        self._render_pending = False

    def _mark_buffer_dirty(self, kind, args=(), kwargs=None) -> None:
        """Bump dirty; journal the call while viewless so catch-up can replay it."""
        self._dirty += 1
        if self._replaying:
            return
        if not self._has_view():
            self._journal.append((kind, tuple(args), dict(kwargs or {})))

    # --- turn API ----------------------------------------------------------

    def begin_continued(self):
        """Open a live sheet after @done without wiping the last turn.

        _do_render replaces the conversation region. Assigning a new
        Conversation onto `current` leaves that region covering the
        finished @done sheet, so the next spinner/tool render erases it.
        `prompt()` freezes the old region and tracks only the new turn.
        """
        cur = self.current
        if cur is not None and not getattr(cur, "has_meta", False):
            cur.working = True
            try:
                self.owner.refresh_tab_title()
            except Exception:
                pass
            return
        self.prompt("(continued)")

    def prompt(self, text, context_names=None, context_refs=None):
        """Start a user turn. Viewless: records conversation, no buffer write."""
        self._mark_buffer_dirty(
            "prompt", (text,),
            {"context_names": context_names, "context_refs": context_refs})
        if self._has_view():
            self.owner.show(focus=False)
        self._render_pending = False
        self._struct_dirty = True
        c = self.owner.composer
        promoted = None
        refs = list(context_refs or [])
        if not refs and context_names:
            refs = [{"name": n, "path": "", "line_range": "", "action": "reveal"}
                    for n in context_names]
        names = list(context_names or [r.get("name") or "?" for r in refs])
        has_ctx = bool(names or refs)

        # Freeze the previous turn *before* touching ◎. Peel is the boundary
        # while the composer is still open; closing first would make a full
        # rewrite swallow a just-promoted prompt.
        prev_todos = []
        prev_goal = None
        if self.current:
            try:
                self._materialize_turn_context_line(self.current)
            except Exception as e:
                print("[Submarine] materialize context line: %s" % e)
            self.current.working = False
            if _goal_is_open(self.current.goal):
                prev_goal = self.current.goal
            self.current.goal = None
            if not self.current.todos_all_done:
                prev_todos = _open_todos(self.current.todos)
            self.current.todos = []
            self.current.todos_all_done = True
            self._render_pending = False
            try:
                self._do_render()
            except Exception as e:
                print("[Submarine] prompt finalize render: %s" % e)
            self.conversations.append(self.current)
            self.conversations, dropped = cap_history(self.conversations)
            if dropped:
                print("[Submarine] conversation history capped: dropped %d oldest turn(s)" % dropped)

        if c.is_input_mode():
            session = get_session_for_view(self.owner.view)
            live = ""
            try:
                live = c.get_input_text()
            except Exception:
                live = ""
            consuming = bool((text or "").strip() and live.strip() == (text or "").strip())
            if consuming:
                promoted = c.promote_to_prompt(text, has_context=has_ctx)
            if promoted is None:
                c.exit_input_mode(keep_text=False)
            if session:
                # Consumed draft is this turn's prompt — never feed it back
                # into the next ◎ (that was "submitted text still in input").
                session.draft_prompt = "" if consuming else live
        else:
            session = get_session_for_view(self.owner.view)
            try:
                if session and hasattr(session, "_clear_queue_phantom"):
                    session._clear_queue_phantom()
            except Exception:
                pass
            view = self.owner.view
            if view:
                content = view.substr(_R(0, view.size()))
                lines = content.split("\n")
                for line in reversed(lines[-5:]):
                    if not line.strip():
                        continue
                    is_input = line.startswith(c._input_marker) and " ▶" not in line
                    is_ctx = line.startswith(CONTEXT_PREFIX)
                    is_queued = line.startswith("⏳ ")
                    if is_input or is_ctx or is_queued:
                        c.reset_input_mode()
                    break
        start, end = c._pending_context_region
        if end > start:
            self.owner._replace(start, end, "")
            c._pending_context_region = (0, 0)
        c._refresh_context_phantoms([])

        self.current = Conversation(
            prompt=text, todos=prev_todos, goal=prev_goal,
            context_names=names, context_refs=refs, working=True)
        self._reset_proj()
        self.owner.sheet.update_title()

        view = self.owner.view
        if promoted is not None:
            self.current.region = promoted
            if view:
                self.owner.sheet.set_hidden_region(
                    keys.CONV_REGION, promoted[0], promoted[1])
                if refs and sublime is not None:
                    sublime.set_timeout(
                        lambda r=list(refs), a=promoted[0], b=promoted[1]:
                            self._refresh_turn_context_phantoms(r, region=(a, b)),
                        15)
            # Promote left only the ◎…▶ line. Paint waiting chrome (busy mark)
            # now — spinner ticks only patch an existing glyph.
            self._struct_dirty = True
            self._render_pending = False
            self._render_current(auto_scroll=True)
            return
        start = view.size() if view else 0
        prefix = "\n" if start > 0 else ""
        line = prefix + format_user_prompt_block(text, has_ctx, CONTEXT_PREFIX)
        if view:
            end = self.owner._write(line)
            self.current.region = (start, end)
            self.owner.sheet.set_hidden_region(keys.CONV_REGION, start, end)
            self.owner.composer.scroll_to_end()
            if refs and sublime is not None:
                sublime.set_timeout(
                    lambda r=list(refs), a=start, b=end: self._refresh_turn_context_phantoms(
                        r, region=(a, b)),
                    15)
            self._struct_dirty = True
            self._render_pending = False
            self._render_current(auto_scroll=True)
        else:
            self.current.region = None

    def tool(self, name, tool_input=None, tool_id=None, background=False):
        """Open a tool row. Viewless: records ToolCall, no buffer write."""
        if not self.current:
            return
        tool_input = tool_input or {}
        if background and not may_background(name):
            background = False
        status = BACKGROUND if background else PENDING
        existing = None
        if tool_id:
            existing = self._find_pending_or_background_by_id(tool_id)
            if existing is not None and existing.status == DONE:
                return
        # ExitPlanMode / EnterPlanMode / ask_user: agent or ACP often opens
        # twice with different toolCallIds. Collapse to one open row. A late
        # tool_use after ✔ must not start a second ☐ of the same question.
        if existing is None and name in (
            "ExitPlanMode", "EnterPlanMode", "ask_user", "AskUserQuestion",
        ):
            for event in reversed(self.current.events):
                if not isinstance(event, ToolCall):
                    continue
                if not same_modal_tool(event.name, name):
                    continue
                if event.status in (PENDING, BACKGROUND):
                    existing = event
                    if tool_id:
                        event.id = tool_id
                    break
                if event.status == DONE and same_ask_payload(
                        event.tool_input, tool_input):
                    return
        if existing is not None and existing.status in (PENDING, BACKGROUND):
            existing.name = name
            if tool_input:
                existing.tool_input = tool_input
            if tool_id and not existing.id:
                existing.id = tool_id
            if background and may_background(name):
                existing.status = BACKGROUND
            elif existing.status == BACKGROUND and (
                    not background or not may_background(name)):
                existing.status = PENDING
            tool_call = existing
        else:
            tool_call = ToolCall(
                name=name, tool_input=tool_input, status=status, id=tool_id)
            self.current.events.append(tool_call)
        session = get_session_for_view(self.owner.view)
        apply_tool_side_effects(self.current, name, tool_input, session)
        self._mark_buffer_dirty(
            "tool", (name,),
            {"tool_input": tool_input, "tool_id": tool_id, "background": background})
        self._struct_dirty = True
        self._render_current()

    def tool_done(self, name, result=None, tool_id=None):
        """Mark a tool done. Viewless: updates ToolCall, no buffer write."""
        targets = []
        if tool_id and self.current:
            for event in self.current.events:
                if (isinstance(event, ToolCall) and event.id == tool_id
                        and event.status in (PENDING, BACKGROUND)):
                    targets.append(event)
        if not targets:
            target = self._find_pending_or_background_by_id(tool_id)
            if target is None and self.current:
                for event in reversed(self.current.events):
                    if (isinstance(event, ToolCall) and event.name == name
                            and event.status == PENDING):
                        target = event
                        break
            if target is not None:
                targets = [target]
        if not targets:
            if self.current:
                for event in reversed(self.current.events):
                    if not isinstance(event, ToolCall):
                        continue
                    if event.status != DONE:
                        continue
                    if tool_id and event.id == tool_id:
                        return
                    if same_modal_tool(event.name, name):
                        return
            if self.current and not is_host_control_tool(name):
                self.current.events.append(ToolCall(
                    name=name, tool_input={}, status=DONE, result=result, id=tool_id))
                self._mark_buffer_dirty(
                    "tool_done", (name,), {"result": result, "tool_id": tool_id})
                self._struct_dirty = True
                self._render_current()
            return
        if any(is_host_control_tool(t.name) for t in targets):
            for t in targets:
                try:
                    self.remove_tool(t)
                except Exception:
                    pass
            return
        self._mark_buffer_dirty(
            "tool_done", (name,), {"result": result, "tool_id": tool_id})
        primary = targets[0]
        cwd = None
        try:
            sess = get_session_for_view(self.owner.view)
            if sess is not None and hasattr(sess, "_cwd"):
                cwd = sess._cwd()
        except Exception:
            pass
        for target in targets:
            old_status = target.status
            target.status = DONE
            target.result = result
            stash_media_path(target, result, cwd)
            if not self._is_in_current(target):
                self._patch_tool_symbol(target, old_status)
        if name in ("TaskList", "TaskCreate", "TaskGet") and result and self.current is not None:
            sync_todos_from_task_result(self.current, name, primary, result)
        if any(self._is_in_current(t) for t in targets):
            self._struct_dirty = True
            self._render_current()

    def tool_error(self, name, result=None, tool_id=None):
        """Mark a tool errored. Viewless: updates ToolCall, no buffer write."""
        target = self._find_pending_or_background_by_id(tool_id)
        if target is None and self.current:
            for event in reversed(self.current.events):
                if isinstance(event, ToolCall) and event.name == name and event.status == PENDING:
                    target = event
                    break
        if target is None:
            if self.current:
                self.current.events.append(ToolCall(
                    name=name, tool_input={}, status=ERROR, result=result, id=tool_id))
                self._mark_buffer_dirty(
                    "tool_error", (name,), {"result": result, "tool_id": tool_id})
                self._struct_dirty = True
                self._render_current()
            return
        old_status = target.status
        target.status = ERROR
        target.result = result
        if self._is_in_current(target):
            self._mark_buffer_dirty(
                "tool_error", (name,), {"result": result, "tool_id": tool_id})
            self._struct_dirty = True
            self._render_current()
        else:
            self._mark_buffer_dirty(
                "tool_error", (name,), {"result": result, "tool_id": tool_id})
            self._patch_tool_symbol(target, old_status)

    def artifact_card(self, path, name, bytes=0, summary="", title=None):
        """Append a transcript artifact card. Viewless: records event, no chrome.

        Card update rule: always append a new card (do not mutate a prior
        row for the same path). Each write/edit is a timeline entry.
        """
        card = ArtifactCard(
            path=path or "",
            name=name or "",
            bytes=int(bytes or 0),
            summary=summary or "",
            title=title,
        )
        conv = self.current
        if conv is None and self.conversations:
            conv = self.conversations[-1]
        if conv is None:
            self.current = Conversation(working=False)
            conv = self.current
        conv.events.append(card)
        self._mark_buffer_dirty(
            "artifact_card",
            (path, name),
            {"bytes": bytes, "summary": summary, "title": title},
        )
        self._struct_dirty = True
        self._render_current()

    def text(self, content):
        """Append assistant text. Viewless: records events, no buffer write."""
        if not self.current:
            return
        if content is None or content == "":
            return
        self._mark_buffer_dirty("text", (content,))
        if self.current.events and isinstance(self.current.events[-1], str):
            self.current.events[-1] += content
        else:
            self.current.events.append(content)
        if not self.current.working and not getattr(self.current, "has_meta", False):
            try:
                sess = get_session_for_view(self.owner.view)
                if sess is not None and getattr(sess, "working", False):
                    self.current.working = True
            except Exception:
                pass
        self._render_current()

    def meta(self, duration, cost=None, usage=None):
        """Finish the live turn. Viewless: records meta, skips buffer flush."""
        if not self.current:
            return
        try:
            self.current.duration = max(0.0, float(duration or 0))
        except (TypeError, ValueError):
            self.current.duration = 0.0
        self.current.has_meta = True
        self.current.usage = usage
        self.current.working = False
        self._mark_buffer_dirty(
            "meta", (duration,), {"cost": cost, "usage": usage})
        self._render_pending = False
        self._struct_dirty = True
        self._do_render()

    def interrupted(self, show_banner=True):
        """Mark the turn interrupted. Viewless: records state, no buffer write."""
        if not self.current:
            try:
                self.owner.clear_asking_state()
            except Exception:
                pass
            return
        self.current.working = False
        for event in self.current.events:
            if isinstance(event, ToolCall) and event.status in (PENDING, BACKGROUND):
                event.status = ERROR
        if self.owner.pending_permission:
            self.owner.modals.remove_permission_block()
            self.owner.pending_permission = None
        if self.owner.pending_plan:
            self.owner.modals.clear_plan_approval()
            self.owner.pending_plan = None
        # Clear question UI without resolving the RPC. Resolving it as
        # dismissed lets Kimi continue; session/cancel while the ask is
        # still outstanding actually stops the turn (sandbox interrupt_during).
        if self.owner.pending_question:
            self.owner.modals.clear_question()
            self.owner.pending_question = None
        # Peel ◎ first. Rewriting the live turn while the composer is open
        # paints *[interrupted]* into the input strip (◎ nterrupted]*).
        c = self.owner.composer
        was_input = False
        draft = ""
        try:
            was_input = bool(c.is_input_mode()) and not c._question_input_mode
            if was_input:
                draft = c.get_input_text()
                c.exit_input_mode(keep_text=False)
        except Exception:
            was_input = False
        if show_banner:
            self.current.events.append("\n\n*[interrupted]*\n")
        self._mark_buffer_dirty("interrupted", (), {"show_banner": show_banner})
        self._struct_dirty = True
        self._render_current()
        if was_input:
            try:
                c.enter_input_mode()
                if draft:
                    c.set_composer_text(draft)
            except Exception:
                pass

    def apply_plan_todos(self, entries):
        """Replace live todos. Viewless: records todos, no buffer write."""
        if not self.current or not isinstance(entries, list):
            return
        from .tools import todos_from_raw_list
        raw = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            raw.append({
                "content": e.get("content") or e.get("title") or e.get("text") or "",
                "status": e.get("status") or "pending",
                "id": str(e.get("id") or ""),
            })
        parsed = todos_from_raw_list(raw)
        if not parsed and not entries:
            return
        self.current.todos = _open_todos(parsed)
        self.current.todos_all_done = not self.current.todos
        self._mark_buffer_dirty("apply_plan_todos", (entries,))
        self._struct_dirty = True
        self._render_current()

    def set_retry_hint(self, text):
        """Set the live retry hint. Viewless: records hint, no buffer write."""
        self._retry_hint = text or None
        if self.current and self.current.working:
            self._mark_buffer_dirty("set_retry_hint", (text,))
            self._struct_dirty = True
            self._render_current()

    def advance_spinner(self, frames=None):
        """Advance the working spinner. Viewless: no-op.

        The glyph in the sheet must move. Do not `_render_current` — a full
        replace plus caret/viewport restore recasts the mouse cursor across
        the window. Patch only the spinner line. Question/permission/plan
        own the tail, so those ticks are title-only.
        """
        if not self.current or not self.current.working or not self._has_view():
            return
        if frames:
            self._spinner_frames = frames
        self._spinner_frame += 1
        frames = self._spinner_frames or SPINNER_FRAMES
        glyph = frames[self._spinner_frame % len(frames)]
        if getattr(self.owner, "has_turn_modal_ui", lambda: False)():
            if self._spinner_frame % 15 == 0:
                try:
                    self.owner.sheet.update_title()
                except Exception:
                    pass
            return
        if not self._patch_spinner_glyph(glyph):
            if self._spinner_frame % 15 == 0:
                try:
                    self.owner.sheet.update_title()
                except Exception:
                    pass

    def _spinner_glyphs(self):
        seen = []
        for blob in (
            self._spinner_frames or "",
            SPINNER_WAITING,
            SPINNER_RESPONDING,
            SPINNER_FRAMES,
        ):
            for ch in blob:
                if ch not in seen:
                    seen.append(ch)
        return seen

    def _patch_spinner_glyph(self, glyph):
        """Replace the live `  ◇\\n` / braille line. No pin, no caret restore."""
        view = self.owner.view
        replace = getattr(self.owner, "_replace", None)
        if view is None or not callable(replace):
            return False
        try:
            if not view.is_valid():
                return False
        except Exception:
            return False
        start, end = 0, 0
        try:
            tracked = view.get_regions(keys.CONV_REGION)
            if tracked and tracked[0].size() > 0:
                start, end = tracked[0].begin(), tracked[0].end()
            elif self.current and self.current.region:
                start, end = self.current.region
            else:
                end = view.size()
        except Exception:
            return False
        try:
            peel = None
            c = getattr(self.owner, "composer", None)
            if c is not None and getattr(c, "is_input_mode", lambda: False)():
                peel = c.peel_start()
            trail = None
            modals = getattr(self.owner, "modals", None)
            if modals is not None:
                trail = modals.trailing_ui_start()
            if peel is not None and peel >= start:
                end = min(end, peel)
            if trail is not None and trail >= start:
                end = min(end, trail)
        except Exception:
            pass
        try:
            end = min(end, view.size())
            start = max(0, min(start, end))
            text = view.substr(_R(start, end))
        except Exception:
            return False
        best = -1
        best_n = 0
        for ch in self._spinner_glyphs():
            needle = "  %s\n" % ch
            pos = text.rfind(needle)
            if pos >= 0 and pos >= best:
                best = pos
                best_n = len(needle)
        if best < 0:
            # One insert per conversation. If the glyph line has gone missing
            # from the span (a clear, a composer that swallowed it), inserting
            # again on every tick paints a column of glyphs; the next full
            # render puts a fresh one in the right place.
            if getattr(self, "_spinner_inserted_for", None) is self.current:
                return False
            self._spinner_inserted_for = self.current
            insert_at = end
            new = "  %s\n" % glyph
            if text and not text.endswith("\n"):
                new = "\n" + new
            grown = len(new)
            composing = bool(
                c is not None and c.is_input_mode()
                and not getattr(c, "_question_input_mode", False))
            try:
                # Shift the composer's anchors BEFORE the edit: on_modified
                # runs inside the replace command and reads the draft from
                # _input_start, so a stale anchor would capture the glyph
                # line as the draft (and replant it after a Cmd+K).
                if composing:
                    try:
                        c.shift_anchors(grown)
                    except Exception:
                        pass
                try:
                    replace(insert_at, insert_at, new)
                except Exception:
                    if composing:
                        try:
                            c.shift_anchors(-grown)
                        except Exception:
                            pass
                    raise
                # Mirror _try_append: grow, never shrink to the clamped
                # insert point.
                if self.current and self.current.region:
                    a, b = self.current.region
                    self.current.region = (a, max(b, insert_at) + grown)
                try:
                    tracked = view.get_regions(keys.CONV_REGION)
                    if tracked:
                        self.owner.sheet.set_hidden_region(
                            keys.CONV_REGION, tracked[0].begin(),
                            tracked[0].end() + grown)
                    elif self.current and self.current.region:
                        na, nb = self.current.region
                        self.owner.sheet.set_hidden_region(keys.CONV_REGION, na, nb)
                except Exception:
                    pass
                return True
            except Exception:
                return False
        new = "  %s\n" % glyph
        if text[best:best + best_n] == new:
            return True
        try:
            replace(start + best, start + best + best_n, new)
            return True
        except Exception:
            return False

    # --- lookup ------------------------------------------------------------

    def _find_pending_or_background_by_id(self, tool_id):
        if not tool_id:
            return None

        def _scan(events):
            done = None
            for event in events:
                if not isinstance(event, ToolCall) or event.id != tool_id:
                    continue
                if event.status in (PENDING, BACKGROUND):
                    return event
                done = event
            return done

        if self.current:
            hit = _scan(self.current.events)
            if hit is not None:
                return hit
        for conv in self.conversations:
            hit = _scan(conv.events)
            if hit is not None:
                return hit
        return None

    def find_tool_by_id(self, tool_id):
        """Lookup a tool by id. Viewless: same, state-only."""
        return self._find_pending_or_background_by_id(tool_id)

    def active_background_tools(self):
        """List BACKGROUND tools. Viewless: same, state-only."""
        result = []
        for conv in self.conversations:
            for event in conv.events:
                if isinstance(event, ToolCall) and event.status == BACKGROUND:
                    result.append(event)
        if self.current:
            for event in self.current.events:
                if isinstance(event, ToolCall) and event.status == BACKGROUND:
                    result.append(event)
        return result

    def _is_in_current(self, target):
        if not self.current:
            return False
        return any(e is target for e in self.current.events)

    def remove_tool(self, target):
        """Drop a tool event. Viewless: mutates events, no buffer patch."""
        in_current = False
        convs = list(self.conversations)
        if self.current is not None:
            convs.append(self.current)
        for conv in convs:
            for i, e in enumerate(conv.events):
                if e is target:
                    del conv.events[i]
                    in_current = (conv is self.current)
                    self._mark_buffer_dirty("remove_tool", (target,))
                    break
            else:
                continue
            break
        if in_current and self.current is not None:
            self._struct_dirty = True
            self._render_current()
            return
        view = self.owner.view
        if not view:
            return
        content = view.substr(_R(0, view.size()))
        sym = SYMBOLS.get(target.status, SYMBOLS.get(BACKGROUND, "⚙"))
        snippet = ""
        for key in ("command", "file_path", "pattern", "url", "task_id", "description"):
            val = target.tool_input.get(key) if isinstance(target.tool_input, dict) else None
            if isinstance(val, str) and val.strip():
                snippet = val.split("\n", 1)[0][:120]
                break
        prefix = "  %s %s" % (sym, target.name)
        pattern = re.escape(prefix) + (r"[^\n]*?" + re.escape(snippet) if snippet else "")
        m = re.search(pattern, content)
        if m is None and snippet:
            m = re.search(re.escape(prefix), content)
        if m is None:
            return
        line_region = view.line(m.start())
        end = min(line_region.end() + 1, view.size())
        self.owner._replace(line_region.begin(), end, "")

    def _patch_tool_symbol(self, target, old_status):
        view = self.owner.view
        if not view or not self._has_view():
            return
        old_sym = SYMBOLS.get(old_status, "☐")
        new_sym = SYMBOLS.get(target.status, "☐")
        if old_sym == new_sym:
            return
        content = view.substr(_R(0, view.size()))
        snippet = ""
        for key in ("command", "file_path", "pattern", "url", "task_id", "description"):
            val = target.tool_input.get(key) if isinstance(target.tool_input, dict) else None
            if isinstance(val, str) and val.strip():
                snippet = val.split("\n", 1)[0][:120]
                break
        prefix = "  %s %s" % (old_sym, target.name)
        if snippet:
            pattern = re.escape(prefix) + r"[^\n]*?" + re.escape(snippet)
        else:
            pattern = re.escape(prefix)
        m = re.search(pattern, content)
        if m is None and snippet:
            m = re.search(re.escape(prefix), content)
        if m is not None:
            self.owner._replace(m.start() + 2, m.start() + 2 + len(old_sym), new_sym)

    # --- clear / repaint ---------------------------------------------------

    def conversation_body(self, conv, leading_nl=False):
        lines = []
        prefix = "\n" if leading_nl else ""
        if conv.prompt:
            prompt_lines = conv.prompt.split("\n")
            if len(prompt_lines) > 1:
                indented = prompt_lines[0] + "\n" + "\n".join(
                    "  " + l for l in prompt_lines[1:])
            else:
                indented = conv.prompt
            if conv.context_names or conv.context_refs:
                lines.append("%s◎ %s ▶\n" % (prefix, indented))
                lines.append("  %s\n" % CONTEXT_PREFIX)
            else:
                lines.append("%s◎ %s ▶\n" % (prefix, indented))
            prefix = ""
        if conv.events:
            lines.append("\n" if not prefix else prefix + "\n")
            prefix = ""
            i = 0
            evs = conv.events
            n_ev = len(evs)
            while i < n_ev:
                event = evs[i]
                if isinstance(event, str):
                    parts = [event]
                    i += 1
                    while i < n_ev and isinstance(evs[i], str):
                        parts.append(evs[i])
                        i += 1
                    block = "".join(parts)
                    if block:
                        lines.append(block)
                        if not block.endswith("\n"):
                            lines.append("\n")
                    continue
                if isinstance(event, ToolCall) and not is_host_control_tool(event.name):
                    lines.append(format_tool_row(self.owner, event))
                elif isinstance(event, ArtifactCard):
                    line = event.line()
                    lines.append(line if line.endswith("\n") else line + "\n")
                i += 1
        if conv.has_meta or conv.duration > 0:
            meta_parts = []
            if conv.duration > 0:
                meta_parts.append("%.1f s" % conv.duration)
            if not meta_parts:
                meta_parts.append("ok")
            lines.append("\n  @done(%s)\n" % ", ".join(meta_parts))
        return "".join(lines)

    def repaint_from_state(self):
        """Reproject conversations onto the bound view. Viewless: no-op.

        None `Conversation.region` tuples mean the live span is unknown —
        treat as a full recompute (this method already rebuilds from state
        and does not read stored offsets).
        """
        view = self.owner.view
        if not view or not view.is_valid():
            return
        parts = []
        for conv in self.conversations:
            body = self.conversation_body(conv, leading_nl=bool(parts))
            start = sum(len(p) for p in parts)
            conv.region = (start, start + len(body)) if body else None
            parts.append(body)
        cur_start = sum(len(p) for p in parts)
        if self.current:
            parts.append(self.conversation_body(self.current, leading_nl=bool(parts)))
        body = "".join(parts)
        self.owner._replace(0, view.size(), body)
        if self.current:
            end = cur_start + (len(parts[-1]) if parts else 0)
            self.current.region = (cur_start, end)
            self.owner.sheet.set_hidden_region(keys.CONV_REGION, cur_start, end)
        self._region_stash = None
        self._reset_proj()

    def clear(self, keep_supportive=True):
        """Clear the transcript. Viewless: drops conversations, no buffer write."""
        self._mark_buffer_dirty("clear", (), {"keep_supportive": keep_supportive})
        try:
            from ui import idle
            idle.clear(self.owner.view)   # this sheet is a session's now
        except Exception:
            pass
        c = self.owner.composer
        was_input_mode = c.is_input_mode()
        sess = get_session_for_view(self.owner.view)
        was_working = bool(
            (self.current and self.current.working)
            or (sess and getattr(sess, "working", False))
        )
        carry_bg = list(self.active_background_tools())
        carry_todos = []
        carry_goal = None
        if self.current and self.current.todos and not self.current.todos_all_done:
            carry_todos = _open_todos(self.current.todos)
        else:
            for conv in reversed(self.conversations):
                if conv.todos and not conv.todos_all_done:
                    carry_todos = _open_todos(conv.todos)
                    break
        if self.current and _goal_is_open(self.current.goal):
            carry_goal = self.current.goal
        else:
            for conv in reversed(self.conversations):
                if _goal_is_open(conv.goal):
                    carry_goal = conv.goal
                    break
        if not keep_supportive:
            carry_bg, carry_todos, carry_goal = [], [], None
        draft = ""
        if was_input_mode:
            draft = c.get_input_text()
            try:
                if sess is not None:
                    sess.draft_prompt = draft
            except Exception:
                pass
            c.exit_input_mode(keep_text=False)
        view = self.owner.view
        if view and view.is_valid():
            self._cleared_content = view.substr(_R(0, view.size()))
            self.owner.sheet.clear_all()
            keys.write_setting(view.settings(), keys.INPUT_MODE, False)
        self.conversations = []
        self.current = None
        self.owner.modals.reset_all()
        self.owner.auto_allow_tools.clear()
        c._pending_context_region = (0, 0)
        c._input_mode = False
        c._input_start = 0
        c._input_area_start = 0
        if view:
            view.erase_regions(keys.PERM_BLOCK)
        self._reset_proj()
        if was_working:
            self.current = Conversation(prompt="(continued)", working=True)
            self.current.region = (0, 0) if self._has_view() else None
            self.current.events.extend(carry_bg)
            self.current.todos = carry_todos
            self.current.goal = carry_goal
            if carry_bg or carry_todos or carry_goal:
                self._struct_dirty = True
                self._render_current()
            self.owner.sheet.update_title()
            if was_input_mode or (
                sess is not None
                and getattr(sess, "working", False)
                and getattr(sess, "_composer_allowed", True)
            ):
                c.enter_input_mode()
                if draft:
                    try:
                        c.set_composer_text(draft)
                    except Exception:
                        pass
            return
        if carry_bg or carry_todos or carry_goal:
            carry = Conversation(prompt="", working=False)
            carry.events = list(carry_bg)
            carry.todos = carry_todos
            carry.todos_all_done = False
            carry.goal = carry_goal
            carry.region = (0, 0) if self._has_view() else None
            self.current = carry
            self._struct_dirty = True
            self._render_current()
        if was_input_mode:
            c.enter_input_mode()
            if draft:
                try:
                    c.set_composer_text(draft)
                except Exception:
                    pass

    def clear_keep_last(self):
        if self.current is not None:
            keep = self.current
            dropped = len(self.conversations)
        elif self.conversations:
            keep = self.conversations[-1]
            dropped = len(self.conversations) - 1
        else:
            if sublime is not None:
                sublime.status_message("Submarine: nothing to clear")
            return
        if dropped <= 0:
            if sublime is not None:
                sublime.status_message("Submarine: already only last round")
            return
        c = self.owner.composer
        was_input = c.is_input_mode()
        draft = ""
        if was_input:
            draft = c.get_input_text()
            try:
                s = get_session_for_view(self.owner.view)
                if s is not None:
                    s.draft_prompt = draft
            except Exception:
                pass
            c.exit_input_mode(keep_text=False)
        view = self.owner.view
        if view and view.is_valid():
            self._cleared_content = view.substr(_R(0, view.size()))
            self.owner.sheet.clear_all()
            keys.write_setting(view.settings(), keys.INPUT_MODE, False)
            try:
                view.erase_regions(keys.CONV_REGION)
                view.erase_regions(keys.PERM_BLOCK)
            except Exception:
                pass
        self.conversations = []
        self.current = keep
        self.current.region = (0, 0) if self._has_view() else None
        self.owner.modals.reset_all(keep_auto=True)
        c._pending_context_region = (0, 0)
        c._input_mode = False
        c._input_start = 0
        c._input_area_start = 0
        self._render_pending = False
        self._mark_buffer_dirty("clear_keep_last")
        self._struct_dirty = True
        self._auto_scroll = True
        try:
            self._do_render()
        except Exception as e:
            print("[Submarine] clear_keep_last render: %s" % e)
        self.owner.sheet.update_title()
        if was_input:
            c.enter_input_mode()
            if draft and c.is_input_mode():
                try:
                    view.set_read_only(False)
                    view.run_command("append", {"characters": draft})
                    c._update_composer_pad_phantom()
                    c.focus(force_show=True, preserve_caret=True, park_at_end=True)
                except Exception:
                    pass
        if sublime is not None:
            sublime.status_message(
                "Submarine: cleared %d older round%s, kept last"
                % (dropped, "s" if dropped != 1 else ""))

    def undo_clear(self):
        if self._cleared_content and self.owner.view and self.owner.view.is_valid():
            if self.current is not None and not self.current.prompt and not self.current.working:
                self.current = None
                self.owner.view.erase_regions(keys.CONV_REGION)
            self.owner._write(self._cleared_content)
            self._cleared_content = None
            self.owner.composer.scroll_to_end()

    def reset_active_states(self, soft=False):
        """Reset composer + pending tools. Viewless: state only, no buffer patch."""
        self.owner.composer.reset_input_mode()
        if soft:
            self.owner.pending_permission = None
            self.owner.modals._permission_queue.clear()
            self.owner.pending_plan = None
            self.owner.pending_question = None
            try:
                self.owner.composer._question_input_mode = False
            except Exception:
                pass
            return

        try:
            self.owner.clear_asking_state()
        except Exception:
            pass
        if self.current:
            had_pending = False
            for event in self.current.events:
                if isinstance(event, ToolCall) and event.status in (PENDING, BACKGROUND):
                    event.status = ERROR
                    had_pending = True
            if had_pending:
                self.current.events.append("\n\n*[session reconnected]*\n")
                self._mark_buffer_dirty("reset_active_states", (), {"soft": False})
                self._struct_dirty = True
                self._render_current()
        view = self.owner.view
        if view:
            bg_sym = SYMBOLS["background"]
            err_sym = SYMBOLS["error"]
            content = view.substr(_R(0, view.size()))
            marker = "  %s " % bg_sym
            idx = 0
            edits = []
            while True:
                pos = content.find(marker, idx)
                if pos < 0:
                    break
                edits.append((pos + 2, pos + 2 + len(bg_sym)))
                idx = pos + len(marker)
            if edits:
                view.set_read_only(False)
                for a, b in reversed(edits):
                    view.run_command(keys.CMD_REPLACE, {
                        "start": a, "end": b, "text": err_sym,
                    })
                view.set_read_only(True)

    def refresh_preserving_input(self):
        """Rewrite the live turn, keep composer. Viewless: no-op."""
        if not self._has_view() or not self.current:
            return
        self._render_pending = False
        self._auto_scroll = False
        self._struct_dirty = True
        self._do_render()

    # --- render ------------------------------------------------------------

    def _reset_proj(self):
        self._proj_event_count = len(self.current.events) if self.current else 0
        self._proj_joined_text = text_events_joined(self.current.events) if self.current else ""
        self._proj_events_end = None
        self._struct_dirty = False

    def _render_current(self, auto_scroll=True):
        if not self.current or not self._has_view():
            return
        if self._render_pending:
            return
        self._render_pending = True
        self._auto_scroll = auto_scroll
        if sublime is not None:
            sublime.set_timeout(self._do_render, 10)
        else:
            self._do_render()

    def _build_live_text(self):
        """Full live-turn projection (prompt + events + live chrome)."""
        conv = self.current
        lines = []
        reg0 = 0
        if conv.region:
            try:
                reg0 = conv.region[0]
            except Exception:
                reg0 = 0
        prefix = "\n" if reg0 > 0 else ""
        if conv.prompt:
            has_ctx = bool(conv.context_names or conv.context_refs)
            lines.append(prefix + format_user_prompt_block(
                conv.prompt, has_ctx, CONTEXT_PREFIX))
        events_text, events_end_off = self._events_block(conv, leading_nl=True)
        if events_text:
            lines.append(events_text)
        chrome = self._live_chrome(conv)
        if chrome:
            lines.append(chrome)
        text = "".join(lines)
        # events_end is offset from start of this projection
        prompt_len = len(text) - len(events_text) - len(chrome)
        events_end = prompt_len + events_end_off
        return text, events_end

    def _events_block(self, conv, leading_nl=True):
        if not conv.events:
            return "", 0
        parts = ["\n"] if leading_nl else []
        i = 0
        evs = conv.events
        n_ev = len(evs)
        while i < n_ev:
            event = evs[i]
            if isinstance(event, str):
                chunk = [event]
                i += 1
                while i < n_ev and isinstance(evs[i], str):
                    chunk.append(evs[i])
                    i += 1
                block = "".join(chunk)
                if block:
                    parts.append(block)
                    if not block.endswith("\n"):
                        parts.append("\n")
                continue
            if isinstance(event, ToolCall) and not is_host_control_tool(event.name):
                parts.append(format_tool_row(self.owner, event))
            elif isinstance(event, ArtifactCard):
                line = event.line()
                parts.append(line if line.endswith("\n") else line + "\n")
            i += 1
        text = "".join(parts)
        return text, len(text)

    def _live_chrome(self, conv):
        lines = []
        open_todos = _open_todos(conv.todos)
        if not open_todos:
            conv.todos_all_done = True
        goal = self._live_goal(conv)
        turn_done = bool(conv.has_meta)
        is_working = bool(conv.working and not turn_done)
        show_goal = bool(goal and _goal_is_open(goal) and not turn_done)
        view = self.owner.view
        expanded = bool(self._tasks_expanded)
        if view:
            expanded = bool(
                keys.read_setting(view.settings(), keys.TASKS_EXPANDED, expanded)
            )
        show, hidden = tasks_fold_rows(open_todos, expanded)
        show_tasks = bool(show)

        def _append_task_lines():
            if not show_tasks:
                return
            for todo in show:
                icon = "▸" if _todo_is_active(todo) else "○"
                content = (todo.content or "").replace("\n", " ").strip()
                if len(content) > 72:
                    content = content[:71] + "…"
                lines.append("  %s %s\n" % (icon, content))
            if hidden > 0:
                lines.append("  … +%d more  (super+click to expand)\n" % hidden)
            elif expanded:
                lines.append("  (super+click to collapse)\n")

        if is_working and (show_goal or show_tasks):
            lines.append("\n")
            if show_goal:
                lines.append(format_goal_strip_line(
                    status=goal.status or "",
                    phase=goal.phase or "",
                    message=goal.message or "",
                    objective=goal.objective or "",
                    pause_message=goal.pause_message or "",
                    blocked_reason=goal.blocked_reason or "",
                    gaps=list(goal.gaps or []),
                    token_budget=goal.token_budget,
                    tokens_used=goal.tokens_used,
                    verify_runs=int(goal.verify_runs or 0),
                    verify_max=int(goal.verify_max or 0),
                    compact=bool(show_tasks),
                ) + "\n")
            _append_task_lines()
        if is_working:
            frames = self._spinner_frames or SPINNER_FRAMES
            spinner = frames[self._spinner_frame % len(frames)]
            lines.append("  %s\n" % spinner)
            hint = self._retry_hint
            if hint is None:
                try:
                    sess = get_session_for_view(self.owner.view)
                    hint = getattr(sess, "_api_retry_hint", None)
                except Exception:
                    hint = None
            if hint:
                lines.append("  %s\n" % hint)
        if conv.has_meta or conv.duration > 0:
            lines.append(self._meta_line(conv))
        if not is_working and show_tasks:
            lines.append("\n")
            _append_task_lines()
        elif not is_working and show_goal:
            lines.append("\n")
            lines.append(format_goal_strip_line(
                status=goal.status or "",
                phase=goal.phase or "",
                message=goal.message or "",
                objective=goal.objective or "",
                pause_message=goal.pause_message or "",
                blocked_reason=goal.blocked_reason or "",
                gaps=list(goal.gaps or []),
                token_budget=goal.token_budget,
                tokens_used=goal.tokens_used,
                verify_runs=int(goal.verify_runs or 0),
                verify_max=int(goal.verify_max or 0),
                compact=False,
            ) + "\n")
        return "".join(lines)

    def _meta_line(self, conv):
        meta_parts = []
        if conv.duration > 0:
            meta_parts.append("%.1fs" % conv.duration)
        if conv.usage:
            u = conv.usage

            def _n(k):
                try:
                    return int(u.get(k) or 0)
                except (TypeError, ValueError):
                    return 0

            input_t = (
                _n("input_tokens")
                + _n("cache_read_input_tokens")
                + _n("cache_creation_input_tokens")
            )
            if input_t:
                if input_t >= 1000:
                    meta_parts.append("%dk ctx" % (input_t // 1000))
                else:
                    meta_parts.append("%d ctx" % input_t)
        view = self.owner.view
        if view is not None:
            st = view.settings()
            label = keys.read_setting(st, keys.PROVIDER_LABEL)
            model = keys.read_setting(st, keys.MODEL)
            effort = keys.read_setting(st, keys.EFFORT)
            if model:
                if label and label != "Claude" and label != "Submarine":
                    meta_parts.append("%s/%s" % (label, model))
                else:
                    meta_parts.append(model)
            elif label and label not in ("Claude", "Submarine"):
                meta_parts.append(label)
            if effort:
                meta_parts.append("effort:%s" % effort)
        if not meta_parts:
            meta_parts.append("ok")
        return "\n  @done(%s)\n" % ", ".join(meta_parts)

    def _live_goal(self, conv):
        goal = conv.goal
        try:
            sess = get_session_for_view(self.owner.view)
            gt = getattr(sess, "goal_tracker", None) if sess else None
            if gt is not None and gt.is_open():
                snap = gt.to_ui_dict()
                goal = GoalState(
                    status=snap["status"],
                    message=snap.get("message") or "",
                    blocked_reason=snap.get("blocked_reason") or "",
                    objective=snap.get("objective") or "",
                    phase=snap.get("phase") or "idle",
                    pause_message=snap.get("pause_message") or "",
                    token_budget=snap.get("token_budget"),
                    tokens_used=snap.get("tokens_used"),
                    verify_runs=int(snap.get("verify_runs") or 0),
                    verify_max=int(snap.get("verify_max") or 0),
                    gaps=list(snap.get("gaps") or []),
                    verifying=bool(snap.get("verifying")),
                    planning=bool(snap.get("planning")),
                    goal_id=snap.get("goal_id") or "",
                )
                conv.goal = goal
            elif gt is not None and not gt.is_open():
                goal = None
                conv.goal = None
        except Exception:
            pass
        return goal

    def _do_render(self):
        self._render_pending = False
        if not self.current or not self._has_view():
            return
        view = self.owner.view
        c = self.owner.composer

        # Incremental path: text-only growth, no structural dirty.
        delta = should_incremental_append(
            self._proj_event_count,
            self._proj_joined_text,
            self.current.events,
            self._struct_dirty,
        )
        if delta is not None and self._proj_events_end is not None:
            if self._try_append(delta):
                return

        # Full rewrite. A pending question/permission/plan owns the tail —
        # replanting ◎ here leaves input_mode true so 1–4/Esc never bind.
        if c.has_turn_modal_ui() and not c._question_input_mode:
            if c.is_input_mode():
                try:
                    c.hide_composer_for_modal()
                except Exception:
                    pass
            was_input = False
        else:
            was_input = bool(c.is_input_mode()) and not c._question_input_mode
        draft = ""
        caret_off = 0
        caret_in_composer = False
        peel = None
        if was_input:
            draft = c.get_input_text()
            try:
                s = get_session_for_view(view)
                if s is not None:
                    s.draft_prompt = draft
            except Exception:
                pass
            peel = c.peel_start()
            owner = c.caret_owner()
            live_in_draft = False
            sel_empty = False
            try:
                sel = view.sel()
                if not sel:
                    sel_empty = True
                elif c._input_start is not None:
                    b = sel[0].begin()
                    if b >= c._input_start:
                        live_in_draft = True
                        caret_off = max(0, min(b - c._input_start, len(draft)))
                        c._draft_caret_off = caret_off
            except Exception:
                sel_empty = True
            has_off = c._draft_caret_off is not None
            caret_in_composer = stream_treat_as_composing(
                live_in_draft, sel_empty, owner, has_off)
            if caret_in_composer and has_off and not live_in_draft:
                caret_off = max(0, min(int(c._draft_caret_off), len(draft or "")))

        view_size = view.size()
        tracked = view.get_regions(keys.CONV_REGION)
        if tracked and tracked[0].size() > 0:
            start, end = tracked[0].begin(), tracked[0].end()
        elif self.current.region:
            start, end = self.current.region
        else:
            # None region: full recompute of the live turn over the buffer.
            start, end = 0, view_size
        if start > view_size or end > view_size:
            if not self.current.prompt:
                return
            content = view.substr(_R(0, view_size))
            prompt_marker = "◎ %s" % self.current.prompt[:20]
            last_pos = content.rfind(prompt_marker)
            if last_pos >= 0:
                start = last_pos
                end = peel if (was_input and peel is not None) else view_size
            else:
                return

        text, events_end_off = self._build_live_text()
        trail = self.owner.modals.trailing_ui_start()
        boundary = None
        if peel is not None and peel >= start:
            boundary = peel
        if trail is not None and trail >= start:
            boundary = trail if boundary is None else min(boundary, trail)
        if boundary is not None:
            end = boundary
        else:
            end = view.size()
        if end < start:
            end = start
        end = min(end, view.size())

        want_scroll = bool(self._auto_scroll)
        at_tail = self.owner.sheet.is_following_tail()
        typing_at_tail = was_input and caret_in_composer and at_tail
        following = (want_scroll and at_tail) or typing_at_tail
        pin = (
            self.owner.sheet.pin_view_state()
            if should_pin_view_state(following, self.owner.composer.caret_owner())
            else None
        )

        old_end = end
        # ST remaps carets inside the replaced span to the new end. Hold the
        # guard until this frame's pin restore so on_selection_modified does
        # not flip history → draft (later ticks would lock caret at line end).
        self.owner._sel_guard = True
        try:
            new_end = self.owner._replace(start, end, text)
        except Exception:
            self.owner._sel_guard = False
            raise
        delta_sz = new_end - old_end
        self.current.region = (start, new_end)
        self.owner.sheet.set_hidden_region(keys.CONV_REGION, start, new_end)
        self._proj_events_end = start + events_end_off
        self._reset_proj()

        if was_input and delta_sz != 0:
            c.shift_anchors(delta_sz)
        if was_input and c.is_input_mode():
            try:
                s = get_session_for_view(view)
                if s and hasattr(s, "_update_queue_phantom"):
                    s._update_queue_phantom()
            except Exception:
                pass
            try:
                c._update_composer_pad_phantom()
            except Exception:
                pass

        self.owner.sheet.update_title()
        try:
            refs = list(getattr(self.current, "context_refs", None) or [])
            if refs and self.current.region:
                self._refresh_turn_context_phantoms(refs, region=self.current.region)
            elif not refs:
                self._clear_turn_context_phantoms()
        except Exception as e:
            print("[Submarine] turn context phantoms: %s" % e)

        if self.owner.pending_question and self.owner.pending_question.callback:
            # Free-text Other... owns the caret; rebuilding the block steals it
            # and reflows the question on every busy-mark rewrite.
            if not c._question_input_mode:
                qreg = view.get_regions(keys.QUESTION_BLOCK)
                need_q = (
                    not qreg or qreg[0].size() == 0
                    or qreg[0].begin() != new_end
                )
                if need_q:
                    self.owner.modals.render_question()

        if was_input:
            view.set_read_only(False)
            if not c.input_marker_intact():
                off = min(caret_off, len(draft or ""))
                c._draft_caret_off = off
                self._replant_composer(draft, off)
                try:
                    s = get_session_for_view(view)
                    if s and hasattr(s, "_update_queue_phantom"):
                        s._update_queue_phantom()
                except Exception:
                    pass
            elif caret_in_composer and c.caret_owner() == "draft":
                max_off = max(0, view.size() - c._input_start)
                pos = c._input_start + max(0, min(caret_off, max_off, len(draft or "")))
                c._draft_caret_off = pos - c._input_start
                try:
                    if sublime is not None:
                        view.sel().clear()
                        view.sel().add(sublime.Region(pos, pos))
                except Exception:
                    pass

        def _reapply_draft_caret_if_composing():
            if (was_input and c.is_input_mode() and caret_in_composer
                    and c.caret_owner() == "draft"):
                c.restore_draft_caret()

        if pin is not None:
            if was_input and caret_in_composer and c.caret_owner() == "draft":
                pin = dict(pin)
                pin["sels"] = []
                pin["preserve_sel"] = True
                if c.input_marker_intact():
                    max_off = max(0, view.size() - c._input_start)
                    pin["composer_caret"] = c._input_start + max(
                        0, min(caret_off, max_off, len(draft or "")))
            elif was_input and c.caret_owner() == "history":
                pin = dict(pin)
                pin.pop("composer_caret", None)
                pin["preserve_sel"] = True
            self.owner.sheet.restore_view_state(pin)
            self.owner.sheet.schedule_viewport_restore(pin)
            _reapply_draft_caret_if_composing()
            if (was_input and caret_in_composer
                    and c.caret_owner() == "draft" and sublime is not None):
                tok = int(getattr(c, "_pending_caret_token", 0) or 0) + 1
                c._pending_caret_token = tok

                def _once(t=tok):
                    if getattr(c, "_pending_caret_token", 0) != t:
                        return
                    _reapply_draft_caret_if_composing()

                sublime.set_timeout(_once, 0)
        else:
            if was_input and (
                    not caret_in_composer or c.caret_owner() == "history"):
                self.owner.sheet._pending_vp_pin = None
            may_scroll = (
                was_input and c.is_input_mode()
                and c.caret_owner() == "draft"
                and (following or want_scroll or typing_at_tail)
            )
            if may_scroll:
                if self.owner.sheet.view_is_focused():
                    c._scroll_layout_to_bottom(
                        force=(delta_sz != 0), reapply_caret=True)
                    _reapply_draft_caret_if_composing()
                    if delta_sz != 0 and caret_in_composer and sublime is not None:
                        tok = int(getattr(c, "_pending_caret_token", 0) or 0) + 1
                        c._pending_caret_token = tok

                        def _after_scroll(t=tok):
                            if getattr(c, "_pending_caret_token", 0) != t:
                                return
                            if c.caret_owner() != "draft":
                                return
                            if self.owner.sheet.view_is_focused():
                                c._scroll_layout_to_bottom(
                                    force=True, reapply_caret=True)
                            c.restore_draft_caret()

                        sublime.set_timeout(_after_scroll, 0)
            elif (want_scroll or typing_at_tail) and c.caret_owner() != "history":
                c.scroll_to_end(force=False)
                _reapply_draft_caret_if_composing()

        if c.is_input_mode() or c._question_input_mode:
            view.set_read_only(False)
            if c._question_input_mode:
                try:
                    regs = view.get_regions(keys.QUESTION_INPUT_MARKER)
                    if regs:
                        c._question_input_start = regs[0].end()
                        c._input_start = c._question_input_start
                except Exception:
                    pass

        if following or not self.current.working:
            if sublime is not None:
                sublime.set_timeout(self._refresh_media_phantoms, 10)
                sublime.set_timeout(self._refresh_artifact_phantoms, 10)
        try:
            s = get_session_for_view(view)
            if s and getattr(s, "_wakeup_armed", None) and s._wakeup_armed():
                fn = getattr(s, "_update_wakeup_banner", None)
                if callable(fn) and sublime is not None:
                    sublime.set_timeout(
                        lambda sess=s: sess._update_wakeup_banner(show=True), 15)
            if s and c.is_input_mode() and was_input:
                if sublime is not None and hasattr(s, "_update_permission_banner"):
                    sublime.set_timeout(
                        lambda sess=s: sess._update_permission_banner(show=True), 15)
            elif s and not c.is_input_mode() and hasattr(s, "_clear_queue_phantom"):
                s._clear_queue_phantom()
        except Exception:
            pass
        self.owner._sel_guard = False

    def _try_append(self, delta):
        """Insert text-only growth before live chrome (spinner / tasks)."""
        view = self.owner.view
        if not view or self._proj_events_end is None:
            return False
        insert_at = self._proj_events_end
        # If the last projected text didn't end with a newline and the delta
        # continues the same paragraph, just insert. If we need a newline
        # after a previous block, the stored joined text already includes it.
        peel = self.owner.composer.peel_start()
        trail = self.owner.modals.trailing_ui_start()
        if peel is not None and insert_at > peel:
            return False
        if trail is not None and insert_at > trail:
            return False
        if insert_at < 0 or insert_at > view.size():
            return False
        span = self.current.region
        if not span:
            return False
        c = self.owner.composer
        was_input = c.is_input_mode() and not c._question_input_mode
        new_end = self.owner._replace(insert_at, insert_at, delta)
        grown = new_end - insert_at
        self._proj_events_end = new_end
        self._proj_joined_text += delta
        self._proj_event_count = len(self.current.events)
        start, old = span
        self.current.region = (start, old + grown)
        try:
            tracked = view.get_regions(keys.CONV_REGION)
            if tracked:
                self.owner.sheet.set_hidden_region(
                    keys.CONV_REGION, tracked[0].begin(), tracked[0].end() + grown)
        except Exception:
            self.owner.sheet.set_hidden_region(
                keys.CONV_REGION, start, old + grown)
        if was_input:
            c.shift_anchors(grown)
            try:
                c._update_composer_pad_phantom()
            except Exception:
                pass
        following = self.owner.sheet.is_following_tail()
        if following and c.caret_owner() == "draft":
            c.scroll_to_end(force=False)
        return True

    def _replant_composer(self, draft="", caret_off=0):
        c = self.owner.composer
        c._input_mode = False
        view = self.owner.view
        if view:
            keys.write_setting(view.settings(), keys.INPUT_MODE, False)
        c.enter_input_mode()
        if not c.is_input_mode() or not view:
            return
        if draft:
            view.set_read_only(False)
            view.run_command("append", {"characters": draft})
            c._update_composer_pad_phantom()
        if sublime is not None:
            pos = min(c._input_start + max(0, caret_off), view.size())
            view.sel().clear()
            view.sel().add(sublime.Region(pos, pos))
        view.set_read_only(False)
        c.focus(force_show=True, preserve_caret=True)

    # --- media / context phantoms (subset of old OutputView) ---------------

    def _media_path_for_tool(self, tool):
        from .formatters import extract_media_path
        if not isinstance(tool.tool_input, dict):
            return extract_media_path(tool.result, None)
        return (
            tool.tool_input.get("_media_path")
            or extract_media_path(tool.result, tool.tool_input)
        )

    def _refresh_media_phantoms(self):
        view = self.owner.view
        if not view or not view.is_valid() or sublime is None:
            return
        from .formatters import is_image_path, is_media_tool_name, media_display_path
        if (self._media_phantom_set is None
                or getattr(self, "_media_phantom_view_id", None) != view.id()):
            try:
                self._media_phantom_set = sublime.PhantomSet(view, keys.PHANTOM_MEDIA)
                self._media_phantom_view_id = view.id()
            except Exception:
                return
        media_tools = []
        for conv in list(self.conversations) + (
                [self.current] if self.current is not None else []):
            for event in conv.events:
                if (isinstance(event, ToolCall)
                        and is_media_tool_name(event.name)
                        and event.status == DONE):
                    media_tools.append(event)
        if not media_tools:
            self._media_anchor.clear()
            try:
                self._media_phantom_set.update([])
            except Exception:
                pass
            return
        content = view.substr(_R(0, view.size()))
        phantoms = []
        used_pts = set()
        anchors = {}
        sym = SYMBOLS.get(DONE, "✔")
        for tool in media_tools:
            path = self._media_path_for_tool(tool)
            if not path or not is_image_path(path) or not os.path.isfile(path):
                continue
            disp = media_display_path(path)
            name_pat = re.escape(tool.name)
            short = tool.name.split("__")[-1] if "__" in tool.name else tool.name
            if short != tool.name:
                name_pat = "(?:%s|%s)" % (re.escape(tool.name), re.escape(short))
            pat = r"(?m)^  " + re.escape(sym) + r" " + name_pat + r".*"
            candidates = []
            base = os.path.basename(path)
            for m in re.finditer(pat, content):
                line = m.group(0)
                if disp and disp in line:
                    candidates.append(m.start())
                elif path in line or (base and base in line):
                    candidates.append(m.start())
                elif not candidates:
                    candidates.append(m.start())
            if not candidates:
                continue
            pt = candidates[-1]
            if pt in used_pts:
                continue
            used_pts.add(pt)
            line_end = content.find("\n", pt)
            if line_end < 0:
                line_end = len(content)
            anchors[path] = line_end
            html = self._media_phantom_html(path, disp)
            if not html:
                continue

            def _nav(href, _path=path, _loc=line_end):
                self.owner._handle_media_href(href, _path, location=_loc)

            try:
                phantoms.append(sublime.Phantom(
                    sublime.Region(line_end, line_end), html,
                    sublime.LAYOUT_BLOCK, _nav))
            except TypeError:
                phantoms.append(sublime.Phantom(
                    sublime.Region(line_end, line_end), html,
                    sublime.LAYOUT_BLOCK))
        self._media_anchor = anchors
        try:
            self._media_phantom_set.update(phantoms)
        except Exception as e:
            print("[Submarine] media phantoms: %s" % e)

    def _minihtml_image_ok(self, path):
        return bool(path) and path.lower().endswith(self._MINIHTML_IMAGE_EXTS)

    def _image_dimensions_from_bytes(self, data):
        try:
            import struct
            if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
                w, h = struct.unpack(">II", data[16:24])
                return int(w), int(h)
            if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
                w, h = struct.unpack("<HH", data[6:10])
                return int(w), int(h)
            if data[:2] == b"\xff\xd8":
                i = 2
                n = len(data)
                while i + 9 < n:
                    if data[i] != 0xFF:
                        break
                    marker = data[i + 1]
                    seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
                    if marker in (
                        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
                    ):
                        h, w = struct.unpack(">HH", data[i + 5:i + 9])
                        return int(w), int(h)
                    i += 2 + seglen
        except Exception:
            pass
        return 0, 0

    def _image_dimensions(self, path):
        try:
            with open(path, "rb") as f:
                return self._image_dimensions_from_bytes(f.read(65536))
        except Exception:
            return 0, 0

    def _scaled_display_size(self, w, h, max_w):
        if w <= 0 or h <= 0:
            return max_w, max_w
        if w <= max_w and h <= max_w:
            return w, h
        scale = min(max_w / float(w), max_w / float(h))
        return max(1, int(round(w * scale))), max(1, int(round(h * scale)))

    def _make_thumbnail_bytes(self, path, max_edge):
        ow, oh = self._image_dimensions(path)
        tw, th = self._scaled_display_size(ow, oh, max_edge)
        try:
            from PIL import Image  # type: ignore
            import io
            with Image.open(path) as im:
                im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
                if im.mode == "L":
                    im = im.convert("RGB")
                resample = (Image.Resampling.LANCZOS
                            if hasattr(Image, "Resampling") else Image.LANCZOS)
                im.thumbnail((max_edge, max_edge), resample)
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=72, optimize=True)
                data = buf.getvalue()
                return data, im.size[0], im.size[1]
        except Exception:
            pass
        plat = ""
        try:
            plat = sublime.platform() if sublime is not None else ""
        except Exception:
            plat = ""
        if plat == "osx" or (not plat and os.path.isfile("/usr/bin/sips")):
            import subprocess
            import tempfile
            sips = "/usr/bin/sips"
            if not os.path.isfile(sips):
                sips = "sips"
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(suffix=".jpg")
                os.close(fd)
                r = subprocess.run(
                    [sips, "-Z", str(max_edge), "-s", "format", "jpeg",
                     path, "--out", tmp],
                    capture_output=True, timeout=15)
                if r.returncode == 0 and os.path.isfile(tmp):
                    with open(tmp, "rb") as f:
                        data = f.read()
                    if data:
                        w, h = self._image_dimensions_from_bytes(data)
                        if w <= 0:
                            w, h = tw, th
                        return data, w, h
            except Exception:
                pass
            finally:
                if tmp and os.path.isfile(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        return None

    def _media_embed(self, path, max_edge=None):
        """(data_uri, width, height) thumbnail for minihtml, or None."""
        if max_edge is None:
            max_edge = self._MEDIA_PHANTOM_MAX_W
        if not self._minihtml_image_ok(path) or not os.path.isfile(path):
            return None
        try:
            mtime = os.path.getmtime(path)
            size = os.path.getsize(path)
            if size <= 0 or size > self._MEDIA_SOURCE_MAX_BYTES:
                return None
            cache_key = "%s|%s" % (path, max_edge)
            cached = self._media_uri_cache.get(cache_key)
            if cached and cached[0] == mtime:
                return cached[1], cached[2], cached[3]
            import base64
            thumb = self._make_thumbnail_bytes(path, max_edge)
            if thumb:
                data, w, h = thumb
                uri = "data:image/jpeg;base64," + base64.b64encode(data).decode(
                    "ascii")
                self._media_uri_cache[cache_key] = (mtime, uri, w, h)
                return uri, w, h
            if size > 40_000:
                return None
            with open(path, "rb") as f:
                raw = f.read()
            ext = os.path.splitext(path)[1].lower()
            mime = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".gif": "image/gif",
            }.get(ext, "image/jpeg")
            uri = "data:%s;base64,%s" % (
                mime, base64.b64encode(raw).decode("ascii"))
            w, h = self._image_dimensions_from_bytes(raw)
            w, h = self._scaled_display_size(w, h, max_edge)
            self._media_uri_cache[cache_key] = (mtime, uri, w, h)
            return uri, w, h
        except Exception as e:
            print("[Submarine] media embed: %s" % e)
            return None

    def _media_phantom_html(self, path, disp):
        import html as _html
        safe_disp = _html.escape(disp or os.path.basename(path) or path)
        reveal_href = "reveal:" + path
        popup_href = "popup:" + path
        edit_href = "edit:" + path
        links = (
            '<a href="%s">reveal</a> · <a href="%s">enlarge</a>'
            ' · <a href="%s" title="use as image_edit target">edit</a>'
            ' · <span style="color:color(var(--foreground) alpha(0.5))">%s</span>'
            % (_html.escape(reveal_href), _html.escape(popup_href),
               _html.escape(edit_href), safe_disp)
        )
        embed = self._media_embed(path, self._MEDIA_PHANTOM_MAX_W)
        if embed:
            uri, w, h = embed
            img = (
                '<div style="margin:2px 0 2px 0">'
                '<a href="%s"><img src="%s" width="%d" height="%d" /></a>'
                "</div>" % (_html.escape(popup_href), uri, w, h)
            )
            return (
                '<body id="submarine-media-phantom" '
                'style="margin:0;padding:0 0 0 24px;font-size:11px;'
                'color:color(var(--foreground) alpha(0.7))">'
                "%s%s</body>" % (img, links)
            )
        return (
            '<body id="submarine-media-phantom" '
            'style="margin:0;padding:2px 0 4px 24px;font-size:11px;'
            'color:color(var(--foreground) alpha(0.7))">'
            "🖼 %s</body>" % links
        )

    def _materialize_turn_context_line(self, conv):
        view = self.owner.view
        if not view or not conv:
            return
        names = list(conv.context_names or [])
        if not names and conv.context_refs:
            names = [r.get("name") or "?" for r in conv.context_refs]
        if not names:
            return
        # Buffer already has 📎; names live in phantoms — leave as-is.

    def _refresh_turn_context_phantoms(self, refs, region=None):
        from features.context import chip_ref

        view = self.owner.view
        if not view or not view.is_valid() or sublime is None:
            return
        try:
            if (self._turn_context_phantom_set is None
                    or getattr(self, "_turn_ctx_view_id", None) != view.id()):
                self._turn_context_phantom_set = sublime.PhantomSet(
                    view, keys.PHANTOM_TURN_CONTEXT)
                self._turn_ctx_view_id = view.id()
        except Exception:
            return
        if not refs:
            self._clear_turn_context_phantoms()
            return
        if region:
            a, b = region
        elif self.current and self.current.region:
            a, b = self.current.region
        else:
            return
        content = view.substr(_R(a, b))
        idx = content.find(CONTEXT_PREFIX.strip())
        if idx < 0:
            return
        pt = a + idx + len(CONTEXT_PREFIX.strip())
        chips = []
        for i, ref in enumerate(refs):
            ref = chip_ref(ref)
            what = "open" if ref["action"] == "open" else "reveal"
            tip = "click to %s %s" % (what, ref["path"] or ref["name"])
            chips.append(
                '<a href="turn:%d" title="%s">%s</a>'
                % (i, _esc(tip), _esc(ref["name"])))
        html = (
            '<body id="submarine-turn-ctx" style="margin:0;padding:0 0 0 6px;'
            'font-size:11px;">%s</body>' % " · ".join(chips)
        )

        def _nav(href, r=list(refs)):
            self.owner._handle_context_href(href)

        try:
            self._turn_context_phantom_set.update([
                sublime.Phantom(sublime.Region(pt, pt), html, sublime.LAYOUT_INLINE, _nav)
            ])
        except Exception:
            pass

    def _clear_turn_context_phantoms(self):
        if self._turn_context_phantom_set is not None:
            try:
                self._turn_context_phantom_set.update([])
            except Exception:
                pass

    def _iter_artifact_cards(self):
        convs = list(self.conversations)
        if self.current is not None:
            convs.append(self.current)
        for conv in convs:
            for event in conv.events:
                if isinstance(event, ArtifactCard):
                    yield event

    def _refresh_artifact_phantoms(self):
        """Overlay [open]/[path] hrefs. Chrome only — skipped when detached."""
        view = self.owner.view
        if not view or not view.is_valid() or sublime is None:
            return
        if (self._artifact_phantom_set is None
                or getattr(self, "_artifact_phantom_view_id", None) != view.id()):
            try:
                self._artifact_phantom_set = sublime.PhantomSet(
                    view, keys.PHANTOM_ARTIFACT)
                self._artifact_phantom_view_id = view.id()
            except Exception:
                return
        cards = list(self._iter_artifact_cards())
        if not cards:
            try:
                self._artifact_phantom_set.update([])
            except Exception:
                pass
            return
        import html as _html
        content = view.substr(_R(0, view.size()))
        phantoms = []
        used = set()
        for card in cards:
            line = (card.line() or "").rstrip("\n")
            if not line:
                continue
            start = 0
            loc = -1
            while True:
                idx = content.find(line, start)
                if idx < 0:
                    break
                if idx not in used:
                    loc = idx
                    used.add(idx)
                    break
                start = idx + 1
            if loc < 0:
                continue
            open_at = content.find("[open]", loc)
            path_at = content.find("[path]", loc)
            line_end = content.find("\n", loc)
            if line_end < 0:
                line_end = loc + len(line)
            if open_at < 0 or open_at > line_end:
                continue
            href_open = "artifact-open:%s" % card.path
            href_copy = "artifact-path:%s" % card.path
            html = (
                '<body id="submarine-artifact" style="margin:0;padding:0;'
                'font-size:11px;"><a href="%s">open</a> · '
                '<a href="%s">path</a></body>'
                % (_html.escape(href_open), _html.escape(href_copy))
            )
            pt = path_at + 6 if path_at >= 0 and path_at <= line_end else open_at + 6

            def _nav(href, _p=card.path):
                self.owner._handle_artifact_href(href, _p)

            try:
                phantoms.append(sublime.Phantom(
                    sublime.Region(pt, pt), html, sublime.LAYOUT_INLINE, _nav))
            except Exception:
                pass
        try:
            self._artifact_phantom_set.update(phantoms)
        except Exception:
            pass


_REPLAYABLE = frozenset((
    "prompt", "text", "tool", "tool_done", "tool_error",
    "artifact_card",
    "meta", "interrupted", "apply_plan_todos", "set_retry_hint",
    "clear", "clear_keep_last", "reset_active_states",
))


def _clone_conv(conv):
    """Deep-ish copy of a Conversation so catch-up can rewind without aliasing."""
    if conv is None:
        return None
    from dataclasses import replace
    events = []
    for e in conv.events:
        if isinstance(e, ToolCall):
            tin = e.tool_input
            events.append(ToolCall(
                name=e.name,
                tool_input=dict(tin) if isinstance(tin, dict) else tin,
                status=e.status,
                result=e.result,
                id=e.id,
            ))
        elif isinstance(e, ArtifactCard):
            events.append(replace(e))
        else:
            events.append(e)
    todos = []
    for t in conv.todos or []:
        try:
            todos.append(replace(t))
        except Exception:
            todos.append(t)
    goal = None
    if conv.goal is not None:
        try:
            goal = replace(conv.goal)
        except Exception:
            goal = conv.goal
    usage = dict(conv.usage) if isinstance(conv.usage, dict) else conv.usage
    region = conv.region
    if region is not None:
        try:
            region = tuple(region)
        except Exception:
            pass
    return Conversation(
        prompt=conv.prompt,
        events=events,
        todos=todos,
        todos_all_done=conv.todos_all_done,
        goal=goal,
        working=conv.working,
        duration=conv.duration,
        has_meta=conv.has_meta,
        usage=usage,
        region=region,
        context_names=list(conv.context_names or []),
        context_refs=[
            dict(r) if isinstance(r, dict) else r
            for r in (conv.context_refs or [])
        ],
    )


def _R(a, b):
    if sublime is not None:
        return sublime.Region(a, b)
    return type("R", (), {"a": a, "b": b})()


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
