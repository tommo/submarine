"""session/update fan-in: live paint, load-replay, mode/commands.

Invariants: drop UI updates whose sessionId ≠ parent (§9.27); ignore
user_message_chunk (plugin already painted ◎) (§9.28); session/load
replay must not paint or set working (§9.29).
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from typing import Optional

_BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_DIR)

from rpc_helpers import send_notification  # noqa: E402


class UpdatesMixin:
    # ── session/update → Sublime message notifications ─────────────────

    def _is_foreign_session(self, params: Optional[dict]) -> bool:
        """True when update/request is for a subagent (or other) sessionId.

        Grok Build streams subagent turns on the *parent* ACP pipe, each tagged
        with the child sessionId. Painting those agent_message_chunk /
        tool_call rows into the parent view floods the transcript. fs/terminal
        *requests* still use child sessionIds and must keep being answered —
        only UI-bound notifications are filtered via this helper.
        """
        if not params or not isinstance(params, dict):
            return False
        if not self.session_id:
            return False
        sid = params.get("sessionId") or params.get("session_id")
        if not sid:
            return False
        return str(sid) != str(self.session_id)

    def _note_foreign_session_drop(self, kind: str, params: dict) -> None:
        self._foreign_session_drops += 1
        n = self._foreign_session_drops
        # Log first few + then every 50th so parallel subagents are visible
        # without drowning the bridge log.
        if n <= 5 or n % 50 == 0:
            sid = params.get("sessionId") or params.get("session_id") or "?"
            self.file_log(
                f"drop foreign session update #{n} kind={kind!r} "
                f"sid={sid} (parent={self.session_id})")

    _SUBAGENT_ID_RE = re.compile(r"subagent_id:\s*([0-9A-Za-z._-]+)", re.I)
    _CHILD_TEXT_CAP = 80_000

    def _register_child_session(self, sid: str) -> dict:
        sid = str(sid or "").strip()
        if not sid:
            return {}
        if not hasattr(self, "_child_sessions"):
            self._child_sessions = {}
        slot = self._child_sessions.get(sid)
        if slot is None:
            slot = {
                "text": "",
                "done": False,
                "exit": None,
                "event": asyncio.Event(),
            }
            self._child_sessions[sid] = slot
        if not slot.get("tool_use_id"):
            tuid = self._unbound_spawn_tool_id()
            if tuid:
                self._apply_child_tool_bind(sid, slot, tuid)
        return slot

    def _bound_child_tool_ids(self) -> set:
        return {
            s.get("tool_use_id")
            for s in (getattr(self, "_child_sessions", {}) or {}).values()
            if s and s.get("tool_use_id")
        }

    def _unbound_spawn_tool_id(self) -> Optional[str]:
        """Most recent Subagent/Task row that has no child yet."""
        bound = self._bound_child_tool_ids()
        found = None
        for tid, name in (getattr(self, "_tool_names_by_id", {}) or {}).items():
            if not self._is_subagent_tool_name(name or ""):
                continue
            if tid in bound:
                continue
            found = tid
        if found:
            return found
        last = getattr(self, "_last_bg_tool_id", None)
        if last and last not in bound:
            return last
        for tid in getattr(self, "_bg_tool_ids", ()) or ():
            if tid not in bound:
                return tid
        return None

    def _apply_child_tool_bind(
            self, sid: str, slot: dict, tool_use_id: str) -> None:
        """Bind sid → spawn tool_use_id. Emit task_started even if done."""
        if not slot or not tool_use_id:
            return
        late = bool(slot.get("done")) and not slot.get("tool_use_id")
        slot["tool_use_id"] = tool_use_id
        inp = (getattr(self, "_tool_inputs_by_id", {}) or {}).get(
            tool_use_id) or {}
        desc = (
            slot.get("description")
            or inp.get("description")
            or inp.get("title")
            or inp.get("prompt")
            or ""
        )
        if desc and not self._is_child_session_id(str(desc)):
            slot["description"] = desc
        if tool_use_id not in getattr(self, "_bg_tool_ids", set()):
            self._bg_tool_ids.add(tool_use_id)
            try:
                send_notification("message", {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": (
                        (getattr(self, "_tool_names_by_id", {}) or {}).get(
                            tool_use_id) or "Subagent"
                    ),
                    "input": {**inp, "run_in_background": True},
                    "background": True,
                })
            except Exception:
                pass
        task_id = f"acp-child-{sid}"
        # Always emit once we know the real tool_use_id so the host can
        # close the ⚙ row — even if completion already fired (zombie fix).
        if task_id not in getattr(self, "_bg_notified_tasks", ()) or late:
            self._emit_system("task_started", {
                "task_id": task_id,
                "tool_use_id": tool_use_id,
            })
        if late:
            es = slot.get("exit") or {}
            status = "failed" if es.get("signal") else "completed"
            self._emit_system("task_updated", {
                "task_id": task_id,
                "tool_use_id": tool_use_id,
                "patch": {"status": status},
            })

    def _note_spawned_child(self, text: Optional[str]) -> None:
        if not text:
            return
        for m in self._SUBAGENT_ID_RE.finditer(str(text)):
            self._register_child_session(m.group(1))

    def _bind_child_to_tool(self, text: Optional[str], tool_use_id: str) -> None:
        if not text or not tool_use_id:
            return
        for m in self._SUBAGENT_ID_RE.finditer(str(text)):
            sid = m.group(1)
            slot = self._register_child_session(sid)
            if not slot:
                continue
            self._apply_child_tool_bind(sid, slot, tool_use_id)

    def _child_complete_status(self, slot: dict) -> str:
        es = slot.get("exit") or {}
        if es.get("signal"):
            return "failed"
        code = es.get("exitCode")
        return "completed" if code in (0, None) else "failed"

    def _emit_bg_child_complete(self, sid: str, slot: dict) -> None:
        if not slot.get("tool_use_id"):
            tuid = self._unbound_spawn_tool_id()
            if tuid:
                self._apply_child_tool_bind(sid, slot, tuid)
        tuid = slot.get("tool_use_id") or ""
        task_id = f"acp-child-{sid}"
        if self._should_skip_bg_notify(task_id, tuid):
            return
        out = slot.get("text") or ""
        path = self._write_bg_output_file("acp-child-", out)
        desc = (slot.get("description") or "").strip()
        if self._is_child_session_id(desc):
            desc = ""
        first = out.strip().split("\n", 1)[0] if out.strip() else ""
        if first and self._is_child_session_id(first):
            first = ""
        line = first or desc or "subagent"
        es = slot.get("exit") or {}
        code = es.get("exitCode")
        status = self._child_complete_status(slot)
        self._emit_bg_finished(
            task_id, tuid, status, self._clip_bg_summary(line, code), path)

    def _cancel_child_sessions(self, reason: str = "interrupt") -> None:
        """Unblock waiters and fail live ⚙ rows (interrupt / clear / shutdown)."""
        cancelled = {"exitCode": None, "signal": "SIGTERM"}
        for sid, slot in list(
                (getattr(self, "_child_sessions", {}) or {}).items()):
            if not slot:
                continue
            already_done = bool(slot.get("done"))
            slot["done"] = True
            if slot.get("exit") is None:
                slot["exit"] = dict(cancelled)
            ev = slot.get("event")
            if ev is not None and not ev.is_set():
                ev.set()
            if already_done:
                continue
            try:
                self._emit_bg_child_complete(str(sid), slot)
            except Exception as e:
                self.file_log(
                    f"cancel child {sid} ({reason}): {e}")

    def _ingest_child_session(self, params: dict) -> None:
        sid = params.get("sessionId") or params.get("session_id")
        if not sid:
            return
        slot = self._register_child_session(str(sid))
        if not slot or slot.get("done"):
            return
        upd = params.get("update") or {}
        if not isinstance(upd, dict):
            return
        kind = upd.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = ((upd.get("content") or {}) or {}).get("text") or ""
            if text:
                buf = (slot.get("text") or "") + str(text)
                slot["text"] = buf[-self._CHILD_TEXT_CAP:]
        elif kind == "turn_completed":
            stop = (
                upd.get("stop_reason") or upd.get("stopReason") or "end_turn")
            ok = str(stop) in ("end_turn", "max_tokens", "stop")
            slot["done"] = True
            slot["exit"] = {
                "exitCode": 0 if ok else 1,
                "signal": None,
            }
            ev = slot.get("event")
            if ev is not None and not ev.is_set():
                ev.set()
            self._emit_bg_child_complete(str(sid), slot)

    def _child_output_payload(self, slot: dict) -> dict:
        out = {
            "output": slot.get("text") or "",
            "truncated": False,
        }
        if slot.get("done") and slot.get("exit") is not None:
            out["exitStatus"] = slot["exit"]
        return out

    def _forward_update(self, params: dict) -> None:
        # Defense in depth: reader already drops foreign sessions; keep
        # filter here if anything calls this path directly.
        if self._is_foreign_session(params):
            kind = (params.get("update") or {}).get("sessionUpdate")
            self._ingest_child_session(params)
            self._note_foreign_session_drop(kind or "forward", params)
            return

        if self._loading_session:
            self._forward_load_replay(params)
            return

        upd = params.get("update", {})
        kind = upd.get("sessionUpdate")
        # Only suppress streams while *our* host prompt is being cancelled.
        # When idle / auto-continue after end_turn, _prompt_cancelled must not
        # black-hole agent activity (that left the view empty while kimi worked).
        host_prompt_live = (
            self._prompt_fut is not None and not self._prompt_fut.done())
        if self._prompt_cancelled and not host_prompt_live:
            # Stale cancel flag after prompt ended — clear so auto-continue paints.
            # Remember leftover_end so turn_completed can close interrupt busy
            # without firing after every successful Grok end_turn.
            self._prompt_cancelled = False
            self._leftover_end_pending = True
        suppress = bool(self._prompt_cancelled and host_prompt_live)
        # After user interrupt: drop *new* tool starts so ☐ rows don't appear
        # post-[interrupted]. Still accept tool_call_update completions so
        # already-open rows can settle.
        if suppress and kind == "tool_call":
            self.file_log(
                f"drop tool_call after cancel: "
                f"{(upd.get('title') or upd.get('toolCallId') or '')!r}")
            return
        if kind == "agent_message_chunk":
            if suppress:
                return
            text = (upd.get("content") or {}).get("text", "")
            if text:
                send_notification("message",
                                  {"type": "text_delta", "text": text})
        elif kind == "agent_thought_chunk":
            if suppress:
                return
            text = (upd.get("content") or {}).get("text", "")
            if text:
                send_notification("message",
                                  {"type": "thinking", "thinking": text})
        elif kind == "tool_call":
            tool_name = self._normalize_tool_name(upd)
            tool_input = self._tool_input_from_update(upd, tool_name)
            tool_name, tool_input = self._reclassify_read_dir(
                tool_name, tool_input)
            tid = upd.get("toolCallId")
            # Kimi lifecycle rows (title "Starting") — never paint ☐/✔ noise
            if self._should_suppress_tool_row(upd, tool_name):
                self.file_log(
                    f"suppress tool_call noise: title={upd.get('title')!r} "
                    f"name={tool_name!r} id={tid!r}")
                return
            if tid:
                if tool_name and tool_name != "tool":
                    self._tool_names_by_id[tid] = tool_name
                if tool_input:
                    prev = self._tool_inputs_by_id.get(tid) or {}
                    self._tool_inputs_by_id[tid] = {**prev, **tool_input}
            # Kimi streams tool_call with empty input before title is useful;
            # still emit when we have a real name so UI is not "☐ tool".
            if tool_name == "tool" and not tool_input:
                # Wait for tool_call_update with title/kind/rawInput
                return
            # Kimi: title "Bash" then JSON drip. Emitting an empty row here
            # plus later run_in_background painted two ⚙ with no process.
            if (self._is_shell_tool_name(tool_name)
                    and not (isinstance(tool_input, dict)
                             and tool_input.get("command"))):
                if tid:
                    self._note_shell_execute(tid, tool_name)
                return
            # One open ExitPlanMode at a time — second toolCallId aliases the first
            # (permission + session/update double-open painted two rows).
            if tool_name in (
                "ExitPlanMode", "EnterPlanMode", "ask_user", "AskUserQuestion",
            ) and tid:
                for oid, oname in list(self._tool_names_by_id.items()):
                    if (self._same_modal_tool(oname, tool_name) and oid != tid
                            and oid in self._tool_ids_emitted):
                        if not hasattr(self, "_tool_id_alias"):
                            self._tool_id_alias = {}
                        self._tool_id_alias[tid] = oid
                        self._tool_names_by_id[tid] = tool_name
                        if tool_input:
                            prev = self._tool_inputs_by_id.get(oid) or {}
                            self._tool_inputs_by_id[oid] = {**prev, **tool_input}
                        self.file_log(
                            f"alias {tool_name} {tid} → open {oid} (no 2nd row)")
                        return
            self._tool_ids_emitted.add(tid)
            self._note_shell_execute(tid, tool_name)
            is_spawn = self._is_subagent_spawn(tool_name, upd, tool_input)
            is_bg = is_spawn or self._looks_like_background_tool(
                upd, tool_input)
            # Shell ⚙ waits for terminal/create. Task spawn is ⚙ immediately.
            if is_bg and not (
                    self._is_shell_tool_name(tool_name)
                    or self._is_subagent_tool_name(tool_name)):
                is_bg = False
                is_spawn = False
            if is_bg and tid and isinstance(tool_input, dict):
                tool_input = {**tool_input, "run_in_background": True}
                self._tool_inputs_by_id[tid] = {
                    **(self._tool_inputs_by_id.get(tid) or {}),
                    **tool_input,
                }
            if is_spawn and tid:
                self._bg_tool_ids.add(tid)
                self._last_bg_tool_id = tid
            send_notification("message", {
                "type": "tool_use",
                "id": tid,
                "name": tool_name,
                "input": tool_input,
                "background": bool(is_spawn),
            })
        elif kind == "tool_call_update":
            usage = self.usage_from_tool_update(upd)
            if usage is not None:
                send_notification("message",
                                  {"type": "turn_usage", "usage": usage})
            status = upd.get("status")
            tid = self._resolve_tool_id(upd.get("toolCallId"))
            tool_name = self._normalize_tool_name(upd)
            # Completed updates often strip title/_meta → name becomes "tool".
            # Recover the name we saw on the open tool_call / earlier update.
            if (not tool_name or tool_name == "tool") and tid:
                tool_name = self._tool_names_by_id.get(tid) or tool_name or "tool"
            elif tid and tool_name and tool_name != "tool":
                self._tool_names_by_id[tid] = tool_name
            if status not in ("completed", "failed"):
                self._note_shell_execute(tid, tool_name)
            # Lifecycle rows that never opened a real tool — drop entirely
            if (self._should_suppress_tool_row(upd, tool_name)
                    and (not tid or tid not in self._tool_ids_emitted)):
                self.file_log(
                    f"suppress tool_call_update noise: "
                    f"title={upd.get('title')!r} name={tool_name!r} "
                    f"status={status!r}")
                return
            # Ext method already closed this id (ask_user / ExitPlanMode).
            # Re-emitting tool_use after ✔ opens a second ☐ (plugin only
            # upserts PENDING rows).
            if tid and tid in getattr(self, "_tool_results_sent", set()):
                return
            # Grok: bare tool_call then richer update. Emit tool_use at most
            # once per id (plugin upserts); re-emitting created a second ☐
            # that never received tool_result → last row stuck pending.
            enriched = self._tool_input_from_update(upd, tool_name)
            tool_name, enriched = self._reclassify_read_dir(tool_name, enriched)
            if tid and tool_name and tool_name != "tool":
                self._tool_names_by_id[tid] = tool_name
            if tid and enriched:
                prev = self._tool_inputs_by_id.get(tid) or {}
                self._tool_inputs_by_id[tid] = {**prev, **enriched}
            if tid not in self._tool_ids_emitted:
                # Skip anonymous early stream chunks (Kimi JSON drip without title)
                if tool_name == "tool" and status not in ("completed", "failed"):
                    return
                # Don't open a brand-new row only to close lifecycle noise
                if (tool_name == "tool"
                        and status in ("completed", "failed")
                        and not enriched
                        and not self._tool_update_has_substance(upd)):
                    return
                if (enriched or upd.get("rawInput") or upd.get("locations")
                        or upd.get("title") or status in ("completed", "failed")):
                    # Skip opening rows for lifecycle titles at completed
                    if self._should_suppress_tool_row(upd, tool_name):
                        return
                    self._tool_ids_emitted.add(tid)
                    send_notification("message", {
                        "type": "tool_use",
                        "id": tid,
                        "name": tool_name,
                        "input": enriched or self._tool_inputs_by_id.get(tid) or {},
                        "background": bool(tid and tid in self._bg_tool_ids),
                    })
            elif tool_name != "tool" or (enriched and status not in ("completed", "failed")):
                # Enrich open row (same id → output.tool upserts). Prefer real name.
                # kimi-cli streams arg JSON one token at a time as tool_call_update;
                # only re-paint when title/args became usable (not every drip).
                enrich_name = tool_name
                if enrich_name == "tool" and tid:
                    enrich_name = self._tool_names_by_id.get(tid) or "tool"
                if not self._should_suppress_tool_row(upd, enrich_name):
                    if status in ("completed", "failed") or self._should_repaint_tool(
                            tid, upd, enriched):
                        send_notification("message", {
                            "type": "tool_use",
                            "id": tid,
                            "name": enrich_name,
                            "input": enriched or self._tool_inputs_by_id.get(tid) or {},
                            # Re-paint must not demote a ⚙ row (spawn ack keeps
                            # background until child/task_notification closes).
                            "background": bool(tid and tid in self._bg_tool_ids),
                        })
            # Cache run_in_background for create pairing. Do not ⚙ / do not
            # drop pending here — that left ⚙ unbound when create arrived
            # later (or never), so the row never cleared.
            is_bg = bool(
                tid and self._is_shell_tool_name(tool_name) and (
                    tid in self._bg_tool_ids
                    or self._looks_like_background_tool(upd, enriched)
                )
            )
            if is_bg and tid:
                cached = dict(self._tool_inputs_by_id.get(tid) or {})
                if isinstance(enriched, dict):
                    cached.update(enriched)
                cached["run_in_background"] = True
                self._tool_inputs_by_id[tid] = cached
                for term_id in self._terminal_ids_from_update(upd):
                    slot = self._terminals.get(term_id)
                    if slot is None:
                        continue
                    if tid not in self._bg_tool_ids:
                        self._register_bg_tool(
                            tid, cached, str(upd.get("title") or ""))
                    self._bind_terminal_to_bg_tool(term_id, tid)
                    slot["bg"] = True
                    slot["tool_use_id"] = tid

            if status in ("completed", "failed"):
                if tid and tid in getattr(self, "_tool_results_sent", set()):
                    return
                # No open row for this id → nothing to close (noise already dropped)
                if tid not in self._tool_ids_emitted and not self._tool_update_has_substance(upd):
                    # may have been suppressed at open
                    if self._should_suppress_tool_row(upd, tool_name) or tool_name == "tool":
                        return
                diff_input = (
                    self._extract_diff_input(upd)
                    if tool_name in ("Edit", "Write") else None
                )
                if diff_input:
                    # Attach diff onto the open row before closing (upsert).
                    payload = dict(enriched or {})
                    payload.update(diff_input)
                    if tid not in self._tool_ids_emitted:
                        self._tool_ids_emitted.add(tid)
                    send_notification("message", {
                        "type": "tool_use",
                        "id": tid,
                        "name": tool_name,
                        "input": payload,
                        "background": bool(tid and tid in self._bg_tool_ids),
                    })
                # Bind terminal ids on completed payload (Kimi attaches them here)
                if tid and tid in self._bg_tool_ids:
                    for term_id in self._terminal_ids_from_update(upd):
                        self._bind_terminal_to_bg_tool(term_id, tid)

                text = self._extract_tool_content(upd, tool_name)
                self._note_spawned_child(text)
                if tid:
                    self._bind_child_to_tool(text, tid)
                is_error = status == "failed"
                # Grok read_file marks images failed ("Cannot read binary file")
                # even after a successful fs/read — pixels need read_image, not
                # text FS. Don't paint a red FAILED when path is an image; the
                # agent can still use the path (image_edit) or read_image.
                if is_error:
                    soft = self._soften_image_read_fail(text, enriched, tool_name)
                    if soft is not None:
                        text, is_error = soft

                # TaskOutput / "Reading output of task …" only *polls* a
                # bash-* job — never register it as a new ⚙ background tool.
                # spawn_subagent is Subagent (launch), not a poll.
                _title_l = str(upd.get("title") or "").lower()
                _inp = enriched if isinstance(enriched, dict) else {}
                is_task_poll = (
                    tool_name in ("TaskGet", "TaskOutput")
                    or "reading output of task" in _title_l
                    or "get task output" in _title_l
                    or bool(_inp.get("task_ids")
                            or _inp.get("task_id")
                            or _inp.get("taskId"))
                ) and not self._is_subagent_spawn(tool_name, upd, _inp)
                if self._kimi_handle_tool_result(
                        tid, tool_name, text, enriched, is_task_poll,
                        status, upd):
                    return

                # ACP-terminal / subagent background: tool_result is only an
                # ack (host keeps ⚙ until task_notification).
                if (
                    tid
                    and tid in self._bg_tool_ids
                    and status == "completed"
                    and (
                        self._is_shell_tool_name(tool_name)
                        or self._is_subagent_tool_name(tool_name)
                    )
                    and not is_task_poll
                ):
                    send_notification("message", {
                        "type": "tool_result",
                        "tool_use_id": tid,
                        "content": text or "background",
                        "is_error": False,
                    })
                    # Keep name/input maps until process / child exit
                    return
                if tid and tid in self._bg_tool_ids and (
                        is_task_poll or not (
                            self._is_shell_tool_name(tool_name)
                            or self._is_subagent_tool_name(tool_name))):
                    # Drop mistaken bg mark so normal tool_result can close the row
                    self._bg_tool_ids.discard(tid)

                send_notification("message", {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": text,
                    "is_error": is_error,
                })
                if tid:
                    self._tool_results_sent.add(tid)
                self._tool_ids_emitted.discard(tid)
                # Drop aliases that pointed at this primary
                for alias, primary in list(
                        getattr(self, "_tool_id_alias", {}).items()):
                    if primary == tid or alias == tid:
                        self._tool_id_alias.pop(alias, None)
                if not is_error and tool_name in (
                    "scheduler_create", "CronCreate", "ScheduleWakeup",
                    "scheduler_delete", "CronDelete", "SchedulerDelete",
                ):
                    # completed updates often drop rawInput — use cached input.
                    cached = self._tool_inputs_by_id.get(tid) or {}
                    merged = {**cached, **(enriched or {})}
                    self.file_log(
                        f"scheduler complete name={tool_name} tid={tid} "
                        f"keys={list(merged.keys())}")
                    self._note_scheduler_tool_result(
                        tool_name, merged, text, tool_call_id=tid or "")
                self._tool_inputs_by_id.pop(tid, None)
                self._tool_names_by_id.pop(tid, None)
                self._bg_tool_ids.discard(tid)
        elif kind == "user_message_chunk":
            # Agents (notably Grok) re-broadcast the user prompt. The plugin
            # already renders ◎ <prompt> — do not double-print as text_delta.
            pass
        elif kind == "plan":
            # Kimi TodoWrite lands as ACP plan entries (title/status), not only
            # tool_use.rawInput. Drive the plugin Tasks strip; never dump a
            # **Plan:** text block into the transcript.
            entries = upd.get("entries") or upd.get("plan") or []
            if isinstance(entries, list) and entries:
                send_notification("message", {
                    "type": "plan_todos",
                    "entries": entries,
                })
        elif kind == "current_mode_update":
            self._handle_mode_update(upd)
        elif kind == "available_commands_update":
            self._handle_commands_update(upd)
        elif kind in (
            "scheduled_task_created", "scheduled_task_fired",
            "scheduled_task_deleted",
        ):
            self._handle_schedule_lifecycle(upd)
        elif kind == "turn_completed":
            # After Esc the prompt RPC is already done; Grok may keep
            # streaming tools then fire turn_completed. That is the closer
            # for interrupt leftover busy. Do not fire after a normal
            # finished prompt — that double-closed every Grok turn.
            pf = getattr(self, "_prompt_fut", None)
            if (
                getattr(self, "_leftover_end_pending", False)
                and (pf is None or pf.done())
            ):
                self._leftover_end_pending = False
                stop = (
                    upd.get("stop_reason")
                    or upd.get("stopReason")
                    or "end_turn"
                )
                send_notification("message", {
                    "type": "result",
                    "session_id": self.session_id or "",
                    "duration_ms": 0,
                    "stop_reason": stop,
                    "leftover_end": True,
                })

    def _forward_load_replay(self, params: dict) -> None:
        """Paint session/load history. Kimi replays before load settles.

        Live `_forward_update` would adopt these as a new turn (⚙) and skip
        `user_message_chunk`. Replay must not pair terminals or mark ⚙.
        """
        upd = params.get("update", {}) or {}
        kind = upd.get("sessionUpdate")
        if kind == "current_mode_update":
            self._handle_mode_update(upd)
            return
        if kind == "available_commands_update":
            self._handle_commands_update(upd)
            return
        # Do not paint load replay into the view. output.prompt() on each
        # user_message_chunk dumps the whole transcript, starts working=True,
        # and exit_input_mode() — ◎ never comes back. Agent already has the
        # history via session/load; UI uses _paint_resume_preview (last turn).
        if kind in (
            "user_message_chunk", "agent_message_chunk",
            "agent_thought_chunk",
        ):
            return
        if kind == "tool_call":
            tool_name = self._normalize_tool_name(upd)
            tool_input = self._tool_input_from_update(upd, tool_name)
            tool_name, tool_input = self._reclassify_read_dir(
                tool_name, tool_input)
            tid = upd.get("toolCallId")
            if self._should_suppress_tool_row(upd, tool_name):
                return
            if tool_name == "tool" and not tool_input:
                return
            if tid:
                if tool_name and tool_name != "tool":
                    self._tool_names_by_id[tid] = tool_name
                if tool_input:
                    prev = self._tool_inputs_by_id.get(tid) or {}
                    self._tool_inputs_by_id[tid] = {**prev, **tool_input}
                self._tool_ids_emitted.add(tid)
            return
        if kind == "tool_call_update":
            status = upd.get("status")
            tid = self._resolve_tool_id(upd.get("toolCallId"))
            tool_name = self._normalize_tool_name(upd)
            if (not tool_name or tool_name == "tool") and tid:
                tool_name = self._tool_names_by_id.get(tid) or tool_name or "tool"
            elif tid and tool_name and tool_name != "tool":
                self._tool_names_by_id[tid] = tool_name
            enriched = self._tool_input_from_update(upd, tool_name)
            tool_name, enriched = self._reclassify_read_dir(tool_name, enriched)
            if tid and enriched:
                prev = self._tool_inputs_by_id.get(tid) or {}
                self._tool_inputs_by_id[tid] = {**prev, **enriched}
            if tid not in self._tool_ids_emitted and (
                    enriched or upd.get("title")
                    or status in ("completed", "failed")):
                if not self._should_suppress_tool_row(upd, tool_name):
                    self._tool_ids_emitted.add(tid)
            if status in ("completed", "failed"):
                self._tool_ids_emitted.discard(tid)

    def _handle_mode_update(self, upd: dict) -> None:
        mode = upd.get("currentModeId") or upd.get("modeId") or ""
        if mode:
            self.agent_mode = mode
        entering_plan = (mode == "plan")
        if entering_plan and not self._in_plan_mode:
            self._in_plan_mode = True
            send_notification("plan_mode_enter", {})
        elif not entering_plan and self._in_plan_mode:
            self._in_plan_mode = False
            send_notification("message", {
                "type": "system",
                "subtype": "mode_update",
                "data": {"mode": mode, "left_plan": True},
            })
        send_notification("message", {
            "type": "system",
            "subtype": "mode_update",
            "data": {
                "mode": mode,
                "permission_mode": self.agent_mode_to_permission_mode(mode),
            },
        })

    def _handle_commands_update(self, upd: dict) -> None:
        cmds = upd.get("availableCommands") or upd.get("commands") or []
        send_notification("message", {
            "type": "system",
            "subtype": "available_commands",
            "data": {"commands": cmds},
        })
