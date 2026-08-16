"""Session persistence: `.sessions.json` store + bookmarks.

Schema is EXACTLY the old on-disk shape. Path is the plugin directory
(parent of `core/`), cap 200, MRU-front.

State derivation when saving a live object:
  client and initialized → "open"
  session_id and not client and not initialized → "sleeping"
  else keep existing or "closed"

View-stamp keys are `submarine_*` with a one-time read fallback to the
old `claude_*` names (ARCHITECTURE §Removal). PersistPort is the adapter;
this module only knows the key names.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    from plat.jsonio import safe_json_dump, safe_json_load
    from plat.constants import SESSIONS_FILE as _SESSIONS_NAME
except ImportError:
    _SESSIONS_NAME = ".sessions.json"

    def safe_json_load(file_path, default=None):  # type: ignore
        import json
        if default is None:
            default = {}
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def safe_json_dump(data, file_path):  # type: ignore
        import json
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            return True
        except Exception:
            return False


SESSIONS_CAP = 200

# View settings (ST persist these across restart).
STAMP_SESSION_ID = "submarine_session_id"
STAMP_AGENT_ID = "submarine_agent_id"
STAMP_SUBSESSION_ID = "submarine_subsession_id"
STAMP_PARENT_AGENT_ID = "submarine_parent_agent_id"
STAMP_BACKEND = "submarine_backend"
STAMP_MODEL = "submarine_model"
STAMP_EFFORT = "submarine_effort"
STAMP_PROVIDER_LABEL = "submarine_provider_label"
STAMP_SLEEPING = "submarine_sleeping"
STAMP_OUTPUT = "submarine_output"

# One-time read fallback after the rename.
_STAMP_LEGACY = {
    STAMP_SESSION_ID: "claude_session_id",
    STAMP_AGENT_ID: "claude_agent_id",
    STAMP_SUBSESSION_ID: "claude_subsession_id",
    STAMP_PARENT_AGENT_ID: "claude_parent_agent_id",
    STAMP_BACKEND: "claude_backend",
    STAMP_MODEL: "claude_model",
    STAMP_EFFORT: "claude_effort",
    STAMP_PROVIDER_LABEL: "claude_provider_label",
    STAMP_SLEEPING: "claude_sleeping",
    STAMP_OUTPUT: "claude_output",
}


def default_sessions_path(plugin_dir: Optional[str] = None) -> str:
    """`.sessions.json` lives in the plugin package directory."""
    if plugin_dir:
        return os.path.join(plugin_dir, _SESSIONS_NAME)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, _SESSIONS_NAME)


def derive_state(client: Any, initialized: bool, session_id: Optional[str]) -> str:
    """Live-object state rule. Never a stored flag on the Session itself."""
    if client is not None and initialized:
        return "open"
    if session_id and client is None and not initialized:
        return "sleeping"
    return "closed"


def read_stamp(persist: Any, key: str) -> Any:
    """Read a view stamp, falling back to the old `claude_*` name."""
    if persist is None:
        return None
    val = persist.read(key)
    if val is not None:
        return val
    legacy = _STAMP_LEGACY.get(key)
    if legacy:
        return persist.read(legacy)
    return None


def stamp_identity(persist: Any, **values: Any) -> None:
    """Write non-None identity stamps under the new key names."""
    if persist is None:
        return
    for key, value in values.items():
        if value is None:
            continue
        persist.stamp(key, value)


@dataclass
class SessionRecord:
    session_id: str
    name: Optional[str] = None
    project: Optional[str] = None
    backend: str = "claude"
    model: Optional[str] = None
    agent_id: Optional[str] = None
    subsession_id: Optional[str] = None
    parent_agent_id: Optional[str] = None
    query_count: int = 0
    total_cost: float = 0.0
    last_activity: float = 0.0
    last_access: float = 0.0
    state: str = "closed"
    resume_session_at: Optional[str] = None
    context_usage: Optional[Dict[str, Any]] = None
    plan_file: Optional[str] = None
    first_prompt: Optional[str] = None
    goal: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "session_id": self.session_id,
            "name": self.name,
            "project": self.project,
            "backend": self.backend,
            "query_count": int(self.query_count or 0),
            "total_cost": float(self.total_cost or 0.0),
            "last_activity": float(self.last_activity or 0.0),
            "last_access": float(self.last_access or 0.0) or float(self.last_activity or 0.0),
            "state": self.state or "closed",
        }  # type: Dict[str, Any]
        if self.model:
            d["model"] = self.model
        if self.agent_id:
            d["agent_id"] = self.agent_id
        if self.subsession_id:
            d["subsession_id"] = self.subsession_id
        if self.parent_agent_id:
            d["parent_agent_id"] = self.parent_agent_id
        if self.resume_session_at:
            d["resume_session_at"] = self.resume_session_at
        if self.context_usage:
            d["context_usage"] = self.context_usage
        if self.plan_file:
            d["plan_file"] = self.plan_file
        if self.first_prompt:
            d["first_prompt"] = str(self.first_prompt)[:200]
        elif self.name:
            d["first_prompt"] = str(self.name).split("\n", 1)[0].strip()[:200]
        if self.goal:
            d["goal"] = self.goal
        return d

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SessionRecord":
        sid = str(raw.get("session_id") or "")
        first = raw.get("first_prompt")
        if first is None and raw.get("name"):
            first = str(raw.get("name")).split("\n", 1)[0].strip()[:200]
        return cls(
            session_id=sid,
            name=raw.get("name"),
            project=raw.get("project"),
            backend=raw.get("backend") or "claude",
            model=raw.get("model"),
            agent_id=raw.get("agent_id"),
            subsession_id=raw.get("subsession_id"),
            parent_agent_id=raw.get("parent_agent_id"),
            query_count=int(raw.get("query_count") or 0),
            total_cost=float(raw.get("total_cost") or 0.0),
            last_activity=float(raw.get("last_activity") or 0.0),
            last_access=float(raw.get("last_access") or raw.get("last_activity") or 0.0),
            state=raw.get("state") or "closed",
            resume_session_at=raw.get("resume_session_at"),
            context_usage=raw.get("context_usage") if isinstance(raw.get("context_usage"), dict) else None,
            plan_file=raw.get("plan_file"),
            first_prompt=first,
            goal=raw.get("goal") if isinstance(raw.get("goal"), dict) else None,
        )


class SessionStore:
    """`.sessions.json` — plugin dir, cap 200, MRU-front."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or default_sessions_path()

    def load(self) -> List[Dict[str, Any]]:
        data = safe_json_load(self.path, default=[])
        if isinstance(data, list):
            return data
        return []

    def save(self, sessions: List[Dict[str, Any]]) -> bool:
        return bool(safe_json_dump(sessions[:SESSIONS_CAP], self.path))

    def find(self, session_id: str) -> Optional[Dict[str, Any]]:
        if not session_id:
            return None
        for entry in self.load():
            if entry.get("session_id") == session_id:
                return entry
        return None

    def upsert(self, entry: Dict[str, Any]) -> None:
        sid = entry.get("session_id")
        if not sid:
            return
        sessions = self.load()
        for i, s in enumerate(sessions):
            if s.get("session_id") == sid:
                sessions.pop(i)
                break
        sessions.insert(0, entry)
        self.save(sessions)

    def persist_state(self, session_id: str, state: str, model: Optional[str] = None) -> None:
        if not session_id:
            return
        sessions = self.load()
        for i, s in enumerate(sessions):
            if s.get("session_id") == session_id:
                sessions[i]["state"] = state
                if model:
                    sessions[i]["model"] = model
                self.save(sessions)
                return
        self.upsert({"session_id": session_id, "state": state, "model": model})

    def rename(self, session_id: str, name: str) -> bool:
        name = (name or "").strip()
        if not session_id or not name:
            return False
        sessions = self.load()
        hit = False
        for s in sessions:
            if s.get("session_id") == session_id:
                s["name"] = name
                hit = True
                break
        if hit:
            self.save(sessions)
        return hit

    def remove(self, session_id: str) -> bool:
        if not session_id:
            return False
        sessions = self.load()
        nxt = [s for s in sessions if s.get("session_id") != session_id]
        if len(nxt) == len(sessions):
            return False
        self.save(nxt)
        return True


