"""Device grants for the web UI: the on-disk store and the HTTP gate.

The store is the plugin side (hashes only, pending cap). The HTTP cases bind
127.0.0.1 on a free port with a client that talks to that store directly, so
nothing here needs Sublime or the real socket.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import features.session_control as sc
import features.web_access as wa
from features.webui.server import (
    CHECK_TTL,
    access_allowed,
    build_server,
    device_cookie,
    is_loopback,
)


def _keys(obj, found):
    if isinstance(obj, dict):
        for key, value in obj.items():
            found.add(key)
            _keys(value, found)
    elif isinstance(obj, list):
        for item in obj:
            _keys(item, found)


class Clock(object):
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "web_access.json")
        self.notes = []
        self.clock = Clock()
        self.store = wa.AccessStore(self.path, clock=self.clock,
                                    notify=self.notes.append)

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def test_grant_check_revoke_and_hashes_only(self):
        pending = self.store.request("phone", "10.4.4.4", "Agent/1")
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(len(self.notes), 1)
        self.assertIn("phone", self.notes[0])
        self.assertIn("10.4.4.4", self.notes[0])

        again = self.store.request("phone", "10.4.4.4", "Agent/1")
        self.assertEqual(again["id"], pending["id"])
        self.assertEqual(len(self.notes), 1, "a repeat request must not re-notify")

        granted = self.store.grant(pending["id"])
        self.assertEqual(granted["status"], "granted")
        self.assertNotIn("token", granted)
        handed = self.store.status(pending["id"])
        self.assertEqual(handed["status"], "granted")
        token = handed["token"]
        self.assertTrue(token)

        raw = self._file()
        parsed = json.loads(raw)
        keys = set()
        _keys(parsed, keys)
        self.assertNotIn("token", keys)
        self.assertNotIn(token, raw)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self.assertEqual(parsed["granted"][0]["token_hash"], digest)
        self.assertNotIn("token_hash", json.dumps(self.store.list_devices()))

        checked = self.store.check(token)
        self.assertTrue(checked["ok"])
        self.assertEqual(checked["name"], "phone")
        self.assertFalse(self.store.check(token + "x")["ok"])
        self.assertFalse(self.store.check("")["ok"])

        self.clock.t += wa.SEEN_INTERVAL + 1
        self.store.check(token)
        seen = self.store.list_devices()["granted"][0]["last_seen"]
        self.assertEqual(seen, self.clock.t)

        self.store.revoke(checked["id"])
        self.assertFalse(self.store.check(token)["ok"])
        self.assertEqual(self.store.list_devices()["granted"], [])
        self.clock.t += 1
        self.assertNotIn("token", self.store.status(pending["id"]))

    def test_deny_and_handoff_expiry(self):
        pending = self.store.request("tablet", "10.5.0.2", "Agent/2")
        self.store.deny(pending["id"])
        self.assertEqual(self.store.status(pending["id"])["status"], "denied")
        self.assertEqual(self.store.list_devices()["pending"], [])
        with self.assertRaises(wa.WebAccessError) as raised:
            self.store.grant(pending["id"])
        self.assertEqual(raised.exception.code, "not_found")

        other = self.store.request("laptop", "10.5.0.3", "Agent/3")
        self.store.grant(other["id"])
        self.clock.t += wa.HANDOFF_SECONDS + 1
        stale = self.store.status(other["id"])
        self.assertEqual(stale["status"], "granted")
        self.assertNotIn("token", stale)

    def test_pending_is_capped_per_address_and_overall(self):
        ip = "10.8.0.1"
        for name in ("a", "b", "c"):
            self.store.request(name, ip, "Agent")
        with self.assertRaises(wa.WebAccessError) as raised:
            self.store.request("d", ip, "Agent")
        self.assertEqual(raised.exception.code, "rate_limited")
        self.assertEqual(len(self.store.list_devices()["pending"]), wa.MAX_PENDING_PER_IP)

        for i in range(wa.MAX_PENDING - wa.MAX_PENDING_PER_IP):
            self.store.request("n%d" % i, "10.9.0.%d" % i, "Agent")
        self.assertEqual(len(self.store.list_devices()["pending"]), wa.MAX_PENDING)
        with self.assertRaises(wa.WebAccessError) as raised:
            self.store.request("overflow", "10.9.1.9", "Agent")
        self.assertEqual(raised.exception.code, "rate_limited")
        self.assertEqual(len(self.store.list_devices()["pending"]), wa.MAX_PENDING)

    def test_empty_name_is_refused_and_dispatch_round_trips(self):
        with self.assertRaises(wa.WebAccessError) as raised:
            self.store.request("   ", "10.0.0.1", "Agent")
        self.assertEqual(raised.exception.code, "bad_request")

        orig = wa.default_store
        wa.default_store = lambda: self.store
        try:
            missing = sc.dispatch({"action": "nope"})
            self.assertIn("web_access_request", missing["data"]["actions"])
            env = sc.dispatch({
                "action": "web_access_request",
                "name": "tablet",
                "ip": "10.2.0.5",
                "user_agent": "Agent/9",
            })
            self.assertTrue(env["ok"])
            rid = env["data"]["id"]
            granted = sc.dispatch({"action": "web_access_grant", "id": rid})
            self.assertTrue(granted["ok"])
            self.assertNotIn("token", granted["data"])
            status = sc.dispatch({"action": "web_access_status", "id": rid})
            self.assertTrue(status["data"].get("token"))
            checked = sc.dispatch({
                "action": "web_access_check",
                "token": status["data"]["token"],
            })
            self.assertTrue(checked["data"]["ok"])
        finally:
            wa.default_store = orig


class PolicyTest(unittest.TestCase):
    def test_loopback_cookie_and_legacy_token(self):
        self.assertTrue(is_loopback("127.0.0.1"))
        self.assertTrue(is_loopback("::1"))
        self.assertTrue(is_loopback("::ffff:127.0.0.1"))
        self.assertFalse(is_loopback("10.0.0.4"))
        peer = "10.0.0.4"
        self.assertFalse(access_allowed(peer, False, False, False))
        self.assertTrue(access_allowed(peer, True, False, False))
        self.assertTrue(access_allowed(peer, False, True, False))
        self.assertTrue(access_allowed("127.0.0.1", False, False, False))
        self.assertFalse(access_allowed("127.0.0.1", False, False, True))
        self.assertLessEqual(CHECK_TTL, 5)
        self.assertGreater(CHECK_TTL, 0)
        plain = device_cookie("abcDEF123_-xyz", False)
        self.assertIn("HttpOnly", plain)
        self.assertIn("SameSite=Strict", plain)
        self.assertIn("Max-Age=", plain)
        self.assertNotIn("Secure", plain)
        self.assertIn("Secure", device_cookie("abcDEF123_-xyz", True))


class _HTTP(unittest.TestCase):
    auth_loopback = False
    check_ttl = 0.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "web_access.json")
        self.notes = []
        self.store = wa.AccessStore(self.path, notify=self.notes.append)
        self.checks = []
        self.client = _StoreClient(self.store, self.checks)
        self.server = build_server(
            "127.0.0.1", 0, token=getattr(self, "token", ""),
            client=self.client, auth_loopback=self.auth_loopback,
            check_ttl=self.check_ttl)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05})
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body) if isinstance(body, (dict, list)) else body
        conn.request(method, path, body=payload, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        header = resp.getheader("Set-Cookie")
        conn.close()
        parsed = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except ValueError:
                parsed = raw.decode("utf-8", "replace")
        return resp.status, parsed, header

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body=body, **kw)


class _StoreClient(object):
    """The HTTP layer's client, backed by the real store (no socket)."""

    def __init__(self, store, checks):
        self.store = store
        self.checks = checks

    def _ok(self, data):
        return {"ok": True, "data": data, "error": None}

    def _err(self, exc):
        return {"ok": False, "error": exc.message, "data": {"code": exc.code}}

    def web_access_request(self, name, ip, user_agent):
        try:
            return self._ok(self.store.request(name, ip, user_agent))
        except wa.WebAccessError as e:
            return self._err(e)

    def web_access_status(self, request_id):
        return self._ok(self.store.status(request_id))

    def web_access_check(self, token):
        self.checks.append(token)
        return self._ok(self.store.check(token))

    def list(self, **fields):
        return self._ok({"sessions": [], "count": 0})

    def health(self):
        return {"ok": True, "socket": "test", "present": True}


