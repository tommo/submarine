"""Put the Submarine package root on sys.path so `plat` / `backend` import.

Also arms a signal safety net for the whole suite (see _guard_signals).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _guard_signals():
    """Fail any test that would signal a process group or every process.

    Tests run on a live desktop. kill(-1) / killpg(1) signal every process
    the user owns (logs out the GNOME session); kill(0) / killpg(0) hit our
    own process group, which can include the editor that launched us. A fake
    proc with pid=1 in a kill path did exactly that — so refuse loudly.
    """
    real_kill, real_killpg = os.kill, os.killpg

    def kill(pid, sig):
        if pid <= 1:
            raise AssertionError(
                "test tried os.kill(%r, %r) — refused (group/all-process "
                "signal); mock os.kill in this test" % (pid, sig))
        return real_kill(pid, sig)

    def killpg(pgid, sig):
        if pgid <= 1:
            raise AssertionError(
                "test tried os.killpg(%r, %r) — refused (pgid <= 1 signals "
                "our own group or every process); mock os.killpg in this "
                "test" % (pgid, sig))
        return real_killpg(pgid, sig)

    os.kill, os.killpg = kill, killpg


_guard_signals()
