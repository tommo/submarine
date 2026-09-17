"""ACP terminal/* client: real subprocess, kill/exit, synth-UI gate.

Invariants: synth Bash after @done must not flip working (§9.31);
login shells rewritten to -c; never send negative exitCode
({exitCode:null, signal:SIGTERM}) (§9.32).
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time
import uuid as uuidlib
from typing import Any, Dict, Optional

_BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_DIR)

from rpc_helpers import send_notification  # noqa: E402
from acp.util import apply_plain_terminal_env, strip_ansi  # noqa: E402


class TerminalMixin:
    def _normalize_terminal_cmd(self, cmd: str, args: list) -> tuple:
        """Return (cmd, args, use_shell) for terminal spawn.

        Grok packs full shell lines into `command` (often `/bin/bash -lc '…'`).
        Login shells (`-l`) can hang on interactive profile prompts when run
        without a TTY, so strip login mode to plain `-c`.
        """
        if args:
            return cmd, list(args), False
        if not isinstance(cmd, str):
            return str(cmd), [], False
        # Rewrite `/bin/bash -lc 'script'` / `bash -lc "script"` → non-login.
        import re
        m = re.match(
            r"^(/bin/bash|bash|(/bin/)?zsh|(/bin/)?sh)\s+-l([c])\s+(.*)$",
            cmd.strip(), re.DOTALL)
        if m:
            shell = m.group(1)
            script = m.group(5)
            # Keep as a shell line so quoting inside the script is preserved.
            return f"{shell} -{m.group(4)} {script}", [], True
        use_shell = (
            " " in cmd
            or cmd.startswith("/bin/bash")
            or cmd.startswith("bash")
        )
        return cmd, [], use_shell

    def _kill_terminal_proc(self, proc) -> None:
        """Kill process and its group (pipelines under bash -c)."""
        if proc is None or proc.returncode is not None:
            return
        pid = proc.pid
        try:
            # start_new_session=True → kill whole group.
            os.killpg(pid, 15)  # SIGTERM
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except (ProcessLookupError, AttributeError):
                pass

        # Escalate after a beat if still alive (done by waiters).

    @staticmethod
    def _exit_status_from_code(code: Optional[int]) -> dict:
        """Map subprocess returncode → ACP TerminalExitStatus.

        ACP schema: exitCode is uint32 | null (minimum 0). Python reports
        signal deaths as negative codes (e.g. -15 for SIGTERM). Returning
        negatives makes Grok fail with: failed to deserialize response.
        Spec: exitCode null when terminated by signal.
        """
        if code is None:
            return {"exitCode": 0, "signal": None}
        if code < 0:
            sig_num = -code
            try:
                import signal as _signal
                sig_name = _signal.Signals(sig_num).name
            except (ValueError, AttributeError):
                sig_name = f"SIG{sig_num}"
            return {"exitCode": None, "signal": sig_name}
        return {"exitCode": int(code), "signal": None}

    def _host_emit_tool_use(
            self, tool_id: str, name: str, tool_input: dict = None,
            background: bool = False) -> None:
        """Push a tool row to the Sublime host (Claude message protocol)."""
        if not tool_id or not name:
            return
        send_notification("message", {
            "type": "tool_use",
            "id": tool_id,
            "name": name,
            "input": tool_input or {},
            "background": bool(background),
        })

    def _host_emit_tool_result(
            self, tool_id: str, content: str = "",
            is_error: bool = False) -> None:
        if not tool_id:
            return
        send_notification("message", {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": content or "",
            "is_error": bool(is_error),
        })

    def _should_synth_terminal_ui(self) -> bool:
        """Kimi-only: terminal/* with no session/update tool_call.

        Grok always streams tool_call first; synthesizing doubles ☐ next to ⚙.
        Live timeout:0 create paired the execute then still synth'd because
        spawn awaited >1.5s past `_last_session_tool_ts`.
        """
        if getattr(self, "BACKEND_NAME", "") != "kimi":
            return False
        pending = getattr(self, "_pending_execute_ids", None) or []
        if pending:
            return False
        last = float(getattr(self, "_last_session_tool_ts", 0) or 0)
        return (time.time() - last) > 1.5

    async def _acp_terminal_create(self, params: dict) -> dict:
        # After cancel, refuse new shells so the agent cannot keep spawning
        # work while the prompt is winding down — only while host prompt lives.
        host_prompt_live = (
            self._prompt_fut is not None and not self._prompt_fut.done())
        if self._cancel_in_flight or getattr(self, "_drop_grok_leftover", False) or (
                self._prompt_cancelled and host_prompt_live):
            raise ValueError("terminal/create rejected: turn cancelled")
        cmd = params.get("command")
        if not cmd:
            raise ValueError("terminal/create requires command")
        args_in = params.get("args") or []
        cwd = params.get("cwd") or self.cwd
        env_in = params.get("env") or []
        env = os.environ.copy()
        for e in env_in:
            if isinstance(e, dict) and "name" in e:
                env[e["name"]] = e.get("value", "")
            elif isinstance(e, (list, tuple)) and len(e) == 2:
                env[str(e[0])] = str(e[1])
        if isinstance(env_in, dict):
            for k, v in env_in.items():
                env[str(k)] = str(v)
        # Non-interactive plain text: agent UIs are not a TTY — colored
        # output shows as raw ESC sequences. Force mono even if parent
        # shell / agent env has TERM=xterm-256color or FORCE_COLOR=1.
        apply_plain_terminal_env(env)
        # ACP outputByteLimit: honor request but hard-cap so one terminal
        # cannot pin unbounded memory. Grok bash default is 20k
        # (DEFAULT_TOOL_OUTPUT_CHARS); bg terminals bump this in
        # _mark_terminal_bg so long editor logs are not frozen there.
        max_out = max(4096, self.terminal_output_max_bytes)
        raw_lim = params.get("outputByteLimit")
        try:
            limit = int(raw_lim) if raw_lim is not None else max_out
        except (TypeError, ValueError):
            limit = max_out
        if limit <= 0 or limit > max_out:
            limit = max_out
        cmd, args, use_shell = self._normalize_terminal_cmd(cmd, args_in)
        # stdin=DEVNULL: inherited bridge stdin is a JSON-RPC pipe; children
        # that read stdin hang forever. start_new_session: killpg on timeout.
        common = dict(
            cwd=cwd, env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        if use_shell:
            proc = await asyncio.create_subprocess_shell(cmd, **common)
        else:
            proc = await asyncio.create_subprocess_exec(cmd, *args, **common)
        tid = "term_" + uuidlib.uuid4().hex[:10]
        # Kimi: command=/bin/bash args=["-c", real] even when use_shell=False
        cmd_show = self._script_from_terminal_params(cmd, args_in)
        if isinstance(cmd_show, str) and len(cmd_show) > 240:
            cmd_show = cmd_show[:239] + "…"
        slot: Dict[str, Any] = {
            "proc": proc, "stdout": "", "stderr": "",
            "limit": limit, "truncated": False, "exit_status": None,
            "cmd": (cmd_show or cmd)[:200],
        }
        self._terminals[tid] = slot
        self._mark_terminal_bg(tid, slot)
        self.file_log(
            f"terminal/create {tid} pid={proc.pid} shell={use_shell} "
            f"bg={bool(slot.get('bg'))} limit={slot.get('limit')}")

        # Kimi often runs tools ONLY via terminal/* with zero session/update
        # tool_call — host UI then shows empty "waiting" while agent is busy.
        # Skip when this create already paired to a streamed tool_call (Grok
        # timeout:0 paints ⚙ then create — synth was the leftover ☐ Bash).
        paired = slot.get("tool_use_id")
        pc = self._call(paired) if paired else None
        already = bool(pc and pc.emitted)
        if already:
            self.file_log(f"terminal/create {tid} paired {paired}; skip synth")
        elif self._should_synth_terminal_ui():
            host_id = f"term-ui-{tid}"
            slot["host_tool_id"] = host_id
            self._host_emit_tool_use(
                host_id, "Bash",
                {"command": cmd_show or str(cmd)[:240]},
                background=bool(slot.get("bg")),
            )
            self.file_log(f"synth host Bash for {tid} (no session tool_call)")

        async def drain(stream, key):
            # ACP: when outputByteLimit is exceeded, truncate from the
            # *beginning* (keep the tail). Prefix-cap froze Grok's
            # reconstructed editor log at ~20k (startup) so the agent
            # never saw shutdown.
            raw = bytearray()
            try:
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    raw.extend(chunk)
                    lim = int(slot.get("limit") or 0)
                    if lim > 0 and len(raw) > lim:
                        slot["truncated"] = True
                        del raw[:len(raw) - lim]
                        i = 0
                        while i < len(raw) and raw[i] & 0xC0 == 0x80:
                            i += 1
                        if i:
                            del raw[:i]
                    # Incremental: Kimi polls terminal/output while running;
                    # publishing only in finally left every poll empty.
                    slot[key] = strip_ansi(raw.decode("utf-8", "replace"))
            finally:
                # Plain text for agent + plugin UI (no raw ESC sequences).
                slot[key] = strip_ansi(bytes(raw).decode("utf-8", "replace"))

        async def wait_and_close():
            try:
                await asyncio.gather(
                    drain(proc.stdout, "stdout"),
                    drain(proc.stderr, "stderr"),
                    return_exceptions=True)
                code = await proc.wait()
                slot["exit_status"] = self._exit_status_from_code(code)
            except asyncio.CancelledError:
                # Detach-keep used to stamp exitCode 0 here. That made
                # Grok's watch_for_exit treat the job as done.
                self._kill_terminal_proc(proc)
                code = None
                try:
                    code = await asyncio.wait_for(proc.wait(), timeout=1.0)
                except Exception:
                    try:
                        os.killpg(proc.pid, 9)
                        code = await asyncio.wait_for(proc.wait(), timeout=0.5)
                    except Exception:
                        code = -9
                slot["exit_status"] = self._exit_status_from_code(
                    code if code is not None else -15)
                raise
            except Exception as e:
                self.file_log(f"terminal {tid} reader error: {e}")
                slot["exit_status"] = {"exitCode": 1, "signal": None}
            finally:
                # Close synthetic host Bash row if we opened one
                hid = slot.get("host_tool_id")
                if hid:
                    try:
                        out = (
                            (slot.get("stdout") or "")
                            + (slot.get("stderr") or "")
                        )
                        es = slot.get("exit_status") or {}
                        code = es.get("exitCode")
                        is_err = code is not None and int(code) != 0
                        if len(out) > 6000:
                            out = out[:6000] + "\n…[truncated]"
                        self._host_emit_tool_result(
                            hid, out or f"exit {code}", is_error=is_err)
                    except Exception as e:
                        self.file_log(f"synth tool_result {tid}: {e}")
                # Claude-compatible wake when host is already idle after end_turn
                try:
                    self._emit_bg_terminal_complete(tid)
                except Exception as e:
                    self.file_log(f"bg terminal complete emit failed: {e}")

        slot["reader"] = asyncio.create_task(wait_and_close())
        return {"terminalId": tid}

    async def _acp_terminal_output(self, params: dict) -> dict:
        tid = params.get("terminalId") or ""
        slot = self._terminals.get(tid)
        if slot:
            out = (slot.get("stdout") or "") + (slot.get("stderr") or "")
            self.file_log(
                f"terminal/output {tid} n={len(out)} "
                f"exit={slot.get('exit_status')!r} "
                f"truncated={bool(slot.get('truncated'))}")
            return {"output": out, "truncated": bool(slot["truncated"]),
                    "exitStatus": slot.get("exit_status")}
        dslot = getattr(self, "_detached_slots", {}).get(tid)
        if dslot is not None:
            out = (dslot.get("stdout") or "") + (dslot.get("stderr") or "")
            return {
                "output": out,
                "truncated": bool(dslot.get("truncated")),
                "exitStatus": dslot.get("exit_status"),
            }
        child = getattr(self, "_child_sessions", {}).get(tid)
        if child:
            self.file_log(
                f"terminal/output {tid} child session "
                f"done={bool(child.get('done'))} n={len(child.get('text') or '')}")
            return self._child_output_payload(child)
        snap = getattr(self, "_detached_snaps", {}).get(tid)
        if snap:
            out = snap.get("output") or ""
            self.file_log(
                f"terminal/output {tid} snap n={len(out)} "
                f"exit={snap.get('exitStatus')!r}")
            return {
                "output": out,
                "truncated": bool(snap.get("truncated")),
                "exitStatus": snap.get("exitStatus"),
            }
        if tid in getattr(self, "_released_terminals", set()) or str(tid).startswith("term_"):
            # Host shell we created (or already released).
            self.file_log(
                f"terminal/output {tid} unknown (released); return cancelled")
            return {
                "output": "",
                "truncated": False,
                "exitStatus": {"exitCode": None, "signal": "SIGTERM"},
            }
        # Grok polls spawn_subagent ids here before the first child update.
        self.file_log(f"terminal/output {tid} unknown child; still running")
        self._register_child_session(tid)
        return {"output": "", "truncated": False}

    def _mark_terminal_bg(self, tid: str, slot: dict) -> None:
        """⚙ only for this execute's explicit detach or native kimi detached."""
        eid = self._take_pending_execute_id()
        call = self._call(eid) if eid else None
        inp = call.input if call else {}
        grok_timeout0 = (
            getattr(self, "BACKEND_NAME", "") == "grok"
            and inp.get("timeout") in (0, 0.0)
        )
        explicit = bool(
            eid and (
                inp.get("run_in_background") is True
                or inp.get("detached") is True
                or inp.get("background") is True
                or grok_timeout0
                or (call and call.background)
            )
        )
        native = None
        if hasattr(self, "_kimi_detached_meta"):
            try:
                native = self._kimi_detached_meta(str(slot.get("cmd") or ""))
            except Exception:
                native = None
        if not explicit and not native:
            if hasattr(self, "_schedule_kimi_detached_probe"):
                try:
                    self._schedule_kimi_detached_probe(tid)
                except Exception:
                    pass
            return
        if not eid:
            eid = f"term-bg-{tid}"
            call = self._call(eid)
        slot["bg"] = True
        slot["tool_use_id"] = eid
        cap = int(getattr(self, "terminal_output_max_bytes", 0) or 0)
        if cap > 0:
            cap = max(4096, cap)
            if int(slot.get("limit") or 0) < cap:
                slot["limit"] = cap
        if not (call and call.background):
            self._register_bg_tool(
                eid, inp if inp else {"command": slot.get("cmd")})
        self._bind_terminal_to_bg_tool(tid, eid)
        if native and hasattr(self, "_link_terminal_to_kimi_task"):
            try:
                self._link_terminal_to_kimi_task(tid, eid, native)
            except Exception:
                pass

    async def _acp_terminal_wait(self, params: dict) -> dict:
        tid = params.get("terminalId") or ""
        slot = self._terminals.get(tid)
        if not slot:
            child = getattr(self, "_child_sessions", {}).get(tid)
            if child is None and tid not in getattr(self, "_released_terminals", set()) \
                    and not str(tid).startswith("term_"):
                child = self._register_child_session(tid)
            if child:
                ev = child.get("event")
                timeout = getattr(self, "terminal_wait_timeout_s", 0) or 0
                if ev is not None and not child.get("done"):
                    try:
                        if timeout and timeout > 0:
                            await asyncio.wait_for(ev.wait(), timeout=timeout)
                        else:
                            # ACP: wait until exit; interrupt/clear set the Event.
                            await ev.wait()
                    except asyncio.TimeoutError:
                        self.file_log(
                            f"terminal/wait_for_exit {tid} child TIMEOUT "
                            f"after {timeout}s")
                        child["done"] = True
                        child["exit"] = {
                            "exitCode": None, "signal": "SIGTERM",
                        }
                        if not ev.is_set():
                            ev.set()
                        try:
                            self._emit_bg_child_complete(tid, child)
                        except Exception as e:
                            self.file_log(
                                f"child wait timeout notify {tid}: {e}")
                    except asyncio.CancelledError:
                        return {"exitCode": None, "signal": "SIGTERM"}
                es = child.get("exit") or {
                    "exitCode": None, "signal": "SIGTERM",
                }
                return {"exitCode": es.get("exitCode"),
                        "signal": es.get("signal")}
            dslot = getattr(self, "_detached_slots", {}).get(tid)
            if dslot is not None:
                reader = dslot.get("reader")
                proc = dslot.get("proc") or getattr(
                    self, "_detached_procs", {}).get(tid)
                try:
                    if reader is not None and not reader.done():
                        await asyncio.shield(reader)
                    elif proc is not None and proc.returncode is None:
                        await proc.wait()
                except asyncio.CancelledError:
                    return {"exitCode": None, "signal": "SIGTERM"}
                except Exception as e:
                    self.file_log(f"terminal/wait_for_exit detached {tid}: {e}")
                es = dslot.get("exit_status") or {"exitCode": 0, "signal": None}
                try:
                    self._emit_bg_terminal_complete(tid)
                except Exception as e:
                    self.file_log(f"wait_for_exit detached complete {tid}: {e}")
                return {"exitCode": es.get("exitCode"),
                        "signal": es.get("signal")}
            snap = getattr(self, "_detached_snaps", {}).get(tid)
            if snap and snap.get("exitStatus"):
                es = snap.get("exitStatus") or {
                    "exitCode": None, "signal": "SIGTERM"}
                return {"exitCode": es.get("exitCode"),
                        "signal": es.get("signal")}
            # Already released/killed (e.g. on interrupt) — report cancelled.
            return {"exitCode": None, "signal": "SIGTERM"}
        # ACP wait_for_exit returns once the command completes. Grok
        # run_background spawns watch_for_exit AFTER create returns —
        # holding wait does not block the turn. Early ack {exitCode:0}
        # made Grok complete the task, release, then poll empty+done.
        # Release kills (official ACP + Zed). Keep _detach_terminal for
        # any remaining internal detach path; do not invent exitCode 0.
        reader = slot.get("reader")
        timeout = self.terminal_wait_timeout_s
        if reader is not None and not reader.done():
            try:
                if timeout and timeout > 0:
                    await asyncio.wait_for(
                        asyncio.shield(reader), timeout=timeout)
                else:
                    # ACP: wait until exit; agent aborts via terminal/kill.
                    await asyncio.shield(reader)
            except asyncio.TimeoutError:
                self.file_log(
                    f"terminal/wait_for_exit {tid} TIMEOUT after {timeout}s "
                    f"cmd={slot.get('cmd')!r}")
                self._kill_terminal_proc(slot.get("proc"))
                # Give reader a moment to collect exit status after kill.
                try:
                    await asyncio.wait_for(asyncio.shield(reader), timeout=2.0)
                except Exception:
                    if not reader.done():
                        reader.cancel()
                        try:
                            await reader
                        except Exception:
                            pass
                if not slot.get("exit_status"):
                    # Optional client timeout: still ACP-valid (no negative).
                    slot["exit_status"] = {
                        "exitCode": None, "signal": "SIGTERM",
                    }
            except asyncio.CancelledError:
                return {"exitCode": None, "signal": "SIGTERM"}
            except Exception as e:
                self.file_log(f"terminal/wait_for_exit {tid} error: {e}")
        elif reader is not None and reader.done():
            # Re-raise nothing — just pick up exit_status.
            try:
                reader.result()
            except Exception:
                pass
        # Terminal may have been closed during wait.
        slot = self._terminals.get(tid) or slot
        es = slot.get("exit_status") or {
            "exitCode": None, "signal": "SIGTERM",
        }
        # Sanitize any legacy negative codes still sitting on the slot.
        code = es.get("exitCode")
        if isinstance(code, int) and code < 0:
            es = self._exit_status_from_code(code)
            slot["exit_status"] = es
        out_n = len((slot.get("stdout") or "") + (slot.get("stderr") or ""))
        self.file_log(
            f"terminal/wait_for_exit {tid} → "
            f"exitCode={es.get('exitCode')} signal={es.get('signal')} "
            f"out={out_n}")
        # Ensure Claude bg wake even if bind raced with process exit
        try:
            self._emit_bg_terminal_complete(tid)
        except Exception as e:
            self.file_log(f"wait_for_exit bg complete: {e}")
        return {"exitCode": es.get("exitCode"), "signal": es.get("signal")}

    async def _acp_terminal_kill(self, params: dict) -> dict:
        tid = params.get("terminalId") or ""
        if tid in getattr(self, "_child_sessions", {}):
            self.file_log(f"terminal/kill {tid} is subagent session; ignore")
            return {}
        slot = self._terminals.get(tid)
        if slot:
            self._kill_terminal_proc(slot.get("proc"))
            self.file_log(f"terminal/kill {tid}")
        return {}

    async def _acp_terminal_release(self, params: dict) -> dict:
        tid = params.get("terminalId") or ""
        if tid in getattr(self, "_child_sessions", {}):
            # Detach the poll handle; the child session keeps running.
            self.file_log(f"terminal/release {tid} is subagent session; ignore")
            return {}
        # Official ACP + Zed: release kills if still running; the id is
        # then invalid. Grok only releases AFTER wait_for_exit returns.
        # Detach-keep cancelled the stdout drain and SIGPIPE'd the child.
        await self._terminal_close(tid)
        return {}

    async def _detach_terminal(self, tid: str) -> None:
        """Drop the ACP handle; leave a timeout:0 / bg process running."""
        slot = self._terminals.get(tid)
        if not slot:
            return
        slot["detached"] = True
        # Process is still running — do not invent exitCode 0 (that made
        # wait_for_exit return early and dropped remaining stdout).
        if not hasattr(self, "_detached_snaps"):
            self._detached_snaps = {}
        if not hasattr(self, "_detached_procs"):
            self._detached_procs = {}
        if not hasattr(self, "_detached_slots"):
            self._detached_slots = {}
        out = (slot.get("stdout") or "") + (slot.get("stderr") or "")
        self._detached_snaps[tid] = {
            "output": out,
            "truncated": bool(slot.get("truncated")),
            "exitStatus": slot.get("exit_status"),
        }
        extra = list(self._detached_snaps)[:-32]
        for old in extra:
            self._detached_snaps.pop(old, None)
        # Keep the stdout reader alive so sleep&&echo still lands in slot.
        self._terminals.pop(tid, None)
        self._detached_slots[tid] = slot
        proc = slot.get("proc")
        if proc is not None and proc.returncode is None:
            self._detached_procs[tid] = proc
            asyncio.create_task(self._watch_detached(tid, proc, slot))
        self.file_log(
            f"terminal/release {tid} detach (bg keep pid="
            f"{getattr(proc, 'pid', None)})")

    async def _watch_detached(self, tid: str, proc, slot: dict) -> None:
        try:
            code = await proc.wait()
            es = self._exit_status_from_code(code)
            slot["exit_status"] = es
            snap = getattr(self, "_detached_snaps", {}).get(tid)
            if snap is not None:
                snap["exitStatus"] = es
                snap["output"] = (slot.get("stdout") or "") + (
                    slot.get("stderr") or "")
            getattr(self, "_detached_procs", {}).pop(tid, None)
            try:
                self._emit_bg_terminal_complete(tid)
            except Exception as e:
                self.file_log(f"detached complete {tid}: {e}")
        except Exception as e:
            self.file_log(f"detached watch {tid}: {e}")
            getattr(self, "_detached_procs", {}).pop(tid, None)

    async def _terminal_close(self, tid: str) -> None:
        slot = self._terminals.pop(tid, None)
        if not slot:
            if not hasattr(self, "_released_terminals"):
                self._released_terminals = set()
            self._released_terminals.add(tid)
            return
        if not hasattr(self, "_released_terminals"):
            self._released_terminals = set()
        self._released_terminals.add(tid)
        self._kill_terminal_proc(slot.get("proc"))
        reader = slot.get("reader")
        if reader and not reader.done():
            reader.cancel()
            try:
                await asyncio.wait_for(reader, timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        # Hard kill if still alive after cancel.
        proc = slot.get("proc")
        if proc and proc.returncode is None:
            try:
                os.killpg(proc.pid, 9)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        out = (slot.get("stdout") or "") + (slot.get("stderr") or "")
        es = slot.get("exit_status") or {
            "exitCode": None, "signal": "SIGTERM"}
        if not hasattr(self, "_detached_snaps"):
            self._detached_snaps = {}
        self._detached_snaps[tid] = {
            "output": out,
            "truncated": bool(slot.get("truncated")),
            "exitStatus": es,
        }
        extra = list(self._detached_snaps)[:-32]
        for old in extra:
            self._detached_snaps.pop(old, None)
        self.file_log(f"terminal/release {tid}")
