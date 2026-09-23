"""Serve the web UI from inside Sublime.

The HTTP accept loop is a daemon thread. Each action runs on the main thread
via ``sublime.set_timeout``, which is the same hop the socket server makes.
There is no second process to restart when the plugin reloads.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
from typing import Optional

from features.webui.client import (
    CHAT_TIMEOUT,
    DEFAULT_HOST,
    DEFAULT_PORT,
    SessionClient,
    caller,
)
from features.webui.server import build_server

TOKEN_ENV = "SUBMARINE_WEB_TOKEN"

_server = None
_gen = 0


def _log(message: str) -> None:
    try:
        from plat.log import log_plugin
        log_plugin("web: %s" % message)
    except Exception:
        print("[Submarine web] %s" % message)


class EditorClient(SessionClient):
    """The SessionClient methods, answered in this process on the main thread."""

    def call(self, action: str, timeout: Optional[float] = None,
             **fields):
        import sublime
        from features.session_control import dispatch

        request = {"action": action, "caller": caller()}
        for key, value in fields.items():
            if value is not None:
                request[key] = value
        # Already on the UI thread: dispatch directly. Waiting for set_timeout
        # here would deadlock, because only this thread drains the queue.
        if threading.current_thread() is threading.main_thread():
            try:
                return dispatch(request)
            except Exception as e:
                return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        box = {}  # type: dict
        done = threading.Event()

        def work():
            try:
                box["env"] = dispatch(request)
            except Exception as e:
                box["env"] = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            finally:
                done.set()

        sublime.set_timeout(work, 0)
        if not done.wait(timeout or self.timeout):
            return {"ok": False, "error": "timed out waiting for Sublime"}
        return box.get("env") or {"ok": False, "error": "Sublime answered nothing"}

    def health(self):
        return {"ok": True, "socket": "in-process", "present": True}

    def chat(self, ref: str, prompt: str, **fields):
        return self.call("chat", timeout=CHAT_TIMEOUT, ref=ref, prompt=prompt,
                         **fields)


def _settings():
    """Host, port, token, loopback flag. A settings port of 0 disables the UI.

    Returns port ``None`` when the setting turns the server off. An explicit
    ``start(..., port=0)`` is different: the OS picks a free port (tests).
    """
    host, port, token, auth_loopback = DEFAULT_HOST, DEFAULT_PORT, "", False
    disabled = False
    try:
        import sublime
        from plat.constants import SETTINGS_FILE
        settings = sublime.load_settings(SETTINGS_FILE)
        host = str(settings.get("web_host") or host)
        raw_port = settings.get("web_port")
        if raw_port is not None and raw_port != "":
            port = int(raw_port)
            disabled = port == 0
        token = str(settings.get("web_token") or "")
        auth_loopback = bool(settings.get("web_require_auth_on_loopback"))
    except Exception as e:
        _log("settings: %s" % e)
    if not token:
        token = os.environ.get(TOKEN_ENV) or ""
    if disabled:
        return host, None, token, auth_loopback
    return host, port, token, auth_loopback


def _listen_pids(port: int):
    try:
        out = subprocess.check_output(
            ["lsof", "-nP", "-iTCP:%d" % int(port), "-sTCP:LISTEN", "-t"],
            stderr=subprocess.DEVNULL, timeout=2)
    except Exception:
        return []
    pids = []
    for line in out.split():
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def _cmdline(pid: int) -> str:
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            stderr=subprocess.DEVNULL, timeout=2)
    except Exception:
        return ""
    return out.decode("utf-8", "replace")


def _reclaim_stale_cli(port: int) -> None:
    """SIGTERM a leftover ``submarine_web.py`` so this process can bind."""
    me = os.getpid()
    for pid in _listen_pids(port):
        if pid == me:
            continue
        command = _cmdline(pid)
        if "submarine_web.py" not in command and "features.webui" not in command:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            _log("stopped leftover web process %s" % pid)
        except OSError as e:
            _log("could not stop pid %s: %s" % (pid, e))


def start(host: Optional[str] = None, port: Optional[int] = None,
          token: Optional[str] = None, auth_loopback: Optional[bool] = None) -> int:
    """Bind and serve. Returns the port, or 0 when disabled or not yet bound."""
    global _gen
    # No arguments: plugin startup. Arguments: a caller picked the bind,
    # including port 0 (ephemeral). Mixing is not used.
    if host is None and port is None and token is None and auth_loopback is None:
        host, port, token, auth_loopback = _settings()
        if port is None:
            return 0
    if port is None or int(port) < 0:
        return 0
    _gen += 1
    _start_now(str(host), int(port), str(token or ""), bool(auth_loopback), _gen, 0)
    if _server is not None:
        return int(_server.server_address[1])
    return 0


def _start_now(host: str, port: int, token: str, auth_loopback: bool,
               gen: int, attempt: int) -> None:
    global _server
    if gen != _gen or _server is not None:
        return
    try:
        server = build_server(
            host, port, token=token, client=EditorClient(),
            auth_loopback=auth_loopback)
    except OSError as e:
        if attempt == 0:
            _reclaim_stale_cli(port)
        if attempt < 15 and gen == _gen:
            try:
                import sublime
                sublime.set_timeout(
                    lambda h=host, p=port, t=token, a=auth_loopback, g=gen, n=attempt: (
                        _start_now(h, p, t, a, g, n + 1)),
                    200)
            except Exception:
                _log("bind %s:%s failed: %s" % (host, port, e))
            return
        _log("bind %s:%s failed: %s" % (host, port, e))
        try:
            import sublime
            sublime.status_message("Submarine web UI: port %s is in use" % port)
        except Exception:
            pass
        return
    _server = server

    def serve(bound=server):
        try:
            bound.serve_forever(poll_interval=0.25)
        except Exception as e:
            if _server is bound:
                _log("server stopped: %s" % e)

    threading.Thread(target=serve, name="submarine-web", daemon=True).start()
    bound_port = int(server.server_address[1])
    _log("listening on %s:%s" % (host, bound_port))
    try:
        import sublime
        sublime.status_message("Submarine web UI http://127.0.0.1:%d/" % bound_port)
    except Exception:
        pass


def stop() -> None:
    """Drop the listening socket. In-flight requests finish on their own thread."""
    global _server, _gen
    _gen += 1
    server = _server
    _server = None
    if server is None:
        return

    def _shutdown(bound=server):
        try:
            bound.shutdown()
        except Exception:
            pass
        try:
            bound.server_close()
        except Exception:
            pass

    # shutdown() waits for the accept loop. Calling it on the UI thread
    # deadlocks when a handler is itself waiting for that thread.
    threading.Thread(target=_shutdown, name="submarine-web-stop", daemon=True).start()
