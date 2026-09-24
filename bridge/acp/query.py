"""Host query / interrupt and session/prompt (+ images).

Invariants: Grok session/cancel is a notification (§9.3); do not
re-send cancel after the turn ended (§9.4); agent_busy wait+retry
without cancel (serialize on `_query_lock`); Kimi PREEMPT_PROMPT=False
(§9.40).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any, Optional

_BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_DIR)

from rpc_helpers import send_error, send_notification, send_result  # noqa: E402


class QueryMixin:
    def _should_precancel_before_prompt(self) -> bool:
        """Whether the next session/prompt needs session/cancel first.

        After Esc we leave `_cancel_in_flight` so Kimi can settle
        `turn.agent_busy`. Re-sending cancel when nothing is live postpones
        or drops the user's follow-up on Grok (orphan cancel).
        """
        if not self._cancel_in_flight:
            return False
        fut = self._prompt_fut
        if fut is not None and not fut.done():
            return True
        if self._query_req_id is not None:
            return True
        if getattr(self, "BACKEND_NAME", "") == "kimi":
            return True
        if getattr(self, "_orphan_turn_notified", False):
            return True
        return False

    # An instant empty end_turn this soon after an interrupt is Kimi refusing
    # the prompt (its own turn is live), not an answer.
    REFUSAL_WINDOW_S = 60.0
    REFUSAL_MAX_S = 1.5

    def _silently_refused(self, result: dict, sent_at: float) -> bool:
        if getattr(self, "BACKEND_NAME", "") != "kimi":
            return False
        if (result or {}).get("stopReason", "end_turn") != "end_turn":
            return False
        now = time.time()
        if now - sent_at > self.REFUSAL_MAX_S:
            return False
        if now - float(getattr(self, "_last_interrupt_ts", 0) or 0) > self.REFUSAL_WINDOW_S:
            return False
        return float(getattr(self, "_last_session_tool_ts", 0) or 0) < sent_at

    def _is_agent_busy_error(self, e: BaseException) -> bool:
        msg = str(e).lower()
        return (
            "agent_busy" in msg
            or "another turn" in msg
            or "turn is active" in msg
            or "cannot launch a new turn" in msg
            or "already in progress" in msg
        )

    async def _cancel_agent_turn(
        self,
        *,
        reason: str = "",
        wait_s: float = 2.0,
        settle_s: float = 0.3,
        force_local: bool = True,
        orphan_ok: bool = True,
    ) -> None:
        """session/cancel + wait until local prompt future settles.

        Kimi rejects a new session/prompt while its agent-side turn is still
        active (``turn.agent_busy`` / "another turn is already in progress").

        Important: after host interrupt we often force the *local* prompt
        future done while the agent turn is still live (auto-continue, slow
        cancel). Next user message must still send session/cancel even when
        ``_prompt_fut`` is already None — otherwise Esc → type fails with
        agent_busy forever.
        """
        fut = self._prompt_fut
        active = fut is not None and not fut.done()
        has_query = self._query_req_id is not None
        if not active and not has_query and not orphan_ok:
            return
        self._prompt_cancelled = True
        self._cancel_in_flight = True
        if getattr(self, "BACKEND_NAME", "") == "grok":
            self._drop_grok_leftover = True
        if self.session_id is not None:
            try:
                await self._notify_acp(
                    "session/cancel", {"sessionId": self.session_id})
                self.file_log(
                    f"cancel_agent_turn: session/cancel ({reason})"
                    f"{'' if active or has_query else ' [orphan agent turn]'}")
            except Exception as e:
                self.log(f"session/cancel failed ({reason}): {e}")
        # Release elicitation/permission waiters AFTER session/cancel is on
        # the wire. Answering elicitation with cancel first lets Kimi
        # continue ("dismissed"); cancel-while-outstanding actually stops
        # the turn (sandbox interrupt_during).
        self._unblock_interaction_waiters()
        fut = self._prompt_fut
        if fut is not None and not fut.done():
            try:
                await asyncio.wait_for(asyncio.shield(fut), timeout=wait_s)
            except (asyncio.TimeoutError, Exception):
                pass
            if force_local and not fut.done():
                fut.set_result({"stopReason": "cancelled"})
                self.file_log(
                    f"cancel_agent_turn: forced local fut ({reason})")
        # Agent-side turn teardown lag (Kimi turn IDs / subagents)
        if settle_s > 0:
            try:
                await asyncio.sleep(settle_s)
            except Exception:
                pass

    async def handle_query(self, req_id: Optional[int],
                            params: dict) -> None:
        if self.session_id is None:
            send_error(req_id, -32000, "session not initialized")
            return
        if getattr(self, "_query_lock", None) is None:
            self._query_lock = asyncio.Lock()
        await self._query_lock.acquire()
        # A new query must not overlap an agent turn (Kimi: turn.agent_busy).
        # Tool ✔ is not end_turn — wait the live prompt out. Cancel only after
        # user Esc (cancel_in_flight) or when the live prompt is stuck.
        # Stale _cancel_in_flight with no live prompt: do NOT orphan-cancel
        # (Grok ChatStateActor dies / new prompt is postponed). Kimi still
        # needs a settle cancel when the local fut was forced done early.
        if self._should_precancel_before_prompt():
            await self._cancel_agent_turn(
                reason="post_interrupt", wait_s=2.0, settle_s=0.8,
                force_local=True, orphan_ok=True)
        elif self._cancel_in_flight:
            self.file_log(
                "query: skip stale orphan session/cancel "
                f"(backend={self.BACKEND_NAME})")
            self._cancel_in_flight = False
        elif self._prompt_fut is not None and not self._prompt_fut.done():
            self.file_log("query: waiting for in-flight session/prompt")
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._prompt_fut), timeout=120.0)
            except (asyncio.TimeoutError, Exception):
                await self._cancel_agent_turn(
                    reason="stale_prompt", wait_s=2.0, settle_s=0.5)
            else:
                try:
                    await asyncio.sleep(0.35)
                except Exception:
                    pass
        elif self._query_req_id is not None and self._query_req_id != req_id:
            self.file_log(
                f"query: superseding in-flight req {self._query_req_id}")
            await self._cancel_agent_turn(
                reason="supersede", wait_s=2.0, settle_s=0.5)
        prompt = params.get("prompt") or params.get("text") or ""
        images = params.get("images") or []
        if not isinstance(images, list):
            images = []
        prompt_blocks = self._build_prompt_blocks(prompt, images)
        self._query_req_id = req_id
        self._prompt_cancelled = False
        self._leftover_end_pending = False
        self._cancel_in_flight = False
        self._overflow_compact_retried = False
        turn_t0 = time.time()
        try:
            result = None
            last_err: Optional[BaseException] = None
            # Busy: wait the live turn out. sandbox/kimi_busy overlap_retry:
            # session/cancel here is what dirtied the path; wait + retry
            # after end_turn delivers the second prompt. Esc still cancels.
            attempt = 0
            refused = 0
            while True:
                try:
                    sent_at = time.time()
                    result = await self._send_prompt(prompt_blocks) or {}
                    last_err = None
                    if (self._silently_refused(result, sent_at)
                            and not self._prompt_cancelled):
                        refused += 1
                        if refused <= 3:
                            # Kimi answered `end_turn` at once without running
                            # the prompt: a turn of its own holds the agent
                            # (after Esc: its reaction to the killed tasks).
                            # Stop that turn — Esc asked for it — and resend.
                            self.file_log(
                                f"query: prompt refused (instant empty end_turn), "
                                f"cancel agent turn and resend #{refused}")
                            await self._cancel_agent_turn(
                                reason="refused_prompt", wait_s=1.5, settle_s=0.8,
                                force_local=True, orphan_ok=True)
                            # That cancel was for the agent's turn, not ours:
                            # the resent prompt must not read as cancelled.
                            self._prompt_cancelled = False
                            self._cancel_in_flight = False
                            continue
                        raise RuntimeError(
                            "the agent is still busy with a turn of its own and "
                            "did not take the prompt — send it again in a moment")
                    break
                except Exception as e:
                    last_err = e
                    if (self._is_agent_busy_error(e)
                            and not self._prompt_cancelled):
                        attempt += 1
                        settle = min(10.0, 0.7 * (2 ** min(attempt, 5)))
                        self.file_log(
                            f"query: agent_busy attempt {attempt} "
                            f"settle={settle:.1f}s (no cancel): {e}")
                        try:
                            await asyncio.sleep(settle)
                        except Exception:
                            pass
                        continue
                    # Context-window 400 (Grok/DeepSeek): compact once, retry.
                    recovered = None
                    if not self._prompt_cancelled:
                        try:
                            recovered = await self.recover_prompt_error(
                                e, prompt_blocks)
                        except Exception as rec_err:
                            self.file_log(
                                f"recover_prompt_error: {rec_err}")
                            recovered = None
                    if recovered is not None:
                        result = recovered or {}
                        last_err = None
                        break
                    raise
            if last_err is not None and result is None:
                raise last_err
            result = result or {}
            stop_reason = result.get("stopReason", "end_turn")
            cancelled = (
                self._prompt_cancelled
                or stop_reason in ("cancelled", "canceled", "interrupted")
            )
            extra = getattr(self, "_pending_ask_followup", None)
            if extra and not cancelled:
                self._pending_ask_followup = None
                self.file_log(
                    "ask_user freetext: session/prompt after end_turn "
                    "(no cancel)")
                extra_blocks = self._build_prompt_blocks(extra, [])
                extra_attempt = 0
                while not self._prompt_cancelled:
                    try:
                        extra_res = await self._send_prompt(extra_blocks)
                        if extra_res:
                            result = extra_res
                            stop_reason = result.get(
                                "stopReason", "end_turn")
                            cancelled = (
                                self._prompt_cancelled
                                or stop_reason in (
                                    "cancelled", "canceled",
                                    "interrupted")
                            )
                        break
                    except Exception as e:
                        if not self._is_agent_busy_error(e):
                            self.file_log(
                                f"ask_user freetext followup: {e}")
                            break
                        extra_attempt += 1
                        settle = min(10.0, 0.7 * (2 ** min(extra_attempt, 5)))
                        self.file_log(
                            f"ask_user freetext busy retry "
                            f"{extra_attempt} settle={settle:.1f}s")
                        await asyncio.sleep(settle)
            usage = self.usage_from_prompt_result(result)
            duration_ms = max(0, int((time.time() - turn_t0) * 1000))
            if usage:
                send_notification("message",
                                  {"type": "turn_usage", "usage": usage})
            send_notification("message", {
                "type": "result",
                "session_id": self.session_id or "",
                "duration_ms": duration_ms,
                "is_error": False,
                "num_turns": 1,
                "total_cost_usd": 0,
                "stop_reason": "interrupted" if cancelled else stop_reason,
                "usage": usage or {},
            })
            if cancelled:
                send_result(req_id, {"status": "interrupted",
                                     "stopReason": stop_reason})
            else:
                send_result(req_id, {
                    "status": "complete",
                    "stopReason": stop_reason})
        except Exception as e:
            if self._prompt_cancelled:
                duration_ms = max(0, int((time.time() - turn_t0) * 1000))
                send_notification("message", {
                    "type": "result",
                    "session_id": self.session_id or "",
                    "duration_ms": duration_ms,
                    "is_error": False,
                    "num_turns": 1,
                    "total_cost_usd": 0,
                    "stop_reason": "interrupted",
                })
                send_result(req_id, {"status": "interrupted"})
            else:
                send_error(req_id, -32000, self.format_query_error(e))
        finally:
            if self._query_req_id == req_id:
                self._query_req_id = None
            self._prompt_cancelled = False
            # Leave _cancel_in_flight set after interrupt so the NEXT query
            # still session/cancel+settles (Kimi agent_busy). Cleared when
            # that next query actually starts sending.
            self._prompt_fut = None
            self._prompt_acp_id = None
            lock = getattr(self, "_query_lock", None)
            if lock is not None and lock.locked():
                try:
                    lock.release()
                except Exception:
                    pass

    def _prompt_caps(self) -> dict:
        return (self.agent_capabilities or {}).get("promptCapabilities") or {}

    def _prompt_supports_images(self) -> bool:
        return bool(self._prompt_caps().get("image"))

    def _prompt_supports_embedded(self) -> bool:
        return bool(self._prompt_caps().get("embeddedContext"))

    def _image_b64(self, img: dict) -> tuple:
        """Return (mime, base64_data) only — never put a filesystem path on the wire."""
        import base64 as _b64
        mime = (img.get("mime_type") or img.get("mimeType") or "image/png")
        data = img.get("data") or ""
        if data:
            return mime, data
        # Optional: load bytes from a local path the *plugin* already has, but
        # still only emit base64 (no uri/path in the ACP prompt).
        path = (img.get("path") or "").strip()
        if path and os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    data = _b64.b64encode(f.read()).decode("ascii")
                if not mime or mime == "image/png":
                    low = path.lower()
                    if low.endswith((".jpg", ".jpeg")):
                        mime = "image/jpeg"
                    elif low.endswith(".gif"):
                        mime = "image/gif"
                    elif low.endswith(".webp"):
                        mime = "image/webp"
                return mime, data
            except OSError as e:
                self.file_log(f"image load failed: {e}")
        return mime, ""

    def _build_prompt_blocks(self, prompt, images: list) -> list:
        """Build ACP ContentBlock[] — images as base64 only, never file paths.

        Grok will otherwise invent an assets/ path and call read_file on the
        PNG (text fs API) → FAILED. Vision is one multimodal image block.
        https://agentclientprotocol.com/protocol/v1/content
        """
        if isinstance(prompt, list):
            blocks = [b for b in prompt if isinstance(b, dict)]
            text = ""
        else:
            text = prompt if isinstance(prompt, str) else str(prompt or "")
            blocks = []

        if not images:
            if not blocks:
                blocks = [{"type": "text", "text": text}]
            elif text:
                blocks.append({"type": "text", "text": text})
            return blocks

        caps = self._prompt_caps()
        use_image_cap = bool(caps.get("image"))
        n_img = 0

        for img in images:
            if not isinstance(img, dict):
                continue
            mime, data = self._image_b64(img)
            if not data:
                self.file_log("query: skipped image with no base64 data")
                continue
            # Never set uri/path/resource_link for images.
            blocks.append({
                "type": "image",
                "mimeType": mime or "image/png",
                "data": data,
            })
            n_img += 1

        self.file_log(
            f"query: images→blocks image={n_img} (base64 only, no paths) "
            f"caps={caps} image_cap={use_image_cap}")

        if text or not any(b.get("type") == "text" for b in blocks):
            blocks.append({"type": "text", "text": text or ""})
        return blocks

    # A prompt with no update for this long gets a "still there" hint: Grok
    # compacts a large context before the turn with no word on the pipe (a
    # resumed 400k session sat ~3 minutes looking hung).
    SILENCE_HINT_S = 20.0

    def _context_window(self) -> Optional[int]:
        """The running model's window, from session/new|load availableModels."""
        for m in getattr(self, "_available_models", None) or []:
            if isinstance(m, dict) and m.get("modelId") == self.model:
                try:
                    return int((m.get("_meta") or {}).get("totalContextTokens") or 0) or None
                except (TypeError, ValueError):
                    return None
        return None

    async def _silence_watch(self, sent_at: float) -> None:
        try:
            await asyncio.sleep(self.SILENCE_HINT_S)
        except asyncio.CancelledError:
            return
        if float(getattr(self, "_last_session_tool_ts", 0) or 0) > sent_at:
            return
        self.file_log(f"prompt silent {self.SILENCE_HINT_S:.0f}s")
        send_notification("message", {
            "type": "system",
            "subtype": "agent_silent",
            "data": {
                "seconds": int(self.SILENCE_HINT_S),
                "tokens_used": getattr(self, "_last_total_tokens", None),
                "context_window": self._context_window(),
            },
        })

    async def _send_prompt(self, prompt_blocks: list) -> Any:
        """session/prompt with a tracked future so interrupt can unblock us."""
        await self._spawn()
        assert self.proc is not None and self.proc.stdin is not None
        rid = self._acp_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        self._prompt_fut = fut
        self._prompt_acp_id = rid
        self._orphan_turn_notified = False
        # A new prompt owns the turn again — leftover lid comes off here.
        self._drop_grok_leftover = False
        params = {"sessionId": self.session_id, "prompt": prompt_blocks}
        # Log without dumping multi-MB base64 image payloads
        def _summarize_block(b: dict) -> dict:
            t = b.get("type")
            if t == "text":
                return {"type": "text", "text": (b.get("text") or "")[:200]}
            if t == "image":
                return {
                    "type": "image",
                    "mimeType": b.get("mimeType"),
                    "data_len": len(b.get("data") or ""),
                    # never log/send path; uri must stay empty
                    "has_uri": bool(b.get("uri")),
                }
            if t == "resource":
                res = b.get("resource") or {}
                return {
                    "type": "resource",
                    "mimeType": res.get("mimeType"),
                    "blob_len": len(res.get("blob") or ""),
                    "uri": res.get("uri"),
                }
            if t == "resource_link":
                return {
                    "type": "resource_link",
                    "uri": b.get("uri"),
                    "name": b.get("name"),
                    "mimeType": b.get("mimeType"),
                }
            return {"type": t}
        log_params = {
            "sessionId": self.session_id,
            "prompt": [
                _summarize_block(b)
                for b in (prompt_blocks or [])
                if isinstance(b, dict)
            ],
        }
        line = json.dumps({
            "jsonrpc": "2.0", "id": rid,
            "method": "session/prompt", "params": params,
        })
        self.file_log(
            f"→ acp session/prompt (id={rid}): "
            f"{json.dumps(log_params)[:800]} (wire_len={len(line)})")
        async with self._get_acp_write_lock():
            self.proc.stdin.write((line + "\n").encode())
            await self.proc.stdin.drain()
        exit_task = None
        watch = asyncio.create_task(self._silence_watch(time.time()))
        try:
            if self.proc is not None:
                exit_task = asyncio.create_task(self.proc.wait())
                done, _pend = await asyncio.wait(
                    {fut, exit_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if fut not in done:
                    rc = self.proc.returncode
                    self.file_log(
                        f"agent exited during session/prompt id={rid} "
                        f"returncode={rc}")
                    raise RuntimeError(
                        f"agent process exited during prompt (returncode={rc})")
            result = await fut
            if isinstance(result, dict):
                meta = result.get("_meta") if isinstance(
                    result.get("_meta"), dict) else {}
                pid = (
                    (meta or {}).get("promptId")
                    or (meta or {}).get("prompt_id")
                    or result.get("promptId")
                    or result.get("prompt_id")
                )
                if pid:
                    self._host_prompt_id = str(pid)
                try:
                    if (meta or {}).get("totalTokens"):
                        self._last_total_tokens = int(meta["totalTokens"])
                except (TypeError, ValueError):
                    pass
            try:
                self.file_log(
                    f"← acp session/prompt (id={rid}) result: "
                    f"{json.dumps(result)[:800]}")
            except Exception:
                self.file_log(
                    f"← acp session/prompt (id={rid}) result: {result!r}")
            return result
        finally:
            if not watch.done():
                watch.cancel()
            if exit_task is not None and not exit_task.done():
                exit_task.cancel()
            self.pending.pop(rid, None)
            if self._prompt_fut is fut:
                self._prompt_fut = None
            if self._prompt_acp_id == rid:
                self._prompt_acp_id = None

    async def handle_interrupt(self, req_id: Optional[int],
                                params: dict) -> None:
        """Cancel the in-flight ACP turn.

        Grok expects session/cancel as a JSON-RPC *notification* (no id).
        Sending it as a request returns Method not found and never unblocks
        the prompt. After notify, session/prompt resolves with
        stopReason=cancelled — handle_query maps that to interrupted.

        Idempotent: extra Esc presses must NOT re-send session/cancel after
        the turn already ended (Grok logs ChatStateActor dead / channel_dropped).

        Kimi: cancel alone is not enough if we force the local future too
        early — agent keeps turn.agent_busy. Wait longer before force; next
        query also re-settles via _cancel_agent_turn.
        """
        # Esc: Kimi answers the killed shells with a turn of its own, which
        # then refuses prompts (see _silently_refused / _stop_post_interrupt_turn).
        self._last_interrupt_ts = time.time()
        self._post_interrupt_cancelled = False
        fut = self._prompt_fut
        active = fut is not None and not fut.done()
        has_query = self._query_req_id is not None
        grok_leftover = (
            getattr(self, "BACKEND_NAME", "") == "grok"
            and (
                getattr(self, "_drop_grok_leftover", False)
                or getattr(self, "_orphan_turn_notified", False)
            )
        )
        # Idle / already cancelled: do not re-send session/cancel (Grok
        # ChatStateActor dies) UNLESS leftover MidTurnAbort is still
        # spawning tools — then one more cancel, not four idle no-ops.
        if ((not active and not has_query) or (
                self._cancel_in_flight and not active)) and not grok_leftover:
            n = 0
            for tid in list(self._terminals):
                try:
                    await self._terminal_close(tid)
                    n += 1
                except Exception:
                    pass
            self.file_log(
                f"interrupt: idle leftover_killed={n} "
                f"cancel_in_flight={self._cancel_in_flight}")
            send_result(req_id, {"status": "interrupted"})
            return

        # Kill client-side terminals so terminal/wait_for_exit unblocks.
        for tid in list(self._terminals):
            try:
                await self._terminal_close(tid)
            except Exception:
                pass
        # Detached children are not SIGTERM'd, but waiters must not hang
        # and the host must close ⚙ rows.
        try:
            self._cancel_child_sessions("interrupt")
        except Exception:
            pass

        # Cancel + wait (longer than old 0.35s force — Kimi turn teardown).
        await self._cancel_agent_turn(
            reason="interrupt", wait_s=1.5, settle_s=0.2, force_local=True)
        send_result(req_id, {"status": "interrupted"})

    def _unblock_interaction_waiters(self) -> None:
        for pid, pfut in list(self.pending_permissions.items()):
            if pfut and not pfut.done():
                pfut.set_result({"kind": "denied-interactively-by-user"})
            self.pending_permissions.pop(pid, None)
        for qid, qfut in list(self.pending_questions.items()):
            if qfut and not qfut.done():
                qfut.set_result(None)
            self.pending_questions.pop(qid, None)
        for pid, pfut in list(self.pending_plan_approvals.items()):
            if pfut and not pfut.done():
                pfut.set_result(None)
            self.pending_plan_approvals.pop(pid, None)
