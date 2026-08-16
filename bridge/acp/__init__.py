"""ACP client package: plugin JSON-RPC ↔ agent ACP NDJSON.

Re-exports AcpBridge and run_bridge so `from acp import AcpBridge` matches
the historical `from acp_base import AcpBridge`. Implementation is split
across mixins; see bridge.py for composition.
"""
from __future__ import annotations

from acp.bridge import AcpBridge, run_bridge

__all__ = ["AcpBridge", "run_bridge"]