class GateTest(_HTTP):
    auth_loopback = True

    def test_open_routes_cookie_and_revoke(self):
        status, page, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Request access", page)
        self.assertEqual(self.get("/static/app.js")[0], 200)
        status, me, _ = self.get("/api/access/me")
        self.assertEqual(status, 200)
        self.assertFalse(me["authenticated"])

        status, body, _ = self.post(
            "/api/access/request", {"name": "tablet"},
            headers={"User-Agent": "TestBrowser/1",
                     "Content-Type": "application/json"})
        self.assertEqual(status, 200, body)
        rid = body["data"]["id"]
        self.assertEqual(self.notes and self.notes[0].count("tablet"), 1)
        stored = json.loads(open(self.path, encoding="utf-8").read())
        self.assertEqual(stored["pending"][0]["ip"], "127.0.0.1")
        self.assertEqual(stored["pending"][0]["user_agent"], "TestBrowser/1")
        self.assertEqual(self.get("/api/list")[0], 401)
        self.assertEqual(self.checks, [])

        self.store.grant(rid)
        status, body, header = self.get("/api/access/status?id=%s" % rid)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["status"], "granted")
        self.assertTrue(body["data"]["cookie"])
        token = self.store.status(rid)["token"]
        self.assertNotIn(token, json.dumps(body))
        self.assertIn("HttpOnly", header)
        self.assertIn("SameSite=Strict", header)
        self.assertNotIn("Secure", header)
        max_age = int(header.split("Max-Age=")[1].split(";")[0])
        self.assertGreaterEqual(max_age, 30 * 24 * 3600)
        pair = header.split(";", 1)[0]
        self.assertEqual(pair, "submarine_device=%s" % token)

        secure_status, secure_body, secure_header = self.get(
            "/api/access/status?id=%s" % rid,
            headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(secure_status, 200)
        self.assertTrue(secure_body["data"]["cookie"])
        self.assertIn("Secure", secure_header)

        listed, _, _ = self.get("/api/list", headers={"Cookie": pair})
        self.assertEqual(listed, 200)
        status, who, _ = self.get("/api/access/me", headers={"Cookie": pair})
        self.assertEqual(status, 200)
        self.assertTrue(who["authenticated"])
        self.assertEqual(who["via"], "cookie")

        device_id = self.store.list_devices()["granted"][0]["id"]
        self.store.revoke(device_id)
        denied, _, _ = self.get("/api/list", headers={"Cookie": pair})
        self.assertEqual(denied, 401)

    def test_missing_cookie_is_401_and_legacy_token_still_works(self):
        self.server.token = "s3cret"
        status, body, _ = self.get("/api/health")
        self.assertEqual(status, 401)
        self.assertIn("token", body["error"])
        status, _, _ = self.get(
            "/api/health", headers={"X-Submarine-Token": "s3cret"})
        self.assertEqual(status, 200)
        status, _, _ = self.get("/api/health?token=s3cret")
        self.assertEqual(status, 200)


class LoopbackTest(_HTTP):
    auth_loopback = False

    def test_loopback_is_open_until_the_flag(self):
        status, me, _ = self.get("/api/access/me")
        self.assertEqual(status, 200)
        self.assertTrue(me["authenticated"])
        self.assertEqual(me["via"], "loopback")
        self.assertEqual(self.get("/api/list")[0], 200)
        self.server.auth_loopback = True
        status, me, _ = self.get("/api/access/me")
        self.assertFalse(me["authenticated"])
        self.assertEqual(self.get("/api/list")[0], 401)


class CacheTest(_HTTP):
    auth_loopback = True
    check_ttl = 30.0

    def test_check_is_cached_for_a_few_seconds(self):
        pending = self.store.request("phone", "10.1.1.1", "Agent")
        self.store.grant(pending["id"])
        token = self.store.status(pending["id"])["token"]
        pair = "submarine_device=%s" % token
        self.assertEqual(self.get("/api/list", headers={"Cookie": pair})[0], 200)
        self.assertEqual(self.get("/api/list", headers={"Cookie": pair})[0], 200)
        self.assertEqual(self.checks, [token])
        self.store.revoke(self.store.list_devices()["granted"][0]["id"])
        # The grant is gone, but the cached check has not expired yet.
        self.assertEqual(self.get("/api/list", headers={"Cookie": pair})[0], 200)
        self.assertEqual(self.checks, [token])


class PaletteTest(unittest.TestCase):
    def test_web_access_command_is_listed(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "Default.sublime-commands"),
                  encoding="utf-8") as fh:
            entries = json.load(fh)
        hit = [e for e in entries if e.get("command") == "submarine_web_access"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["caption"], "Submarine: Web Access\u2026")


if __name__ == "__main__":
    unittest.main()
