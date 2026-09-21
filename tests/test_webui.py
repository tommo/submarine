"""The web UI's HTTP layer, over a fake session client.

Nothing here needs Sublime or the plugin socket: the server takes its client as
an argument, so these tests assert the contract the browser depends on — routes,
status mapping, request coercion and the token gate.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from features.webui.server import build_server, lan_addresses


class FakeClient(object):
    """Records the calls the HTTP layer makes; replies with one envelope."""

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply or {"ok": True, "data": {"sessions": [], "count": 0},
                               "error": None, "instance": {"pid": 4242}}

    def _record(self, action, **fields):
        self.calls.append((action, fields))
        return self.reply

    def list(self, **fields):
        return self._record("list", **fields)

    def view(self, ref, **fields):
        return self._record("view", ref=ref, **fields)

    def chat(self, ref, prompt, **fields):
        return self._record("chat", ref=ref, prompt=prompt, **fields)

    def interrupt(self, ref):
        return self._record("interrupt", ref=ref)

    def pending(self, ref):
        return self._record("pending", ref=ref)

    def answer(self, ref, **fields):
        return self._record("answer", ref=ref, **fields)

    def backends(self):
        return self._record("backends")

    def create(self, **fields):
        return self._record("create", **fields)

    def rename(self, ref, name):
        return self._record("rename", ref=ref, name=name)

    def close(self, ref, remove=False):
        return self._record("close", ref=ref, remove=remove)

    def open(self, ref, file_path, line=None):
        return self._record("open", ref=ref, file_path=file_path, line=line)

    def read(self, ref, file_path, max_bytes=None):
        return self._record("read", ref=ref, file_path=file_path, max_bytes=max_bytes)

    def health(self):
        self.calls.append(("health", {}))
        return {"ok": True, "socket": "/tmp/submarine_mcp.sock", "present": True}


class ServerCase(unittest.TestCase):
    token = ""

    def setUp(self):
        self.client = FakeClient()
        self.server = build_server("127.0.0.1", 0, token=self.token,
                                   client=self.client)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05})
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body) if isinstance(body, (dict, list)) else body
        conn.request(method, path, body=payload, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        parsed = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except ValueError:
                parsed = raw.decode("utf-8", "replace")
        return resp.status, parsed

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body=body, **kw)


class TestStatic(ServerCase):
    def test_index_is_served_at_root(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Submarine", body)
        self.assertIn("/static/app.js", body)

    def test_assets_are_served(self):
        for path, needle in (("/static/app.js", "submarine_web_token"),
                             ("/static/style.css", ".row"),
                             ("/static/highlight.js", "SubmarineHL"),
                             ("/static/editor.js", "createSheet")):
            status, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(needle, body)

    def test_index_wires_the_sheet_editor(self):
        """The import map pins one @codemirror/state for every CM package, and
        the page loads the tokenizer before the module that decorates with it."""
        import json
        import re
        _status, body = self.get("/")
        m = re.search(r'<script type="importmap">\s*(\{.*?\})\s*</script>', body, re.S)
        self.assertIsNotNone(m, "no import map")
        imports = json.loads(m.group(1))["imports"]
        for pkg in ("@codemirror/state", "@codemirror/view", "@codemirror/language",
                    "@codemirror/commands", "@codemirror/search", "@lezer/common",
                    "@lezer/highlight", "style-mod", "w3c-keyname", "crelt"):
            self.assertIn(pkg, imports)
            self.assertTrue(imports[pkg].startswith("https://esm.sh/*"), pkg)
        self.assertLess(body.index("/static/highlight.js"), body.index("/static/editor.js"))
        self.assertLess(body.index("/static/editor.js"), body.index("/static/app.js"))

    def test_unknown_path_is_404(self):
        status, body = self.get("/nope")
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])

    def test_traversal_is_refused(self):
        for path in ("/static/../server.py", "/static/..%2fserver.py",
                     "/static/../../../../etc/passwd"):
            status, _ = self.get(path)
            self.assertEqual(status, 404, path)


class TestApiReads(ServerCase):
    def test_health(self):
        status, body = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["socket"], "/tmp/submarine_mcp.sock")
        self.assertTrue(body["present"])

    def test_list_passes_scope(self):
        status, body = self.get("/api/list?scope=children&parent=agent-1")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.client.calls[-1],
                         ("list", {"scope": "children", "parent": "agent-1",
                                   "window": None}))

    def test_view_coerces_ints_and_names_the_ref(self):
        status, _ = self.get("/api/view?ref=agent-1&mode=tail&turns=5&max_chars=900")
        self.assertEqual(status, 200)
        action, fields = self.client.calls[-1]
        self.assertEqual(action, "view")
        self.assertEqual(fields["ref"], "agent-1")
        self.assertEqual(fields["turns"], 5)
        self.assertEqual(fields["max_chars"], 900)
        self.assertNotIn("offset", fields)           # not asked for -> server default

    def test_view_ignores_unparsable_numbers(self):
        self.get("/api/view?ref=x&turns=abc")
        self.assertNotIn("turns", self.client.calls[-1][1])

    def test_view_without_ref_is_400_and_calls_nothing(self):
        status, body = self.get("/api/view?mode=tail")
        self.assertEqual(status, 400)
        self.assertEqual(body["http"], 400)
        self.assertEqual(self.client.calls, [])

    def test_unknown_api_route_is_404(self):
        status, body = self.get("/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["http"], 404)

    def test_writes_reject_get(self):
        for path in ("/api/chat", "/api/interrupt"):
            status, body = self.get(path)
            self.assertEqual(status, 405, path)
            self.assertEqual(body["http"], 405)


class TestApiWrites(ServerCase):
    def test_chat_sends_ref_prompt_and_policy(self):
        status, body = self.post("/api/chat", {"ref": "agent-1", "prompt": "hi",
                                               "queue": "interrupt", "idem": "k1"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        action, fields = self.client.calls[-1]
        self.assertEqual(action, "chat")
        self.assertEqual(fields["ref"], "agent-1")
        self.assertEqual(fields["prompt"], "hi")
        self.assertEqual(fields["queue"], "interrupt")
        self.assertEqual(fields["idem"], "k1")
        self.assertIsNone(fields["display"])

    def test_chat_needs_a_prompt(self):
        status, body = self.post("/api/chat", {"ref": "agent-1", "prompt": "   "})
        self.assertEqual(status, 400)
        self.assertIn("prompt", body["error"])
        self.assertEqual(self.client.calls, [])

    def test_chat_needs_a_ref(self):
        status, body = self.post("/api/chat", {"prompt": "hi"})
        self.assertEqual(status, 400)
        self.assertIn("ref", body["error"])

    def test_broken_json_is_400(self):
        status, body = self.post("/api/chat", b"{not json",
                                  headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertIn("JSON", body["error"])

    def test_non_object_json_is_400(self):
        status, body = self.post("/api/chat", b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertIn("object", body["error"])

    def test_interrupt(self):
        status, body = self.post("/api/interrupt", {"ref": "agent-9"})
        self.assertEqual(status, 200)
        self.assertEqual(self.client.calls[-1], ("interrupt", {"ref": "agent-9"}))

    def test_unknown_post_route_is_404(self):
        status, body = self.post("/api/nope", {"ref": "x"})
        self.assertEqual(status, 404)

    def test_create_rename_close_routes(self):
        status, _ = self.get("/api/backends")
        self.assertEqual(status, 200)
        self.assertEqual(self.client.calls[-1], ("backends", {}))
        status, _ = self.post("/api/create", {"backend": "grok", "project": "/p",
                                              "name": "n", "prompt": "hi", "ref": "ignored"})
        self.assertEqual(status, 200)
        self.assertEqual(self.client.calls[-1],
                         ("create", {"backend": "grok", "project": "/p", "name": "n", "prompt": "hi"}))
        status, _ = self.post("/api/rename", {"ref": "agent-1", "name": "renamed"})
        self.assertEqual(self.client.calls[-1], ("rename", {"ref": "agent-1", "name": "renamed"}))
        status, _ = self.post("/api/close", {"ref": "agent-1", "remove": True})
        self.assertEqual(self.client.calls[-1], ("close", {"ref": "agent-1", "remove": True}))
        status, _ = self.post("/api/rename", {"name": "x"})
        self.assertEqual(status, 400)
        status, _ = self.post("/api/open", {"ref": "agent-1", "file_path": "/p/a.nim", "line": 12})
        self.assertEqual(status, 200)
        self.assertEqual(self.client.calls[-1], ("open", {"ref": "agent-1", "file_path": "/p/a.nim", "line": 12}))
        status, _ = self.post("/api/open", {"line": 3})
        self.assertEqual(status, 400)
        status, _ = self.get("/api/file?path=%2Fp%2Fa.nim&ref=agent-1")
        self.assertEqual(status, 200)
        self.assertEqual(self.client.calls[-1], ("read", {"ref": "agent-1", "file_path": "/p/a.nim", "max_bytes": None}))
        status, _ = self.get("/api/file")
        self.assertEqual(status, 400)

    def test_pending_needs_a_ref(self):
        status, body = self.get("/api/pending")
        self.assertEqual(status, 400)
        status, body = self.get("/api/pending?ref=agent-3")
        self.assertEqual(status, 200)
        self.assertEqual(self.client.calls[-1], ("pending", {"ref": "agent-3"}))

    def test_answer_passes_only_the_answer_fields(self):
        status, body = self.post("/api/answer", {
            "ref": "agent-3", "kind": "question", "option": 2, "qid": 7,
            "prompt": "smuggled", "code": "raise SystemExit"})
        self.assertEqual(status, 200)
        action, fields = self.client.calls[-1]
        self.assertEqual(action, "answer")
        self.assertEqual(fields, {"ref": "agent-3", "kind": "question", "option": 2, "qid": 7})
        status, _ = self.post("/api/answer", {"ref": "agent-3", "kind": "permission",
                                              "response": "allow", "id": 5})
        self.assertEqual(self.client.calls[-1][1]["response"], "allow")
        status, _ = self.get("/api/answer?ref=x")
        self.assertEqual(status, 405)


class TestStatusMapping(ServerCase):
    def _failing(self, code):
        self.client.reply = {"ok": False, "error": "nope",
                             "data": {"code": code} if code else {}}
        return self.get("/api/list")

    def test_codes_map_to_status(self):
        self.assertEqual(self._failing("bad_request")[0], 400)
        self.assertEqual(self._failing("not_found")[0], 404)
        self.assertEqual(self._failing("ambiguous")[0], 409)
        self.assertEqual(self._failing("busy")[0], 409)
        self.assertEqual(self._failing("no_view")[0], 409)
        self.assertEqual(self._failing("no_session")[0], 409)
        self.assertEqual(self._failing("transcript")[0], 500)
        self.assertEqual(self._failing("internal")[0], 500)

    def test_transport_failure_is_503(self):
        status, body = self._failing(None)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "nope")
        self.assertEqual(body["http"], 503)

    def test_successes_are_200_without_an_http_key(self):
        status, body = self.get("/api/list")
        self.assertEqual(status, 200)
        self.assertNotIn("http", body)


class TestToken(ServerCase):
    token = "s3cret"

    def test_api_needs_the_token(self):
        status, body = self.get("/api/list")
        self.assertEqual(status, 401)
        self.assertIn("token", body["error"])

    def test_header_token_is_accepted(self):
        status, _ = self.get("/api/list", headers={"X-Submarine-Token": "s3cret"})
        self.assertEqual(status, 200)

    def test_query_token_is_accepted(self):
        status, _ = self.get("/api/list?token=s3cret")
        self.assertEqual(status, 200)

    def test_wrong_token_is_refused(self):
        status, _ = self.get("/api/list?token=nope")
        self.assertEqual(status, 401)

    def test_posts_are_gated_too(self):
        status, _ = self.post("/api/interrupt", {"ref": "a"})
        self.assertEqual(status, 401)
        self.assertEqual(self.client.calls, [])

    def test_the_console_itself_needs_no_token(self):
        self.assertEqual(self.get("/")[0], 200)
        self.assertEqual(self.get("/static/app.js")[0], 200)


class TestHelpers(unittest.TestCase):
    def test_lan_addresses_is_a_list(self):
        self.assertIsInstance(lan_addresses(), list)


if __name__ == "__main__":
    unittest.main()
