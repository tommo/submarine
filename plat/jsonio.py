"""Safe JSON file I/O. Extracted from the old error_handler; nothing else survived."""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any


def safe_json_load(file_path: str, default: Any = None) -> Any:
    """Load JSON from a file. Returns `default` ({} if None) on any failure."""
    if default is None:
        default = {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError, OSError):
        return default
    except Exception:
        return default


def safe_json_dump(data: Any, file_path: str) -> bool:
    """Write JSON to a file (indent=2, utf-8). Returns True on success.

    Atomic: the payload goes to a temp file in the same directory, is fsynced,
    then renamed over the target, so a crash, a full disk or a serialization
    error mid-write leaves the previous file intact rather than truncated.
    """
    tmp = None
    try:
        parent = os.path.dirname(os.path.abspath(file_path))
        fd, tmp = tempfile.mkstemp(
            prefix="." + os.path.basename(file_path) + ".", suffix=".tmp",
            dir=parent)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, file_path)
        return True
    except Exception:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass
        return False
