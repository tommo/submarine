"""Session control for callers outside Sublime.

`submarine_sessions.py` (and any MCP façade built later) reach this through the
plugin socket's `{"op": "sessions", "action": ...}` namespace, so an agent
running outside the editor can list, read, talk to and interrupt the sessions
this Sublime is running.

Four actions, JSON only:
  list       every live session plus the saved rows, viewless-safe
  view       a session's transcript tail, its rendered sheet, or its edits
  chat       deliver a prompt: send now, queue behind the turn, or refuse
  interrupt  cancel the current turn, reporting the state it leaves behind

Design notes that the callers depend on:
- `ref` resolves exactly (ids first, then a *unique* name). It never falls back
  to "the active view" or "the only working session": acting on the wrong
  session is the one failure a control surface must not have.
- Nothing here evaluates code, unlike the `code` op that shares the socket.
- Sessions may be viewless (detached, backgrounded, sleeping, or this host in
  single mode without a sheet), so every action works from registry and store
  state alone; only `view:mode=text` needs a sheet and says `no_view` without
  one instead of creating one.
- `chat` never stops a session and never opens a sheet. A sleeping target is
  woken first (`session.wake()`, as `send_to_session` does) and then handed to
  the socket server's `_wait_for_init` path, which waits for the bridge and
  sends; that wait runs off the editor's main thread, so neither blocking call
  is made here.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

ACTIONS = frozenset(("list", "view", "chat", "interrupt"))
VIEW_MODES = frozenset(("tail", "text", "edits"))
CHAT_POLICIES = frozenset(("queue", "interrupt", "reject"))

DEFAULT_TURNS = 3
MAX_TURNS = 50
DEFAULT_MAX_CHARS = 20000
DEFAULT_EDITS = 20
_IDEM_CAP = 64


class ControlError(Exception):
    """A refused action: `code` is stable, `message` is for the caller."""

    def __init__(self, code: str, message: str, data: Optional[dict] = None):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.data = data or {}


# ─── envelope + audit ───────────────────────────────────────────────────────


def instance_info() -> dict:
    """Which Sublime answered. Any instance rebinds the same socket path, so a
    caller that cares should compare this between calls."""
    return {
        "pid": os.getpid(),
        "plugin_dir": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    }


def _audit(action: str, caller: Any, target: Any, ok: bool, detail: str = "") -> None:
    try:
        from features.devtools.server import log as _log
        _log("sessions:%s" % action, level="info" if ok else "warn",
             caller=caller, target=target, ok=bool(ok), detail=detail)
    except Exception:
        pass


def _envelope(ok: bool, data: Any = None, error: Optional[str] = None,
              target: Optional[dict] = None) -> dict:
    out = {
        "ok": bool(ok),
        "data": data if data is not None else {},
        "error": error,
        "ts": time.time(),
        "instance": instance_info(),
    }
    if target:
        out["ref_resolved"] = target
    return out


def dispatch(request: dict) -> dict:
    """Socket entry point: `{"op": "sessions", "action": ..., "ref": ..., ...}`."""
    action = str((request or {}).get("action") or "").strip().lower()
    if action not in ACTIONS:
        return _envelope(False, {"actions": sorted(ACTIONS)},
                         "unknown_action: %r" % (action or None))
    caller = (request or {}).get("caller")
    try:
        if action == "list":
            body, target = action_list(request or {}), None
        elif action == "view":
            body, target = action_view(request or {})
        elif action == "chat":
            body = action_chat(request or {}, caller)
            return body          # may carry the sleep/wake hand-off
        else:
            body, target = action_interrupt(request or {})
    except ControlError as e:
        _audit(action, caller, None, False, e.message)
        return _envelope(False, dict(e.data, code=e.code), e.message)
    except Exception as e:  # never leak a traceback at the protocol level
        _audit(action, caller, None, False, "%s: %s" % (type(e).__name__, e))
        return _envelope(False, {"code": "internal"},
                         "%s: %s" % (type(e).__name__, e))
    _audit(action, caller, target, True)
    return _envelope(True, body, None, target)


# ─── addressing ─────────────────────────────────────────────────────────────


def _registry():
    from core.registry import default_registry
    return default_registry


def _live_sessions() -> List[Any]:
    try:
        return list(_registry().iter_sessions())
    except Exception:
        return []


def _saved_rows() -> List[dict]:
    try:
        from core.records import load_saved_sessions
        return [r for r in (load_saved_sessions() or []) if isinstance(r, dict)]
    except Exception:
        return []


def _view_id(session: Any) -> Optional[int]:
    try:
        view = session.output.view if getattr(session, "output", None) else None
        return view.id() if view is not None and view.is_valid() else None
    except Exception:
        return None


def _session_id(session: Any) -> str:
    return str(getattr(session, "session_id", None)
               or getattr(session, "submarine_session_id", None) or "")


def _project(session: Any) -> str:
    try:
        cwd = session._cwd()
        if cwd:
            return str(cwd)
    except Exception:
        pass
    try:
        window = getattr(session, "window", None)
        folders = window.folders() if window is not None else None
        if folders:
            return str(folders[0])
    except Exception:
        pass
    return ""


def _row_ref(session: Any) -> dict:
    return {
        "agent_id": getattr(session, "agent_id", None),
        "session_id": _session_id(session) or None,
        "name": getattr(session, "name", None),
        "backend": getattr(session, "backend", None),
        "view_id": _view_id(session),
    }


def _is_sleeping(session: Any) -> bool:
    if bool(getattr(session, "is_sleeping", False)):
        return True
    try:
        view = session.output.view if getattr(session, "output", None) else None
        if view is not None and view.is_valid():
            st = view.settings()
            return bool(st.get("submarine_sleeping") or st.get("claude_sleeping"))
    except Exception:
        pass
    return False


def _phase(session: Any) -> str:
    try:
        return str(getattr(getattr(session, "turn", None), "kind", "") or "")
    except Exception:
        return ""


def _live_state(session: Any) -> str:
    if _is_sleeping(session):
        return "sleeping"
    if getattr(session, "_error_halt", None) or getattr(session, "error_halted", None):
        return "error"
    if bool(getattr(session, "working", False)):
        return "working"
    if not getattr(session, "initialized", False) and not _session_id(session):
        return "closed"
    return "idle"


def _candidate(row: dict) -> dict:
    """The short form of a row, for an `ambiguous` or `not_found` answer."""
    return {k: row.get(k) for k in
            ("agent_id", "session_id", "name", "backend", "state", "view_id")}


def _match_name(sessions: List[Any], name: str) -> List[Any]:
    exact = [s for s in sessions if (getattr(s, "name", None) or "") == name]
    if exact:
        return exact
    low = name.lower()
    return [s for s in sessions if (getattr(s, "name", None) or "").lower() == low]


def _resolve_live(ref: Any) -> Any:
    """A live Session from `ref`, or ControlError. See the module docstring."""
    if ref is None:
        raise ControlError("bad_request", "no ref given",
                           {"hint": "use an agent_id, session_id or unique name"})
    sessions = _live_sessions()
    if isinstance(ref, dict):
        if ref.get("agent_id"):
            wanted = str(ref["agent_id"])
            for s in sessions:
                if wanted in (str(getattr(s, "agent_id", "")),
                              str(getattr(s, "subsession_id", ""))):
                    return s
            raise ControlError("not_found", "no live session %s" % wanted,
                               {"candidates": [_candidate(_live_row(s)) for s in sessions]})
        if ref.get("session_id"):
            wanted = str(ref["session_id"])
            for s in sessions:
                if _session_id(s) == wanted:
                    return s
            raise ControlError("not_found", "no live session %s" % wanted,
                               {"candidates": [_candidate(_live_row(s)) for s in sessions]})
        if ref.get("view_id") is not None:
            wanted = int(ref["view_id"])
            try:
                session = _registry().for_view_id(wanted)
            except Exception:
                session = None
            if session is not None:
                return session
            raise ControlError("not_found", "no live session on view %s" % wanted,
                               {"candidates": [_candidate(_live_row(s)) for s in sessions]})
        if ref.get("name"):
            hits = _match_name(sessions, str(ref["name"]))
            backend = (ref.get("backend") or "").lower()
            if backend:
                hits = [s for s in hits
                        if str(getattr(s, "backend", "") or "").lower() == backend]
            if len(hits) == 1:
                return hits[0]
            if not hits:
                raise ControlError("not_found", "no live session named %r" % ref["name"],
                                   {"candidates": [_candidate(_live_row(s)) for s in sessions]})
            raise ControlError("ambiguous", "%d live sessions named %r" % (len(hits), ref["name"]),
                               {"candidates": [_candidate(_live_row(s)) for s in hits]})
        raise ControlError("bad_request", "unusable ref", {"ref": ref})

    text = str(ref).strip()
    if text.isdigit():
        try:
            session = _registry().for_view_id(int(text))
        except Exception:
            session = None
        if session is not None:
            return session
    for s in sessions:
        if text in (str(getattr(s, "agent_id", "")), str(getattr(s, "subsession_id", "")),
                    _session_id(s)):
            return s
    hits = _match_name(sessions, text)
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ControlError("not_found", "no live session %r" % text,
                           {"candidates": [_candidate(_live_row(s)) for s in sessions]})
    raise ControlError("ambiguous", "%d live sessions named %r" % (len(hits), text),
                       {"candidates": [_candidate(_live_row(s)) for s in hits]})


def _resolve_any(ref: Any) -> dict:
    """A live session or a saved row.

    Returns `{"kind", "session", "row", "agent_id", "session_id", "backend",
    "cwd", "name"}` — reading a transcript must work for a session that is not
    running, which is most of what HISTORY holds.
    """
    try:
        session = _resolve_live(ref)
    except ControlError as e:
        if e.code != "not_found" or ref is None:
            raise
        session = None
    if session is not None:
        row = None
        try:
            row = session.store.find(_session_id(session))
        except Exception:
            row = None
        return {
            "kind": "live",
            "session": session,
            "row": row,
            "agent_id": str(getattr(session, "agent_id", "") or ""),
            "session_id": _session_id(session),
            "backend": str(getattr(session, "backend", "") or ""),
            "cwd": _project(session) or str((row or {}).get("project") or ""),
            "name": getattr(session, "name", None),
        }
    text = str(ref if not isinstance(ref, dict) else (ref.get("agent_id")
                 or ref.get("session_id") or ref.get("name") or "")).strip()
    rows = _saved_rows()
    hits = [r for r in rows
            if text and text in (str(r.get("session_id") or ""),
                                 str(r.get("agent_id") or ""),
                                 str(r.get("subsession_id") or ""))]
    if not hits and text:
        hits = [r for r in rows if str(r.get("name") or "") == text]
    if len(hits) == 1:
        r = hits[0]
        return {
            "kind": "saved",
            "session": None,
            "row": r,
            "agent_id": str(r.get("agent_id") or ""),
            "session_id": str(r.get("session_id") or ""),
            "backend": str(r.get("backend") or ""),
            "cwd": str(r.get("project") or ""),
            "name": r.get("name"),
        }
    if not hits:
        raise ControlError("not_found", "no live or saved session %r" % text,
                           {"candidates": [_candidate(_live_row(s)) for s in _live_sessions()]})
    raise ControlError("ambiguous", "%d saved sessions match %r" % (len(hits), text),
                       {"candidates": [_candidate(_saved_row(r)) for r in hits]})


# ─── rows ───────────────────────────────────────────────────────────────────


def _live_row(session: Any) -> dict:
    view_id = _view_id(session)
    window = getattr(session, "window", None)
    try:
        bound = _registry().bound_view_id(session)
    except Exception:
        bound = None
    budget = {}
    try:
        budget = session.context_budget_snapshot() or {}
    except Exception:
        budget = {}
    return {
        "kind": "live",
        "agent_id": getattr(session, "agent_id", None),
        "session_id": _session_id(session) or None,
        "subsession_id": getattr(session, "subsession_id", None),
        "parent_agent_id": getattr(session, "parent_agent_id", None),
        "name": getattr(session, "name", None),
        "backend": getattr(session, "backend", None),
        "model": getattr(session, "model", None),
        "state": _live_state(session),
        "turn_phase": _phase(session) or None,
        "view": {
            "bound": bound is not None,
            "view_id": view_id,
            "window": getattr(window, "id", lambda: None)() if window is not None else None,
            "project": _project(session) or None,
        },
        "query_count": getattr(session, "query_count", None),
        "last_access": getattr(session, "last_access", None),
        "context_summary": budget.get("summary"),
        "context_pct": budget.get("context_pct"),
        "headroom": budget.get("headroom"),
    }


def _saved_row(row: dict) -> dict:
    return {
        "kind": "saved",
        "agent_id": row.get("agent_id"),
        "session_id": row.get("session_id"),
        "subsession_id": row.get("subsession_id"),
        "parent_agent_id": row.get("parent_agent_id"),
        "name": row.get("name"),
        "backend": row.get("backend"),
        "model": row.get("model"),
        "state": str(row.get("state") or "closed"),
        "turn_phase": None,
        "view": {"bound": False, "view_id": None, "window": None,
                 "project": row.get("project") or None},
        "query_count": row.get("query_count"),
        "last_access": row.get("last_access") or row.get("last_activity"),
    }


def action_list(params: dict) -> dict:
    scope = str(params.get("scope") or "all").strip().lower()
    if scope not in ("all", "window", "children"):
        raise ControlError("bad_request", "unknown scope %r" % scope,
                           {"scopes": ["all", "window", "children"]})
    live = _live_sessions()
    parent = None
    if scope == "children":
        parent_ref = params.get("parent") or params.get("ref")
        parent = _resolve_live(parent_ref)
    rows = [_live_row(s) for s in live]
    if scope == "children":
        pid = str(getattr(parent, "agent_id", "") or "")
        rows = [r for r in rows if str(r.get("parent_agent_id") or "") == pid]
    elif scope == "window":
        window = params.get("window")
        rows = [r for r in rows
                if window is None or str(r["view"].get("window")) == str(window)]
    live_ids = {r["session_id"] for r in rows if r.get("session_id")}
    live_agents = {r["agent_id"] for r in rows if r.get("agent_id")}
    if scope == "all":
        rows.extend(_saved_row(r) for r in _saved_rows()
                    if str(r.get("session_id") or "") not in live_ids
                    and (not r.get("agent_id") or r.get("agent_id") not in live_agents))
    rows.sort(key=lambda r: (-float(r.get("last_access") or 0), str(r.get("name") or "")))
    counts = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    return {"scope": scope, "count": len(rows), "states": counts, "sessions": rows}


# ─── view ───────────────────────────────────────────────────────────────────


def _turn_summary(turn: dict, max_chars: int) -> dict:
    reply = str(turn.get("reply") or "")
    prompt = str(turn.get("prompt") or "")
    cut = len(reply) > max_chars
    tools = turn.get("tools") or []
    return {
        "prompt": prompt[:max_chars],
        "prompt_truncated": len(prompt) > max_chars,
        "reply": reply[:max_chars],
        "reply_truncated": cut,
        "tools": [str(t)[:120] for t in tools][:20],
        "ts": turn.get("ts"),
    }


def _sheet_text(session: Any, max_chars: int) -> Optional[str]:
    try:
        view = session.output.view if getattr(session, "output", None) else None
    except Exception:
        view = None
    if view is None or not view.is_valid():
        return None
    import sublime
    text = view.substr(sublime.Region(0, view.size()))
    return text[-max_chars:] if max_chars > 0 else text


def action_view(params: dict) -> (dict, dict):
    mode = str(params.get("mode") or "tail").strip().lower()
    if mode not in VIEW_MODES:
        raise ControlError("bad_request", "unknown mode %r" % mode,
                           {"modes": sorted(VIEW_MODES)})
    target = _resolve_any(params.get("ref"))
    ref = {"agent_id": target["agent_id"], "session_id": target["session_id"],
           "name": target["name"], "backend": target["backend"], "kind": target["kind"]}
    try:
        max_chars = int(params.get("max_chars") or DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        max_chars = DEFAULT_MAX_CHARS

    if mode == "text":
        session = target["session"]
        if session is None:
            raise ControlError("no_view", "session %s is not running" % ref["session_id"],
                               {"kind": target["kind"]})
        text = _sheet_text(session, max_chars)
        if text is None:
            raise ControlError("no_view", "session %s has no sheet" % ref["name"],
                               {"kind": target["kind"]})
        return {"mode": "text", "session": ref, "text": text,
                "chars": len(text)}, ref

    if mode == "edits":
        session = target["session"]
        if session is None:
            raise ControlError("no_session", "edits need a running session",
                               {"kind": target["kind"]})
        from core.session_edits import collect_session_edits, conversations_of, page_edits
        edits = collect_session_edits(conversations_of(session))
        page = page_edits(edits, params.get("offset") or 0,
                          params.get("limit") or DEFAULT_EDITS,
                          params.get("file_path"))
        page["session"] = ref
        return page, ref

    from features import resume as _resume
    sid = target["session_id"]
    if not sid:
        raise ControlError("no_session", "session has no id on disk yet", {"kind": target["kind"]})
    try:
        turns = _resume.load_turns(sid, target["backend"], target["cwd"],
                                   agent_id=target["agent_id"])
    except Exception as e:
        raise ControlError("transcript", "cannot read transcript: %s" % e)
    try:
        path = _resume.find_session_jsonl(sid, target["backend"], target["cwd"],
                                          target["agent_id"])
    except Exception:
        path = None
    try:
        want = max(1, min(MAX_TURNS, int(params.get("turns") or DEFAULT_TURNS)))
    except (TypeError, ValueError):
        want = DEFAULT_TURNS
    picked = list(turns or [])[-want:]
    return ({
        "mode": "tail",
        "session": ref,
        "transcript": path,
        "turn_count": len(turns or []),
        "turns": [_turn_summary(t, max_chars) for t in picked],
    }, ref)


# ─── chat ───────────────────────────────────────────────────────────────────


_IDEM_FALLBACK = {}  # type: Dict[str, dict]


def _idem_state() -> dict:
    """Bounded replay cache, hung off `sublime` so a reload keeps it."""
    try:
        import sublime
        st = getattr(sublime, "_submarine_control_idem", None)
        if not isinstance(st, dict):
            st = {}
            sublime._submarine_control_idem = st  # type: ignore[attr-defined]
        return st
    except Exception:
        return _IDEM_FALLBACK


def _idem_get(key: str) -> Optional[dict]:
    if not key:
        return None
    return _idem_state().get(str(key))


def _idem_put(key: str, data: dict) -> None:
    if not key:
        return
    st = _idem_state()
    st[str(key)] = dict(data)
    while len(st) > _IDEM_CAP:
        st.pop(next(iter(st)), None)


def _default_display(caller: Any) -> str:
    name = ""
    if isinstance(caller, dict):
        name = str(caller.get("name") or caller.get("kind") or "").strip()
    return "📨 from %s" % (name or "outside agent")


def action_chat(params: dict, caller: Any = None) -> dict:
    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        raise ControlError("bad_request", "empty prompt")
    policy = str(params.get("queue") or "queue").strip().lower()
    if policy not in CHAT_POLICIES:
        raise ControlError("bad_request", "unknown queue policy %r" % policy,
                           {"policies": sorted(CHAT_POLICIES)})
    wait = bool(params.get("wait"))
    display = str(params.get("display") or _default_display(caller))
    key = params.get("idem")
    session = _resolve_live(params.get("ref"))
    ref = _row_ref(session)
    data = {
        "accepted": True,
        "action": "pending",
        "policy": policy,
        "wait": wait,
        "display": display,
        "session": ref,
    }

    seen = _idem_get(key) if key else None
    if seen is not None:
        out = dict(seen)
        out["duplicate"] = True
        return _envelope(True, out, None, ref)

    asleep = _is_sleeping(session)
    sleeping = asleep or not getattr(session, "initialized", False)
    busy = bool(getattr(session, "working", False))
    if policy == "reject" and busy:
        raise ControlError("busy", "%s is mid-turn" % (ref.get("name") or ref.get("agent_id")),
                           {"session": ref, "turn_phase": _phase(session) or None})

    if policy == "interrupt" and not sleeping:
        # Cancel first, then hoist: no hand-off, because the bridge has to
        # settle the old turn before the new prompt means anything.
        if busy:
            session.send_now(prompt)
            data["action"] = "send_now"
        else:
            session.query(prompt, display_prompt=display)
            data["action"] = "sent"
        data["waited"] = False
        if key:
            _idem_put(key, data)
        _audit("chat", caller, ref, True, data["action"])
        return _envelope(True, data, None, ref)

    if sleeping or wait:
        # A sleeping session has to be started here: the hand-off below only
        # polls `initialized`, so without this wake the bridge never comes up
        # and the prompt is dropped when that poll times out. Same call
        # `send_to_session` makes before it hands over.
        if asleep:
            session.wake()
        # The socket server owns this hand-off: it wakes on its own thread,
        # waits for the bridge, delivers the prompt and (for `wait`) follows the
        # turn. Blocking here would stall the editor's main thread. The idem
        # record is written now, while "delivered" is still unknown, so a retry
        # cannot send the prompt twice.
        data["action"] = "woke" if sleeping else "queued"
        data["waited"] = False
        data["note"] = ("the prompt is delivered once the bridge is up"
                        if sleeping else "the prompt is delivered as soon as the "
                                         "session is free")
        if key:
            _idem_put(key, data)
        _audit("chat", caller, ref, True, data["action"])
        return {
            "ok": True,
            "data": data,
            "error": None,
            "ref_resolved": ref,
            "ts": time.time(),
            "instance": instance_info(),
            "_wait_for_init": True,
            "_session": session,
            "_prompt": prompt,
            "_display_prompt": display,
            "_wait_for_completion": wait,
            "waking": sleeping,
        }

    if busy:
        if policy == "reject":
            raise ControlError("busy", "%s is mid-turn" % (ref.get("name") or ref.get("agent_id")),
                               {"session": ref, "turn_phase": _phase(session) or None})
        if policy == "interrupt":
            session.send_now(prompt)
            data["action"] = "send_now"
        else:
            session.queue_prompt(prompt)
            data["action"] = "queued"
    else:
        session.query(prompt, display_prompt=display)
        data["action"] = "sent"
    data["waited"] = False
    if key:
        _idem_put(key, data)
    _audit("chat", caller, ref, True, data["action"])
    return _envelope(True, data, None, ref)


# ─── interrupt ──────────────────────────────────────────────────────────────


def action_interrupt(params: dict) -> (dict, dict):
    session = _resolve_live(params.get("ref"))
    ref = _row_ref(session)
    before = {"working": bool(getattr(session, "working", False)),
              "turn_phase": _phase(session) or None}
    session.interrupt()
    after_phase = _phase(session) or None
    settling = bool(getattr(session, "working", False)) and after_phase == "interrupting"
    data = {
        "interrupted": bool(before["working"]),
        "settling": settling,
        "state_before": before,
        "turn_phase": after_phase,
        "user_cancelled": bool(getattr(session, "_user_cancelled_turn", False)),
        "session": ref,
        "note": ("the bridge acknowledge settles the turn; a stale one is "
                 "reconciled by the host's interrupt timer") if settling else None,
    }
    return data, ref
