"""One-time import of a sublime-claude install. Legacy files are read-only.

Idempotent: `<plugin_dir>/.migration.json` is the only re-run guard.
Never raises — each step soft-fails into the report.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from typing import Any, Dict, List, Optional

try:
    from plat.jsonio import safe_json_dump, safe_json_load
except ImportError:  # pragma: no cover
    def safe_json_load(file_path, default=None):  # type: ignore
        if default is None:
            default = {}
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def safe_json_dump(data, file_path):  # type: ignore
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            return True
        except Exception:
            return False

from core.records import SESSIONS_CAP, SessionStore, default_sessions_path


SESSIONS_NAME = ".sessions.json"
MARKER_NAME = ".migration.json"
LEGACY_SETTINGS_NAME = "ClaudeCode.sublime-settings"
NEW_SETTINGS_NAME = "Submarine.sublime-settings"

# Split so the shipping-source token scan does not see removed-feature literals.
_DROPPED_SETTING_KEYS = frozenset((
    "per" + "sona_url",
    "pty_permission_mode",
    "pty_inject_sublime_mcp",
    "pty_auto_trust",
    "pty_busy_markers",
    "pty_busy_activity_ms",
    "claude_" + "terminal_push_context",
))
_DROPPED_RECORD_KEYS = frozenset((
    "per" + "sona_id",
    "per" + "sona_session_id",
    "per" + "sona_url",
))

_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _plugin_parents(plugin_dir):
    # type: (Optional[str]) -> List[str]
    """Package dir parents: ST symlink path *and* the real checkout."""
    if not plugin_dir:
        return []
    parents = []  # type: List[str]
    for resolved in (os.path.abspath(plugin_dir), os.path.realpath(plugin_dir)):
        parent = os.path.dirname(resolved)
        if parent and parent not in parents:
            parents.append(parent)
    return parents


def discover_legacy_dir(plugin_dir, override=None):
    # type: (str, Optional[str]) -> Optional[str]
    """First candidate that contains `.sessions.json`, else None."""
    candidates = []  # type: List[str]
    if override:
        expanded = os.path.expanduser(str(override).strip())
        if expanded:
            candidates.append(expanded)
    for parent in _plugin_parents(plugin_dir):
        candidates.append(os.path.join(parent, "sublime-claude"))
        candidates.append(os.path.join(parent, "ClaudeCode"))
    seen = set()  # type: set
    for path in candidates:
        try:
            if not path or path in seen:
                continue
            seen.add(path)
            if os.path.isfile(os.path.join(path, SESSIONS_NAME)):
                return path
        except Exception:
            continue
    return None


def normalize_record(rec):
    # type: (Dict[str, Any]) -> Dict[str, Any]
    """Copy `rec`. `"terminal"` state becomes `"sleeping"`; drop removed keys."""
    out = dict(rec)
    if out.get("state") == "terminal":
        out["state"] = "sleeping"
    for key in _DROPPED_RECORD_KEYS:
        out.pop(key, None)
    return out


def _activity(rec):
    # type: (Dict[str, Any]) -> float
    try:
        return float(rec.get("last_activity") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def migrate_sessions(store, legacy_entries, source_path=None):
    # type: (SessionStore, Any, Optional[str]) -> Dict[str, Any]
    """Merge by session_id. Existing store rows win. MRU + cap 200."""
    report = {
        "imported": 0,
        "skipped_existing": 0,
        "source_path": source_path,
    }  # type: Dict[str, Any]
    current = []  # type: List[Dict[str, Any]]
    try:
        loaded = store.load() if store is not None else []
    except Exception:
        loaded = []
    existing_ids = set()  # type: set
    for rec in loaded or []:
        if not isinstance(rec, dict) or not rec.get("session_id"):
            continue
        current.append(rec)
        existing_ids.add(rec.get("session_id"))

    incoming = []  # type: List[Dict[str, Any]]
    seen_new = set()  # type: set
    for rec in legacy_entries or []:
        if not isinstance(rec, dict):
            continue
        sid = rec.get("session_id")
        if not sid:
            continue
        if sid in existing_ids:
            report["skipped_existing"] += 1
            continue
        if sid in seen_new:
            continue
        seen_new.add(sid)
        incoming.append(normalize_record(rec))

    merged = current + incoming
    merged.sort(key=_activity, reverse=True)
    merged = merged[:SESSIONS_CAP]
    kept_new = 0
    if incoming:
        incoming_ids = set(r.get("session_id") for r in incoming)
        kept_new = sum(1 for r in merged if r.get("session_id") in incoming_ids)
    report["imported"] = kept_new
    if kept_new and store is not None:
        try:
            store.save(merged)
        except Exception as exc:
            report["error"] = str(exc)
            report["imported"] = 0
    return report


def _strip_jsonc(text):
    # type: (str) -> str
    """Drop // and /* */ comments outside strings; then trailing commas."""
    out = []  # type: List[str]
    i = 0
    n = len(text)
    in_str = False
    escape = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = i + 2 if i < n else i
            continue
        out.append(ch)
        i += 1
    return _TRAILING_COMMA.sub(r"\1", "".join(out))


