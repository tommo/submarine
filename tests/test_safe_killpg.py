"""_safe_killpg never signals our own group or every process.

glibc killpg(g) is kill(-g): pgid 1 reaches every process the user owns
(a fake pid=1 in a test logged out the desktop session), pgid 0 reaches our
own process group. Real terminal children are session leaders (pgid > 1).
"""
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from acp.terminal import TerminalMixin, _safe_killpg  # noqa: E402


class TestSafeKillpg(unittest.TestCase):
    def test_refuses_dangerous_or_bogus_pgids(self):
        for bad in (1, 0, -1, -42, None, True, "123", 3.0):
            with mock.patch("acp.terminal.os.killpg") as killpg:
                with self.assertRaises(ProcessLookupError, msg=repr(bad)):
                    _safe_killpg(bad, 15)
                killpg.assert_not_called()

    def test_real_pgid_is_signalled(self):
        with mock.patch("acp.terminal.os.killpg") as killpg:
            _safe_killpg(4242, 15)
        killpg.assert_called_once_with(4242, 15)

    def test_kill_terminal_proc_falls_back_to_terminate(self):
        class _P:
            returncode = None
            pid = 1

            def terminate(self):
                self.returncode = -15

        proc = _P()
        with mock.patch("acp.terminal.os.killpg") as killpg:
            TerminalMixin._kill_terminal_proc(object(), proc)
        killpg.assert_not_called()
        self.assertEqual(proc.returncode, -15)


if __name__ == "__main__":
    unittest.main()
