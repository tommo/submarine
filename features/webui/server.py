"""HTTP surface for the session-control socket.

One action per endpoint, named after the CLI's subcommands, so the API really is
`submarine_sessions` with HTTP framing:

  GET  /api/health                 socket path + whether it exists
  GET  /api/list?scope=all         live + saved sessions  (`list`)
  GET  /api/view?ref=…&mode=tail   transcript / sheet / edits  (`view`)
  POST /api/chat                   {"ref": …, "prompt": …, "queue": …}  (`chat`)
  POST /api/interrupt              {"ref": …}                  (`interrupt`)
  GET  /api/pending?ref=…          the question / permission / plan the sheet waits on  (`pending`)
  POST /api/answer                 {"ref", "kind", "option"|"options"|"text"|"response", "qid"|"id"}  (`answer`)
  GET  /api/backends               backends + windows a new session can use  (`backends`)
  POST /api/create                 {"backend", "model", "name", "window"|"project", "prompt"}  (`create`)
  POST /api/rename                 {"ref", "name"}  (`rename`)
  POST /api/close                  {"ref", "remove"}  (`close`)
  POST /api/open                   {"ref", "file_path", "line"} — open the file in Sublime  (`open`)
  GET  /api/file?path=…&ref=…      a text file's contents, for the code view  (`read`)

Plus `GET /` and `/static/*` for the console itself. The plugin's envelope is
returned verbatim, with `http` added, and `data.code` decides the status code —
so a caller sees the CLI's own error text and the same stable codes.

Stdlib only and Sublime-free: `ThreadingHTTPServer` over the CLI's socket
client. Binding 0.0.0.0 is deliberate (see `features/webui/cli.py`); `--token`
optionally gates every `/api` route.
"""
from __future__ import annotations

import hmac
import json
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from features.webui.client import (DEFAULT_HOST, DEFAULT_PORT, SessionClient)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

#: `features.session_control.ControlError.code` → HTTP status. Codes absent
#: here are the plugin's transport failures (no socket, no answer), which are
#: 503: something the caller can retry once Sublime is up.
_STATUS = {
    "bad_request": 400,
    "unknown_action": 400,
    "not_found": 404,
    "ambiguous": 409,
    "busy": 409,
    "stale": 409,
    "no_view": 409,
    "no_session": 409,
    "transcript": 500,
    "internal": 500,
}
TRANSPORT_STATUS = 503
MAX_BODY_BYTES = 1024 * 1024

_POST_ROUTES = ("/api/chat", "/api/interrupt", "/api/answer", "/api/create",
                "/api/rename", "/api/close", "/api/open")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

#: Query field → coercion for the two read endpoints.
_VIEW_INTS = ("turns", "max_chars", "offset", "limit")


def _first(query: Dict[str, list], key: str) -> Optional[str]:
    values = query.get(key) or []
    return values[0] if values and values[0] != "" else None


