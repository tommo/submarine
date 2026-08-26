"""Stable agent identity + display-only view binding.

agent_id is the only session handle (host-stable, persisted in
.sessions.json). view_id is a Sublime runtime display binding — it
answers "which view currently shows this session" and is never stored
on the Session.

  by_agent:  agent_id (str) -> Session   # ALL live sessions
  binding:   view_id (int) -> agent_id   # display binding only
  waits:     child_id -> [wait entries]  # parent_agent_id only

"Background" is derived: live and not in binding.values().
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Any, List, Optional


def new_agent_id() -> str:
    return "agent-%s" % uuid.uuid4().hex[:12]


def resolve_spawn_model(
    requested=None,  # type: Optional[str]
    source_model=None,  # type: Optional[str]
    forking=False,  # type: bool
):
    # type: (...) -> Optional[str]
    """Submodel to pin on spawn_session / UI fork.

    Explicit ``model=`` wins. Forking inherits the source session's
    submodel when none is passed. Fresh spawn with no model uses the
    backend default later in resolve_init_model.
    """
    req = (requested or "").strip()
    if req:
        return req
    if forking:
        src = (source_model or "").strip()
        if src:
            return src
    return None


def resolve_init_model(
    profile_model=None,  # type: Optional[str]
    session_model=None,  # type: Optional[str]
    view_model=None,  # type: Optional[str]
    saved_model=None,  # type: Optional[str]
    default_model=None,  # type: Optional[str]
    resume=False,  # type: bool
    requested_model=None,  # type: Optional[str]
):
    # type: (...) -> Optional[str]
    """Model to send on initialize.

    One chain: spawn/fork pin → profile pin → this session → saved entry
    → view stamp (resume only) → backend default. Always pick a model
    when a default exists. New sessions ignore a leftover view stamp so
    DeepSeek does not leak onto the next Grok sheet.
    """
    chain = [requested_model, profile_model, session_model, saved_model]
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


_legacy_view_ref_logged = False


def _log_legacy_view_ref():
    # type: () -> None
    """Once per process — integer resolve_ref is deprecated."""
    global _legacy_view_ref_logged
    if _legacy_view_ref_logged:
        return
    _legacy_view_ref_logged = True
    try:
        from plat.log import log_plugin
        log_plugin("resolve_ref: integer view_id is deprecated; use agent_id")
    except Exception:
        pass


def _output_view_id(session):
    # type: (Any) -> Optional[int]
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
    """by_agent + binding + subsession wait entries. Plain class, no sublime."""

    def __init__(self, keep_running_on_close=True):
        # type: (bool) -> None
        self.by_agent = {}  # type: dict
        self.binding = {}  # type: dict
        self.waits = {}  # type: dict
        self.keep_running_default = keep_running_on_close

    @property
    def sessions(self):
        # type: () -> dict
        """Derived view_id → Session for currently bound sheets."""
        out = {}
        for vid, aid in self.binding.items():
            s = self.by_agent.get(aid)
            if s is not None:
                out[vid] = s
        return out

    @property
    def agents(self):
        # type: () -> dict
        """Derived agent_id → view_id reverse of binding."""
        return {aid: vid for vid, aid in self.binding.items()}

    @property
    def background(self):
        # type: () -> dict
        """Live sessions not currently bound to a view."""
        bound = set(self.binding.values())
        return {
            aid: s for aid, s in self.by_agent.items()
            if aid not in bound
        }

    def clear(self):
        # type: () -> None
        """Drop all mappings (plugin reload / hard reset)."""
        self.by_agent.clear()
        self.binding.clear()
        self.waits.clear()

    def register(self, session):
        # type: (Any) -> None
        """Put session in by_agent. Does not bind a view."""
        if session is None:
            return
        aid = getattr(session, "agent_id", None)
        if not aid:
            aid = new_agent_id()
            session.agent_id = aid
        for existing_aid, s in list(self.by_agent.items()):
            if s is session and existing_aid != aid:
                self.by_agent.pop(existing_aid, None)
                for vid, baid in list(self.binding.items()):
                    if baid == existing_aid:
                        self.binding[vid] = aid
        self.by_agent[aid] = session

    def bind(self, agent_id, view_id):
        # type: (str, int) -> None
        """Bind agent to view. Evicts whatever agent currently holds the view."""
        if not agent_id or view_id is None:
            return
        try:
            vid = int(view_id)
        except (TypeError, ValueError):
            return
        aid = str(agent_id)
        prev = self.binding.get(vid)
        if prev and prev != aid:
            prev_s = self.by_agent.get(prev)
            if prev_s is not None:
                prev_s.backgrounded = True
        for v, a in list(self.binding.items()):
            if a == aid and v != vid:
                self.binding.pop(v, None)
        self.binding[vid] = aid
        session = self.by_agent.get(aid)
        if session is not None:
            session.backgrounded = False

    def unbind(self, agent_id):
        # type: (str) -> None
        """Drop every binding for this agent. Session stays in by_agent."""
        if not agent_id:
            return
        aid = str(agent_id)
        for v, a in list(self.binding.items()):
            if a == aid:
                self.binding.pop(v, None)
        session = self.by_agent.get(aid)
        if session is not None:
            session.backgrounded = True

    def by_agent_id(self, agent_id):
        # type: (str) -> Optional[Any]
        """Resolve stable agent_id (also accepts subsession_id alias)."""
        if not agent_id:
            return None
        aid = str(agent_id).strip()
        s = self.by_agent.get(aid)
        if s is not None:
            return s
        for s in list(self.by_agent.values()):
            if getattr(s, "agent_id", None) == aid:
                return s
            if getattr(s, "subsession_id", None) == aid:
                return s
        return None

    def for_view(self, view):
        # type: (Any) -> Optional[Any]
        if view is None:
            return None
        try:
            return self.for_view_id(view.id())
        except Exception:
            return None

    def for_view_id(self, view_id):
        # type: (Any) -> Optional[Any]
        if view_id is None:
            return None
        try:
            vid = int(view_id)
        except (TypeError, ValueError):
            return None
        aid = self.binding.get(vid)
        if not aid:
            return None
        return self.by_agent.get(aid)

    def bound_view_id(self, session):
        # type: (Any) -> Optional[int]
        """Reverse lookup: which view currently shows this session."""
        if session is None:
            return None
        aid = getattr(session, "agent_id", None)
        if aid:
            for vid, a in self.binding.items():
                if a == aid:
                    return vid
        return _output_view_id(session)

    def register_session(self, session):
        # type: (Any) -> None
        """Register into by_agent and bind the output view if present."""
        self.register(session)
        vid = _output_view_id(session)
        if vid is None:
            return
        aid = getattr(session, "agent_id", None)
        if aid:
            self.bind(aid, vid)

    def unregister_view(self, view_id):
        # type: (int) -> None
        """Drop the display binding. Bound non-background sessions leave by_agent."""
        if view_id is None:
            return
        try:
            vid = int(view_id)
        except (TypeError, ValueError):
            return
        aid = self.binding.pop(vid, None)
        if not aid:
            return
        session = self.by_agent.get(aid)
        if session is not None and not getattr(session, "backgrounded", False):
            self.by_agent.pop(aid, None)

    def get_session_for_view_id(self, view_id):
        # type: (int) -> Optional[Any]
        return self.for_view_id(view_id)

    def get_session_by_agent_id(self, agent_id):
        # type: (str) -> Optional[Any]
        return self.by_agent_id(agent_id)

    def resolve_ref(self, ref):
        # type: (Any) -> Optional[Any]
        """Resolve agent_id (str). Int / digit-string is a deprecated binding lookup."""
        if ref is None or ref == "":
            return None
        if isinstance(ref, bool):
            return None
        if isinstance(ref, int):
            _log_legacy_view_ref()
            return self.for_view_id(ref)
        s = str(ref).strip()
        if not s:
            return None
        if s.isdigit():
            _log_legacy_view_ref()
            return self.for_view_id(int(s))
        return self.by_agent_id(s)

    def get_session_by_ref(self, ref):
        # type: (Any) -> Optional[Any]
        return self.resolve_ref(ref)

    def iter_sessions(self):
        # type: () -> List[Any]
        """All live sessions, including background (no sheet)."""
        out = []
        seen = set()
        for s in list(self.by_agent.values()):
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
        """Drop the sheet, keep the live session (derived background)."""
        if not session:
            return False
        aid = getattr(session, "agent_id", None)
        if not aid:
            aid = new_agent_id()
            session.agent_id = aid
        self.register(session)
        self.unbind(aid)
        try:
            if hasattr(session, "reset_phantoms_for_new_view"):
                session.reset_phantoms_for_new_view()
        except Exception:
            pass
        _clear_output_view(session)
        session.backgrounded = True
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
        if vid is None:
            vid = self.bound_view_id(session)
        if vid is not None:
            self.unregister_view(vid)
        aid = getattr(session, "agent_id", None)
        if aid:
            self.by_agent.pop(aid, None)
        return "stop"

    def resolve_parent_session(self, child):
        # type: (Any) -> Optional[Any]
        """Find parent session via stable parent_agent_id."""
        paid = getattr(child, "parent_agent_id", None)
        if paid:
            return self.by_agent_id(paid)
        return None

    def is_child_of(self, session, parent_view_id=None, parent_agent_id=None):
        # type: (Any, Optional[int], Optional[str]) -> bool
        if parent_agent_id:
            if getattr(session, "parent_agent_id", None) == parent_agent_id:
                return True
        if parent_view_id is not None and parent_agent_id is None:
            parent = self.for_view_id(parent_view_id)
            if parent:
                paid = getattr(parent, "agent_id", None)
                if paid and getattr(session, "parent_agent_id", None) == paid:
                    return True
        return False

    def list_children_of(self, parent_view_id=None, parent_agent_id=None):
        # type: (Optional[int], Optional[str]) -> List[Any]
        if parent_view_id is not None and not parent_agent_id:
            parent = self.for_view_id(parent_view_id)
            if parent:
                parent_agent_id = getattr(parent, "agent_id", None)
        out = []
        for s in self.iter_sessions():
            if parent_agent_id and getattr(s, "agent_id", None) == parent_agent_id:
                continue
            if self.is_child_of(s, parent_view_id=parent_view_id, parent_agent_id=parent_agent_id):
                out.append(s)
        return out

    def runtime_view_id(self, session):
        # type: (Any) -> Optional[int]
        return self.bound_view_id(session)

    def register_subsession_wait(
        self,
        child_id,  # type: str
        parent_agent_id=None,  # type: Optional[str]
        wake_prompt="",  # type: str
        parent_view_id=None,  # type: Optional[int]
    ):
        # type: (...) -> str
        """Register a parent waiter for child agent_id/subsession_id.

        parent_view_id is accepted and ignored (legacy callers).
        """
        key = str(child_id).strip()
        wid = "wait-%s" % uuid.uuid4().hex[:10]
        paid = parent_agent_id
        if not paid and parent_view_id is not None:
            parent = self.for_view_id(parent_view_id)
            if parent:
                paid = getattr(parent, "agent_id", None)
        entry = {
            "wait_id": wid,
            "child_id": key,
            "parent_agent_id": paid,
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

        entries = self.pop_subsession_waits(aliases)
        if not entries:
            return 0

        delivered_parents = set()
        n = 0
        for entry in entries:
            parent = None
            paid = entry.get("parent_agent_id")
            if paid:
                parent = self.by_agent_id(paid)
            if parent is None:
                parent = self.resolve_parent_session(child_session)
            if parent is None:
                continue

            parent_key = getattr(parent, "agent_id", None) or id(parent)
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


def register(session):
    # type: (Any) -> None
    default_registry.register(session)


def bind(agent_id, view_id):
    # type: (str, int) -> None
    default_registry.bind(agent_id, view_id)


def unbind(agent_id):
    # type: (str) -> None
    default_registry.unbind(agent_id)


def by_agent_id(agent_id):
    # type: (str) -> Optional[Any]
    return default_registry.by_agent_id(agent_id)


def for_view(view):
    # type: (Any) -> Optional[Any]
    return default_registry.for_view(view)


def for_view_id(view_id):
    # type: (Any) -> Optional[Any]
    return default_registry.for_view_id(view_id)


def bound_view_id(session):
    # type: (Any) -> Optional[int]
    return default_registry.bound_view_id(session)


def register_session(session):
    # type: (Any) -> None
    default_registry.register_session(session)


def unregister_view(view_id):
    # type: (int) -> None
    default_registry.unregister_view(view_id)


def get_session_for_view_id(view_id):
    # type: (int) -> Optional[Any]
    return default_registry.for_view_id(view_id)


def get_session_by_agent_id(agent_id):
    # type: (str) -> Optional[Any]
    return default_registry.by_agent_id(agent_id)


def resolve_ref(ref):
    # type: (Any) -> Optional[Any]
    return default_registry.resolve_ref(ref)


def get_session_by_ref(ref):
    # type: (Any) -> Optional[Any]
    return default_registry.resolve_ref(ref)


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


def resolve_parent_session(child):
    # type: (Any) -> Optional[Any]
    return default_registry.resolve_parent_session(child)


def is_child_of(session, parent_view_id=None, parent_agent_id=None):
    # type: (Any, Optional[int], Optional[str]) -> bool
    return default_registry.is_child_of(session, parent_view_id, parent_agent_id)


def list_children_of(parent_view_id=None, parent_agent_id=None):
    # type: (Optional[int], Optional[str]) -> List[Any]
    return default_registry.list_children_of(parent_view_id, parent_agent_id)


def runtime_view_id(session):
    # type: (Any) -> Optional[int]
    return default_registry.bound_view_id(session)


def register_subsession_wait(child_id, parent_agent_id=None, wake_prompt="", parent_view_id=None):
    # type: (str, Optional[str], str, Optional[int]) -> str
    return default_registry.register_subsession_wait(
        child_id=child_id,
        parent_agent_id=parent_agent_id,
        wake_prompt=wake_prompt,
        parent_view_id=parent_view_id,
    )


def pop_subsession_waits(child_ids):
    # type: (Any) -> list
    return default_registry.pop_subsession_waits(child_ids)


def fire_subsession_waits(child_session, result_summary=None, default_body=None):
    # type: (Any, Optional[str], Optional[str]) -> int
    return default_registry.fire_subsession_waits(
        child_session, result_summary, default_body)
