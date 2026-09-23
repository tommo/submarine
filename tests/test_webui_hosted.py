"""The web UI served inside the plugin process, on a background thread."""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.client import HTTPConnection

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from features.webui import hosted


class HostedServerTest(unittest.TestCase):
    def test_start_serves_access_and_stop_releases_the_port(self):
        port = hosted.start("127.0.0.1", 0, "", False)
        self.addCleanup(hosted.stop)
        self.assertGreater(port, 0)
        deadline = time_plus(2.0)
        status = None
        body = None
        while time_left(deadline):
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=1)
                conn.request("GET", "/api/access/me")
                resp = conn.getresponse()
                status = resp.status
                body = json.loads(resp.read().decode("utf-8"))
                conn.close()
                break
            except OSError:
                threading.Event().wait(0.05)
        self.assertEqual(status, 200)
        self.assertTrue(body["authenticated"])
        self.assertEqual(body["via"], "loopback")
        hosted.stop()
        # The accept loop drops the socket on its own thread.
        deadline = time_plus(2.0)
        while time_left(deadline):
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=0.2)
                conn.request("GET", "/api/access/me")
                conn.getresponse()
                conn.close()
            except OSError:
                return
            threading.Event().wait(0.05)
        self.fail("port %s still accepted after stop" % port)


def time_plus(seconds):
    import time
    return time.time() + seconds


def time_left(deadline):
    import time
    return time.time() < deadline


if __name__ == "__main__":
    unittest.main()
