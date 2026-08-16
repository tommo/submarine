"""Safe JSON file I/O. Extracted from the old error_handler; nothing else survived."""
from __future__ import annotations

import json
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
    """Write JSON to a file (indent=2, utf-8). Returns True on success."""
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception:
        return False