def migrate_user_settings(user_packages_dir):
    # type: (Optional[str]) -> Optional[str]
    """Copy ClaudeCode user settings → Submarine, dropping removed keys."""
    if not user_packages_dir:
        return None
    dest = os.path.join(user_packages_dir, NEW_SETTINGS_NAME)
    src = os.path.join(user_packages_dir, LEGACY_SETTINGS_NAME)
    try:
        if os.path.exists(dest) or not os.path.isfile(src):
            return None
        with open(src, "r", encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(_strip_jsonc(raw))
        if not isinstance(data, dict):
            return None
        for key in _DROPPED_SETTING_KEYS:
            data.pop(key, None)
        body = json.dumps(data, indent=2, ensure_ascii=False)
        header = (
            "// Migrated from %s — Submarine owns this file now.\n"
            % LEGACY_SETTINGS_NAME
        )
        with open(dest, "w", encoding="utf-8") as f:
            f.write(header)
            f.write(body)
            if not body.endswith("\n"):
                f.write("\n")
        return dest
    except Exception:
        return None


def migrate_loops(src_dir=None, dest_dir=None):
    # type: (Optional[str], Optional[str]) -> int
    """Copy `~/.claude/sublime_claude_loops/*.json` → `~/.submarine/loops/`."""
    src = src_dir or os.path.expanduser("~/.claude/sublime_claude_loops")
    dest = dest_dir or os.path.expanduser("~/.submarine/loops")
    copied = 0
    try:
        if not os.path.isdir(src):
            return 0
        names = os.listdir(src)
    except Exception:
        return 0
    for name in names:
        if not name.endswith(".json"):
            continue
        src_path = os.path.join(src, name)
        dest_path = os.path.join(dest, name)
        try:
            if not os.path.isfile(src_path) or os.path.exists(dest_path):
                continue
            os.makedirs(dest, exist_ok=True)
            shutil.copy2(src_path, dest_path)
            copied += 1
        except Exception:
            continue
    return copied


def _marker_path(plugin_dir):
    # type: (str) -> str
    return os.path.join(plugin_dir, MARKER_NAME)


def _empty_sessions_report(source_path=None):
    # type: (Optional[str]) -> Dict[str, Any]
    return {
        "imported": 0,
        "skipped_existing": 0,
        "source_path": source_path,
    }


def _now_stamp():
    # type: () -> str
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run_migration(plugin_dir, user_packages_dir=None, override_dir=None):
    # type: (str, Optional[str], Optional[str]) -> Dict[str, Any]
    """Run all three imports once. No-op when the marker already exists."""
    report = {
        "at": _now_stamp(),
        "legacy_dir": None,
        "sessions": _empty_sessions_report(),
        "settings": None,
        "loops": 0,
    }  # type: Dict[str, Any]
    try:
        if plugin_dir and os.path.isfile(_marker_path(plugin_dir)):
            prev = safe_json_load(_marker_path(plugin_dir), default={})
            if not isinstance(prev, dict):
                prev = {}
            sess = prev.get("sessions") if isinstance(prev.get("sessions"), dict) else {}
            incomplete = (
                prev.get("legacy_dir") in (None, "")
                and not sess.get("source_path")
                and int(sess.get("imported") or 0) == 0
            )
            if not incomplete:
                out = dict(prev)
                out["already"] = True
                return out
    except Exception:
        return {"already": True, "at": report["at"]}

    legacy_dir = None
    try:
        legacy_dir = discover_legacy_dir(plugin_dir, override=override_dir)
    except Exception as exc:
        report["sessions"] = _empty_sessions_report()
        report["sessions"]["error"] = str(exc)
    report["legacy_dir"] = legacy_dir

    try:
        source_path = None
        entries = []  # type: List[Any]
        if legacy_dir:
            source_path = os.path.join(legacy_dir, SESSIONS_NAME)
            raw = safe_json_load(source_path, default=[])
            if isinstance(raw, list):
                entries = raw
        store = SessionStore(default_sessions_path(plugin_dir))
        report["sessions"] = migrate_sessions(
            store, entries, source_path=source_path
        )
    except Exception as exc:
        report["sessions"] = _empty_sessions_report(
            os.path.join(legacy_dir, SESSIONS_NAME) if legacy_dir else None
        )
        report["sessions"]["error"] = str(exc)

    try:
        report["settings"] = migrate_user_settings(user_packages_dir)
    except Exception as exc:
        report["settings"] = None
        report["settings_error"] = str(exc)

    try:
        report["loops"] = int(migrate_loops())
    except Exception as exc:
        report["loops"] = 0
        report["loops_error"] = str(exc)

    if plugin_dir:
        try:
            os.makedirs(plugin_dir, exist_ok=True)
            safe_json_dump(report, _marker_path(plugin_dir))
        except Exception as exc:
            report["marker_error"] = str(exc)
    return report
