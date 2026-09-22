"""Changing a live session's effort.

Claude bridge, low–xhigh: live through the CLI's flag settings (bridge
`set_effort` → `apply_flag_settings`), no restart. `max` is a start flag
only, so it restarts the session with its history; so does Grok. The pick
belongs to the session: saved with it, and a wake starts with it.
"""
from __future__ import annotations

import unittest

from tests.fakes import FakeClient, make_session


def _sent(s, method):
    # The session drops its client while a restart sleeps; the fake factory
    # hands the same instance back on wake, so read the one it was built with.
    client = s.client or s._test_client
    return [(p, cb) for m, p, cb in client.sent if m == method]


def _make(**kw):
    client = FakeClient()
    s = make_session(client=client, **kw)
    s._test_client = client
    return s


class SetEffortTest(unittest.TestCase):
    def _session(self, backend="claude", **kw):
        s = _make(initialized=True, backend=backend,
                  settings={"effort": "high"}, **kw)
        s.start()
        s._on_init({"status": "initialized", "session_id": "sess-1"})
        return s

    def test_claude_goes_live_without_a_restart(self):
        s = self._session()
        reports = []
        ok, detail = s.set_effort("xhigh", on_done=lambda *a: reports.append(a))
        self.assertTrue(ok)
        (params, cb), = _sent(s, "set_effort")
        self.assertEqual(params, {"effort": "xhigh"})
        self.assertEqual(s.effort, "high", "nothing changes until the CLI confirms")
        cb({"result": {"ok": True, "live": True, "applied": "xhigh"}})
        self.assertEqual(s.effort, "xhigh")
        self.assertEqual(s.effort_override, "xhigh")
        self.assertEqual(reports, [(True, "effort xhigh — live")])
        self.assertTrue(s.initialized, "no restart")

    def test_max_restarts_and_the_restart_carries_it(self):
        s = self._session()
        ok, detail = s.set_effort("max")
        self.assertTrue(ok)
        self.assertIn("restarting", detail)
        self.assertEqual(_sent(s, "set_effort"), [])
        s.scheduler.fire_all()                     # sleep → wake
        params = [p for p, _cb in _sent(s, "initialize")][-1]
        self.assertEqual(params.get("effort"), "max")
        self.assertEqual(params.get("resume"), "sess-1")

    def test_a_refused_live_change_falls_back_to_a_restart(self):
        s = self._session()
        s.set_effort("low")
        (_params, cb), = _sent(s, "set_effort")
        cb({"result": {"ok": False, "live": False, "reason": "this SDK cannot change effort live"}})
        s.scheduler.fire_all()
        self.assertEqual([p for p, _cb in _sent(s, "initialize")][-1].get("effort"), "low")

    def test_asleep_it_is_stored_for_the_wake(self):
        s = _make(backend="claude", resume_id="old", settings={"effort": "high"})
        ok, detail = s.set_effort("medium")
        self.assertTrue(ok)
        self.assertIn("applies when the session starts", detail)
        s.start()
        self.assertEqual([p for p, _cb in _sent(s, "initialize")][-1].get("effort"), "medium")

    def test_the_pick_is_saved_and_restored(self):
        s = self._session()
        s.query_count = 1                          # an empty session is not saved
        s.set_effort("max")
        saved = s.store.find("sess-1")
        self.assertEqual(saved.get("effort"), "max")
        fresh = _make(backend="claude", resume_id="sess-1", settings={"effort": "high"})
        fresh.store = s.store
        fresh.start()
        self.assertEqual([p for p, _cb in _sent(fresh, "initialize")][-1].get("effort"), "max")

    def test_grok_restarts_and_has_no_max(self):
        s = self._session(backend="grok")
        self.assertNotIn("max", s.effort_levels())
        ok, why = s.set_effort("max")
        self.assertFalse(ok)
        ok, _detail = s.set_effort("xhigh")
        self.assertTrue(ok)
        self.assertEqual(_sent(s, "set_effort"), [], "grok has no live path")

    def test_mid_turn_restart_paths_refuse(self):
        s = self._session()
        s.query("busy")
        ok, why = s.set_effort("max")
        self.assertFalse(ok)
        self.assertIn("mid-turn", why)
        ok, _ = s.set_effort("low")                # live path is fine mid-turn
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()


