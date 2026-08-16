"""Compatibility shim — implementation lives in the ``acp`` package.

Historical importers (kimi_main, grok_main, tests) keep
``from acp_base import AcpBridge``. See ``bridge/acp/``.
"""
from acp import AcpBridge, run_bridge
from acp.util import apply_plain_terminal_env, strip_ansi

__all__ = ["AcpBridge", "run_bridge", "apply_plain_terminal_env", "strip_ansi"]
