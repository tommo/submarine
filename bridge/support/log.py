"""File logger for bridge subprocesses.

Shared Claude/Codex/Pi log: $TMPDIR/submarine_bridge.log
Per-pid ACP logs: $TMPDIR/submarine_acp_bridge.<pid>.log
"""
from __future__ import annotations

import os
from typing import Optional


def _tmpdir() -> str:
    return (
        os.environ.get("TMPDIR")
        or os.environ.get("TEMP")
        or os.environ.get("TMP")
        or "/tmp"
    )


BRIDGE_LOG_PATH = os.path.join(_tmpdir(), "submarine_bridge.log")
ACP_LOG_NAME = "submarine_acp_bridge.log"


def acp_log_path(pid: Optional[int] = None) -> str:
    """Per-process ACP log path (shared file is truncated on every spawn)."""
    root, ext = os.path.splitext(os.path.join(_tmpdir(), ACP_LOG_NAME))
    return f"{root}.{pid if pid is not None else os.getpid()}{ext or '.log'}"


class Logger:
    """Simple file-based logger with context support."""

    def __init__(self, log_path: str, prefix: str = ""):
        self.log_path = log_path
        self.prefix = prefix

    def log(self, message: str, prefix: Optional[str] = None) -> None:
        actual_prefix = prefix if prefix is not None else self.prefix
        try:
            with open(self.log_path, "a") as f:
                f.write(f"{actual_prefix}{message}\n")
        except Exception:
            pass

    def info(self, message: str) -> None:
        self.log(message, "  ")

    def error(self, message: str) -> None:
        self.log(message, "ERROR: ")

    def warning(self, message: str) -> None:
        self.log(message, "WARNING: ")

    def debug(self, message: str) -> None:
        self.log(message, "DEBUG: ")

    def separator(self, char: str = "=", length: int = 50) -> None:
        self.log(char * length, "")

    def clear(self) -> None:
        try:
            if os.path.exists(self.log_path):
                os.remove(self.log_path)
        except Exception:
            pass


class ContextLogger:
    """Logger with automatic context tracking."""

    def __init__(self, logger: Logger, context: str = ""):
        self.logger = logger
        self.context = context

    def log(self, message: str, prefix: Optional[str] = None) -> None:
        ctx_prefix = f"[{self.context}] " if self.context else ""
        self.logger.log(f"{ctx_prefix}{message}", prefix)

    def info(self, message: str) -> None:
        self.log(message, "  ")

    def error(self, message: str) -> None:
        self.log(message, "ERROR: ")

    def warning(self, message: str) -> None:
        self.log(message, "WARNING: ")

    def debug(self, message: str) -> None:
        self.log(message, "DEBUG: ")


_bridge_logger: Optional[Logger] = None


def get_bridge_logger(log_path: str = BRIDGE_LOG_PATH) -> Logger:
    """Get or create the bridge logger singleton."""
    global _bridge_logger
    if _bridge_logger is None:
        _bridge_logger = Logger(log_path, prefix="")
    return _bridge_logger
