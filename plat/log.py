"""Bridge file logger + tiny plugin console logger. Sublime-free.

Drops the unused ContextLogger machinery from the old plugin logger.
"""
from __future__ import annotations

from typing import Optional

from .constants import BRIDGE_LOG_PATH, PLUGIN_NAME


class FileLogger:
    """Append-only file logger. Failures are swallowed so logging cannot break the host."""

    def __init__(self, log_path: str, prefix: str = "") -> None:
        self.log_path = log_path
        self.prefix = prefix

    def log(self, message: str, prefix: Optional[str] = None) -> None:
        actual_prefix = self.prefix if prefix is None else prefix
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write("%s%s\n" % (actual_prefix, message))
        except Exception:
            pass

    def info(self, message: str) -> None:
        self.log(message, "  ")

    def error(self, message: str) -> None:
        self.log(message, "ERROR: ")

    def debug(self, message: str) -> None:
        self.log(message, "DEBUG: ")

    def separator(self, char: str = "=", length: int = 50) -> None:
        self.log(char * length, "")

    def clear(self) -> None:
        try:
            import os
            if os.path.exists(self.log_path):
                os.remove(self.log_path)
        except Exception:
            pass


class PluginLogger:
    """Print-based logger for the plugin host. Prefix is always `[Submarine]`."""

    prefix = "[%s] " % PLUGIN_NAME

    def log(self, message: str, prefix: Optional[str] = None) -> None:
        actual_prefix = self.prefix if prefix is None else prefix
        print("%s%s" % (actual_prefix, message))

    def info(self, message: str) -> None:
        self.log(message)

    def error(self, message: str) -> None:
        self.log("ERROR: %s" % message)

    def debug(self, message: str) -> None:
        self.log("DEBUG: %s" % message)


_bridge_logger = None  # type: Optional[FileLogger]
_plugin_logger = None  # type: Optional[PluginLogger]


def get_bridge_logger(log_path: Optional[str] = None) -> FileLogger:
    """Singleton file logger for bridges (`$TMPDIR/submarine_bridge.log`)."""
    global _bridge_logger
    if _bridge_logger is None:
        _bridge_logger = FileLogger(log_path or BRIDGE_LOG_PATH, prefix="")
    return _bridge_logger


def get_plugin_logger() -> PluginLogger:
    """Singleton print logger for the plugin host."""
    global _plugin_logger
    if _plugin_logger is None:
        _plugin_logger = PluginLogger()
    return _plugin_logger


def log_bridge(message: str) -> None:
    get_bridge_logger().info(message)


def log_bridge_error(message: str) -> None:
    get_bridge_logger().error(message)


def log_plugin(message: str) -> None:
    get_plugin_logger().info(message)


def log_plugin_error(message: str) -> None:
    get_plugin_logger().error(message)
