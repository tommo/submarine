"""Devtools ring + CLI dispatch.

Do not import ``server`` at module load — it needs ``sublime``. The CLI
(``python3 submarine_devtools.py``) talks to the socket and must import
cleanly from a normal python3.
"""
from __future__ import annotations

__all__ = ["dispatch", "start", "stop", "log", "ping", "help_text", "goal_command"]


def __getattr__(name):
    if name in __all__:
        from . import server
        return getattr(server, name)
    raise AttributeError(name)
