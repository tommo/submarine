"""Grok native scheduler / /loop: interval parse, lifecycle, inject.

Client backup timers fire notification_wake when the host does not
inject scheduled prompts. cancel_loop tears down client timers.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import sys
import time
from typing import Any, Optional

_BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_DIR)

from rpc_helpers import send_notification, send_result  # noqa: E402


class SchedulerMixin:
    # ── Scheduler / /loop (Grok native) ────────────────────────────────

    @staticmethod
    def _parse_interval_seconds(interval: str) -> Optional[float]:
        """Parse Grok interval strings: 60s, 5m, 2h, 1d (min 60s)."""
        if not interval or not isinstance(interval, str):
            return None
        s = interval.strip().lower()
        m = re.fullmatch(r"(\d+)\s*([smhd])?", s)
        if not m:
            return None
        n = int(m.group(1))
        unit = m.group(2) or "s"
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        sec = float(n * mult)
        return max(60.0, sec) if sec > 0 else None

    @staticmethod
    def _parse_fire_at(value: Any) -> Optional[float]:
        """Parse next_fire_at from epoch, ms, or ISO string → epoch seconds."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            t = float(value)
            # ms timestamps
            if t > 1e12:
                t = t / 1000.0
            return t if t > 0 else None
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return None
            try:
                return float(s)
            except ValueError:
                pass
            try:
                # ISO-8601
                from datetime import datetime
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                return datetime.fromisoformat(s).timestamp()
            except Exception:
                return None
        return None

    def _emit_loop_scheduled(self, fire_at: Optional[float]) -> None:
        self._schedule_next_fire = fire_at
        send_notification("loop_scheduled", {"fire_at": fire_at})
        self.file_log(
            f"loop_scheduled fire_at={fire_at!r}"
            + (f" ({datetime.datetime.fromtimestamp(fire_at).isoformat()})"
               if fire_at else ""))

    def _handle_schedule_lifecycle(self, upd: dict) -> None:
        """Grok sessionUpdate: scheduled_task_created|fired|deleted."""
        kind = (
            upd.get("sessionUpdate")
            or upd.get("kind")
            or upd.get("type")
            or ""
        )
        kind = str(kind).replace("-", "_")
        if kind == "scheduled_task_created" or "created" in kind and "schedul" in kind:
            fire = self._parse_fire_at(
                upd.get("next_fire_at")
                or upd.get("nextFireAt")
                or upd.get("fire_at")
                or upd.get("fireAt")
            )
            if fire is None:
                # Derive from interval on create payload
                interval = (
                    upd.get("interval")
                    or (upd.get("task") or {}).get("interval")
                    or ""
                )
                sec = self._parse_interval_seconds(str(interval)) if interval else None
                if sec:
                    fire = time.time() + sec
            if fire:
                self._emit_loop_scheduled(fire)
            return
        if kind == "scheduled_task_fired" or kind.endswith("task_fired"):
            prompt = (
                upd.get("prompt")
                or upd.get("human_schedule")
                or (upd.get("task") or {}).get("prompt")
                or ""
            )
            # Next fire for recurring (if provided)
            fire = self._parse_fire_at(
                upd.get("next_fire_at") or upd.get("nextFireAt")
            )
            self._emit_loop_scheduled(fire)
            if prompt:
                display = "↻ " + str(prompt).split("\n", 1)[0][:60]
                send_notification("notification_wake", {
                    "wake_prompt": prompt,
                    "display_message": display,
                })
                self.file_log(f"scheduled_task_fired → wake: {prompt[:80]!r}")
            return
        if kind == "scheduled_task_deleted" or "deleted" in kind and "schedul" in kind:
            # Best-effort: clear banner; list may still have other jobs.
            # Prefer next_fire_at if agent includes remaining tasks' soonest fire.
            fire = self._parse_fire_at(
                upd.get("next_fire_at") or upd.get("nextFireAt")
            )
            self._emit_loop_scheduled(fire)
            return

    def _cancel_client_schedule(self, key: str) -> None:
        t = self._client_schedule_tasks.pop(key, None)
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass

    def _arm_client_schedule_backup(
            self, key: str, interval_sec: float, prompt: str,
            fire_immediately: bool, recurring: bool) -> None:
        """Bridge-local timer: inject wake if host never sends scheduled_task_*.

        Grok ACP often creates the schedule server-side but does not always
        push x.ai/scheduled_task_inject_prompt back into the bridge. Claude
        ScheduleWakeup already uses an in-process timer; mirror that here so
        goal+cron dogfood actually re-enters the session.
        """
        if not prompt or interval_sec <= 0:
            return
        self._cancel_client_schedule(key)

        async def _run() -> None:
            try:
                # Let the current turn finish emitting tool_result / end_turn.
                first_delay = 1.5 if fire_immediately else interval_sec
                self.file_log(
                    f"client_schedule[{key}]: first_delay={first_delay:.1f}s "
                    f"interval={interval_sec:.0f}s immediate={fire_immediately} "
                    f"recurring={recurring}")
                await asyncio.sleep(first_delay)
                while True:
                    nxt = (time.time() + interval_sec) if recurring else None
                    self._emit_loop_scheduled(nxt)
                    display = "↻ " + prompt.strip().split("\n", 1)[0][:60]
                    send_notification("notification_wake", {
                        "wake_prompt": prompt,
                        "display_message": display,
                    })
                    self.file_log(
                        f"client_schedule[{key}]: wake fired "
                        f"({len(prompt)} chars)")
                    if not recurring:
                        self._emit_loop_scheduled(None)
                        break
                    await asyncio.sleep(interval_sec)
            except asyncio.CancelledError:
                self.file_log(f"client_schedule[{key}]: cancelled")
            except Exception as e:
                self.file_log(f"client_schedule[{key}]: error {e}")
            finally:
                self._client_schedule_tasks.pop(key, None)

        try:
            loop = asyncio.get_running_loop()
            self._client_schedule_tasks[key] = loop.create_task(_run())
        except RuntimeError:
            self.file_log(
                f"client_schedule[{key}]: no running loop — cannot arm timer")

    def _note_scheduler_tool_result(
            self, tool_name: str, tool_input: dict, text: str,
            tool_call_id: str = "") -> None:
        """Arm loop banner + client wake backup from scheduler tool results.

        Completed tool_call_update often omits rawInput; callers must pass the
        cached create payload. Host sessionUpdate inject remains preferred when
        present — client timer is a reliability layer for ACP.
        """
        name = (tool_name or "").strip()
        # Delete / cancel → drop backup timer + clear banner if no other jobs.
        if name in ("scheduler_delete", "CronDelete", "SchedulerDelete"):
            del_id = (
                (tool_input or {}).get("id")
                or (tool_input or {}).get("task_id")
                or ""
            )
            data = None
            try:
                data = json.loads(text) if text and text.lstrip().startswith("{") else None
            except Exception:
                pass
            if isinstance(data, dict):
                del_id = del_id or data.get("id") or ""
            if del_id:
                self._cancel_client_schedule(str(del_id))
            if tool_call_id:
                self._cancel_client_schedule(f"tc:{tool_call_id}")
            # If no client jobs left, clear banner.
            if not self._client_schedule_tasks:
                self._emit_loop_scheduled(None)
            self.file_log(f"scheduler delete: cancelled client timer id={del_id!r}")
            return

        interval = (
            (tool_input or {}).get("interval")
            or (tool_input or {}).get("cron")
            or ""
        )
        delay = (tool_input or {}).get("delaySeconds") or (tool_input or {}).get("delay_seconds")
        prompt = (tool_input or {}).get("prompt") or (tool_input or {}).get("message") or ""
        fire_immediately = bool(
            (tool_input or {}).get("fire_immediately")
            or (tool_input or {}).get("fireImmediately")
        )
        recurring = (tool_input or {}).get("recurring")
        if recurring is None:
            recurring = True
        else:
            recurring = bool(recurring)

        fire = None
        task_id = ""
        # Prefer explicit next fire in tool output JSON
        try:
            data = json.loads(text) if text and text.lstrip().startswith("{") else None
        except Exception:
            data = None
        if isinstance(data, dict):
            task_id = str(data.get("id") or data.get("task_id") or "")
            fire = self._parse_fire_at(
                data.get("next_fire_at")
                or data.get("nextFireAt")
                or data.get("fire_at")
            )
            if fire is None and isinstance(data.get("task"), dict):
                fire = self._parse_fire_at(data["task"].get("next_fire_at"))
                task_id = task_id or str(data["task"].get("id") or "")
            # humanSchedule "every 2 minutes" — fall through to interval parse
        sec = None
        if interval:
            sec = self._parse_interval_seconds(str(interval))
            if fire is None and sec:
                fire = time.time() + (1.5 if fire_immediately else sec)
        if fire is None and delay is not None:
            try:
                d = float(delay)
                sec = max(60.0, min(d, 7 * 86400))
                fire = time.time() + sec
            except (TypeError, ValueError):
                pass
        if fire:
            self._emit_loop_scheduled(fire)
            self.file_log(
                f"scheduler tool {tool_name}: armed next_fire≈{fire:.0f} "
                f"immediate={fire_immediately}")
        # Client backup timer (host inject often missing in ACP).
        key = task_id or (f"tc:{tool_call_id}" if tool_call_id else "")
        if key and prompt and sec:
            self._arm_client_schedule_backup(
                key, float(sec), str(prompt), fire_immediately, recurring)
        elif key and prompt and delay is not None and sec:
            self._arm_client_schedule_backup(
                key, float(sec), str(prompt), True, False)

    async def handle_cancel_loop(self, req_id: Optional[int],
                                  params: dict) -> None:
        """Cancel all client-side schedule backups and clear the loop banner."""
        keys = list(self._client_schedule_tasks.keys())
        for key in keys:
            self._cancel_client_schedule(key)
        self._emit_loop_scheduled(None)
        self.file_log(f"cancel_loop: cleared {len(keys)} client schedule(s)")
        send_result(req_id, {"ok": True, "cancelled": len(keys)})

    async def _acp_scheduled_task_inject(self, params: dict) -> dict:
        """Grok fires a scheduled task: inject prompt as a new session turn.

        Wire: x.ai/scheduled_task_inject_prompt { sessionId, prompt, ... }
        Plugin path: notification_wake → Session.query (same as Claude cron).
        """
        prompt = (
            params.get("prompt")
            or params.get("text")
            or params.get("message")
            or ""
        )
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(
                "x.ai/scheduled_task_inject_prompt: missing or empty prompt")
        sid = params.get("sessionId") or params.get("session_id") or ""
        if self.session_id and sid and sid != self.session_id:
            self.file_log(
                f"scheduled_task_inject: sessionId mismatch "
                f"{sid!r} vs {self.session_id!r} (still injecting)")
        fire = self._parse_fire_at(
            params.get("next_fire_at") or params.get("nextFireAt")
        )
        # Update loop banner for recurring schedules.
        self._emit_loop_scheduled(fire)
        display = "↻ " + prompt.strip().split("\n", 1)[0][:60]
        send_notification("notification_wake", {
            "wake_prompt": prompt,
            "display_message": display,
        })
        self.file_log(
            f"scheduled_task_inject → wake ({len(prompt)} chars): "
            f"{prompt[:100]!r}")
        return {}
