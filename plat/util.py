"""Small helpers with callers in plat/ (and later core/). Sublime-free."""
from __future__ import annotations

import os
import tempfile
from typing import Any, Dict


def temp_dir() -> str:
    """Process temp directory (TMPDIR / TEMP / TMP, else the system default)."""
    return (
        os.environ.get("TMPDIR")
        or os.environ.get("TEMP")
        or os.environ.get("TMP")
        or tempfile.gettempdir()
    )


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow-copy `base`, then overlay `override`. Nested dicts merge one level.

    Used by the Claude CLI settings cascade. List / scalar values are replaced,
    not concatenated.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = {**result[key], **value}
        else:
            result[key] = value
    return result
