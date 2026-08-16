"""Agent ↔ Sublime MCP integration.

Two processes, one contract:

- In-plugin: ``from mcp.socket_server import start, stop``
- Stdio process (spawned per session by bridges): ``python3 mcp/server.py``
- Shared catalog: ``from mcp.tools import TOOL_TABLE``

Do not import ``socket_server`` from this package init — it requires sublime.
"""
from __future__ import annotations
