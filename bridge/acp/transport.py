"""ACP child process I/O: spawn, NDJSON readers, request/response writes.

Invariants live here: session/cancel is a notification not a request
(§9.3); do not re-send cancel after the turn ended (§9.4); inbound
line cap 8 MiB fail-closed (§9.9); stdin write lock so concurrent
fs/permission/terminal replies do not interleave JSON (§9.39).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, Optional

_BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_DIR)

from rpc_helpers import send_notification  # noqa: E402


class TransportMixin:
    def _get_acp_write_lock(self) -> asyncio.Lock:
        if self._acp_write_lock is None:
            self._acp_write_lock = asyncio.Lock()
        return self._acp_write_lock

    def _fail_all_pending(self, err: BaseException) -> None:
        """Unblock prompt/RPC waiters when the agent stdio dies."""
        for _rid, fut in list(self.pending.items()):
            if fut is not None and not fut.done():
                fut.set_exception(err)
        self.pending.clear()
        pf = self._prompt_fut
        if pf is not None and not pf.done():
            pf.set_exception(err)

    def _mark_agent_dead(self, reason: str) -> None:
        """Drop a dead agent handle so we do not write to a closed stdin."""
        self._agent_exited = True
        proc = self.proc
        self.proc = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                pass
        self.file_log(f"agent dead: {reason}")
        pf = getattr(self, "_prompt_fut", None)
        in_flight = pf is not None and not pf.done()
        if not in_flight:
            # Prompt already returned end_turn — Grok often closes stdio
            # after a long turn. Do not paint ⚠ turn failed on a finished @done.
            return
        try:
            send_notification("message", {
                "type": "result",
                "session_id": self.session_id or "",
                "duration_ms": 0,
                "is_error": True,
                "num_turns": 1,
                "total_cost_usd": 0,
                "stop_reason": "error",
                "error": reason,
            })
        except Exception:
            pass

    # ── Subprocess lifecycle ───────────────────────────────────────────

    async def _spawn(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            return
        if self.proc is not None:
            self.proc = None
        if self._agent_exited and self.session_id:
            raise RuntimeError("agent process died; restart the session")
        args = self.agent_argv()
        try:
            path = self.log_path()
            with open(path, "w") as f:
                f.write(
                    f"# {self.BACKEND_NAME}-bridge — {args}, "
                    f"cwd={self.cwd} pid={os.getpid()}\n")
            shared = getattr(self, "LOG_PATH", None)
            if shared and shared != path:
                with open(shared, "a") as f:
                    f.write(
                        f"# {self.BACKEND_NAME} pid={os.getpid()} -> {path}\n")
        except Exception:
            pass
        env = self.spawn_env()
        # Default asyncio limit is 64KiB — routine tool_call_update lines exceed
        # that and kill the reader (session freeze). Raise high; app-level gates
        # reject/drop messages that are still unreasonably large.
        self.proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=env,
            limit=max(self.acp_stream_limit, 1024 * 1024))
        self.reader_task = asyncio.create_task(self._read_agent_stdout())
        asyncio.create_task(self._read_agent_stderr())

    async def _read_agent_stderr(self) -> None:
        if self.proc is None or self.proc.stderr is None:
            return
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            try:
                self.file_log("[agent stderr] " + line.decode(errors="replace").rstrip())
            except Exception:
                pass

    async def _read_agent_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        max_line = max(self.acp_max_inbound_line, 64 * 1024)
        while True:
            try:
                line = await self.proc.stdout.readline()
            except ValueError as e:
                # Over stream limit — transport may be wedged; stop cleanly.
                self.file_log(f"agent stdout readline failed (limit): {e}")
                self._fail_all_pending(RuntimeError(
                    f"agent stdout readline failed: {e}"))
                self._mark_agent_dead(f"agent stdout readline failed: {e}")
                break
            if not line:
                rc = self.proc.returncode if self.proc else None
                reason = f"agent stdout closed (returncode={rc})"
                self.file_log(f"agent stdout EOF (returncode={rc})")
                self._fail_all_pending(RuntimeError(reason))
                self._mark_agent_dead(reason)
                break
            if len(line) > max_line:
                self.file_log(
                    f"drop oversized agent NDJSON line: {len(line)} bytes "
                    f"(max {max_line}); not parsing")
                continue
            try:
                msg = json.loads(line.decode().strip())
            except json.JSONDecodeError:
                continue
            has_id = "id" in msg
            method = msg.get("method")
            if has_id and method is None:
                fut = self.pending.pop(msg["id"], None)
                if fut is not None and not fut.done():
                    if "error" in msg:
                        err_txt = self._format_acp_error(msg.get("error") or {})
                        self.file_log(
                            f"← acp id={msg.get('id')} error: {err_txt}")
                        fut.set_exception(RuntimeError(err_txt))
                    else:
                        fut.set_result(msg.get("result"))
                continue
            params = msg.get("params", {})
            if has_id:
                # Agent → client request (permission, fs, terminal, …).
                self.file_log(
                    f"← acp REQ {method} (id={msg.get('id')}): "
                    f"{json.dumps(params)[:600]}")
                asyncio.create_task(self._dispatch_acp_request(
                    msg["id"], method, params))
                continue
            if method == "session/update":
                kind = (params.get("update") or {}).get("sessionUpdate")
                # Drop child/subagent streams before logging noise (Grok fans
                # subagent tool_call + agent_message onto this same stdio).
                if self._is_foreign_session(params):
                    self._ingest_child_session(params)
                    self._note_foreign_session_drop(
                        kind or "session/update", params)
                    continue
                if kind in (
                    "tool_call", "tool_call_update", "current_mode_update",
                    "scheduled_task_created", "scheduled_task_fired",
                    "scheduled_task_deleted",
                ):
                    self.file_log(f"← acp update {kind}: {json.dumps(params)[:400]}")
                if kind in ("tool_call", "tool_call_update",
                            "agent_message_chunk", "agent_thought_chunk"):
                    # Host uses this to avoid double-painting synthetic
                    # terminal/* tool rows when Kimi also emits tool_call.
                    self._last_session_tool_ts = time.time()
                try:
                    self._forward_update(params)
                except Exception as e:
                    # A crash here used to kill the ACP reader — Kimi's
                    # terminal/create then sat in the pipe and ⚙ never ended.
                    self.file_log(
                        f"forward_update failed kind={kind}: {e}")
            elif method and "mcp" in method.lower():
                # Surface MCP lifecycle (servers_updated, init_progress, …)
                # Still skip foreign-session MCP chatter if tagged.
                if self._is_foreign_session(params):
                    self._ingest_child_session(params)
                    self._note_foreign_session_drop(method, params)
                    continue
                self.file_log(
                    f"← acp {method}: {json.dumps(params)[:600]}")
            elif method in (
                "x.ai/session/update", "_x.ai/session/update",
            ):
                # Grok may nest schedule lifecycle under x.ai/session/update.
                if self._is_foreign_session(params):
                    self._ingest_child_session(params)
                    self._note_foreign_session_drop(method, params)
                    continue
                self.file_log(
                    f"← acp {method}: {json.dumps(params)[:600]}")
                upd = params.get("update") or params
                if isinstance(upd, dict):
                    self._handle_schedule_lifecycle(upd)
            elif method and self._is_foreign_session(params):
                self._ingest_child_session(params)
                self._note_foreign_session_drop(method, params)
            # Other parent notifications (_x.ai/*, etc.) are intentionally ignored.

    def _acp_id(self) -> int:
        self.next_acp_id += 1
        return self.next_acp_id

    @staticmethod
    def _format_acp_error(err: Any) -> str:
        """JSON-RPC error → 'message: details' (Kimi Internal error hides data)."""
        if not isinstance(err, dict):
            return str(err) or "acp error"
        msg = str(err.get("message") or "acp error")
        data = err.get("data")
        if isinstance(data, dict):
            details = data.get("details")
            if details:
                return f"{msg}: {details}"
            nested = data.get("_errors") or data.get("mcpServers")
            if nested:
                try:
                    return f"{msg}: {json.dumps(data)[:400]}"
                except Exception:
                    return f"{msg}: {data}"
        if data:
            return f"{msg}: {data}"
        return msg

    @staticmethod
    def _is_mcp_runtime_identity_error(e: BaseException) -> bool:
        t = str(e).lower()
        return "runtime identity" in t or (
            "internal error" in t and "mcp" in t
        )

    async def _send_acp(self, method: str, params: dict,
                         *, timeout: Optional[float] = None) -> Any:
        await self._spawn()
        assert self.proc is not None and self.proc.stdin is not None
        rid = self._acp_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        line = json.dumps({
            "jsonrpc": "2.0", "id": rid, "method": method, "params": params,
        })
        self.file_log(f"→ acp {method} (id={rid}): {line[:800]}")
        async with self._get_acp_write_lock():
            self.proc.stdin.write((line + "\n").encode())
            await self.proc.stdin.drain()
        try:
            if timeout is not None:
                result = await asyncio.wait_for(fut, timeout=timeout)
            else:
                result = await fut
        except asyncio.TimeoutError:
            self.pending.pop(rid, None)
            self.file_log(f"← acp {method} (id={rid}) TIMEOUT after {timeout}s")
            raise
        try:
            self.file_log(
                f"← acp {method} (id={rid}) result: {json.dumps(result)[:800]}")
        except Exception:
            self.file_log(f"← acp {method} (id={rid}) result: {result!r}")
        return result

    async def _notify_acp(self, method: str, params: dict) -> None:
        """JSON-RPC notification (no id) — used for session/cancel on Grok."""
        await self._spawn()
        assert self.proc is not None and self.proc.stdin is not None
        line = json.dumps({
            "jsonrpc": "2.0", "method": method, "params": params,
        })
        self.file_log(f"→ acp NOTIFY {method}: {line[:800]}")
        async with self._get_acp_write_lock():
            self.proc.stdin.write((line + "\n").encode())
            await self.proc.stdin.drain()

    # ── ACP agent→client REQUEST handling ──────────────────────────────

    _ACP_REQUEST_HANDLERS = {
        "session/request_permission": "_acp_request_permission",
        "elicitation/create": "_acp_elicitation_create",
        # Grok Build asks the user via an xAI extension (not a plain tool result).
        "_x.ai/ask_user_question": "_acp_ask_user_question",
        "x.ai/ask_user_question": "_acp_ask_user_question",
        "_x.ai/exit_plan_mode": "_acp_exit_plan_mode",
        "x.ai/exit_plan_mode": "_acp_exit_plan_mode",
        # Grok scheduler fire → client injects the prompt as a new turn.
        "x.ai/scheduled_task_inject_prompt": "_acp_scheduled_task_inject",
        "_x.ai/scheduled_task_inject_prompt": "_acp_scheduled_task_inject",
        "fs/read_text_file": "_acp_fs_read",
        "fs/write_text_file": "_acp_fs_write",
        "terminal/create": "_acp_terminal_create",
        "terminal/output": "_acp_terminal_output",
        "terminal/wait_for_exit": "_acp_terminal_wait",
        "terminal/kill": "_acp_terminal_kill",
        "terminal/release": "_acp_terminal_release",
    }

    async def _dispatch_acp_request(self, rid: int, method: Optional[str],
                                     params: dict) -> None:
        handler_name = self._ACP_REQUEST_HANDLERS.get(method or "")
        if not handler_name:
            await self._send_acp_response(rid, error={
                "code": -32601,
                "message": f"Method not supported: {method}"})
            return
        try:
            result = await getattr(self, handler_name)(params)
            await self._send_acp_response(rid, result=result or {})
        except FileNotFoundError as e:
            await self._send_acp_response(rid, error={
                "code": -32000, "message": str(e)})
            return
        except Exception as e:
            self.log(f"ACP {method} error: {e}")
            await self._send_acp_response(rid, error={
                "code": -32000, "message": str(e)})
            return
        # cancel+reprompt anti-pattern. listed Q1 goes through elicitation.
        # self._flush_ask_followup()

    async def _send_acp_response(self, rid: int, *, result: Any = None,
                                  error: Optional[dict] = None) -> None:
        if self.proc is None or self.proc.stdin is None:
            return
        env: Dict[str, Any] = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            env["error"] = error
        else:
            env["result"] = result if result is not None else {}
        line = json.dumps(env) + "\n"
        # Keep response logs short — full fs/read payloads are huge.
        if error is not None:
            self.file_log(f"→ acp RESP id={rid} error: {json.dumps(error)[:300]}")
        else:
            preview = json.dumps(env.get("result"))[:200]
            self.file_log(f"→ acp RESP id={rid} ok: {preview}")
        async with self._get_acp_write_lock():
            if self.proc is None or self.proc.stdin is None:
                return
            try:
                self.proc.stdin.write(line.encode())
                await self.proc.stdin.drain()
            except Exception as e:
                self.file_log(f"→ acp RESP id={rid} write failed: {e}")