def _bookmarks_path(project_path: Optional[str] = None) -> str:
    if project_path:
        return os.path.join(project_path, ".claude", "bookmarks.json")
    return os.path.expanduser("~/.claude/bookmarks.json")


def load_bookmarks(project_path: Optional[str] = None) -> set:
    path = _bookmarks_path(project_path)
    data = safe_json_load(path, default={})
    if isinstance(data, dict):
        return set(data.get("starred") or [])
    return set()


def save_bookmarks(starred: set, project_path: Optional[str] = None) -> None:
    path = _bookmarks_path(project_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    safe_json_dump({"starred": list(starred)}, path)


def toggle_bookmark(session_id: str, project_path: Optional[str] = None) -> bool:
    starred = load_bookmarks(project_path)
    if session_id in starred:
        starred.discard(session_id)
        now_starred = False
    else:
        starred.add(session_id)
        now_starred = True
    save_bookmarks(starred, project_path)
    return now_starred


# Module-level helpers matching the old session.py surface (used by ui later).
_default_store = None  # type: Optional[SessionStore]


def default_store(plugin_dir: Optional[str] = None) -> SessionStore:
    global _default_store
    if plugin_dir:
        return SessionStore(default_sessions_path(plugin_dir))
    if _default_store is None:
        _default_store = SessionStore()
    return _default_store


def load_saved_sessions(plugin_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    return default_store(plugin_dir).load()


def save_sessions(sessions: List[Dict[str, Any]], plugin_dir: Optional[str] = None) -> None:
    default_store(plugin_dir).save(sessions)


def rename_saved_session(session_id: str, name: str, plugin_dir: Optional[str] = None) -> bool:
    return default_store(plugin_dir).rename(session_id, name)


def remove_saved_session(session_id: str, plugin_dir: Optional[str] = None) -> bool:
    return default_store(plugin_dir).remove(session_id)