def _int_or_none(raw: Optional[str]) -> Optional[int]:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class WebUIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SubmarineWebUI/1"

    # ─── plumbing ───────────────────────────────────────────────────────────

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        """A line per request on stderr. The console polls, so only failures
        (and `--verbose`) get logged by default."""
        status = int(code) if str(code).isdigit() else 0
        if not getattr(self.server, "verbose", False) and status < 400:
            return
        BaseHTTPRequestHandler.log_request(self, code, size)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        sys.stderr.write("[webui] %s %s\n" % (self.address_string(), fmt % args))

    @property
    def _client(self) -> SessionClient:
        return self.server.client  # type: ignore[attr-defined]

    def _route(self) -> Tuple[str, Dict[str, list]]:
        parts = urlparse(self.path)
        return unquote(parts.path), parse_qs(parts.query)

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _reply(self, env: Dict[str, Any]) -> None:
        """Status from the envelope's `data.code`, body as the envelope."""
        if not isinstance(env, dict):
            return self._json(502, {"ok": False, "error": "malformed envelope"})
        if env.get("ok"):
            return self._json(200, env)
        data = env.get("data") if isinstance(env.get("data"), dict) else {}
        code = data.get("code")
        status = _STATUS.get(str(code), TRANSPORT_STATUS if code is None else 500)
        body = dict(env)
        body["http"] = status
        self._json(status, body)

    def _token_ok(self, query: Dict[str, list], header: bool = True) -> bool:
        want = getattr(self.server, "token", "")
        if not want:
            return True
        got = _first(query, "token") or ""
        if not got and header:
            got = self.headers.get("X-Submarine-Token") or ""
        return hmac.compare_digest(str(want), str(got))

    def _refuse(self, status: int, payload: Dict[str, Any]) -> None:
        """Refuse a POST before its body is read: the connection has to end here,
        or the unread body is parsed as the next request line."""
        self.close_connection = True
        payload.setdefault("http", status)
        self._json(status, payload)

    def _body(self) -> Optional[Dict[str, Any]]:
        """The JSON request body, or None once an error reply was sent."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(400, {"ok": False, "error": "empty or oversized body",
                             "http": 400})
            return None
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            self._json(400, {"ok": False, "error": "bad JSON body: %s" % e,
                             "http": 400})
            return None
        if not isinstance(parsed, dict):
            self._json(400, {"ok": False, "error": "body must be a JSON object",
                             "http": 400})
            return None
        return parsed

    # ─── GET ────────────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802
        route, query = self._route()
        if route in _POST_ROUTES:
            return self._json(405, {"ok": False, "http": 405,
                                    "error": "use POST for %s" % route})
        if not route.startswith("/api/"):
            return self._static(route)
        if not self._token_ok(query):
            return self._json(401, {"ok": False, "http": 401,
                                    "error": "missing or wrong token",
                                    "hint": "append ?token=… or send "
                                            "X-Submarine-Token"})
        if route == "/api/health":
            return self._json(200, self._client.health())
        if route == "/api/list":
            return self._reply(self._client.list(
                scope=_first(query, "scope"), parent=_first(query, "parent"),
                window=_first(query, "window")))
        if route == "/api/backends":
            return self._reply(self._client.backends())
        if route == "/api/file":
            path = _first(query, "path")
            if not path:
                return self._json(400, {"ok": False, "http": 400,
                                        "error": "path is required"})
            return self._reply(self._client.read(
                _first(query, "ref"), path[:4096], _int_or_none(_first(query, "max_bytes"))))
        if route == "/api/pending":
            ref = _first(query, "ref")
            if not ref:
                return self._json(400, {"ok": False, "http": 400,
                                        "error": "ref is required"})
            return self._reply(self._client.pending(ref))
        if route == "/api/view":
            fields = {"mode": _first(query, "mode")}  # type: Dict[str, Any]
            for key in _VIEW_INTS:
                value = _int_or_none(_first(query, key))
                if value is not None:
                    fields[key] = value
            fields["file_path"] = _first(query, "file_path")
            ref = _first(query, "ref")
            if not ref:
                return self._json(400, {"ok": False, "http": 400,
                                        "error": "ref is required"})
            return self._reply(self._client.view(ref, **fields))
        return self._json(404, {"ok": False, "http": 404,
                                "error": "no route %s" % route})

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    # ─── POST ───────────────────────────────────────────────────────────────

    def do_POST(self) -> None:  # noqa: N802
        route, query = self._route()
        if route not in _POST_ROUTES:
            return self._refuse(404, {"ok": False, "error": "no route %s" % route})
        if not self._token_ok(query):
            return self._refuse(401, {"ok": False,
                                      "error": "missing or wrong token"})
        body = self._body()
        if body is None:
            return
        if route == "/api/create":
            fields = {}  # type: Dict[str, Any]
            for key in ("backend", "model", "name", "window", "project", "prompt", "idem"):
                if body.get(key) not in (None, ""):
                    fields[key] = body[key]
            if isinstance(fields.get("prompt"), str):
                fields["prompt"] = fields["prompt"][:MAX_BODY_BYTES]
            return self._reply(self._client.create(**fields))
        if route == "/api/open":
            path = str(body.get("file_path") or "").strip()
            if not path:
                return self._json(400, {"ok": False, "http": 400,
                                        "error": "file_path is required"})
            return self._reply(self._client.open(
                str(body.get("ref") or "").strip() or None, path[:4096], body.get("line")))
        ref = str(body.get("ref") or "").strip()
        if not ref:
            return self._json(400, {"ok": False, "http": 400,
                                    "error": "ref is required"})
        if route == "/api/rename":
            return self._reply(self._client.rename(ref, str(body.get("name") or "")[:200]))
        if route == "/api/close":
            return self._reply(self._client.close(ref, remove=bool(body.get("remove"))))
        if route == "/api/interrupt":
            return self._reply(self._client.interrupt(ref))
        if route == "/api/answer":
            fields = {}  # type: Dict[str, Any]
            for key in ("kind", "option", "options", "text", "response", "qid", "id"):
                if body.get(key) is not None:
                    fields[key] = body[key]
            if isinstance(fields.get("text"), str):
                fields["text"] = fields["text"][:MAX_BODY_BYTES]
            return self._reply(self._client.answer(ref, **fields))
        prompt = str(body.get("prompt") or "")
        if not prompt.strip():
            return self._json(400, {"ok": False, "http": 400,
                                    "error": "prompt is required"})
        return self._reply(self._client.chat(
            ref, prompt[:MAX_BODY_BYTES],
            queue=body.get("queue") or None,
            idem=str(body["idem"])[:200] if body.get("idem") else None,
            display=str(body["display"])[:200] if body.get("display") else None))

    # ─── static files ───────────────────────────────────────────────────────

    def _static(self, route: str) -> None:
        if route in ("/", "/index.html"):
            name = "index.html"
        elif route.startswith("/static/"):
            name = route[len("/static/"):]
        else:
            return self._json(404, {"ok": False, "http": 404,
                                    "error": "no route %s" % route})
        target = os.path.normpath(os.path.join(STATIC_DIR, name))
        if not target.startswith(STATIC_DIR + os.sep):
            return self._json(404, {"ok": False, "http": 404,
                                    "error": "no file %s" % name})
        try:
            with open(target, "rb") as f:
                body = f.read()
        except OSError:
            return self._json(404, {"ok": False, "http": 404,
                                    "error": "no file %s" % name})
        ctype = _CONTENT_TYPES.get(os.path.splitext(target)[1].lower(),
                                   "application/octet-stream")
        self._send(200, body, ctype)


class WebUIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], client: SessionClient,
                 token: str = "", verbose: bool = False) -> None:
        ThreadingHTTPServer.__init__(self, address, WebUIHandler)
        self.client = client
        self.token = token or ""
        self.verbose = bool(verbose)


def build_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 socket_path: str = "", token: str = "",
                 client: Optional[SessionClient] = None,
                 verbose: bool = False) -> WebUIServer:
    """A bound server. Port 0 picks a free one (the tests use that)."""
    return WebUIServer((host, port), client or SessionClient(socket_path),
                       token, verbose)


def lan_addresses(limit: int = 3) -> list:
    """Best-effort IPv4 addresses of this host, for the startup banner."""
    found = []  # type: list
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET, socket.SOCK_STREAM):
            addr = info[4][0]
            if addr not in found and not addr.startswith("127."):
                found.append(addr)
            if len(found) >= limit:
                break
    except OSError:
        pass
    return found


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
        socket_path: str = "", token: str = "", quiet: bool = False,
        verbose: bool = False) -> int:
    """Bind, print where to point a browser, then serve until Ctrl-C."""
    try:
        server = build_server(host, port, socket_path, token, verbose=verbose)
    except OSError as e:
        sys.stderr.write("error: cannot bind %s:%s: %s\n" % (host, port, e))
        return 1
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    if not quiet:
        out = sys.stdout
        client = server.client
        out.write("Submarine web UI\n")
        out.write("  local     http://127.0.0.1:%d/\n" % bound_port)
        out.write("  all       http://%s:%d/  (bound to every interface)\n"
                  % (bound_host, bound_port))
        for addr in lan_addresses():
            out.write("  reachable http://%s:%d/\n" % (addr, bound_port))
        out.write("  socket    %s%s\n" % (
            client.socket_path,
            "" if os.path.exists(client.socket_path) else "  (missing — start "
                                                          "Sublime with Submarine)"))
        if server.token:
            out.write("  token     required (X-Submarine-Token or ?token=…)\n")
        else:
            out.write("  token     off — anyone who can reach this port can "
                      "read and prompt your sessions\n")
        out.write("Ctrl-C to stop.\n")
        out.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write("\nstopped\n")
    finally:
        server.server_close()
    return 0
