"""Browser UI for the session-control socket.

  python3 -m features.webui [--host 0.0.0.0] [--port 8787]

`client` is a thin wrapper over the same socket helpers `submarine_sessions`
uses, `server` is the HTTP layer over it, `cli` is the entry point. Nothing here
imports Sublime: the UI is a process outside the editor, like the CLI.
"""
from __future__ import annotations

from features.webui.client import DEFAULT_HOST, DEFAULT_PORT, SessionClient
from features.webui.server import build_server, run

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "SessionClient", "build_server", "run"]
