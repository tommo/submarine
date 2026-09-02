"""Compatibility shim — implementation lives in the ``acp`` package.

Historical importers (kimi_main, grok_main, tests) keep
``from acp_base import AcpBridge``. See ``bridge/acp/``.
"""
from acp import AcpBridge, run_bridge
from acp.util import (
    AcpCall,
    _acp_is_compact_text,
    apply_plain_terminal_env,
    retain_terminal_tail,
    strip_ansi,
)

__all__ = [
    "AcpBridge", "run_bridge", "AcpCall",
    "_acp_is_compact_text", "apply_plain_terminal_env",
    "retain_terminal_tail", "strip_ansi",
]
