"""Plugin self-debug surface for agents and humans.

Starts with the plugin (MCP socket already up). Outside Sublime:

  python3 submarine_devtools.py ping
  python3 submarine_devtools.py snapshot
  python3 submarine_devtools.py sessions
  python3 submarine_devtools.py composer [view_id]
  python3 submarine_devtools.py log --tail 80
  python3 submarine_devtools.py eval 'return list(sublime._submarine_sessions)'
  python3 submarine_devtools.py reload          # soft in-process package reload
  python3 submarine_devtools.py reload --hard   # ignored_packages cycle

Socket protocol (newline JSON on MCP_SOCKET_PATH):

  {"op":"debug","action":"ping"|"snapshot"|"sessions"|"composer"|"log"|"event"|"reload"|"help", ...}
  {"code":"..."}   # existing eval path

Host-only for plugin authors (CLI / socket). Not advertised in agent MCP tools/list.
"""
from __future__ import annotations

import collections
import json
import os
import threading
import time
import traceback
from typing import Any, Deque, Dict, List, Optional

import sublime

from plat.constants import BRIDGE_LOG_PATH, INPUT_MARKER, MCP_SOCKET_PATH

# ─── ring buffer (process-global on sublime so importlib.reload keeps history) ─

_MAX_EVENTS = 2000
_log_path = os.path.join(
    os.environ.get("TMPDIR")
    or os.environ.get("TEMP")
    or os.environ.get("TMP")
    or "/tmp",
    "submarine_devtools.log",
)


def _state() -> dict:
    """Singleton state hung on the sublime module (survives module reload)."""
    st = getattr(sublime, "_submarine_devtools", None)
    if not isinstance(st, dict) or "events" not in st:
        st = {
            "events": collections.deque(maxlen=_MAX_EVENTS),
            "lock": threading.Lock(),
            "started": False,
        }
        sublime._submarine_devtools = st  # type: ignore[attr-defined]
    return st


