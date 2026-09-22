"""The Claude bridge's stream reader attributes every SDK message to the
host's query or to a turn the CLI started by itself (`_route`).

Driven with fake SDK messages; the wire out (send_result / send_notification)
is captured. Needs the Claude Agent SDK importable (the bridge imports it).
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

def _import_bridge():
    """Import the SDK and the bridge the way the bridge process sees them:
    the repo's own `mcp/` package shadows the pip `mcp` the SDK needs, so
    the repo root leaves sys.path (and the module cache) for the import."""
    import importlib
    saved_path = list(sys.path)
    saved_mods = {k: v for k, v in sys.modules.items()
                  if k == "mcp" or k.startswith("mcp.")}
    for k in saved_mods:
        del sys.modules[k]
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _ROOT]
    try:
        sdk = importlib.import_module("claude_agent_sdk")
        bridge = importlib.import_module("claude_main")
    finally:
        sys.path[:] = saved_path
        # The repo's `mcp` package is what everything else in the suite
        # expects to find under that name.
        for k in [k for k in sys.modules if k == "mcp" or k.startswith("mcp.")]:
            del sys.modules[k]
        sys.modules.update(saved_mods)
    return sdk, bridge


try:
    _SDK, _BRIDGE_MOD = _import_bridge()
    _HAVE_SDK = True
except Exception:
    _SDK = _BRIDGE_MOD = None
    _HAVE_SDK = False


def _msgs():
    AssistantMessage = _SDK.AssistantMessage
    ResultMessage = _SDK.ResultMessage
    SystemMessage = _SDK.SystemMessage
    TextBlock = _SDK.TextBlock
    UserMessage = _SDK.UserMessage

    def user(text, kind="human"):
        return UserMessage(content=text, origin={"kind": kind} if kind else None)

    def assistant(text):
        return AssistantMessage(content=[TextBlock(text=text)], model="m")

    def result(kind=None):
        return ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", stop_reason="end_turn",
            origin={"kind": kind} if kind else None)

    def system(subtype, **data):
        return SystemMessage(subtype=subtype, data=dict(data))

    return user, assistant, result, system


@unittest.skipUnless(_HAVE_SDK, "claude_agent_sdk not installed")
class RouteTest(unittest.TestCase):
    def setUp(self):
        claude_main = _BRIDGE_MOD
        self.mod = claude_main
        self.out = []
        self._orig = (claude_main.send_result, claude_main.send_notification)
        claude_main.send_result = lambda rid, res: self.out.append(("result", rid, res))
        claude_main.send_notification = lambda m, p: self.out.append(("notify", m, p))
        self.b = claude_main.Bridge()
        self.b.client = types.SimpleNamespace()
        self.user, self.assistant, self.result, self.system = _msgs()

    def tearDown(self):
        self.mod.send_result, self.mod.send_notification = self._orig

    def _route(self, *messages):
        async def go():
            for m in messages:
                await self.b._route(m)
        asyncio.run(go())

    def _open_query(self, rid=7):
        fut = asyncio.new_event_loop().create_future()
        self.b._host_query = {"id": rid, "done": fut, "owned": False, "absorbed": False}
        return fut

    def _events(self):
        return [(k, m if k == "notify" else r.get("status"),
                 (p.get("type"), p.get("origin")) if k == "notify" else None)
                for k, m, p in [(e[0], e[1], e[2]) for e in self.out]
                for r in [p if k == "result" else {}]]

    def test_our_turn_closes_the_query_untagged(self):
        fut = self._open_query()
        self._route(self.user("hi"), self.assistant("yo"), self.result("human"))
        self.assertTrue(fut.done())
        results = [e for e in self.out if e[0] == "result"]
        self.assertEqual(results, [("result", 7, {"status": "complete"})])
        closer = [p for k, m, p in self.out if k == "notify" and p.get("type") == "result"]
        self.assertEqual(len(closer), 1)
        self.assertNotIn("origin", closer[0])
        self.assertIsNone(self.b._host_query)

    def test_a_cli_turn_while_idle_is_announced_and_tagged(self):
        self._route(self.system("task_notification", summary="sleep done"),
                    self.assistant("Sleep finished."), self.result("task-notification"))
        methods = [(m, p) for k, m, p in self.out if k == "notify" and m != "message"]
        self.assertEqual(methods[0][0], "injected_turn")
        self.assertEqual(methods[0][1]["summaries"], ["sleep done"])
        closer = [p for k, m, p in self.out if k == "notify" and p.get("type") == "result"]
        self.assertEqual(closer[0]["origin"], "task-notification")
        self.assertFalse(self.b._injected)
        self.assertEqual([e for e in self.out if e[0] == "result"], [], "no query to close")

    def test_a_query_absorbed_into_the_cli_turn_is_closed_by_that_turn(self):
        """The CLI attaches a prompt sent mid-turn to the running turn: no
        result of its own ever comes. Left open, the host stayed 'working'
        until Esc and then re-sent the message — that was the double."""
        self._route(self.assistant("working on the bg result…"))
        self.assertTrue(self.b._injected)
        fut = self._open_query(9)
        self._route(self.user("also do this"), self.assistant("ok, both"),
                    self.result("task-notification"))
        self.assertTrue(fut.done())
        self.assertEqual([e for e in self.out if e[0] == "result"],
                         [("result", 9, {"status": "complete"})])
        closer = [p for k, m, p in self.out if k == "notify" and p.get("type") == "result"]
        self.assertEqual(len(closer), 1)
        self.assertNotIn("origin", closer[0], "closed as the host's own turn")
        self.assertFalse(self.b._injected)

    def test_a_cli_turn_that_ran_ahead_of_our_query_does_not_close_it(self):
        fut = self._open_query(3)
        self.b._echo_seen = True                       # replay works on this CLI
        self._route(self.assistant("bg follow-up"), self.result("task-notification"))
        self.assertFalse(fut.done(), "our query is still queued behind it")
        closer = [p for k, m, p in self.out if k == "notify" and p.get("type") == "result"]
        self.assertEqual(closer[0]["origin"], "task-notification")
        self._route(self.user("mine"), self.assistant("yours"), self.result("human"))
        self.assertTrue(fut.done())

    def test_inject_goes_into_a_running_cli_turn_and_idle_says_so(self):
        sent = []

        async def q(text):
            sent.append(text)
        self.b.client.query = q

        async def go():
            await self.b.inject_message(1, {"message": "while idle"})
            self.b._injected = True
            await self.b.inject_message(2, {"message": "into the cli turn"})
        asyncio.run(go())
        self.assertEqual(sent, ["into the cli turn"])
        self.assertEqual([e for e in self.out if e[0] == "result"],
                         [("result", 1, {"status": "idle"}), ("result", 2, {"status": "ok"})])


if __name__ == "__main__":
    unittest.main()
