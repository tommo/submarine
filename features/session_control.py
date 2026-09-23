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

from features.web_access import WebAccessError, handle as web_access_handle

_SESSION_ACTIONS = (
    "list", "view", "chat", "interrupt", "pending", "answer",
    "backends", "create", "rename", "close", "open", "read", "clear",
)
_WEB_ACCESS_ACTIONS = (
    "web_access_request", "web_access_status", "web_access_check",
    "web_access_list", "web_access_grant", "web_access_deny",
    "web_access_revoke",
)
ACTIONS = frozenset(_SESSION_ACTIONS + _WEB_ACCESS_ACTIONS)
#: `read` returns at most this much of a file (the browser code view).
MAX_READ_BYTES = 2 * 1024 * 1024
PERMISSION_RESPONSES = frozenset(("allow", "deny", "allow_session", "allow_all"))
PLAN_RESPONSES = frozenset(("approve", "reject"))
#: A permission's tool_input on the wire: a Write's content or a long Bash
#: script is summarised, not streamed, through the list.
MAX_INPUT_CHARS = 2000
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
        elif action == "pending":
            body, target = action_pending(request or {})
        elif action == "answer":
            body, target = action_answer(request or {})
        elif action == "backends":
            body, target = action_backends(request or {}), None
        elif action == "create":
            return action_create(request or {}, caller)   # may carry the chat hand-off
        elif action == "rename":
            body, target = action_rename(request or {})
        elif action == "close":
            body, target = action_close(request or {})
        elif action == "open":
            body, target = action_open(request or {})
        elif action == "read":
            body, target = action_read(request or {})
        elif action == "clear":
            body, target = action_clear(request or {})
        elif action in _WEB_ACCESS_ACTIONS:
            body, target = _web_access(action, request or {}), None
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