def log(message: str, level: str = "info", **fields: Any) -> None:
    """Append a structured event (also mirrored to the log file)."""
    entry = {
        "ts": time.time(),
        "level": level,
        "msg": str(message),
    }
    if fields:
        entry["fields"] = {k: _safe(v) for k, v in fields.items()}
    st = _state()
    with st["lock"]:
        st["events"].append(entry)
    try:
        with open(_log_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def start() -> None:
    """Called from plugin_loaded — idempotent across reloads."""
    st = _state()
    if st.get("started"):
        return
    st["started"] = True
    log("devtools started", socket=MCP_SOCKET_PATH, log_path=_log_path)
    print(f"[Submarine Devtools] ready  socket={MCP_SOCKET_PATH}  log={_log_path}")


def stop() -> None:
    st = _state()
    if st.get("started"):
        log("devtools stopped")
    st["started"] = False


# ─── public snapshots ─────────────────────────────────────────────────────────

def ping() -> dict:
    st = _state()
    return {
        "ok": True,
        "plugin": "Submarine",
        "socket": MCP_SOCKET_PATH,
        "log_path": _log_path,
        "events": len(st["events"]),
        "sessions": len((getattr(sublime, "_submarine_sessions", None) or getattr(sublime, "_claude_sessions", None) or {})),
        "time": time.time(),
        "started": bool(st.get("started")),
    }


def help_text() -> dict:
    return {
        "actions": [
            "ping",
            "snapshot [view_id]",
            "sessions",
            "composer [view_id]",
            "log [tail=N] [grep=substr]",
            "event message=...  (write agent note into ring)",
            "reload [mode=soft|hard]  (no ST restart)",
            "goal <objective> | status|pause|resume|clear  [view_id=]",
            "capture            (ui_mode + views + list + host transcript)",
            "views",
            "session_list",
            "output [view_id]   (transcript head/tail)",
            "ui_mode [tabs|single]",
            "help",
        ],
        "cli": "python3 submarine_devtools.py <action> ...",
        "socket": MCP_SOCKET_PATH,
        "log_path": _log_path,
    }


def reload_plugin(mode: str = "soft", **kwargs: Any) -> dict:
    """Schedule a correct package reload (soft or hard). See package_reloader."""
    from features import reload as package_reloader
    log(f"reload scheduled mode={mode}")
    return package_reloader.schedule_reload(mode=mode, **kwargs)


def goal_command(args: str = "status", agent_id: Optional[str] = None) -> dict:
    """Run /goal harness on a host session (same as typing /goal in the sheet)."""
    sess, vid = _resolve_session(agent_id)
    if not sess:
        return {
            "ok": False,
            "error": "no session",
            "agent_id": agent_id,
            "available": list(getattr(sublime, "_submarine_by_agent", None)
                              or getattr(sublime, "_submarine_sessions", None)
                              or getattr(sublime, "_claude_sessions", None)
                              or {}),
        }
    if not hasattr(sess, "handle_goal_command"):
        return {"ok": False, "error": "session has no handle_goal_command (stale class? reload)", "agent_id": agent_id}
    try:
        sess.handle_goal_command(args or "status")
        return {
            "ok": True,
            "agent_id": getattr(sess, "agent_id", None),
            "args": args,
            "goal": _goal_dump(sess),
            "working": bool(getattr(sess, "working", False)),
            "initialized": bool(getattr(sess, "initialized", False)),
            "sleeping": bool(getattr(sess, "is_sleeping", False)),
        }
    except Exception as e:
        log(f"goal_command error: {e}", level="error")
        return {"ok": False, "error": str(e), "traceback": traceback.format_exc(), "agent_id": getattr(sess, "agent_id", None)}


def sessions_dump() -> dict:
    """All host sessions (not just MCP-spawned subsessions)."""
    try:
        from core.registry import default_registry
        live = list(default_registry.iter_sessions())
        rows = []
        for s in live:
            vid = default_registry.bound_view_id(s)
            rows.append(_session_row(vid, s))
        rows.sort(key=lambda r: (not r.get("working"), r.get("agent_id") or ""))
        return {"count": len(rows), "sessions": rows, "log_path": _log_path}
    except Exception:
        reg = (getattr(sublime, "_submarine_sessions", None) or getattr(sublime, "_claude_sessions", None) or {})
        rows = []
        for vid, s in list(reg.items()):
            if s is None or isinstance(s, (int, str)):
                continue
            rows.append(_session_row(vid, s))
        rows.sort(key=lambda r: (not r.get("working"), str(r.get("agent_id") or r.get("view_id") or "")))
        return {"count": len(rows), "sessions": rows, "log_path": _log_path}


def snapshot(view_id: Optional[int] = None) -> dict:
    """Host + optional focused session/composer dump."""
    out: Dict[str, Any] = {
        "ping": ping(),
        "windows": _windows_brief(),
        "sessions": sessions_dump()["sessions"],
    }
    sess, vid = _resolve_session(view_id)
    if sess is not None:
        out["focus"] = {
            "view_id": vid,
            "session": _session_row(vid, sess, deep=True),
            "composer": _composer_dump(sess),
            "goal": _goal_dump(sess),
            "view_settings": _view_settings_dump(sess),
        }
    else:
        out["focus"] = {"error": "no session", "view_id": view_id}
    return out


def composer_dump(view_id: Optional[int] = None) -> dict:
    sess, vid = _resolve_session(view_id)
    if not sess:
        return {"error": "no session", "view_id": view_id,
                "available": list(((getattr(sublime, "_submarine_sessions", None) or getattr(sublime, "_claude_sessions", None) or {})).keys())}
    return {
        "view_id": vid,
        "session": _session_row(vid, sess),
        "composer": _composer_dump(sess),
        "view_settings": _view_settings_dump(sess),
    }


def log_tail(tail: int = 80, grep: Optional[str] = None) -> dict:
    tail = max(1, min(int(tail or 80), _MAX_EVENTS))
    st = _state()
    with st["lock"]:
        items = list(st["events"])[-tail:]
        ring_count = len(st["events"])
    if grep:
        g = grep.lower()
        items = [
            e for e in items
            if g in (e.get("msg") or "").lower()
            or g in json.dumps(e.get("fields") or {}, default=str).lower()
        ]
    bridge_tail = _tail_file(BRIDGE_LOG_PATH, 40)
    file_tail = _tail_file(_log_path, tail)
    return {
        "ring": items,
        "ring_count": ring_count,
        "log_path": _log_path,
        "log_file_tail": file_tail,
        "bridge_log_path": BRIDGE_LOG_PATH,
        "bridge_tail": bridge_tail,
    }


def dispatch(action: str, **kwargs: Any) -> Any:
    """Route a debug action (socket / MCP / ST command)."""
    # Lazy-start if package hasn't re-run plugin_loaded yet
    if not _state().get("started"):
        start()
    action = (action or "help").strip().lower()
    try:
        if action in ("ping", "status"):
            return ping()
        if action in ("help", "?"):
            return help_text()
        if action in ("sessions", "list"):
            return sessions_dump()
        if action == "snapshot":
            return snapshot(kwargs.get("view_id"))
        if action == "composer":
            return composer_dump(kwargs.get("view_id"))
        if action == "log":
            return log_tail(tail=kwargs.get("tail", 80), grep=kwargs.get("grep"))
        if action == "event":
            msg = kwargs.get("message") or kwargs.get("msg") or ""
            log(msg or "(empty)", level="agent", **{
                k: v for k, v in kwargs.items()
                if k not in ("message", "msg", "action")
            })
            return {"ok": True, "logged": msg}
        if action in ("reload", "reload_plugin"):
            mode = kwargs.get("mode") or "soft"
            return reload_plugin(mode=mode, **{
                k: v for k, v in kwargs.items() if k != "mode"
            })
        if action == "goal":
            args = kwargs.get("args") or kwargs.get("message") or kwargs.get("cmd") or "status"
            return goal_command(args=args, agent_id=kwargs.get("agent_id"))
        if action in ("capture", "ui"):
            return capture_dump(
                tail=kwargs.get("tail", 1200),
                list_lines=kwargs.get("list_lines", 24),
            )
        if action == "views":
            return {"windows": views_dump()}
        if action in ("session_list", "list_view"):
            return session_list_dump(lines=kwargs.get("list_lines", 40))
        if action in ("output", "transcript"):
            return output_dump(
                kwargs.get("view_id") or kwargs.get("agent_id"),
                tail=kwargs.get("tail", 2000),
            )
        if action == "ui_mode":
            mode = kwargs.get("mode") or kwargs.get("value") or kwargs.get("args")
            return ui_mode_command(mode)
        return {"error": f"unknown action: {action}", "help": help_text()}
    except Exception as e:
        log(f"dispatch error: {e}", level="error", action=action)
        return {"error": str(e), "traceback": traceback.format_exc()}


# ─── internals ────────────────────────────────────────────────────────────────

def _safe(v: Any) -> Any:
    try:
        json.dumps(v)
        return v
    except Exception:
        return repr(v)[:500]


def _tail_file(path: str, n: int) -> List[str]:
    try:
        if not path or not os.path.isfile(path):
            return []
        with open(path, "r", errors="replace") as f:
            lines = f.readlines()
        return [ln.rstrip("\n") for ln in lines[-n:]]
    except Exception as e:
        return [f"<read error: {e}>"]


def _resolve_session(ref=None):
    try:
        from core.registry import default_registry
        if ref is not None:
            s = default_registry.by_agent_id(str(ref))
            if s is not None:
                return s, default_registry.bound_view_id(s)
            s = default_registry.for_view_id(ref)
            if s is not None:
                return s, default_registry.bound_view_id(s)
        win = sublime.active_window()
        if win:
            v = win.active_view()
            s = default_registry.for_view(v) if v else None
            if s is not None:
                return s, default_registry.bound_view_id(s)
            aid = win.settings().get("submarine_active_agent")
            if aid:
                s = default_registry.by_agent_id(str(aid))
                if s is not None:
                    return s, default_registry.bound_view_id(s)
        working = None
        any_s = None
        for s in default_registry.iter_sessions():
            any_s = (s, default_registry.bound_view_id(s))
            if getattr(s, "working", False) and not getattr(s, "quick_mode", False):
                working = any_s
                break
        if working:
            return working
        if any_s:
            return any_s
        return None, None
    except Exception:
        return None, None


def _session_row(vid: int, s, deep: bool = False) -> dict:
    view = None
    try:
        view = s.output.view if s.output else None
    except Exception:
        view = None

    sleeping = bool(getattr(s, "is_sleeping", False))
    if not sleeping and view is not None:
        try:
            sleeping = bool(view.settings().get("submarine_sleeping")
                            or view.settings().get("claude_sleeping"))
        except Exception:
            pass
    bound = False
    try:
        from core.registry import default_registry
        bound = default_registry.bound_view_id(s) is not None
    except Exception:
        bound = bool(view and getattr(view, "is_valid", lambda: False)())
    row = {
        "agent_id": getattr(s, "agent_id", None),
        "view_id": vid,
        "name": getattr(s, "name", None),
        "backend": getattr(s, "backend", None),
        "working": bool(getattr(s, "working", False)),
        "initialized": bool(getattr(s, "initialized", False)),
        "session_id": getattr(s, "session_id", None) or getattr(s, "submarine_session_id", None),
        "quick_mode": bool(getattr(s, "quick_mode", False)),
        "sleeping": sleeping,
        "unread": bool(getattr(s, "unread", False)),
        "bound": bound,
        "detached": (not bound) and not bool(getattr(s, "quick_mode", False)),
        "query_count": getattr(s, "query_count", None),
        "parent_agent_id": getattr(s, "parent_agent_id", None),
        "composer_allowed": getattr(s, "_composer_allowed", None),
        "input_mode_entered": getattr(s, "_input_mode_entered", None),
        "client_alive": None,
        "view_valid": bool(view and view.is_valid()) if view is not None else False,
        "view_size": view.size() if view and view.is_valid() else None,
        "has_background": bool(getattr(getattr(s, "bg", None), "has_background", lambda: False)()),
        "bg_tool_ids": list(getattr(getattr(s, "bg", None), "bg_task_ids", ()) or ())[:12],
    }
    try:
        c = getattr(s, "client", None)
        if c is not None:
            row["client_alive"] = bool(getattr(c, "is_alive", lambda: None)())
    except Exception as e:
        row["client_alive"] = f"err:{e}"

    gt = getattr(s, "goal_tracker", None)
    if gt is not None:
        active = False
        try:
            if callable(getattr(gt, "is_active", None)):
                active = bool(gt.is_active())
            else:
                active = bool(getattr(gt, "active", False))
        except Exception:
            active = False
        row["goal"] = {
            "active": active,
            "phase": getattr(gt, "phase", None),
            "goal_id": getattr(gt, "goal_id", None),
            "status": getattr(gt, "status", None),
        }

    if deep:
        row["attrs_sample"] = sorted(
            a for a in dir(s)
            if not a.startswith("__") and not callable(getattr(s, a, None))
        )[:60]
    return row


def _goal_dump(s) -> dict:
    gt = getattr(s, "goal_tracker", None)
    if not gt:
        return {"present": False}
    out = {"present": True}
    for key in (
        "phase", "goal_id", "objective", "active", "paused",
        "plan_path", "status", "completed", "blocked_reason",
    ):
        if hasattr(gt, key):
            try:
                out[key] = _safe(getattr(gt, key))
            except Exception as e:
                out[key] = f"err:{e}"
    for meth in ("has_plan", "is_active", "summary", "to_dict", "state_dict"):
        fn = getattr(gt, meth, None)
        if callable(fn):
            try:
                out[meth] = _safe(fn())
            except Exception as e:
                out[meth] = f"err:{e}"
    return out


def _view_settings_dump(s) -> dict:
    try:
        view = s.output.view if s.output else None
    except Exception:
        view = None
    if not view or not view.is_valid():
        return {"error": "no view"}
    keys = (
        "submarine_output", "submarine_input_mode", "submarine_sleeping",
        "submarine_backend", "submarine_quick", "submarine_queue",
        "submarine_session_id", "submarine_name", "scroll_past_end",
        "word_wrap", "gutter",
    )
    st = view.settings()
    return {k: st.get(k) for k in keys}


def _composer_dump(s) -> dict:
    out: Dict[str, Any] = {}
    try:
        ov = s.output
    except Exception as e:
        return {"error": f"no output: {e}"}
    if not ov:
        return {"error": "output is None"}

    view = getattr(ov, "view", None)
    out["input_mode"] = bool(getattr(ov, "_input_mode", False))
    out["question_input_mode"] = bool(getattr(ov, "_question_input_mode", False))
    out["input_start"] = getattr(ov, "_input_start", None)
    out["input_area_start"] = getattr(ov, "_input_area_start", None)
    out["input_marker"] = getattr(ov, "_input_marker", INPUT_MARKER)
    out["has_pad_phantom"] = getattr(ov, "_pad_phantom_set", None) is not None
    out["has_media_phantom"] = getattr(ov, "_media_phantom_set", None) is not None
    out["render_pending"] = getattr(ov, "_render_pending", None)

    if not view or not view.is_valid():
        out["view"] = "invalid"
        return out

    size = view.size()
    out["size"] = size
    out["sel"] = [(r.a, r.b) for r in view.sel()]
    try:
        out["viewport_position"] = list(view.viewport_position())
        out["viewport_extent"] = list(view.viewport_extent())
        out["layout_extent"] = list(view.layout_extent())
        out["line_height"] = view.line_height()
    except Exception as e:
        out["geometry_error"] = str(e)

    # Last lines of buffer (composer lives at EOF)
    try:
        tail_start = view.line(max(0, size - 1)).begin()
        # back up a few lines
        for _ in range(8):
            if tail_start <= 0:
                break
            prev = view.line(max(0, tail_start - 1)).begin()
            if prev == tail_start:
                break
            tail_start = prev
        text = view.substr(sublime.Region(tail_start, size))
        out["tail_text"] = text[-800:]
        out["tail_has_marker"] = INPUT_MARKER.rstrip() in text or "◎" in text
        # count trailing blank lines after last non-empty
        lines = text.split("\n")
        trailing = 0
        for ln in reversed(lines):
            if ln.strip() == "":
                trailing += 1
            else:
                break
        out["trailing_empty_lines"] = trailing
        # find last ◎ line geometry
        marker_pt = text.rfind("◎")
        if marker_pt >= 0:
            abs_pt = tail_start + marker_pt
            out["marker_pt"] = abs_pt
            try:
                out["marker_layout"] = list(view.text_to_layout(abs_pt))
                out["eof_layout"] = list(view.text_to_layout(size))
            except Exception:
                pass
    except Exception as e:
        out["tail_error"] = str(e)

    # Regions / phantoms summary
    try:
        out["regions"] = {
            k: [(r.a, r.b) for r in view.get_regions(k)]
            for k in ("submarine_queue", "submarine_input", "submarine_context", "mark")
            if view.get_regions(k)
        }
    except Exception as e:
        out["regions_error"] = str(e)

    return out


def _windows_brief() -> List[dict]:
    rows = []
    for w in sublime.windows():
        av = w.active_view()
        rows.append({
            "id": w.id(),
            "folders": w.folders()[:4],
            "active_view_id": av.id() if av else None,
            "active_name": av.name() if av else None,
            "submarine_active_view": w.settings().get("submarine_active_view"),
            "submarine_active_agent": w.settings().get("submarine_active_agent"),
            "view_count": len(w.views()),
            "submarine_views": sum(
                1 for v in w.views() if v.settings().get("submarine_output")
            ),
        })
    return rows


def _view_brief(v) -> dict:
    st = v.settings()
    return {
        "id": v.id(),
        "name": v.name(),
        "size": v.size(),
        "file": v.file_name(),
        "output": bool(st.get("submarine_output") or st.get("claude_output")),
        "host": bool(st.get("submarine_host")) and _ui_mode_now() == "single",
        "slist": bool(st.get("submarine_session_list") or st.get("claude_session_list")),
        "quick": bool(st.get("submarine_quick") or st.get("claude_quick")),
        "backend": st.get("submarine_backend") or st.get("claude_backend"),
        "sid": (st.get("submarine_session_id") or st.get("claude_session_id") or "") or None,
        "sleeping": bool(st.get("submarine_sleeping") or st.get("claude_sleeping")),
        "input_mode": st.get("submarine_input_mode"),
        "syntax": st.get("syntax"),
    }


def views_dump() -> List[dict]:
    rows = []
    for w in sublime.windows():
        av = w.active_view()
        host_id = None
        try:
            from ui.host import is_single_mode
            single = is_single_mode()
        except Exception:
            single = False
        if single:
            for v in w.views():
                if v.settings().get("submarine_host"):
                    host_id = v.id()
                    break
        rows.append({
            "id": w.id(),
            "folders": w.folders()[:6],
            "active_view_id": av.id() if av else None,
            "active_name": av.name() if av else None,
            "active_agent": w.settings().get("submarine_active_agent"),
            "host_view_id": host_id,
            "views": [_view_brief(v) for v in w.views()],
        })
    return rows


def session_list_dump(lines: int = 40) -> dict:
    lines = max(1, min(int(lines or 40), 200))
    for w in sublime.windows():
        for v in w.views():
            st = v.settings()
            if not (st.get("submarine_session_list") or st.get("claude_session_list")):
                continue
            text = v.substr(sublime.Region(0, v.size()))
            raw = st.get("submarine_session_list_rows") or st.get("claude_session_list_rows") or "[]"
            try:
                index = json.loads(raw) if isinstance(raw, str) else (raw or [])
            except Exception:
                index = []
            sections = {}
            for r in index:
                sec = r.get("section") or "?"
                sections[sec] = sections.get(sec, 0) + 1
            preview = "\n".join(text.splitlines()[:lines])
            return {
                "present": True,
                "view_id": v.id(),
                "name": v.name(),
                "size": v.size(),
                "focused": bool(w.active_view() and w.active_view().id() == v.id()),
                "n_rows": len(index),
                "sections": sections,
                "preview": preview,
            }
    return {"present": False}


def output_dump(ref=None, tail: int = 2000) -> dict:
    tail = max(80, min(int(tail or 2000), 20000))
    sess, vid = _resolve_session(ref)
    view = None
    if sess is not None:
        try:
            view = sess.output.view if sess.output else None
        except Exception:
            view = None
    if view is None or not getattr(view, "is_valid", lambda: False)():
        for w in sublime.windows():
            av = w.active_view()
            if av and (av.settings().get("submarine_output") or av.settings().get("claude_output")):
                view = av
                break
    if view is None or not view.is_valid():
        return {"error": "no output view", "ref": ref}
    size = view.size()
    head_n = min(400, size)
    text = view.substr(sublime.Region(0, size))
    return {
        "view_id": view.id(),
        "name": view.name(),
        "size": size,
        "backend": view.settings().get("submarine_backend"),
        "host": bool(view.settings().get("submarine_host")),
        "sid": view.settings().get("submarine_session_id"),
        "head": text[:head_n],
        "tail": text[-tail:] if size else "",
        "session": _session_row(vid, sess) if sess is not None else None,
    }


def _ui_mode_now() -> str:
    try:
        from ui.host import ui_mode
        return ui_mode()
    except Exception:
        try:
            from plat.constants import SETTINGS_FILE
            return sublime.load_settings(SETTINGS_FILE).get("ui_mode", "tabs") or "tabs"
        except Exception:
            return "tabs"


def capture_dump(tail: int = 1200, list_lines: int = 24) -> dict:
    """One blob for an agent: mode, sheets, live sessions, list, host transcript."""
    try:
        from ui.host import HostView
        hv_info = []
        for w in sublime.windows():
            hv = HostView.for_window(w)
            view = getattr(hv, "_view", None)
            hv_info.append({
                "window_id": w.id(),
                "host_view_id": view.id() if view and view.is_valid() else None,
            })
    except Exception as e:
        hv_info = [{"error": str(e)}]
    host_out = None
    try:
        from ui.host import is_single_mode
        if is_single_mode():
            host_out = output_dump(tail=tail)
    except Exception:
        host_out = None
    if host_out is None:
        host_out = output_dump(tail=tail)
    return {
        "ui_mode": _ui_mode_now(),
        "ping": ping(),
        "host": hv_info,
        "windows": views_dump(),
        "sessions": sessions_dump()["sessions"],
        "session_list": session_list_dump(lines=list_lines),
        "output": host_out,
    }


def ui_mode_command(mode=None) -> dict:
    """Read or switch tabs|single (in-memory; does not write the settings file)."""
    from plat.constants import SETTINGS_FILE
    from ui.host import apply_ui_mode, ui_mode as _ui_mode
    if not mode:
        return {"ui_mode": _ui_mode(), "ok": True}
    mode = str(mode).strip().lower()
    if mode not in ("tabs", "single"):
        return {"ok": False, "error": "mode must be tabs|single", "ui_mode": _ui_mode()}
    try:
        sublime.load_settings(SETTINGS_FILE).set("ui_mode", mode)
    except Exception as e:
        return {"ok": False, "error": str(e), "ui_mode": _ui_mode()}
    for w in sublime.windows():
        try:
            apply_ui_mode(w, mode)
        except Exception:
            pass
    return {"ok": True, "ui_mode": _ui_mode(), "capture": capture_dump()}
