"""Stable agent identity + runtime view_id mapping.

view_id is a Sublime runtime handle — it changes on restart and must not be
the public identity for MCP tools. agent_id is host-stable (persisted on the
view and in sessions.json).

The three maps used to hang on the `sublime` module so they survived import
cache (not ST restart). `default_registry` replaces that: the ST entry point
must keep a reference across package reloads (e.g. re-bind it onto the
`sublime` module in plugin_loaded). Tests and plain python3 get a process-
local instance; call default_registry.clear() on plugin_loaded.

  sessions:    view_id (int) -> Session     # ST lookup
  agents:      agent_id (str) -> view_id    # stable → runtime
  background:  agent_id (str) -> Session    # live, no sheet
  waits:       child_id -> [wait entries]   # host wait_for_subsession
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Any, List, Optional


def new_agent_id() -> str:
    return "agent-%s" % uuid.uuid4().hex[:12]


def resolve_init_model(
    profile_model=None,  # type: Optional[str]
    session_model=None,  # type: Optional[str]
    view_model=None,  # type: Optional[str]
    saved_model=None,  # type: Optional[str]
    default_model=None,  # type: Optional[str]
    resume=False,  # type: bool
):
    # type: (...) -> Optional[str]
    """Model to send on initialize.

    One chain: profile pin → this session → saved entry → view stamp
    (resume only) → backend default. Always pick a model when a default
    exists. New sessions ignore a leftover view stamp so DeepSeek does
    not leak onto the next Grok sheet.
    """
    chain = [profile_model, session_model, saved_model]
    if resume:
        chain.append(view_model)
    chain.append(default_model)
    for raw in chain:
        if raw:
            text = str(raw).strip()
            if text:
                return text
    return None


_SENDER_LINE = re.compile(r"^\[from (user|agent)(?: [^\]]*)?\]")


def stamp_sender_prompt(
    prompt,  # type: str
    sender_agent_id="",  # type: str
    sender_session_id="",  # type: str
    sender_name="",  # type: str
    from_user=False,  # type: bool
):
    # type: (...) -> str
    """Prefix a communication-tool prompt so the receiver sees who sent it.

    User ◎ input is unstamped. send_to_session always goes through here.
    Idempotent if the body already starts with ``[from user]`` / ``[from agent …]``.
    """
    body = (prompt or "").strip()
    if not body:
        return body
    if _SENDER_LINE.match(body):
        return body
    if from_user:
        return "[from user]\n%s" % body
    aid = (sender_agent_id or "").strip()
    sid = (sender_session_id or "").strip()
    name = (sender_name or "").strip()
    header = "[from agent %s]" % aid if aid else "[from agent]"
    extra = []
    if sid and sid != aid:
        extra.append("session_id=%s" % sid)
    if name:
        extra.append("name=%s" % name)
    if extra:
        header = header + " " + " ".join(extra)
    return "%s\n%s" % (header, body)


def sender_display_prompt(stamped):
    # type: (str) -> str
    """Short transcript label for a stamped send_to_session body."""
    first = (stamped or "").split("\n", 1)[0].strip()
    m = _SENDER_LINE.match(first)
    if not m:
        return first[:50] if first else "📬"
    if m.group(1) == "user":
        return "📬 from user"
    rest = first[len("[from agent"):].strip(" ]")
    token = (rest.split() or ["agent"])[0]
    return "📬 from %s" % token


def _session_view_id(session):
    # type: (Any) -> Optional[int]
    vid = getattr(session, "view_id", None)
    if vid is not None:
        try:
            return int(vid)
        except (TypeError, ValueError):
            pass
    view = getattr(getattr(session, "output", None), "view", None)
    if not view:
        return None
    try:
        if hasattr(view, "is_valid") and not view.is_valid():
            return None
        return view.id()
    except Exception:
        return None


def _clear_output_view(session):
    # type: (Any) -> None
    session.view_id = None
    output = getattr(session, "output", None)
    if output is None:
        return
    if hasattr(output, "view"):
        try:
            output.view = None
        except Exception:
            pass
    if hasattr(output, "_input_mode"):
        try:
            output._input_mode = False
        except Exception:
            pass


_AGENT_ID_RE = re.compile(r"agent-[0-9a-f]{8,}", re.I)


def subsession_notify_key(prompt):
    # type: (str) -> str
    """Stable key for one child-completion notify (dedupe queue entries)."""
    if not prompt:
        return ""
    m = _AGENT_ID_RE.search(prompt)
    return ("subsession:" + m.group(0).lower()) if m else ""


def is_stock_subsession_wake(prompt):
    # type: (str) -> bool
    s = (prompt or "").lstrip()
    return s.startswith("✅ Subsession ") or s.startswith("Subsession ")


def merge_subsession_queue(queued, incoming):
    # type: (list, str) -> list
    """At most one completion notify per child. Prefer custom wait text."""
    incoming = (incoming or "").strip()
    if not incoming:
        return list(queued or [])
    key = subsession_notify_key(incoming)
    if not key:
        return list(queued or []) + [incoming]
    out = []
    replaced = False
    for p in queued or []:
        if subsession_notify_key(p) != key:
            out.append(p)
            continue
        if is_stock_subsession_wake(incoming) and not is_stock_subsession_wake(p):
            out.append(p)
        else:
            out.append(incoming)
        replaced = True
    if not replaced:
        out.append(incoming)
    return out


def child_parent_already_notified(child_session):
    # type: (Any) -> bool
    return bool(getattr(child_session, "_parent_notified", False))


def parent_notify_should_inject(n_waits, child_session=None):
    # type: (int, Any) -> bool
    """False when this delivery already went through a waiter.

    Only this completion: waiter query/queue XOR default inject — not a
    lifetime lock. Next signal_complete is a new delivery.
    """
    return int(n_waits or 0) <= 0


def mark_child_parent_notified(child_session):
    # type: (Any) -> None
    try:
        child_session._parent_notified = True
    except Exception:
        pass


class SessionRegistry:
    """Three maps + subsession wait entries. Plain class, no sublime."""

    def __init__(self, keep_running_on_close=True):
        # type: (bool) -> None
        self.sessions = {}  # type: dict
        self.agents = {}  # type: dict
        self.background = {}  # type: dict
        self.waits = {}  # type: dict
        self.keep_running_default = keep_running_on_close

    def clear(self):
        # type: () -> None
        """Drop all mappings (plugin reload / hard reset)."""
        self.sessions.clear()
        self.agents.clear()
        self.background.clear()
        self.waits.clear()

    def register_session(self, session):
        # type: (Any) -> None
        """Bind session into view_id and agent_id maps."""
        vid = _session_view_id(session)
        if vid is None:
            return
        aid = getattr(session, "agent_id", None)
        if not aid:
            aid = new_agent_id()
            session.agent_id = aid

        old_vid = self.agents.get(aid)
        if old_vid is not None and old_vid != vid:
            old = self.sessions.get(old_vid)
            if old is session or old is None:
                self.sessions.pop(old_vid, None)

        for a, v in list(self.agents.items()):
            if v == vid and a != aid:
                self.agents.pop(a, None)

        self.sessions[vid] = session
        self.agents[aid] = vid
        self.background.pop(aid, None)
        session.backgrounded = False
        session.view_id = vid
        try:
            self.relink_parent_view(session)
        except Exception:
            pass

    def unregister_view(self, view_id):
        # type: (int) -> None
        session = self.sessions.pop(view_id, None)
        if session is None:
            for a, v in list(self.agents.items()):
                if v == view_id:
                    self.agents.pop(a, None)
            return
        aid = getattr(session, "agent_id", None)
        if aid and self.agents.get(aid) == view_id:
            self.agents.pop(aid, None)

    def get_session_for_view_id(self, view_id):
        # type: (int) -> Optional[Any]
        return self.sessions.get(view_id)

    def get_session_by_agent_id(self, agent_id):
        # type: (str) -> Optional[Any]
        """Resolve stable agent_id (also accepts subsession_id alias)."""
        if not agent_id:
            return None
        aid = str(agent_id).strip()
        vid = self.agents.get(aid)
        if vid is not None:
            s = self.sessions.get(vid)
            if s is not None:
                return s
        bg = self.background.get(aid)
        if bg is not None:
            return bg
        for s in self.sessions.values():
            if getattr(s, "agent_id", None) == aid:
                self.register_session(s)
                return s
            if getattr(s, "subsession_id", None) == aid:
                self.register_session(s)
                return s
        for s in list(self.background.values()):
            if getattr(s, "agent_id", None) == aid:
                return s
            if getattr(s, "subsession_id", None) == aid:
                return s
        return None

    def get_session_by_ref(self, ref):
        # type: (Any) -> Optional[Any]
        """Resolve agent_id (str) or view_id (int / digit-string)."""
        if ref is None or ref == "":
            return None
        if isinstance(ref, bool):
            return None
        if isinstance(ref, int):
            return self.get_session_for_view_id(ref)
        s = str(ref).strip()
        if not s:
            return None
        if s.isdigit():
            return self.get_session_for_view_id(int(s))
        return self.get_session_by_agent_id(s)

    def iter_sessions(self):
        # type: () -> List[Any]
        """All live sessions, including background (no sheet)."""
        out = []
        seen = set()
        for s in list(self.sessions.values()) + list(self.background.values()):
            if s is None or id(s) in seen:
                continue
            seen.add(id(s))
            out.append(s)
        return out

    def find_live_by_session_id(self, session_id):
        # type: (str) -> Optional[Any]
        if not session_id:
            return None
        for s in self.iter_sessions():
            if getattr(s, "session_id", None) == session_id:
                return s
        return None

    def sessions_for_window(self, window):
        # type: (Any) -> List[Any]
        if window is None:
            return []
        return [s for s in self.iter_sessions() if getattr(s, "window", None) == window]

    def keep_running_on_close(self, session):
        # type: (Any) -> bool
        """True when closing the sheet should detach, not kill the bridge."""
        if not session or getattr(session, "quick_mode", False):
            return False
        if getattr(session, "is_sleeping", False):
            return False
        flag = getattr(session, "keep_running_on_close", None)
        if flag is False:
            return False
        if flag is None and not self.keep_running_default:
            return False
        if getattr(session, "client", None) is not None:
            return True
        if getattr(session, "working", False):
            return True
        return bool(getattr(session, "initialized", False))

    def detach_session(self, session):
        # type: (Any) -> bool
        """Drop the sheet, keep the live session in the background map."""
        if not session:
            return False
        aid = getattr(session, "agent_id", None)
        if not aid:
            aid = new_agent_id()
            session.agent_id = aid
        vid = _session_view_id(session)
        if vid is not None:
            if self.sessions.get(vid) is session:
                self.sessions.pop(vid, None)
            if self.agents.get(aid) == vid:
                self.agents.pop(aid, None)
        try:
            if hasattr(session, "reset_phantoms_for_new_view"):
                session.reset_phantoms_for_new_view()
        except Exception:
            pass
        _clear_output_view(session)
        session.backgrounded = True
        self.background[aid] = session
        return True

    def close_or_detach_session(self, session, view=None):
        # type: (Any, Any) -> str
        """Detach a live session, or stop a sleeping/disabled one.

        Returns 'detach' or 'stop'.
        """
        if self.keep_running_on_close(session):
            if view is not None:
                try:
                    settings = view.settings()
                    settings.set("submarine_soft_close", True)
                    # Fallback key for listeners that still read the old name.
                    settings.set("claude_soft_close", True)
                except Exception:
                    pass
            self.detach_session(session)
            return "detach"
        try:
            session.stop()
        except Exception:
            pass
        vid = None
        try:
            if view is not None:
                vid = view.id()
        except Exception:
            vid = None
        if vid is not None:
            self.unregister_view(vid)
        else:
            try:
                ov = getattr(session, "output", None)
                v = getattr(ov, "view", None) if ov else None
                if v:
                    self.unregister_view(v.id())
                elif getattr(session, "view_id", None) is not None:
                    self.unregister_view(int(session.view_id))
            except Exception:
                pass
        return "stop"

    def relink_parent_view(self, session):
        # type: (Any) -> Optional[int]
        """Refresh session.parent_view_id from parent_agent_id."""
        parent = self.resolve_parent_session(session)
        if not parent:
            return getattr(session, "parent_view_id", None)
        pvid = _session_view_id(parent)
        if pvid is not None:
            session.parent_view_id = pvid
            return pvid
        return getattr(session, "parent_view_id", None)

    def resolve_parent_session(self, child):
        # type: (Any) -> Optional[Any]
        """Find parent session via stable parent_agent_id, else parent_view_id."""
        paid = getattr(child, "parent_agent_id", None)
        if paid:
            p = self.get_session_by_agent_id(paid)
            if p is not None:
                return p
        pvid = getattr(child, "parent_view_id", None)
        if pvid is not None:
            try:
                return self.get_session_for_view_id(int(pvid))
            except (TypeError, ValueError):
                pass
        return None

    def is_child_of(self, session, parent_view_id=None, parent_agent_id=None):
        # type: (Any, Optional[int], Optional[str]) -> bool
        if parent_agent_id:
            if getattr(session, "parent_agent_id", None) == parent_agent_id:
                return True
        if parent_view_id is not None:
            if getattr(session, "parent_view_id", None) == parent_view_id:
                return True
            if parent_agent_id is None:
                parent = self.get_session_for_view_id(parent_view_id)
                if parent:
                    paid = getattr(parent, "agent_id", None)
                    if paid and getattr(session, "parent_agent_id", None) == paid:
                        return True
        return False

    def list_children_of(self, parent_view_id=None, parent_agent_id=None):
        # type: (Optional[int], Optional[str]) -> List[Any]
        if parent_view_id is not None and not parent_agent_id:
            parent = self.get_session_for_view_id(parent_view_id)
            if parent:
                parent_agent_id = getattr(parent, "agent_id", None)
        out = []
        for vid, s in self.sessions.items():
            if parent_view_id is not None and vid == parent_view_id:
                continue
            if self.is_child_of(s, parent_view_id=parent_view_id, parent_agent_id=parent_agent_id):
                self.relink_parent_view(s)
                out.append(s)
        return out

    def relink_all_parents(self):
        # type: () -> int
        """After multi-tab restore, re-resolve parent_view_id for all children."""
        n = 0
        for s in list(self.sessions.values()):
            if getattr(s, "parent_agent_id", None) or getattr(s, "parent_view_id", None):
                before = getattr(s, "parent_view_id", None)
                after = self.relink_parent_view(s)
                if after and after != before:
                    n += 1
                self.register_session(s)
        return n

    def runtime_view_id(self, session):
        # type: (Any) -> Optional[int]
        return _session_view_id(session)

    def register_subsession_wait(
        self,
        child_id,  # type: str
        parent_view_id=None,  # type: Optional[int]
        parent_agent_id=None,  # type: Optional[str]
        wake_prompt="",  # type: str
    ):
        # type: (...) -> str
        """Register a parent waiter for child agent_id/subsession_id."""
        key = str(child_id).strip()
        wid = "wait-%s" % uuid.uuid4().hex[:10]
        entry = {
            "wait_id": wid,
            "child_id": key,
            "parent_view_id": parent_view_id,
            "parent_agent_id": parent_agent_id,
            "wake_prompt": wake_prompt or "",
            "created": time.time(),
        }
        self.waits.setdefault(key, []).append(entry)
        return wid

    def pop_subsession_waits(self, child_ids):
        # type: (Any) -> list
        out = []
        seen = set()
        for cid in child_ids:
            if not cid:
                continue
            key = str(cid).strip()
            for entry in self.waits.pop(key, []) or []:
                wid = entry.get("wait_id")
                if wid in seen:
                    continue
                seen.add(wid)
                out.append(entry)
        return out

    def fire_subsession_waits(self, child_session, result_summary=None, default_body=None):
        # type: (Any, Optional[str], Optional[str]) -> int
        """Deliver host-local wait_for_subsession prompts. Returns count."""
        aliases = []
        for attr in ("agent_id", "subsession_id"):
            v = getattr(child_session, attr, None)
            if v:
                aliases.append(str(v))
        try:
            vid = _session_view_id(child_session)
            if vid is not None:
                aliases.append(str(vid))
        except Exception:
            pass

        entries = self.pop_subsession_waits(aliases)
        if not entries:
            return 0

        delivered_parents = set()
        n = 0
        for entry in entries:
            parent = None
            paid = entry.get("parent_agent_id")
            if paid:
                parent = self.get_session_by_agent_id(paid)
            if parent is None and entry.get("parent_view_id") is not None:
                parent = self.get_session_for_view_id(entry["parent_view_id"])
            if parent is None:
                parent = self.resolve_parent_session(child_session)
            if parent is None:
                continue

            parent_key = (
                getattr(parent, "agent_id", None)
                or _session_view_id(parent)
                or id(parent)
            )
            if parent_key in delivered_parents:
                continue

            reg = (entry.get("wake_prompt") or "").rstrip()
            stock = (not reg) or reg.startswith("✅ Subsession ")
            if stock and default_body:
                body = default_body
            else:
                body = reg
                if result_summary:
                    if body:
                        body = "%s\n\n%s" % (body, result_summary)
                    else:
                        body = result_summary
            if not body:
                body = "✅ Subsession %s completed (wait_for_subsession)" % (
                    entry.get("child_id"),
                )

            try:
                if getattr(parent, "working", False):
                    parent.queue_prompt(body)
                elif getattr(parent, "is_sleeping", False):
                    parent._queued_prompts = merge_subsession_queue(
                        getattr(parent, "_queued_prompts", None) or [], body)
                    parent.wake()
                else:
                    parent.query(body, display_prompt="📬 Subsession complete")
                delivered_parents.add(parent_key)
                n += 1
                mark_child_parent_notified(child_session)
            except Exception:
                pass
        return n


# The ST entry point must keep this object alive across package reloads
# (re-bind onto the sublime module in plugin_loaded). Tests get a fresh
# process-local instance.
default_registry = SessionRegistry()


def ensure_registries():
    # type: () -> SessionRegistry
    return default_registry


def clear_registries():
    # type: () -> None
    default_registry.clear()


def register_session(session):
    # type: (Any) -> None
    default_registry.register_session(session)


def unregister_view(view_id):
    # type: (int) -> None
    default_registry.unregister_view(view_id)


def get_session_for_view_id(view_id):
    # type: (int) -> Optional[Any]
    return default_registry.get_session_for_view_id(view_id)


def get_session_by_agent_id(agent_id):
    # type: (str) -> Optional[Any]
    return default_registry.get_session_by_agent_id(agent_id)


def get_session_by_ref(ref):
    # type: (Any) -> Optional[Any]
    return default_registry.get_session_by_ref(ref)


def iter_sessions():
    # type: () -> List[Any]
    return default_registry.iter_sessions()


def find_live_by_session_id(session_id):
    # type: (str) -> Optional[Any]
    return default_registry.find_live_by_session_id(session_id)


def sessions_for_window(window):
    # type: (Any) -> List[Any]
    return default_registry.sessions_for_window(window)


def keep_running_on_close(session):
    # type: (Any) -> bool
    return default_registry.keep_running_on_close(session)


def detach_session(session):
    # type: (Any) -> bool
    return default_registry.detach_session(session)


def close_or_detach_session(session, view=None):
    # type: (Any, Any) -> str
    return default_registry.close_or_detach_session(session, view)


def relink_parent_view(session):
    # type: (Any) -> Optional[int]
    return default_registry.relink_parent_view(session)


def resolve_parent_session(child):
    # type: (Any) -> Optional[Any]
    return default_registry.resolve_parent_session(child)


def is_child_of(session, parent_view_id=None, parent_agent_id=None):
    # type: (Any, Optional[int], Optional[str]) -> bool
    return default_registry.is_child_of(session, parent_view_id, parent_agent_id)


def list_children_of(parent_view_id=None, parent_agent_id=None):
    # type: (Optional[int], Optional[str]) -> List[Any]
    return default_registry.list_children_of(parent_view_id, parent_agent_id)


def relink_all_parents():
    # type: () -> int
    return default_registry.relink_all_parents()


def runtime_view_id(session):
    # type: (Any) -> Optional[int]
    return default_registry.runtime_view_id(session)


def register_subsession_wait(child_id, parent_view_id=None, parent_agent_id=None, wake_prompt=""):
    # type: (str, Optional[int], Optional[str], str) -> str
    return default_registry.register_subsession_wait(
        child_id=child_id,
        parent_view_id=parent_view_id,
        parent_agent_id=parent_agent_id,
        wake_prompt=wake_prompt,
    )


def pop_subsession_waits(child_ids):
    # type: (Any) -> list
    return default_registry.pop_subsession_waits(child_ids)


def fire_subsession_waits(child_session, result_summary=None, default_body=None):
    # type: (Any, Optional[str], Optional[str]) -> int
    return default_registry.fire_subsession_waits(
        child_session, result_summary, default_body)