def _web_access(action: str, params: dict) -> dict:
    """Device grants. The raw token is not part of the audit detail."""
    try:
        return web_access_handle(action, params)
    except WebAccessError as e:
        raise ControlError(e.code, e.message)


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
            from core.agent_ids import canon_agent_id
            wanted = str(canon_agent_id(str(ref["agent_id"])))
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
    # What the sheet is waiting on, if anything: the list surfaces these first
    # (a question or permission is the one thing a phone must not miss).
    waiting = None
    modals = getattr(getattr(session, "output", None), "modals", None)
    for attr, kind in (("pending_question", "question"),
                       ("pending_permission", "permission"),
                       ("pending_plan", "plan")):
        if getattr(modals, attr, None) is not None:
            waiting = kind
            break
    return {
        "kind": "live",
        "waiting": waiting,
        "unread": bool(getattr(session, "unread", False)),
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
    from features.resume import display_prompt
    reply = str(turn.get("reply") or "")
    # A host-injected prompt (task notification, wake) reads as the ⚙ label
    # the sheet showed, not the raw tag block.
    prompt = display_prompt(str(turn.get("prompt") or ""))
    cut = len(reply) > max_chars
    tools = turn.get("tools") or []
    out = {
        "prompt": prompt[:max_chars],
        "prompt_truncated": len(prompt) > max_chars,
        "reply": reply[:max_chars],
        "reply_truncated": cut,
        "tools": [str(t)[:120] for t in tools][:20],
        "ts": turn.get("ts"),
    }
    # The turn in order — text between its tool calls — for a client that
    # rebuilds the sheet (the web UI); `reply`/`tools` stay the flat views.
    events = []
    budget = max_chars
    for kind, value in turn.get("events") or []:
        if kind == "tool":
            events.append(["tool", str(value)[:120]])
        elif budget > 0:
            text = str(value or "")
            events.append(["text", text[:budget]])
            budget -= len(text)
        if len(events) >= 400:
            break
    if events:
        out["events"] = events
    return out


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


def _default_display(caller: Any, prompt: str = "") -> str:
    """The sheet's prompt line for a prompt sent from outside Sublime: the
    📨 mark, then the message. The display text replaces the prompt on the
    sheet (`◎ … ▶`), so the message itself has to be in it. Who sent it is in
    the audit log; the mark alone says "not typed here"."""
    _ = caller
    prompt = str(prompt or "").strip()
    if not prompt:
        return "📨"
    return "📨 %s" % prompt


def _stamp_agent_sender(caller: Any, target: Any, prompt: str, display: str,
                        explicit: bool = False):
    """A chat sent by an agent inside another session (the sessions CLI from
    its Bash tool carries SUBMARINE_AGENT_ID) gets the same `[from agent …]`
    header send_to_session uses, so the target knows who asked and can
    reply. Anyone else — the web UI, a terminal — sends as before."""
    aid = str((caller or {}).get("agent_id") or "") if isinstance(caller, dict) else ""
    if not aid or aid == str(getattr(target, "agent_id", "") or ""):
        return prompt, display
    try:
        from core.registry import (default_registry, sender_display_prompt,
                                   stamp_sender_prompt)
    except Exception:
        return prompt, display
    sender = None
    try:
        sender = default_registry.by_agent_id(aid)
    except Exception:
        sender = None
    stamped = stamp_sender_prompt(
        prompt, sender_agent_id=aid,
        sender_session_id=str(getattr(sender, "session_id", "") or "") if sender else "",
        sender_name=str(getattr(sender, "name", "") or "") if sender else "")
    shown = display if explicit else sender_display_prompt(stamped)
    return stamped, shown


def action_chat(params: dict, caller: Any = None) -> dict:
    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        raise ControlError("bad_request", "empty prompt")
    policy = str(params.get("queue") or "queue").strip().lower()
    if policy not in CHAT_POLICIES:
        raise ControlError("bad_request", "unknown queue policy %r" % policy,
                           {"policies": sorted(CHAT_POLICIES)})
    wait = bool(params.get("wait"))
    display = str(params.get("display") or _default_display(caller, prompt))
    key = params.get("idem")
    session = _resolve_live(params.get("ref"))
    prompt, display = _stamp_agent_sender(caller, session, prompt, display,
                                          explicit=bool(params.get("display")))
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


# ─── pending modals / answer ────────────────────────────────────────────────


def _clip(value: Any, limit: int = MAX_INPUT_CHARS) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, dict):
        return {str(k): _clip(v, limit) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        return [_clip(v, limit) for v in list(value)[:40]]
    return value


def _modals_of(session: Any) -> Any:
    return getattr(getattr(session, "output", None), "modals", None)


def _pending_body(session: Any) -> dict:
    modals = _modals_of(session)
    items = []  # type: List[dict]
    try:
        items = list(modals.descriptors()) if modals is not None else []
    except Exception:
        items = []
    out = []
    for d in items:
        kind = d.get("kind")
        payload = dict(d.get("payload") or {})
        if kind == "permission":
            payload["tool_input"] = _clip(payload.get("tool_input") or {})
        elif kind == "question":
            qs = payload.get("questions") or []
            idx = int(payload.get("current_idx") or 0)
            payload["total"] = len(qs)
            payload["question"] = _clip(qs[idx]) if 0 <= idx < len(qs) else None
            payload["questions"] = [_clip(q) for q in qs]
        out.append({"kind": kind, "payload": payload})
    # What the sheet shows first: a permission, then the plan, then the
    # question — the order ModalUI.descriptors() lists them.
    return {"modals": out, "waiting": (out[0]["kind"] if out else None),
            "count": len(out)}


def action_pending(params: dict) -> (dict, dict):
    session = _resolve_live(params.get("ref"))
    ref = _row_ref(session)
    body = _pending_body(session)
    body["session"] = ref
    return body, ref


def action_answer(params: dict) -> (dict, dict):
    """Answer the visible modal the way the sheet's keys would.

    question:   {"kind":"question", "option": 2 | "label", "options": [...],
                 "text": "free text", "qid": …}
    permission: {"kind":"permission", "response": allow|deny|allow_session|allow_all, "id": …}
    plan:       {"kind":"plan", "response": approve|reject, "id": …}
    """
    session = _resolve_live(params.get("ref"))
    ref = _row_ref(session)
    modals = _modals_of(session)
    if modals is None:
        raise ControlError("no_session", "session has no modal surface")
    kind = str(params.get("kind") or "").strip().lower()
    applied = False
    if kind == "question":
        q_req = getattr(modals, "pending_question", None)
        if not q_req or getattr(q_req, "callback", None) is None:
            raise ControlError("not_found", "no question is pending", {"kind": kind})
        qid = params.get("qid")
        if qid is not None and str(qid) != str(getattr(q_req, "qid", "")):
            raise ControlError("stale", "that question is no longer the pending one",
                               {"qid": getattr(q_req, "qid", None)})
        q = q_req.questions[q_req.current_idx]
        options = q.get("options") or []
        labels = [o.get("label", str(o)) if isinstance(o, dict) else str(o) for o in options]

        def _label(v):
            if isinstance(v, int) or (isinstance(v, str) and v.isdigit()):
                i = int(v)
                if 1 <= i <= len(labels):
                    return labels[i - 1]
                raise ControlError("bad_request", "option %s is out of range" % v,
                                   {"options": labels})
            return str(v)

        if params.get("text") is not None and str(params.get("text")).strip():
            answer = str(params["text"]).strip()  # type: Any
        elif params.get("options") is not None:
            if not q.get("multiSelect"):
                raise ControlError("bad_request", "this question takes one option",
                                   {"options": labels})
            answer = [_label(v) for v in (params.get("options") or [])]
        elif params.get("option") is not None:
            answer = _label(params["option"])
            if q.get("multiSelect"):
                answer = [answer]
        else:
            raise ControlError("bad_request", "give option, options or text",
                               {"options": labels, "multiSelect": bool(q.get("multiSelect"))})
        applied = bool(modals.answer_question(answer))
    elif kind == "permission":
        response = str(params.get("response") or "").strip().lower()
        if response not in PERMISSION_RESPONSES:
            raise ControlError("bad_request", "response must be one of %s"
                               % ", ".join(sorted(PERMISSION_RESPONSES)))
        perm = getattr(modals, "pending_permission", None)
        if not perm or getattr(perm, "callback", None) is None:
            raise ControlError("not_found", "no permission is pending", {"kind": kind})
        if params.get("id") is not None and str(params["id"]) != str(perm.id):
            raise ControlError("stale", "that permission is no longer the pending one",
                               {"id": perm.id})
        applied = bool(modals.answer_permission(response, params.get("id")))
    elif kind == "plan":
        response = str(params.get("response") or "").strip().lower()
        if response not in PLAN_RESPONSES:
            raise ControlError("bad_request", "response must be approve or reject")
        plan = getattr(modals, "pending_plan", None)
        if not plan or getattr(plan, "callback", None) is None:
            raise ControlError("not_found", "no plan is pending", {"kind": kind})
        if params.get("id") is not None and str(params["id"]) != str(plan.id):
            raise ControlError("stale", "that plan is no longer the pending one",
                               {"id": plan.id})
        applied = bool(modals.answer_plan(response, params.get("id")))
    else:
        raise ControlError("bad_request", "kind must be question, permission or plan")
    if not applied:
        raise ControlError("not_found", "nothing to answer", {"kind": kind})
    body = _pending_body(session)
    body["answered"] = kind
    body["session"] = ref
    return body, ref


# ─── backends / create / rename / close ─────────────────────────────────────


def _windows() -> list:
    try:
        import sublime
        return list(sublime.windows() or [])
    except Exception:
        return []


def _window_project(window: Any) -> str:
    try:
        folders = window.folders() if window else None
    except Exception:
        folders = None
    return (folders[0] or "").rstrip("/") if folders else ""


def _pick_window(params: dict) -> Any:
    """The Sublime window a new session lives in: by id, by project folder,
    else the active one. A session needs a window (its sheet, its project)."""
    windows = _windows()
    if not windows:
        raise ControlError("no_session", "Sublime has no window to create a session in")
    wid = params.get("window")
    if wid not in (None, ""):
        for w in windows:
            try:
                if str(w.id()) == str(wid):
                    return w
            except Exception:
                continue
        raise ControlError("not_found", "no window %s" % wid,
                           {"windows": [_window_row(w) for w in windows]})
    project = str(params.get("project") or "").rstrip("/")
    if project:
        for w in windows:
            if _window_project(w) == project:
                return w
        raise ControlError("not_found", "no window has project %s" % project,
                           {"windows": [_window_row(w) for w in windows]})
    try:
        import sublime
        active = sublime.active_window()
        if active is not None:
            return active
    except Exception:
        pass
    return windows[0]


def _window_row(window: Any) -> dict:
    try:
        wid = window.id()
    except Exception:
        wid = None
    count = 0
    try:
        from core.registry import sessions_for_window
        count = len([s for s in sessions_for_window(window)
                     if not getattr(s, "quick_mode", False)])
    except Exception:
        count = 0
    return {"id": wid, "project": _window_project(window) or None, "sessions": count}


def action_backends(params: dict) -> dict:
    """What a new session can be made of: backends (with availability and
    model aliases) and the windows (projects) it can be created in."""
    _ = params
    out = []
    default = "claude"
    try:
        import sublime
        from plat.constants import SETTINGS_FILE
        st = sublime.load_settings(SETTINGS_FILE)
        default = str(st.get("default_backend", "claude") or "claude")
        settings = {k: st.get(k) for k in ("providers", "default_models", "default_model")}
    except Exception:
        settings = None
    try:
        from backend.specs import all_backends, is_available
        for name, spec in all_backends(settings).items():
            try:
                avail = bool(is_available(name, settings))
            except Exception:
                avail = True
            out.append({
                "name": name,
                "label": spec.label or name,
                "available": avail,
                "pinned": bool(getattr(spec, "pinned", True)),
                "models": [list(m) for m in (spec.default_models or [])],
            })
    except Exception as e:
        raise ControlError("internal", "cannot list backends: %s" % e)
    return {"backends": out, "default": default,
            "windows": [_window_row(w) for w in _windows()]}


def action_create(params: dict, caller: Any = None) -> dict:
    """Start a new session in a window. `backend`, `model`, `name`, `window`
    or `project`, and an optional first `prompt` (delivered through `chat`
    once the bridge is up). The sheet is not shown: the session lives in the
    list until someone opens it."""
    backend = str(params.get("backend") or "").strip().lower() or None
    model = str(params.get("model") or "").strip() or None
    name = str(params.get("name") or "").strip()
    prompt = str(params.get("prompt") or "").strip()
    window = _pick_window(params)
    if backend:
        try:
            from backend.specs import all_backends
            if backend not in all_backends():
                raise ControlError("bad_request", "unknown backend %r" % backend,
                                   {"backends": sorted(all_backends())})
        except ControlError:
            raise
        except Exception:
            pass
    try:
        from ui.session_api import create_session
        session = create_session(window, backend=backend, model=model,
                                 focus=False, show=False, start=True)
    except Exception as e:
        raise ControlError("internal", "create failed: %s" % e)
    if session is None:
        raise ControlError("internal", "create failed")
    if name:
        try:
            session._set_name(name)
        except Exception:
            session.name = name
    ref = _row_ref(session)
    _audit("create", caller, ref, True, backend or "")
    data = {"created": True, "session": ref, "backend": getattr(session, "backend", None),
            "model": getattr(session, "model", None), "window": _window_row(window)}
    if not prompt:
        return _envelope(True, data, None, ref)
    env = action_chat({"ref": {"agent_id": ref["agent_id"]}, "prompt": prompt,
                       "queue": "queue", "idem": params.get("idem")}, caller)
    if isinstance(env, dict):
        env.setdefault("data", {})
        if isinstance(env["data"], dict):
            env["data"].update({"created": True, "window": data["window"]})
    return env


def action_rename(params: dict) -> (dict, dict):
    name = str(params.get("name") or "").strip()
    if not name:
        raise ControlError("bad_request", "name is required")
    if len(name) > 200:
        name = name[:200]
    target = _resolve_any(params.get("ref"))
    ref = {"agent_id": target["agent_id"], "session_id": target["session_id"],
           "name": name, "backend": target["backend"], "kind": target["kind"]}
    session = target["session"]
    if session is not None:
        try:
            session._set_name(name)
        except Exception:
            session.name = name
            try:
                session._save_session()
            except Exception:
                pass
    else:
        from core.records import rename_saved_session
        if not rename_saved_session(target["session_id"], name):
            raise ControlError("not_found", "saved session %s not found" % target["session_id"])
    try:
        from ui.session_list import schedule_session_list_refresh
        schedule_session_list_refresh()
    except Exception:
        pass
    return {"renamed": True, "from": target["name"], "name": name, "session": ref}, ref


def action_close(params: dict) -> (dict, dict):
    """Live session: stop it (the host sheet hands off to a peer in single
    mode, as Cmd+W does). Saved row: drop it from history when `remove` is
    set, else refuse — history rows are not running anything."""
    target = _resolve_any(params.get("ref"))
    ref = {"agent_id": target["agent_id"], "session_id": target["session_id"],
           "name": target["name"], "backend": target["backend"], "kind": target["kind"]}
    remove = bool(params.get("remove"))
    session = target["session"]
    if session is None:
        if not remove:
            raise ControlError("bad_request", "%s is not running; pass remove to drop "
                               "it from history" % (ref["name"] or ref["session_id"]))
        from core.records import remove_saved_session
        dropped = bool(remove_saved_session(target["session_id"]))
        try:
            from ui.session_list import schedule_session_list_refresh
            schedule_session_list_refresh()
        except Exception:
            pass
        return {"closed": False, "removed": dropped, "session": ref}, ref
    window = getattr(session, "window", None)
    if window is None:
        try:
            view = session.output.view if session.output else None
            window = view.window() if view is not None else None
        except Exception:
            window = None
    row = {"kind": "live", "session_id": target["session_id"], "agent_id": target["agent_id"],
           "name": target["name"], "section": "CURRENT"}
    try:
        from ui.session_list import close_row
        ok = bool(close_row(window, row))
    except Exception:
        ok = False
    if not ok:
        try:
            session.stop()
            ok = True
        except Exception as e:
            raise ControlError("internal", "close failed: %s" % e)
    removed = False
    if remove and target["session_id"]:
        from core.records import remove_saved_session
        removed = bool(remove_saved_session(target["session_id"]))
    try:
        from ui.session_list import schedule_session_list_refresh
        schedule_session_list_refresh()
    except Exception:
        pass
    return {"closed": True, "removed": removed, "session": ref}, ref


# ─── open a file in Sublime ─────────────────────────────────────────────────


def action_open(params: dict) -> (dict, dict):
    """Open `file_path` (at `line`) in the session's window — the Edits list's
    "open in Sublime". Only files that exist; nothing is created."""
    path = str(params.get("file_path") or "").strip()
    if not path:
        raise ControlError("bad_request", "file_path is required")
    import os
    if not os.path.isfile(path):
        raise ControlError("not_found", "no such file: %s" % path)
    target = _resolve_any(params.get("ref")) if params.get("ref") else None
    ref = None
    window = None
    if target is not None:
        ref = {"agent_id": target["agent_id"], "session_id": target["session_id"],
               "name": target["name"], "backend": target["backend"], "kind": target["kind"]}
        session = target["session"]
        if session is not None:
            window = getattr(session, "window", None)
            if window is None:
                try:
                    view = session.output.view if session.output else None
                    window = view.window() if view is not None else None
                except Exception:
                    window = None
    if window is None:
        try:
            import sublime
            window = sublime.active_window()
        except Exception:
            window = None
    if window is None:
        raise ControlError("no_session", "Sublime has no window to open the file in")
    try:
        line = int(params.get("line") or 0)
    except (TypeError, ValueError):
        line = 0
    try:
        import sublime
        spec = "%s:%d" % (path, line) if line > 0 else path
        view = window.open_file(spec, sublime.ENCODED_POSITION if line > 0 else 0)
        try:
            window.focus_view(view)
        except Exception:
            pass
    except Exception as e:
        raise ControlError("internal", "open failed: %s" % e)
    return {"opened": path, "line": line or None, "window": _window_row(window),
            "session": ref}, ref


def action_read(params: dict) -> (dict, dict):
    """A file's text for the browser code view: existing regular files only,
    UTF-8 (binary refused), clipped to MAX_READ_BYTES."""
    import os
    path = str(params.get("file_path") or "").strip()
    if not path:
        raise ControlError("bad_request", "file_path is required")
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise ControlError("not_found", "no such file: %s" % path)
    ref = None
    if params.get("ref"):
        target = _resolve_any(params.get("ref"))
        ref = {"agent_id": target["agent_id"], "session_id": target["session_id"],
               "name": target["name"], "backend": target["backend"], "kind": target["kind"]}
    try:
        limit = int(params.get("max_bytes") or MAX_READ_BYTES)
    except (TypeError, ValueError):
        limit = MAX_READ_BYTES
    limit = max(1024, min(limit, MAX_READ_BYTES))
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            raw = f.read(limit + 1)
    except OSError as e:
        raise ControlError("internal", "cannot read %s: %s" % (path, e))
    if b"\x00" in raw[:8192]:
        raise ControlError("bad_request", "%s is not a text file" % path)
    truncated = len(raw) > limit
    text = raw[:limit].decode("utf-8", "replace")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    return {"file_path": path, "size": size, "truncated": truncated,
            "text": text, "lines": text.count("\n") + (0 if text.endswith("\n") else 1),
            "mtime": mtime, "session": ref}, ref


def action_clear(params: dict) -> (dict, dict):
    """Clear the session's sheet the way Cmd+K / Cmd+Shift+K do: keep the
    last round (default) or wipe it. Nothing about the session changes —
    the transcript on disk and the bridge are untouched; only the sheet's
    text, which otherwise grows for as long as the session lives."""
    session = _resolve_live(params.get("ref"))
    ref = _row_ref(session)
    keep_last = params.get("keep_last")
    keep_last = True if keep_last is None else bool(keep_last)
    out = getattr(session, "output", None)
    if out is None:
        raise ControlError("no_view", "session has no sheet")
    try:
        if keep_last:
            out.clear_keep_last()
        else:
            out.clear(keep_supportive=True)
    except Exception as e:
        raise ControlError("internal", "clear failed: %s" % e)
    return {"cleared": True, "keep_last": keep_last, "session": ref}, ref
