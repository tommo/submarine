"""Per-device grants for the web UI.

A browser asks (`request`); the owner grants or denies from Sublime. The JSON
file stores the sha256 of a token, never the token. The raw token stays in
this process until the browser's status poll collects it, so a revoke is a
file change the next check sees.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional

#: The list the owner sees. A LAN host cannot grow it past these.
MAX_PENDING = 20
MAX_PENDING_PER_IP = 3
#: How long a granted token waits in memory for the browser to poll.
HANDOFF_SECONDS = 600
#: Avoid rewriting the file on every API call just to bump last_seen.
SEEN_INTERVAL = 15.0
DENIED_CAP = 64
NAME_LIMIT = 64
UA_LIMIT = 240
IP_LIMIT = 64

Clock = Callable[[], float]
Notify = Callable[[str], None]


class WebAccessError(Exception):
    """A refused access action. `code` is stable for the HTTP layer."""

    def __init__(self, code: str, message: str):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def store_path() -> str:
    """Where grants live. Override with SUBMARINE_WEB_ACCESS_FILE in tests."""
    override = os.environ.get("SUBMARINE_WEB_ACCESS_FILE") or ""
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".submarine", "web_access.json")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hash_eq(stored: str, digest: str) -> bool:
    if not stored or len(stored) != len(digest):
        return False
    return hmac.compare_digest(stored, digest)


def _clean(value: Any, limit: int) -> str:
    """One line, no control characters, capped. Names are shown in a panel."""
    chars = []  # type: List[str]
    for ch in str(value or ""):
        if ch in "\r\n\t":
            chars.append(" ")
        elif ch < " " or ch == "\x7f":
            continue
        else:
            chars.append(ch)
    return " ".join("".join(chars).split())[:limit]


def _empty() -> Dict[str, list]:
    return {"pending": [], "granted": [], "denied": []}


def _sublime_notify(message: str) -> None:
    """Status bar only. Importing sublime at module load would break the tests."""
    try:
        import sublime
    except Exception:
        return
    try:
        sublime.status_message(message)
    except Exception:
        return


class AccessStore(object):
    """The pending / granted / denied lists and the in-memory token handoff."""

    def __init__(self, path: str, clock: Optional[Clock] = None,
                 notify: Optional[Notify] = None) -> None:
        self.path = path
        self._clock = clock or time.time
        self._notify = _sublime_notify if notify is None else notify
        self._lock = threading.Lock()
        # request id -> (raw token, expiry epoch). Never written to disk.
        self._handoff = {}  # type: Dict[str, Any]

    def request(self, name: str, ip: str, user_agent: str) -> dict:
        cleaned = _clean(name, NAME_LIMIT)
        if not cleaned:
            raise WebAccessError("bad_request", "device name is required")
        peer = _clean(ip, IP_LIMIT)
        agent = _clean(user_agent, UA_LIMIT)
        message = None  # type: Optional[str]
        with self._lock:
            data = self._load()
            for row in data["pending"]:
                if row.get("ip") == peer and row.get("name") == cleaned:
                    return {"id": row.get("id"), "status": "pending",
                            "name": cleaned}
            if sum(1 for row in data["pending"] if row.get("ip") == peer) >= MAX_PENDING_PER_IP:
                raise WebAccessError(
                    "rate_limited", "too many pending requests from this address")
            if len(data["pending"]) >= MAX_PENDING:
                raise WebAccessError(
                    "rate_limited", "too many pending web access requests")
            now = self._clock()
            row = {
                "id": "r" + secrets.token_hex(8),
                "name": cleaned,
                "ip": peer,
                "user_agent": agent,
                "created": now,
            }
            data["pending"].append(row)
            self._save(data)
            message = ("Web access: %s (%s) is asking — grant it with "
                       "Submarine: Web Access\u2026" % (cleaned, peer or "unknown address"))
            result = {"id": row["id"], "status": "pending", "name": cleaned}
        self._tell(message)
        return result

    def status(self, request_id: str) -> dict:
        ident = _clean(request_id, 64)
        with self._lock:
            data = self._load()
            for row in data["pending"]:
                if row.get("id") == ident:
                    return {"status": "pending", "id": ident}
            for row in data["denied"]:
                if row.get("id") == ident:
                    return {"status": "denied", "id": ident}
            for row in data["granted"]:
                if row.get("request_id") != ident and row.get("id") != ident:
                    continue
                out = {
                    "status": "granted",
                    "id": row.get("request_id") or ident,
                    "device_id": row.get("id"),
                }  # type: Dict[str, Any]
                handed = self._handoff.get(row.get("request_id") or "")
                if handed and handed[1] >= self._clock():
                    out["token"] = handed[0]
                return out
        return {"status": "unknown", "id": ident}

    def check(self, token: str) -> dict:
        raw = str(token or "")
        digest = _token_hash(raw) if raw else ""
        with self._lock:
            if not digest:
                return {"ok": False}
            data = self._load()
            found = None
            for row in data["granted"]:
                if _hash_eq(str(row.get("token_hash") or ""), digest):
                    found = row
                    break
            if found is None:
                return {"ok": False}
            now = self._clock()
            try:
                seen = float(found.get("last_seen") or 0)
            except (TypeError, ValueError):
                seen = 0.0
            if now - seen >= SEEN_INTERVAL:
                found["last_seen"] = now
                self._save(data)
            return {"ok": True, "id": found.get("id"), "name": found.get("name") or ""}

    def list_devices(self) -> dict:
        """What the quick panel shows. Hashes stay in the file."""
        with self._lock:
            data = self._load()
        pending = []
        for row in data["pending"]:
            pending.append({
                "id": row.get("id"),
                "name": row.get("name") or "",
                "ip": row.get("ip") or "",
                "created": row.get("created"),
            })
        granted = []
        for row in data["granted"]:
            granted.append({
                "id": row.get("id"),
                "name": row.get("name") or "",
                "ip": row.get("ip") or "",
                "created": row.get("created"),
                "last_seen": row.get("last_seen"),
            })
        return {"pending": pending, "granted": granted}

    def grant(self, request_id: str) -> dict:
        ident = _clean(request_id, 64)
        token = secrets.token_urlsafe(32)
        digest = _token_hash(token)
        with self._lock:
            data = self._load()
            row = None
            for item in data["pending"]:
                if item.get("id") == ident:
                    row = item
                    break
            if row is None:
                raise WebAccessError("not_found", "no pending request")
            now = self._clock()
            device = {
                "id": "d" + secrets.token_hex(8),
                "request_id": row.get("id"),
                "name": row.get("name") or "",
                "ip": row.get("ip") or "",
                "user_agent": row.get("user_agent") or "",
                "token_hash": digest,
                "created": now,
                "last_seen": now,
            }
            data["pending"] = [item for item in data["pending"] if item.get("id") != ident]
            data["granted"].append(device)
            self._save(data)
            # Only after the hash is on disk, so a crash cannot hand out a
            # token the file does not know.
            self._handoff[ident] = (token, now + HANDOFF_SECONDS)
            device_id = device["id"]
            name = device["name"]
        return {"id": device_id, "request_id": ident, "name": name,
                "status": "granted"}

    def deny(self, request_id: str) -> dict:
        ident = _clean(request_id, 64)
        with self._lock:
            data = self._load()
            if not any(item.get("id") == ident for item in data["pending"]):
                raise WebAccessError("not_found", "no pending request")
            data["pending"] = [item for item in data["pending"] if item.get("id") != ident]
            data["denied"].append({"id": ident, "at": self._clock()})
            if len(data["denied"]) > DENIED_CAP:
                data["denied"] = data["denied"][-DENIED_CAP:]
            self._save(data)
        return {"id": ident, "status": "denied"}

    def revoke(self, device_id: str) -> dict:
        ident = _clean(device_id, 64)
        with self._lock:
            data = self._load()
            removed = None
            kept = []
            for row in data["granted"]:
                if removed is None and row.get("id") == ident:
                    removed = row
                else:
                    kept.append(row)
            if removed is None:
                raise WebAccessError("not_found", "no granted device")
            data["granted"] = kept
            self._handoff.pop(removed.get("request_id") or "", None)
            self._save(data)
        return {"id": ident, "status": "revoked"}

    def _tell(self, message: Optional[str]) -> None:
        if not message or self._notify is None:
            return
        try:
            self._notify(message)
        except Exception:
            return

    def _load(self) -> Dict[str, list]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return _empty()
        except (OSError, ValueError) as e:
            raise WebAccessError("internal", "web access store is unreadable: %s" % e)
        if not isinstance(data, dict):
            raise WebAccessError("internal", "web access store is unreadable")
        out = _empty()
        for key in out:
            rows = data.get(key)
            if isinstance(rows, list):
                out[key] = [row for row in rows if isinstance(row, dict)]
        return out

    def _save(self, data: Dict[str, list]) -> None:
        """Atomic replace so a crash cannot leave a half-written grant list."""
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".web-access.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


_default = None  # type: Optional[AccessStore]
_default_guard = threading.Lock()


def default_store() -> AccessStore:
    """The process-wide store. The handoff tokens live on this instance."""
    global _default
    path = store_path()
    with _default_guard:
        if _default is None or _default.path != path:
            _default = AccessStore(path)
        return _default


def handle(action: str, params: dict, store: Optional[AccessStore] = None) -> dict:
    """One socket action. Raises WebAccessError instead of returning ok:false."""
    store = store or default_store()
    params = params or {}
    if action == "web_access_request":
        return store.request(params.get("name"), params.get("ip"),
                             params.get("user_agent"))
    if action == "web_access_status":
        return store.status(params.get("id"))
    if action == "web_access_check":
        return store.check(params.get("token"))
    if action == "web_access_list":
        return store.list_devices()
    if action == "web_access_grant":
        return store.grant(params.get("id"))
    if action == "web_access_deny":
        return store.deny(params.get("id"))
    if action == "web_access_revoke":
        return store.revoke(params.get("id"))
    raise WebAccessError("unknown_action", "unknown web access action")
