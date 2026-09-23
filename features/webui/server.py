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
  POST /api/clear                  {"ref", "keep_last"} — clear the sheet (Cmd+K / Cmd+Shift+K)  (`clear`)

Plus `GET /` and `/static/*` for the console itself, and `/api/access/*` so a
browser can ask for a device grant. Every other `/api` route needs a device
cookie, the legacy `--token`, or a loopback peer (unless
`--require-auth-on-loopback`). The plugin's envelope is returned verbatim,
with `http` added, and `data.code` decides the status code — so a caller sees
the CLI's own error text and the same stable codes.

Stdlib only and Sublime-free: `ThreadingHTTPServer` over the CLI's socket
client. Binding 0.0.0.0 is deliberate (see `features/webui/cli.py`).
"""
from __future__ import annotations

import hmac
import json
import os
import socket
import sys
import threading
import time
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
    "rate_limited": 429,
}
TRANSPORT_STATUS = 503
MAX_BODY_BYTES = 1024 * 1024
COOKIE_NAME = "submarine_device"
COOKIE_MAX_AGE = 365 * 24 * 60 * 60
#: A revoke shows up once this expires, without a socket call on every request.
CHECK_TTL = 3.0

_POST_ROUTES = ("/api/chat", "/api/interrupt", "/api/answer", "/api/create",
                "/api/rename", "/api/close", "/api/open", "/api/clear")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
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


def is_loopback(ip: str) -> bool:
    host = (ip or "").strip().lower()
    if host.startswith("::ffff:"):
        host = host[len("::ffff:"):]
    return host in ("127.0.0.1", "::1")


def access_allowed(peer_ip: str, cookie_ok: bool, legacy_ok: bool,
                   auth_loopback: bool) -> bool:
    """A device cookie, the legacy shared secret, or loopback unless the flag."""
    if cookie_ok or legacy_ok:
        return True
    if not auth_loopback and is_loopback(peer_ip):
        return True
    return False


def cookie_value(header: str, name: str) -> str:
    if not header:
        return ""
    for part in header.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip() == name:
            return value.strip()
    return ""


def _cookie_token_safe(token: str) -> bool:
    if not token or len(token) > 200:
        return False
    for ch in token:
        if not (ch.isalnum() or ch in "-_"):
            return False
    return True


def device_cookie(token: str, secure: bool) -> str:
    """HttpOnly so page script cannot read the grant. Secure only on https."""
    parts = [
        "%s=%s" % (COOKIE_NAME, token),
        "Path=/",
        "Max-Age=%d" % COOKIE_MAX_AGE,
        "HttpOnly",
        "SameSite=Strict",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


class CheckCache(object):
    """A few seconds of `web_access_check` results, keyed by the raw token."""

    def __init__(self, ttl: float) -> None:
        self.ttl = float(ttl)
        self._lock = threading.Lock()
        self._hits = {}  # type: Dict[str, Tuple[float, bool]]

    def get(self, token: str) -> Optional[bool]:
        if self.ttl <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            row = self._hits.get(token)
            if row is None:
                return None
            if row[0] <= now:
                self._hits.pop(token, None)
                return None
            return row[1]

    def put(self, token: str, ok: bool) -> None:
        if self.ttl <= 0 or not token:
            return
        now = time.monotonic()
        with self._lock:
            self._hits[token] = (now + self.ttl, bool(ok))
            if len(self._hits) <= 256:
                return
            stale = [key for key, item in self._hits.items() if item[0] <= now]
            for key in stale:
                self._hits.pop(key, None)
            while len(self._hits) > 256:
                self._hits.pop(next(iter(self._hits)), None)


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

    def _send(self, status: int, body: bytes, ctype: str,
              extra: Optional[list] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for item in extra or []:
            self.send_header(item[0], item[1])
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any, extra: Optional[list] = None) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra)

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

    def _legacy_ok(self, query: Dict[str, list]) -> bool:
        want = str(getattr(self.server, "token", "") or "")
        if not want:
            return False
        got = _first(query, "token") or ""
        if not got:
            got = self.headers.get("X-Submarine-Token") or ""
        got = str(got)
        if len(got) != len(want):
            return False
        return hmac.compare_digest(want, got)

    def _peer_ip(self) -> str:
        addr = self.client_address
        if isinstance(addr, tuple) and addr:
            return str(addr[0])
        return ""

    def _cookie_ok(self) -> bool:
        token = cookie_value(self.headers.get("Cookie") or "", COOKIE_NAME)
        if not token:
            return False
        cache = getattr(self.server, "check_cache", None)
        if cache is not None:
            hit = cache.get(token)
            if hit is not None:
                return hit
        fn = getattr(self._client, "web_access_check", None)
        if fn is None:
            return False
        try:
            env = fn(token)
        except Exception:
            return False
        if not isinstance(env, dict) or not env.get("ok"):
            return False
        data = env.get("data") if isinstance(env.get("data"), dict) else {}
        valid = bool(data.get("ok"))
        if cache is not None:
            cache.put(token, valid)
        return valid

    def _access_ok(self, query: Dict[str, list]) -> bool:
        return access_allowed(
            self._peer_ip(),
            self._cookie_ok(),
            self._legacy_ok(query),
            bool(getattr(self.server, "auth_loopback", False)),
        )

    def _request_is_https(self) -> bool:
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        if proto == "https":
            return True
        try:
            import ssl
            return isinstance(self.connection, ssl.SSLSocket)
        except Exception:
            return False

    def _unauthorized(self, closing: bool) -> None:
        if getattr(self.server, "token", ""):
            payload = {"ok": False, "http": 401,
                       "error": "missing or wrong token",
                       "hint": "append ?token=\u2026 or send X-Submarine-Token"}
        else:
            payload = {"ok": False, "http": 401, "error": "access required",
                       "hint": "request access, then grant it in Sublime: "
                               "Submarine: Web Access\u2026"}
        if closing:
            self._refuse(401, payload)
        else:
            self._json(401, payload)

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
        if route in _POST_ROUTES or route == "/api/access/request":
            return self._json(405, {"ok": False, "http": 405,
                                    "error": "use POST for %s" % route})
        if not route.startswith("/api/"):
            return self._static(route)
        if route == "/api/access/me":
            return self._access_me(query)
        if route == "/api/access/status":
            return self._access_status(query)
        if not self._access_ok(query):
            return self._unauthorized(closing=False)
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
        if route == "/api/access/request":
            return self._access_request()
        if route not in _POST_ROUTES:
            return self._refuse(404, {"ok": False, "error": "no route %s" % route})
        if not self._access_ok(query):
            return self._unauthorized(closing=True)
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
        if route == "/api/clear":
            keep = body.get("keep_last")
            return self._reply(self._client.clear(ref, keep_last=True if keep is None else bool(keep)))
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

    # ─── device access ──────────────────────────────────────────────────────

    def _access_me(self, query: Dict[str, list]) -> None:
        if self._cookie_ok():
            via = "cookie"  # type: Optional[str]
            auth = True
        elif self._legacy_ok(query):
            via = "token"
            auth = True
        elif access_allowed(self._peer_ip(), False, False,
                            bool(getattr(self.server, "auth_loopback", False))):
            via = "loopback"
            auth = True
        else:
            via = None
            auth = False
        self._json(200, {"ok": True, "authenticated": auth, "via": via})

    def _access_status(self, query: Dict[str, list]) -> None:
        ident = _first(query, "id") or ""
        if not ident:
            return self._json(400, {"ok": False, "http": 400,
                                    "error": "id is required"})
        fn = getattr(self._client, "web_access_status", None)
        if fn is None:
            return self._json(503, {"ok": False, "http": 503,
                                    "error": "access status is unavailable"})
        env = fn(ident[:64])
        if not isinstance(env, dict) or not env.get("ok"):
            return self._reply(env if isinstance(env, dict)
                               else {"ok": False, "error": "malformed envelope"})
        data = env.get("data") if isinstance(env.get("data"), dict) else {}
        token = data.get("token") if isinstance(data.get("token"), str) else ""
        public = {
            "status": data.get("status") or "unknown",
            "id": data.get("id") or ident,
            "cookie": False,
        }
        extra = None
        if token and _cookie_token_safe(token):
            public["cookie"] = True
            extra = [("Set-Cookie", device_cookie(token, self._request_is_https()))]
        self._json(200, {"ok": True, "data": public}, extra)

    def _access_request(self) -> None:
        body = self._body()
        if body is None:
            return
        fn = getattr(self._client, "web_access_request", None)
        if fn is None:
            return self._json(503, {"ok": False, "http": 503,
                                    "error": "access requests are unavailable"})
        env = fn(str(body.get("name") or ""), self._peer_ip(),
                 self.headers.get("User-Agent") or "")
        self._reply(env if isinstance(env, dict)
                    else {"ok": False, "error": "malformed envelope"})

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
                 token: str = "", verbose: bool = False,
                 auth_loopback: bool = False,
                 check_ttl: float = CHECK_TTL) -> None:
        ThreadingHTTPServer.__init__(self, address, WebUIHandler)
        self.client = client
        self.token = token or ""
        self.verbose = bool(verbose)
        self.auth_loopback = bool(auth_loopback)
        self.check_cache = CheckCache(check_ttl)


def build_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 socket_path: str = "", token: str = "",
                 client: Optional[SessionClient] = None,
                 verbose: bool = False, auth_loopback: bool = False,
                 check_ttl: float = CHECK_TTL) -> WebUIServer:
    """A bound server. Port 0 picks a free one (the tests use that)."""
    server = WebUIServer((host, port), client or SessionClient(socket_path),
                         token, verbose, auth_loopback, check_ttl)
    return server


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
        verbose: bool = False, auth_loopback: bool = False) -> int:
    """Bind, print where to point a browser, then serve until Ctrl-C."""
    try:
        server = build_server(host, port, socket_path, token, verbose=verbose,
                              auth_loopback=auth_loopback)
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
            out.write("  token     shared secret accepted "
                      "(X-Submarine-Token or ?token=…)\n")
        else:
            out.write("  token     off\n")
        if server.auth_loopback:
            out.write("  access    this machine also needs a device cookie "
                      "or the shared secret\n")
        else:
            out.write("  access    this machine is open; other machines need "
                      "a device cookie or the shared secret\n")
        out.write("Ctrl-C to stop.\n")
        out.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write("\nstopped\n")
    finally:
        server.server_close()
    return 0
