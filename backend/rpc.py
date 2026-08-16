"""JSON-RPC 2.0 NDJSON client for a bridge subprocess. Sublime-free at import.

`send` callbacks and notifications are marshalled onto the Sublime main thread
via `sublime.set_timeout` when sublime is available. Without sublime (tests /
plain python3) the callback runs on the reader thread.

`send_wait` completes on the **reader thread**, not via set_timeout. Do NOT
call it from the UI thread for long ops — it freezes the editor until the
bridge replies (deadlock if the reply itself needs the UI thread). Prefer
async `send()` + callback for UI actions.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from typing import Any, Callable, Dict, Optional

try:
    from plat.constants import PLUGIN_NAME
except ImportError:
    PLUGIN_NAME = "Submarine"


def _run_on_main_thread(fn: Callable[[], None]) -> None:
    """Marshal `fn` onto the Sublime main thread; call it directly if no sublime."""
    try:
        import sublime
    except ImportError:
        fn()
        return
    sublime.set_timeout(fn, 0)


class JsonRpcClient:
    def __init__(self, on_notification: Callable[[str, dict], None]) -> None:
        self.proc = None  # type: Optional[subprocess.Popen]
        self.request_id = 0
        self.pending = {}  # type: Dict[int, Callable[[dict], None]]
        # send_wait: id → (holder_dict, Event) completed on the reader thread
        # so a blocked UI thread cannot deadlock waiting for set_timeout.
        self._sync_waits = {}  # type: Dict[int, tuple]
        self._send_lock = threading.Lock()
        self.on_notification = on_notification
        self.reader_thread = None  # type: Optional[threading.Thread]
        self.running = False
        self.stderr_thread = None  # type: Optional[threading.Thread]

    def start(self, cmd: list, env: Optional[Dict[str, str]] = None) -> None:
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=proc_env,
        )
        self.running = True
        self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.reader_thread.start()
        self.stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self.stderr_thread.start()

    def _stderr_loop(self):
        # type: () -> None
        """Forward stderr. Lines starting with `[` go through verbatim; others get `[bridge]`."""
        while self.running and self.proc and self.proc.stderr:
            try:
                line = self.proc.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                if not text:
                    continue
                if text.startswith("["):
                    print(text)
                else:
                    print("[bridge] %s" % text)
            except Exception as e:
                print("[%s] stderr_loop error: %s" % (PLUGIN_NAME, e))
                continue

    def stop(self) -> None:
        self.running = False
        self.pending.clear()
        for rid, (holder, ev) in list(self._sync_waits.items()):
            holder["error"] = {"message": "Bridge stopped"}
            ev.set()
        self._sync_waits.clear()
        if self.proc:
            self.proc.terminate()
            self.proc.wait()
            self.proc = None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def send(
        self,
        method: str,
        params: dict,
        callback: Optional[Callable[[dict], None]] = None,
    ) -> bool:
        """Send a request. Returns False if the bridge is dead.

        Response callbacks run on the Sublime main thread (via set_timeout)
        when sublime is importable; otherwise they run on the reader thread.
        """
        if not self.proc or not self.proc.stdin:
            return False
        if self.proc.poll() is not None:
            print("[%s] Bridge process died with code %s" % (
                PLUGIN_NAME, self.proc.returncode))
            return False

        with self._send_lock:
            self.request_id += 1
            rid = self.request_id
            req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            if callback:
                self.pending[rid] = callback
            try:
                self.proc.stdin.write((json.dumps(req) + "\n").encode())
                self.proc.stdin.flush()
            except Exception as e:
                self.pending.pop(rid, None)
                print("[%s] Bridge send failed: %s" % (PLUGIN_NAME, e))
                return False
        return True

    def send_wait(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        """Send a request and wait. Returns `{"result": ...}` or `{"error": ...}`.

        Completes on the reader thread (not set_timeout). Must NOT be used
        from the UI thread for long ops — it freezes the editor until the
        bridge replies, and deadlocks if that reply needs the UI thread.
        Prefer async send() + callback for UI actions.
        """
        if not self.proc or not self.proc.stdin:
            return {"error": {"message": "Failed to send request - bridge is dead"}}
        if self.proc.poll() is not None:
            return {"error": {"message": "Bridge process died with code %s" % (
                self.proc.returncode,)}}

        holder = {}  # type: dict
        done = threading.Event()
        with self._send_lock:
            self.request_id += 1
            rid = self.request_id
            self._sync_waits[rid] = (holder, done)
            req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            try:
                self.proc.stdin.write((json.dumps(req) + "\n").encode())
                self.proc.stdin.flush()
            except Exception as e:
                self._sync_waits.pop(rid, None)
                return {"error": {"message": "Bridge send failed: %s" % e}}

        if not done.wait(timeout):
            self._sync_waits.pop(rid, None)
            return {"error": {"message": "Request timed out after %ss" % timeout}}

        if "error" in holder:
            return {"error": holder["error"]}
        return {"result": holder.get("result", {})}

    def _read_loop(self):
        # type: () -> None
        while self.running and self.proc and self.proc.stdout:
            try:
                line = self.proc.stdout.readline()
                if not line:
                    self._handle_stdout_closed()
                    break

                text = line.decode(errors="replace")
                if not text.strip():
                    continue

                try:
                    msg = json.loads(text)
                except json.JSONDecodeError as e:
                    snippet = text.strip().replace("\n", "\\n")[:200]
                    print("[%s RPC] read_loop: invalid JSON line: %s: %r" % (
                        PLUGIN_NAME, e, snippet))
                    continue

                mid = msg.get("id")
                if mid is not None and mid in self._sync_waits:
                    holder, ev = self._sync_waits.pop(mid)
                    if "error" in msg:
                        holder["error"] = msg["error"]
                    else:
                        holder["result"] = msg.get("result", {})
                    ev.set()
                    continue

                _run_on_main_thread(lambda m=msg: self._handle(m))
            except Exception as e:
                print("[%s RPC] read_loop error: %s" % (PLUGIN_NAME, e))
                continue

    def _handle_stdout_closed(self):
        # type: () -> None
        """Bridge stdout reached EOF; fail pending RPC requests promptly."""
        proc = self.proc
        returncode = proc.poll() if proc else None
        if returncode is None:
            detail = "Bridge stdout closed while process is still running"
        else:
            detail = "Bridge process exited with code %s" % returncode

        print("[%s RPC] read_loop: %s" % (PLUGIN_NAME, detail))
        self.running = False

        for rid, (holder, ev) in list(self._sync_waits.items()):
            holder["error"] = {"message": detail}
            ev.set()
        self._sync_waits.clear()

        pending = list(self.pending.items())
        self.pending.clear()
        if not pending:
            return

        response = {"error": {"message": detail}}
        for _, callback in pending:
            _run_on_main_thread(lambda cb=callback, r=response: cb(r))

    def _handle(self, msg):
        # type: (dict) -> None
        if "id" in msg and msg["id"] in self.pending:
            cb = self.pending.pop(msg["id"])
            # Historical shape: bare result dict, or {"error": ...}
            cb({"error": msg["error"]} if "error" in msg else msg.get("result", {}))
        elif "method" in msg:
            self.on_notification(msg["method"], msg.get("params", {}))
