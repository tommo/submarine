#!/usr/bin/env python3
"""JsonRpcClient works under plain python3 (no sublime)."""
from __future__ import annotations

import sys
import textwrap
import unittest

from backend.rpc import JsonRpcClient


_ECHO = textwrap.dedent(
    r"""
    import json, sys
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if "id" in msg:
            out = {"jsonrpc": "2.0", "id": msg["id"], "result": {"method": msg["method"], "params": msg.get("params")}}
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
        if msg.get("method") == "shutdown":
            break
    """
).strip()


class TestJsonRpcClient(unittest.TestCase):
    def test_send_wait_without_sublime(self):
        client = JsonRpcClient(lambda method, params: None)
        client.start([sys.executable, "-c", _ECHO])
        try:
            self.assertTrue(client.is_alive())
            reply = client.send_wait("initialize", {"cwd": "/tmp"}, timeout=5.0)
            self.assertIn("result", reply)
            self.assertEqual(reply["result"]["method"], "initialize")
            self.assertEqual(reply["result"]["params"]["cwd"], "/tmp")
        finally:
            client.send_wait("shutdown", {}, timeout=2.0)
            client.stop()
        self.assertFalse(client.is_alive())


if __name__ == "__main__":
    unittest.main()