class OtherBackendsTest(unittest.TestCase):
    """Every backend with an effort control changes it — live where the
    agent has a runtime knob, by restart where it only reads it at spawn."""

    def _session(self, backend, **kw):
        s = _make(initialized=True, backend=backend, settings={"effort": "high"}, **kw)
        s.start()
        s._on_init({"status": "initialized", "session_id": "sess-1"})
        return s

    def test_live_backends_ask_the_bridge(self):
        for backend, level in (("kimi", "max"), ("codex", "xhigh"), ("pi", "off")):
            s = self._session(backend)
            ok, _ = s.set_effort(level)
            self.assertTrue(ok, backend)
            (params, cb), = _sent(s, "set_effort")
            self.assertEqual(params, {"effort": level}, backend)
            cb({"result": {"ok": True, "live": True, "applied": level}})
            self.assertEqual(s.effort, level, backend)

    def test_kimi_reports_the_level_it_mapped_to(self):
        s = self._session("kimi")
        s._test_client = s.client
        reports = []
        s.set_effort("low", on_done=lambda *a: reports.append(a))
        (_p, cb), = _sent(s, "set_effort")
        cb({"result": {"ok": True, "live": True, "applied": "low"}})
        self.assertEqual(reports, [(True, "effort low — live")])

    def test_levels_follow_the_backend(self):
        self.assertEqual(self._session("kimi").effort_levels(), ["low", "high", "max"])
        self.assertNotIn("max", self._session("codex").effort_levels())
        self.assertIn("off", self._session("pi").effort_levels())
        ok, why = self._session("kimi").set_effort("medium")
        self.assertFalse(ok)
        self.assertIn("low / high / max", why)

    def test_grok_byok_deepseek_takes_none(self):
        s = self._session("grok")
        s.model = "deepseek-v4-pro"
        self.assertEqual(s.effort_levels(), [])
        ok, why = s.set_effort("high")
        self.assertFalse(ok)
        self.assertIn("takes no effort setting", why)

    def test_every_backend_sends_effort_on_a_resume(self):
        for backend in ("claude", "kimi", "codex", "pi", "grok"):
            s = _make(backend=backend, resume_id="old", settings={"effort": "medium"})
            s.start()
            params = [p for p, _cb in _sent(s, "initialize")][-1]
            self.assertEqual(params.get("effort"), "medium", backend)


class ProviderPinTest(unittest.TestCase):
    """CLAUDE_CODE_EFFORT_LEVEL in a custom provider's env beats --effort and
    live settings alike (probed on claude 2.1.280): effort cannot change, so
    say so instead of restarting into the same pinned value."""

    SETTINGS = {
        "effort": "high",
        "custom_providers": {"deepseek": {
            "abbrev": "DS", "auth_env_var": "DEEPSEEK_API_KEY",
            "base_url": "https://api.deepseek.com/anthropic", "label": "DeepSeek",
            "opus_model": "deepseek-v4-pro[1m]",
            "extra_env": {"CLAUDE_CODE_EFFORT_LEVEL": "max"},
        }},
    }

    def test_a_pinned_provider_refuses_with_the_reason(self):
        import os
        os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
        s = _make(initialized=True, backend="deepseek", settings=self.SETTINGS)
        s.start()
        self.assertEqual(s.effort_pin, "max")
        self.assertEqual(s.effort, "max", "shows what the CLI really uses")
        self.assertEqual(s.effort_levels(), [])
        ok, why = s.set_effort("low")
        self.assertFalse(ok)
        self.assertIn("pinned to max by the (CC) DeepSeek config", why)
        self.assertEqual(_sent(s, "set_effort"), [])
