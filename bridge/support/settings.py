"""Project/user settings loading for bridge subprocesses.

Reads Claude-native settings files (~/.claude.json, .claude/settings.json)
and merges permissions.allow into autoAllowedMcpTools. Sublime-free.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


USER_SETTINGS_FILE = Path.home() / ".claude.json"
PROJECT_SETTINGS_DIR = ".claude"
SETTINGS_FILE = "settings.json"
MCP_CONFIG_FILE = ".mcp.json"


def _safe_json_load(file_path: str, default=None):
    if default is None:
        default = {}
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError, OSError):
        return default
    except Exception:
        return default


def load_project_settings(cwd: str = None) -> dict:
    """Load and merge user-level and project settings.

    User settings from ~/.claude.json are loaded first,
    then project settings (.claude/settings.json) override them.
    """
    user_settings = _safe_json_load(str(USER_SETTINGS_FILE), default={})

    if not cwd:
        return user_settings

    settings_path = os.path.join(cwd, PROJECT_SETTINGS_DIR, SETTINGS_FILE)
    project_settings = _safe_json_load(settings_path, default={})

    if not project_settings:
        mcp_path = os.path.join(cwd, MCP_CONFIG_FILE)
        project_settings = _safe_json_load(mcp_path, default={})

    local_path = os.path.join(cwd, PROJECT_SETTINGS_DIR, "settings.local.json")
    local_settings = _safe_json_load(local_path, default={})

    result = merge_settings(user_settings, project_settings)
    result = merge_settings(result, local_settings)

    permissions_allow = result.get("permissions", {}).get("allow", [])
    if permissions_allow:
        auto_allowed = result.get("autoAllowedMcpTools", [])
        existing = set(auto_allowed)
        for pattern in permissions_allow:
            if pattern not in existing:
                auto_allowed.append(pattern)
        result["autoAllowedMcpTools"] = auto_allowed

    return result


def merge_settings(user: dict, project: dict) -> dict:
    """Deep merge project settings into user settings."""
    result = user.copy()

    for key, value in project.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = {**result[key], **value}
        else:
            result[key] = value

    return result
