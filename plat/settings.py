"""Claude CLI settings cascade + plugin profiles store. Sublime-free."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

try:
    from .constants import (
        MCP_CONFIG_FILE,
        PROFILES_FILE,
        PROJECT_LOCAL_SETTINGS_FILE,
        PROJECT_SETTINGS_DIR,
        PROJECT_SETTINGS_FILE,
        USER_PROFILES_DIR,
        USER_SETTINGS_FILE,
    )
    from .jsonio import safe_json_load
    from .util import deep_merge
except ImportError:  # standalone (future bridge support package)
    from constants import (  # type: ignore
        MCP_CONFIG_FILE,
        PROFILES_FILE,
        PROJECT_LOCAL_SETTINGS_FILE,
        PROJECT_SETTINGS_DIR,
        PROJECT_SETTINGS_FILE,
        USER_PROFILES_DIR,
        USER_SETTINGS_FILE,
    )
    from jsonio import safe_json_load  # type: ignore
    from util import deep_merge  # type: ignore


def merge_settings(user: Dict[str, Any], project: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge project settings into user settings (project wins)."""
    return deep_merge(user, project)


def load_project_settings(cwd: Optional[str] = None) -> dict:
    """Load and merge the Claude CLI settings cascade.

    Order (later wins): `~/.claude.json` < `.claude/settings.json` <
    `.claude/settings.local.json`. If `.claude/settings.json` is missing/empty,
    `.mcp.json` (MCP servers only) is used in its place — same as the old
    plugin. `permissions.allow` patterns are appended onto `autoAllowedMcpTools`.
    """
    user_settings = safe_json_load(str(USER_SETTINGS_FILE), default={})
    if not cwd:
        return user_settings

    settings_path = os.path.join(cwd, PROJECT_SETTINGS_DIR, PROJECT_SETTINGS_FILE)
    project_settings = safe_json_load(settings_path, default={})

    if not project_settings:
        mcp_path = os.path.join(cwd, MCP_CONFIG_FILE)
        project_settings = safe_json_load(mcp_path, default={})

    local_path = os.path.join(cwd, PROJECT_SETTINGS_DIR, PROJECT_LOCAL_SETTINGS_FILE)
    local_settings = safe_json_load(local_path, default={})

    result = merge_settings(user_settings, project_settings)
    result = merge_settings(result, local_settings)

    permissions_allow = result.get("permissions", {}).get("allow", [])
    if permissions_allow:
        auto_allowed = list(result.get("autoAllowedMcpTools") or [])
        existing = set(auto_allowed)
        for pattern in permissions_allow:
            if pattern not in existing:
                auto_allowed.append(pattern)
                existing.add(pattern)
        result["autoAllowedMcpTools"] = auto_allowed

    return result


def load_profiles(project_path: Optional[str] = None) -> Dict[str, Any]:
    """Load profiles with cascade: user (`~/.submarine/profiles.json`) < project.

    `project_path` is a file path (same as the old API), not a directory.
    Only the `"profiles"` map is loaded; other top-level keys are ignored.
    """
    profiles = {}  # type: Dict[str, Any]

    user_profiles_path = os.path.join(str(USER_PROFILES_DIR), PROFILES_FILE)
    data = safe_json_load(user_profiles_path, default={})
    if isinstance(data, dict):
        profiles.update(data.get("profiles") or {})

    if project_path:
        data = safe_json_load(project_path, default={})
        if isinstance(data, dict):
            profiles.update(data.get("profiles") or {})

    drop = (
        "per" + "sona_id",
        "per" + "sona_session_id",
        "per" + "sona_url",
    )
    cleaned = {}  # type: Dict[str, Any]
    for name, config in profiles.items():
        if isinstance(config, dict):
            config = dict(config)
            for key in drop:
                config.pop(key, None)
        cleaned[name] = config
    return cleaned
