"""Scratch view: live + saved sessions, jump to focus or resume."""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import sublime
    import sublime_plugin
except ImportError:
    sublime = None  # type: ignore

    class _SP(object):
        EventListener = object
        WindowCommand = object
        TextCommand = object

    sublime_plugin = _SP()  # type: ignore

from . import keys
from .session_api import (
    abbrev_for,
    create_session,
    find_live_by_session_id,
    get_active_session,
    get_session_by_agent_id,
    iter_sessions,
    load_bookmarks,
    load_bookmark_records,
    load_saved_sessions,
    place_in_last_session_split,
    register_session,
    remember_active_session,
    remove_saved_session,
    rename_saved_session,
    save_bookmarks,
    sessions_map,
    unregister_view,
)


SETTING = keys.SESSION_LIST
ROWS_KEY = keys.SESSION_LIST_ROWS
WRITING_KEY = "submarine_slist_writing"
FOLLOW_GEN_KEY = "submarine_slist_follow_gen"
HISTORY_CAP = 500  # default; override with session_list_history_limit
# Full row needs ~backend(8) + title(16+) + status/time. Below this, abbrev.
COMPACT_COLS = 56
BACKEND_COL = 8  # pad/clip so deepseek (8) and grok (4) share a column
TREE_INDENT = 1
TREE_DEPTH_CAP = 6
CHILD_MARK = "↳"
# Leftmost row column: the current-session pointer. Fixed width so every row's
# state mark lines up, and so the pointer never displaces that mark.
CUR_MARK = "▸"
CUR_CELL = CUR_MARK + " "
BLANK_CELL = " " * len(CUR_CELL)
STAR_MARK = "✨"  # pinned session, before the title
_LIVE_BAND = {
    "input": 0, "error": 0, "unread": 0,
    "working": 1, "bg": 1, "ready": 1, "sleeping": 2,
}


def one_line_title(name: str, limit: int = 200) -> str:
    """Keep a list row on one line; show ↵ where the name had a newline."""
    text = (name or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", "↵")
    text = " ".join(text.split())
    return text[:limit].strip()


def _name_from_prompt(prompt: str, limit: int = 200) -> str:
    return one_line_title(prompt, limit)


def _is_truncated_name(name: str) -> bool:
    name = (name or "").strip()
    return name.endswith("...") or name.endswith("…")


def recover_title(name: str, *candidates: str) -> str:
    """Uncut a 30-char `...` name when a candidate still starts with that stem."""
    name = (name or "").strip()
    shown = one_line_title(name)
    if not _is_truncated_name(name):
        return shown or "(unnamed)"
    stem = name.rstrip(".… ").strip()
    best = shown
    for cand in candidates:
        one = _name_from_prompt(cand or "")
        if not one or not stem:
            continue
        if one.startswith(stem) and len(one) > len(best or ""):
            best = one
    return best or "(unnamed)"


def saved_title(saved: dict) -> str:
    """History-row title: stored name, or a longer first_prompt behind `...`."""
    saved = saved or {}
    return recover_title(saved.get("name") or "", saved.get("first_prompt") or "")


def _title_candidates(session) -> List[str]:
    out: List[str] = []
    fp = getattr(session, "first_prompt", None)
    if fp:
        out.append(str(fp))
    try:
        port = getattr(session, "output", None)
        convs = list(getattr(port, "conversations", None) or [])
        cur = getattr(port, "current", None)
        if cur is not None:
            convs.append(cur)
        for c in convs:
            out.append(getattr(c, "prompt", None) or "")
    except Exception:
        pass
    try:
        store = getattr(session, "store", None)
        sid = getattr(session, "session_id", None) or getattr(session, "resume_id", None)
        if store and sid:
            saved = store.find(sid)
            if saved:
                out.append(saved.get("first_prompt") or "")
                out.append(saved.get("name") or "")
    except Exception:
        pass
    return out


def _transcript_first_prompt(session) -> str:
    """First user turn from the backend jsonl. Cached via `_recovered_title`."""
    try:
        from features.resume import display_prompt, load_turns
        sid = getattr(session, "session_id", None) or getattr(session, "resume_id", None)
        if getattr(session, "fork", False):
            sid = getattr(session, "resume_id", None)
        if not sid:
            return ""
        backend = getattr(session, "backend", None) or "claude"
        cwd = getattr(session, "cwd", None) or ""
        turns = load_turns(sid, backend, cwd,
                           agent_id=getattr(session, "agent_id", None) or "")
        if turns:
            return display_prompt(turns[0].get("prompt") or "")
    except Exception:
        pass
    return ""


def session_title(session) -> str:
    """Full stored name, or first prompt if the name was the old 30-char cut."""
    name = (getattr(session, "name", None) or "").strip()
    cached = getattr(session, "_recovered_title", None)
    if isinstance(cached, str) and cached and _is_truncated_name(name):
        stem = name.rstrip(".… ").strip()
        if stem and cached.startswith(stem):
            return cached
    title = recover_title(name, *_title_candidates(session))
    if _is_truncated_name(name) and (
        _is_truncated_name(title) or title == (one_line_title(name) or "(unnamed)")
    ):
        extra = _transcript_first_prompt(session)
        if extra:
            title = recover_title(name, extra, title)
    if _is_truncated_name(name) and title:
        try:
            session._recovered_title = title
        except Exception:
            pass
    return title or "(unnamed)"


def awaiting_input(session) -> bool:
    """True while a permission, question, or plan UI is waiting on the user."""
    out = getattr(session, "output", None)
    if not out:
        return False
    for attr in ("pending_permission", "pending_question", "pending_plan"):
        req = getattr(out, attr, None)
        if req is not None and getattr(req, "callback", None):
            return True
    return False


def _has_live_bg_tools(session) -> bool:
    """⚙ Bash still running after the turn closed (Grok timeout:0 / bg)."""
    try:
        ov = getattr(session, "output", None)
        return bool(ov and ov.active_background_tools())
    except Exception:
        return False


def _status_of(session) -> str:
    # Unread outranks sleeping: a reply you have not seen still needs you,
    # whether or not the bridge was put to sleep since. Opening the sheet
    # clears it (HostView._finish_bound), not the sleep.
    if getattr(session, "unread", False) and getattr(session, "is_sleeping", False):
        return "unread"
    if getattr(session, "is_sleeping", False):
        return "sleeping"
    if awaiting_input(session):
        return "input"
    # A halted turn (bridge died, provider error, failed handshake) needs
    # you: the session is idle but nothing will happen until you look.
    # `query()` clears the flag, so the row stops shouting on the next turn.
    if getattr(session, "error_halted", False):
        return "error"
    # Kimi /compact: session/prompt returns end_turn immediately while
    # compaction continues. working can drop; _compacting is the live flag.
    if getattr(session, "working", False) or getattr(session, "_compacting", False):
        return "working"
    if _has_live_bg_tools(session):
        return "bg"
    if getattr(session, "unread", False):
        return "unread"
    return "ready"


def access_ts(obj) -> float:
    """Most recent focus or activity. `obj` is a live session or a row/saved dict."""
    if obj is None:
        return 0.0
    getter = obj.get if isinstance(obj, dict) else lambda k, d=0: getattr(obj, k, d)
    try:
        acc = float(getter("last_access", 0) or 0)
    except (TypeError, ValueError):
        acc = 0.0
    if acc > 0:
        return acc
    try:
        return float(getter("last_activity", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _section_sort_key(row: dict) -> Tuple[int, float]:
    """CURRENT: status band then recency. HISTORY: recency only."""
    band = 0
    if row.get("kind") == "live":
        band = _LIVE_BAND.get(row.get("status") or "", 1)
    return (band, -access_ts(row))


def _mark(status: str) -> str:
    return {
        "input": "?",
        "error": "✘",
        "unread": "!",
        "working": "●",
        "bg": "⚙",
        "sleeping": "⏸",
        "ready": "○",
    }.get(status, "·")


def backend_abbrev(backend: str) -> str:
    return abbrev_for(backend)


def backend_cell(backend: str) -> str:
    """Fixed-width backend label for wide list rows (`deepseek` is 8)."""
    be = (backend or "claude").strip() or "claude"
    if len(be) > BACKEND_COL:
        be = be[:BACKEND_COL]
    return f"{be:<{BACKEND_COL}}"


def view_cols(view, fallback: int = 80) -> int:
    """Columns in the text box (viewport minus ST's own left/right margin)."""
    if not view:
        return fallback
    try:
        vw = float(view.viewport_extent()[0])
        em = float(view.em_width() or 0)
        if em <= 0 or vw < 8:
            return fallback
        try:
            margin = float(view.settings().get("margin") or 0)
        except Exception:
            margin = 0
        usable = max(em, vw - 2 * margin)
        # One column of slack for gutter/scrollbar / wide-glyph overflow.
        return max(24, int(usable / em) - 1)
    except Exception:
        return fallback


def use_compact(cols: int) -> bool:
    return 0 < cols < COMPACT_COLS


def format_header(cols: int = 0) -> str:
    left = "SESSIONS"
    right = "enter open · v reveal · t tear · f fork · s star · r rename · del close"
    if not cols:
        return f"{left}                  {right}"
    for cand in (
        right,
        "enter open · v reveal · f fork · s star · r rename · del close",
        "↵ open · v · t · f · s · r · del",
        "v · f · s · r · del",
        "f · s · r · del",
        "s · r · del",
        "r · del",
        "r rename",
    ):
        if cols >= len(left) + 1 + len(cand):
            right = cand
            break
    else:
        right = ""
    if not right:
        return left[:cols]
    gap = max(1, cols - len(left) - len(right))
    line = f"{left}{' ' * gap}{right}"
    return line[:cols] if len(line) > cols else line





# Ranges whose glyphs take two cells. Kept narrow on purpose: the marks this
# list draws (`△` `·` `⊡` `⚙` `⏸` `↳`) are all one cell — measured in-editor —
# while the pin ✨ and CJK/emoji titles are not.
_WIDE_RANGES = (
    (0x1100, 0x115F), (0x2E80, 0x303E), (0x3041, 0x33FF), (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF), (0xA000, 0xA4CF), (0xAC00, 0xD7A3), (0xF900, 0xFAFF),
    (0xFE30, 0xFE6F), (0xFF00, 0xFF60), (0xFFE0, 0xFFE6), (0x2728, 0x2728),
    (0x1F300, 0x1FAFF),
)


def cell_width(text: str) -> int:
    """Columns `text` occupies, counting wide glyphs as two.

    The row layout is a grid of columns, so a title or a pin that renders two
    cells wide has to be measured that way or the meta after it lands short.
    """
    text = text or ""
    try:
        from wcwidth import wcswidth  # the terminal port already requires it
        n = wcswidth(text)
        if n >= 0:
            return n
    except Exception:
        pass
    total = 0
    for ch in text:
        cp = ord(ch)
        total += 2 if any(lo <= cp <= hi for lo, hi in _WIDE_RANGES) else 1
    return total


def fit_title(name: str, width: int) -> str:
    """Pad or ellipsize a session title to exactly `width` columns."""
    name = name or ""
    if width <= 0:
        return ""
    if cell_width(name) <= width:
        return name + (" " * (width - cell_width(name)))
    keep = []
    used = 0
    for ch in name:
        w = cell_width(ch)
        if used + w > max(0, width - 1):
            break
        keep.append(ch)
        used += w
    return "".join(keep) + "…"


def _name_budget(prefix: str, extra: str, cols: int, compact: bool) -> int:
    if cols <= 0:
        return 48 if compact else 40
    return max(8, cols - cell_width(prefix) - cell_width(extra))


def format_when(ts, now=None) -> str:
    """Elapsed since last access: now / <1m / 5m / 3h / 2d / 4w."""
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return ""
    try:
        now_t = time.time() if now is None else float(now)
    except (TypeError, ValueError):
        now_t = time.time()
    sec = max(0, int(now_t - t))
    if sec < 5:
        return "now"
    # No per-second stamp — a 900ms poll + undo stack was leaking GBs.
    if sec < 60:
        return "<1m"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    if sec < 86400 * 14:
        return f"{sec // 86400}d"
    return f"{sec // (86400 * 7)}w"


def _stamp_change_in(ts, now) -> Optional[int]:
    """Seconds until `format_when(ts)` renders a different label.

    Mirrors its thresholds: the elapsed-time column is the one part of a row
    that changes with the clock alone, so this is what schedules the next poll.
    """
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return None
    if t <= 0:
        return None
    try:
        sec = max(0, int(float(now) - t))
    except (TypeError, ValueError):
        return None
    if sec < 5:
        return 5 - sec
    if sec < 60:
        return 60 - sec
    if sec < 3600:
        return 60 - (sec % 60)
    if sec < 86400:
        return 3600 - (sec % 3600)
    if sec < 86400 * 14:
        return 86400 - (sec % 86400)
    return 604800 - (sec % 604800)


def _next_stamp_change(index: List[dict], now=None) -> float:
    """Wall clock when the soonest rendered elapsed time changes label.

    0.0 when no row shows one (nothing to wait for).
    """
    now = time.time() if now is None else float(now)
    soonest = None
    for r in index or ():
        if r.get("kind") == "live" and (r.get("status") or "") in _STAMP:
            continue  # live rows show a state word, not a clock
        secs = _stamp_change_in(r.get("last_access") or r.get("last_activity"), now)
        if secs is None:
            continue
        soonest = secs if soonest is None else min(soonest, secs)
    return (now + soonest) if soonest is not None else 0.0


def window_project(window) -> str:
    """This window's project root (first folder). Empty if none."""
    try:
        folders = window.folders() if window else None
    except Exception:
        folders = None
    if folders:
        return (folders[0] or "").rstrip("/")
    return ""


def session_project(session) -> str:
    return window_project(getattr(session, "window", None))


def belongs_to_window(session, window) -> bool:
    """True when the session is this window's project (or this window if none)."""
    want = window_project(window)
    got = session_project(session)
    if want:
        if got:
            return got == want
        return getattr(session, "window", None) == window
    return getattr(session, "window", None) == window


def collect_live(window) -> List[dict]:
    out = []
    try:
        live_sessions = list(iter_sessions())
    except Exception:
        live_sessions = []
    if not live_sessions:
        live_sessions = list(sessions_map().values())
    for s in live_sessions:
        if getattr(s, "quick_mode", False):
            continue
        if not belongs_to_window(s, window):
            continue
        try:
            view = s.output.view if s.output else None
            view_ok = bool(view and view.is_valid())
            view_id = view.id() if view_ok else None
        except Exception:
            view_ok = False
            view_id = None
        torn_off = bool(getattr(s, "torn_off", False))
        bound = False
        try:
            from ui.host import is_single_mode
            if is_single_mode() and not torn_off:
                from core.registry import default_registry
                bvid = default_registry.bound_view_id(s)
                bound = bool(view_ok and bvid is not None and bvid == view_id)
        except Exception:
            bound = False
        out.append({
            "kind": "live",
            "session_id": getattr(s, "session_id", None),
            "agent_id": getattr(s, "agent_id", None),
            "parent_agent_id": getattr(s, "parent_agent_id", None),
            "view_id": view_id,
            "name": session_title(s),
            "backend": getattr(s, "backend", None) or "claude",
            "model": getattr(s, "model", None),
            "status": _status_of(s),
            "query_count": int(getattr(s, "query_count", 0) or 0),
            "same_window": True,
            "last_access": access_ts(s),
            "last_activity": float(getattr(s, "last_activity", 0) or 0),
            "bound": bound,
            "torn_off": torn_off,
        })
    # Input wait first, then awake, then sleeping; access time within each band.
    out.sort(key=_section_sort_key)
    return out


def _recency(row: dict) -> tuple:
    """Sort key for "which incarnation of a session is the current one"."""
    try:
        activity = float(row.get("last_activity") or 0)
    except (TypeError, ValueError):
        activity = 0.0
    try:
        seen = float(access_ts(row) or 0)
    except (TypeError, ValueError):
        seen = 0.0
    return (activity, seen)


def _chain_ids_of(agent_id: str, saved: List[dict],
                  own_sid: str = "") -> List[str]:
    """Ids of every incarnation of a session, from its saved records."""
    ids = [s.get("session_id") for s in saved or ()
           if agent_id and (s.get("agent_id") or "") == agent_id
           and s.get("session_id")]
    if own_sid and own_sid not in ids:
        ids.append(own_sid)
    return ids


def tag_live_chains(live: List[dict], saved: List[dict]) -> None:
    """Give a live row the ids of every incarnation of its session.

    A live row renders from the registry, which knows only the id the backend
    is on now; the pin and the delete commands need the rest of the chain (see
    `collapse_chains`).
    """
    for r in live or ():
        ids = _chain_ids_of(r.get("agent_id") or "", saved, r.get("session_id") or "")
        if len(ids) > 1:
            r["chain_ids"] = ids


def collapse_chains(rows: List[dict], starred: Optional[set] = None,
                    live_agents: Optional[set] = None) -> List[dict]:
    """One row per session: a resume is not a new session.

    Every resume mints a new session id, and switching backend keeps the same
    `agent_id`, so the store holds one record per incarnation — HISTORY listed
    the same session again and again, soonest with another backend's tag.
    Records sharing an `agent_id` are one session: keep its newest incarnation
    (the one a resume should target) and carry the chain's ids so a pin and a
    delete still reach every incarnation.
    """
    starred = set(starred or ())
    live_agents = set(live_agents or ())
    chains: Dict[str, List[dict]] = {}
    out: List[dict] = []
    for r in rows or ():
        aid = r.get("agent_id")
        if not aid:
            out.append(r)  # nothing to chain it with
            continue
        if aid in live_agents:
            continue  # a sibling is live: the session sits in CURRENT
        chains.setdefault(aid, []).append(r)
    for members in chains.values():
        if len(members) == 1:
            out.append(members[0])
            continue
        ids = [m["session_id"] for m in members if m.get("session_id")]
        row = dict(max(members, key=_recency))
        row["chain_ids"] = ids
        if any(sid in starred for sid in ids):
            row["pinned"] = True
        out.append(row)
    return out


_PERSISTED_CURRENT_STATES = ("open", "sleeping")


def collect_persisted_current(window, live_ids=None, live_agents=None,
                              saved=None) -> List[dict]:
    """Saved open/sleeping rows that are not live yet — still CURRENT.

    After a restart the registry only has sheets Sublime restored. Everyone
    else who was CURRENT is in `.sessions.json` with state open/sleeping.
    """
    live_ids = set(live_ids or ())
    live_agents = set(live_agents or ())
    cwd = window_project(window)
    if saved is None:
        saved = load_saved_sessions()
    out = []
    for s in saved or ():
        sid = s.get("session_id")
        if not sid or sid in live_ids:
            continue
        aid = s.get("agent_id")
        if aid and aid in live_agents:
            continue
        state = (s.get("state") or "closed").lower()
        if state not in _PERSISTED_CURRENT_STATES:
            continue
        try:
            q = int(s.get("query_count") or 0)
        except (TypeError, ValueError):
            q = 0
        if q <= 0 and not str(s.get("first_prompt") or "").strip():
            continue
        proj = (s.get("project") or "").rstrip("/")
        if cwd and proj and proj != cwd:
            continue
        out.append({
            "kind": "live",
            "session_id": sid,
            "agent_id": aid,
            "parent_agent_id": s.get("parent_agent_id"),
            "view_id": None,
            "name": saved_title(s),
            "backend": s.get("backend") or "claude",
            "model": s.get("model"),
            "status": "sleeping",
            "query_count": q,
            "same_window": True,
            "last_activity": s.get("last_activity"),
            "last_access": access_ts(s),
            "bound": False,
            "torn_off": False,
        })
    return out


def collect_history(live_ids: set, cwd: str,
                    live_agents: Optional[set] = None,
                    saved: Optional[List[dict]] = None
                    ) -> Tuple[List[dict], List[dict]]:
    here, other = [], []
    cwd = (cwd or "").rstrip("/")
    live_agents = set(live_agents or ())
    if saved is None:
        saved = load_saved_sessions()
    for s in saved:
        sid = s.get("session_id")
        if not sid or sid in live_ids:
            continue
        if s.get("agent_id") and s.get("agent_id") in live_agents:
            continue  # another incarnation of a live session: not history
        row = {
            "kind": "saved",
            "session_id": sid,
            "agent_id": s.get("agent_id"),
            "parent_agent_id": s.get("parent_agent_id"),
            "view_id": None,
            "name": saved_title(s),
            "backend": s.get("backend") or "claude",
            "model": s.get("model"),
            "status": s.get("state") or "closed",
            "query_count": int(s.get("query_count") or 0),
            "project": s.get("project") or "",
            "last_activity": s.get("last_activity"),
            "last_access": access_ts(s),
        }
        proj = (row["project"] or "").rstrip("/")
        if cwd and proj == cwd:
            here.append(row)
        else:
            other.append(row)
    here.sort(key=access_ts, reverse=True)
    other.sort(key=access_ts, reverse=True)
    cap = history_cap()
    return here[:cap], other[:cap]


def history_cap() -> int:
    try:
        if sublime is None:
            return HISTORY_CAP
        from plat.constants import SETTINGS_FILE
        n = sublime.load_settings(SETTINGS_FILE).get(
            "session_list_history_limit", HISTORY_CAP)
        n = int(n)
        if n > 0:
            return n
    except Exception:
        pass
    return HISTORY_CAP


# 4 letters so the state column is a fixed width.
_STAMP = {
    "working": "busy",
    "bg": "busy",
    "ready": "idle",
    "input": "wait",
    "unread": "new",
    "error": "err",
}
_GAP = 2   # after Nq, before state — must be > 1 (the space before Nq)
_STAMP_W = 4


def _stamp_of(r: dict) -> str:
    live = r.get("kind") == "live"
    status = r.get("status") or ""
    if live and status in _STAMP:
        return _STAMP[status]
    return format_when(r.get("last_access") or r.get("last_activity"))


def is_empty_session_row(r: dict) -> bool:
    """HISTORY-only: never sent a turn. Live CURRENT still lists these.

    query_count <= 0 is the check. Do not use this to decide whether the
    host sheet can close — unused live sessions are still handoff targets.
    """
    try:
        return int((r or {}).get("query_count") or 0) <= 0
    except (TypeError, ValueError):
        return True


def _pinned(row: dict, starred: Optional[set] = None) -> bool:
    """Is this row's session pinned?

    A row stands for a whole chain of incarnations (`chain_ids`), so a star
    left on whichever one the user pinned keeps showing — and unstarring the
    row clears the chain instead of leaving the pin behind.
    """
    if not row:
        return False
    if row.get("pinned"):
        return True
    starred = starred or ()
    if not starred:
        return False
    sid = row.get("session_id")
    if sid and sid in starred:
        return True
    return any(x in starred for x in (row.get("chain_ids") or ()))


def row_ids(row: dict) -> List[str]:
    """Every session id this row stands for (a chain's ids, newest last)."""
    ids = [sid for sid in (row.get("chain_ids") or []) if sid]
    sid = row.get("session_id")
    if sid and sid not in ids:
        ids.append(sid)
    return ids


def drop_empty_sessions(rows: List[dict], starred: Optional[set] = None) -> List[dict]:
    out = []
    for r in rows or []:
        if is_empty_session_row(r) and not _pinned(r, starred):
            continue
        out.append(r)
    return out


def _q_col(r: dict) -> str:
    try:
        n = int(r.get("query_count") or 0)
    except (TypeError, ValueError):
        n = 0
    return f" {n}q" if n else ""


def _right_meta(r: dict) -> str:
    """` 12q` (if any) + 2 spaces + state. No blank Nq column — that
    stole title width and left a hole (the Libr... 1q        1h shot).
    """
    stamp = _stamp_of(r) or ""
    if len(stamp) < _STAMP_W:
        stamp = f"{stamp:>{_STAMP_W}}"
    return _q_col(r) + (" " * _GAP) + stamp


def pin_starred(rows: List[dict], starred: set) -> List[dict]:
    """Starred rows first within a group; relative order otherwise."""
    if not starred:
        return list(rows or ())
    pinned, rest = [], []
    for r in rows or []:
        (pinned if _pinned(r, starred) else rest).append(r)
    return pinned + rest


def tree_prefix(depth: int) -> str:
    """Indent + child glyph. Empty for roots. Visual depth is capped."""
    try:
        d = int(depth or 0)
    except (TypeError, ValueError):
        d = 0
    if d <= 0:
        return ""
    vis = d if d < TREE_DEPTH_CAP else TREE_DEPTH_CAP
    return (" " * (TREE_INDENT * vis)) + CHILD_MARK


def tree_order(rows: List[dict], starred: Optional[set] = None) -> List[dict]:
    """Forest-order a section; attach ``depth`` on each copied row.

    Parent link is ``parent_agent_id``. A child whose parent is missing from
    this section (or whose parent link would cycle) is a root.

    Roots and siblings sort by ``_section_sort_key``. Each root is followed
    by its subtree, depth-first; a tree stays contiguous.

    Starred pinning: a starred node with no starred ancestor pins to the
    section top as a depth-0 root and carries its subtree. A starred child
    whose parent tree is not pinned therefore loses indent. A starred root
    carries its whole subtree (starred children stay indented under it).
    """
    src = list(rows or [])
    n = len(src)
    if n == 0:
        return []
    ids = set(starred or ())

    by_aid = {}  # type: Dict[str, int]
    for i, r in enumerate(src):
        aid = r.get("agent_id")
        if aid and aid not in by_aid:
            by_aid[aid] = i

    kids = [[] for _ in range(n)]  # type: List[List[int]]
    parent_of = [None] * n  # type: List[Optional[int]]
    for i, r in enumerate(src):
        paid = r.get("parent_agent_id")
        if not paid:
            continue
        p = by_aid.get(paid)
        if p is None or p == i:
            continue
        seen = {i}
        cur = p  # type: Optional[int]
        cyclic = False
        while cur is not None:
            if cur in seen:
                cyclic = True
                break
            seen.add(cur)
            pp = src[cur].get("parent_agent_id")
            # A row that names itself as its parent (a stale host-view stamp
            # once did that) is a root, not a cycle: its children still nest.
            if pp and pp == src[cur].get("agent_id"):
                pp = None
            cur = by_aid.get(pp) if pp else None
        if cyclic:
            continue
        kids[p].append(i)
        parent_of[i] = p

    for i in range(n):
        kids[i].sort(key=lambda j, _src=src: _section_sort_key(_src[j]))

    def has_starred_ancestor(i: int) -> bool:
        cur = parent_of[i]
        seen = set()  # type: set
        while cur is not None and cur not in seen:
            if _pinned(src[cur], ids):
                return True
            seen.add(cur)
            cur = parent_of[cur]
        return False

    leaders = [
        i for i in range(n)
        if _pinned(src[i], ids) and not has_starred_ancestor(i)
    ]
    leaders.sort(key=lambda i: _section_sort_key(src[i]))
    leader_set = set(leaders)
    for i in leaders:
        p = parent_of[i]
        if p is not None:
            kids[p] = [c for c in kids[p] if c != i]
            parent_of[i] = None

    rest_roots = [
        i for i in range(n)
        if parent_of[i] is None and i not in leader_set
    ]
    rest_roots.sort(key=lambda i: _section_sort_key(src[i]))
    ordered_roots = leaders + rest_roots

    out = []  # type: List[dict]
    visited = set()  # type: set

    def walk(i: int, depth: int) -> None:
        if i in visited:
            return
        visited.add(i)
        rec = dict(src[i])
        rec["depth"] = depth
        out.append(rec)
        for c in kids[i]:
            walk(c, depth + 1)

    for ridx in ordered_roots:
        walk(ridx, 0)
    leftover = [i for i in range(n) if i not in visited]
    leftover.sort(key=lambda i: _section_sort_key(src[i]))
    for i in leftover:
        walk(i, 0)
    return out


def _fmt_row(r: dict, starred: set, compact: bool = False, cols: int = 0) -> str:
    live = r.get("kind") == "live"
    name = one_line_title(r.get("name") or "")
    mark = _mark(r["status"]) if live else "·"
    if r.get("torn_off"):
        mark = "⊡"
    # The current-session column is the row's leftmost cell, and only live rows —
    # the CURRENT section, where a current session can be — carry it: a history
    # row never holds a cell open for a glyph it cannot show.
    cur = (CUR_CELL if r.get("bound") else BLANK_CELL) if live else ""
    star = STAR_MARK + " " if _pinned(r, starred) else ""
    tree = tree_prefix(r.get("depth") or 0)
    if compact:
        pre = f"{cur}{mark} {tree}{backend_abbrev(r.get('backend'))} "
        return pre + star + fit_title(
            name, _name_budget(pre + star, "", cols, True))
    pre = f"{cur}{mark} {tree}{backend_cell(r.get('backend'))} "
    extra = _right_meta(r)
    return (
        pre + star
        + fit_title(name, _name_budget(pre + star, extra, cols, False))
        + extra
    )


def render_list(live: List[dict], here: List[dict], other: List[dict],
                starred: Optional[set] = None,
                cols: int = 0) -> Tuple[str, List[dict]]:
    starred = starred or set()
    compact = use_compact(cols)
    lines = [
        format_header(cols),
        "",
    ]
    index: List[dict] = []

    def add_section(title: str, rows: List[dict], fmt):
        lines.append(f"{title} ({len(rows)})")
        if not rows:
            lines.append("  (none)")
            lines.append("")
            return
        for r in rows:
            lines.append(fmt(r, starred, compact, cols))
            rec = dict(r)
            rec["line"] = len(lines)  # 1-based
            rec["section"] = title
            index.append(rec)
        lines.append("")

    add_section("CURRENT", tree_order(live, starred), _fmt_row)
    add_section("HISTORY", tree_order(here, starred), _fmt_row)
    return "\n".join(lines).rstrip() + "\n", index


def build_for_window(window, cols: int = 0) -> Tuple[str, List[dict]]:
    cwd = ""
    if window and window.folders():
        cwd = window.folders()[0]
    starred = load_bookmarks(cwd or None)
    # Unused live sheets stay in CURRENT so you can switch away; close drops
    # them. Empty HISTORY rows are still omitted (except starred).
    live = collect_live(window)
    live_ids = {r["session_id"] for r in live if r.get("session_id")}
    live_agents = {r.get("agent_id") for r in live if r.get("agent_id")}
    saved = load_saved_sessions()
    persisted = collect_persisted_current(
        window, live_ids, live_agents, saved=saved)
    if persisted:
        live = list(live) + persisted
        live_ids |= {r["session_id"] for r in persisted if r.get("session_id")}
        live_agents |= {r.get("agent_id") for r in persisted if r.get("agent_id")}
    if saved:
        # Only pins need this: a live row's other incarnations matter to a star.
        tag_live_chains(live, saved)
    here, _other = collect_history(live_ids, cwd, live_agents=live_agents,
                                   saved=saved)
    here = collapse_chains(here, starred, live_agents)
    have = set(live_ids)
    for r in here:
        have.update(row_ids(r))
    here = _include_starred_saved(here, have, cwd, starred)
    here = drop_empty_sessions(here, starred)
    return render_list(live, here, [], starred, cols=cols)


def _include_starred_saved(here: List[dict], live_ids: set, cwd: str,
                           starred: set) -> List[dict]:
    """History cap / sessions.json prune can drop a starred row — put it back.

    Starred ids belong to this window's bookmarks, so they list even when the
    saved `project` path differs (symlink cwd) or the disk cap already deleted
    the resume row. Missing metadata becomes a stub so the star still shows.
    """
    if not starred:
        return here
    have = {r.get("session_id") for r in here}
    saved_by = {}
    for s in load_saved_sessions():
        sid = (s or {}).get("session_id")
        if sid:
            saved_by[sid] = s
    records = {}
    try:
        records = load_bookmark_records(cwd or None) or {}
    except Exception:
        records = {}
    extra = []
    for sid in starred:
        if not sid or sid in live_ids or sid in have:
            continue
        s = saved_by.get(sid) or records.get(sid) or {}
        if not isinstance(s, dict):
            s = {}
        try:
            q = int(s.get("query_count") or 0)
        except (TypeError, ValueError):
            q = 0
        extra.append({
            "kind": "saved",
            "session_id": sid,
            "agent_id": s.get("agent_id"),
            "parent_agent_id": s.get("parent_agent_id"),
            "view_id": None,
            "name": saved_title(s) if saved_by.get(sid) else (s.get("name") or sid),
            "backend": s.get("backend") or "claude",
            "model": s.get("model"),
            "status": s.get("state") or "closed",
            "query_count": q,
            "project": s.get("project") or cwd or "",
            "last_activity": s.get("last_activity"),
            "last_access": access_ts(s),
        })
        have.add(sid)
    return here + extra if extra else here


def row_at_line(index: List[dict], line: int) -> Optional[dict]:
    for r in index:
        if r.get("line") == line:
            return r
    return None


def focus_live(window, row: dict) -> bool:
    aid = row.get("agent_id")
    vid = row.get("view_id")
    sid = row.get("session_id")
    sessions = sessions_map()
    session = None
    if aid:
        session = get_session_by_agent_id(aid)
    if session is None and vid is not None and vid in sessions:
        session = sessions[vid]
    if session is None and sid:
        try:
            session = find_live_by_session_id(sid)
        except Exception:
            session = None
        if session is None:
            for s in sessions.values():
                if getattr(s, "session_id", None) == sid:
                    session = s
                    break
    if session is None:
        return False
    view = session.output.view if session.output else None
    if not view or not view.is_valid():
        return reveal_live_session(window, session)
    win = view.window() or window
    win.focus_view(view)
    try:
        remember_active_session(win, view)
    except Exception:
        from .keys import ACTIVE_AGENT, write_setting
        aid = getattr(session, "agent_id", None)
        if aid:
            write_setting(win.settings(), ACTIVE_AGENT, aid)
    reveal_session_bottom(session)
    return True


def reveal_live_session(window, session, focus: bool = True,
                        force_sheet: bool = False) -> bool:
    """Show a live session, reattaching a sheet if it was backgrounded."""
    if not session or not window:
        return False
    if getattr(session, "torn_off", False):
        force_sheet = True
    view = session.output.view if session.output else None
    if view and view.is_valid():
        win = view.window() or window
        try:
            av = win.active_view() if win else None
            if av is not None and av.id() == view.id():
                return True
        except Exception:
            pass
        try:
            from ui import idle
            idle.clear(view)   # the sheet belongs to this session again
        except Exception:
            pass
        prev = None if focus else win.active_view()
        win.focus_view(view)
        if focus:
            try:
                remember_active_session(win, view)
            except Exception:
                pass
        elif prev and prev.is_valid() and prev.id() != view.id():
            win.focus_view(prev)
        reveal_session_bottom(session)
        return True
    session.window = window
    if session.output:
        session.output.window = window
        session.output.view = None
    try:
        session.reset_phantoms_for_new_view()
    except Exception:
        pass
    if not session.output:
        return False
    session.output.show(focus=focus, create=force_sheet)
    view = session.output.view
    if not view or not view.is_valid():
        return False
    try:
        place_in_last_session_split(window, view)
        if focus:
            window.focus_view(view)
            remember_active_session(window, view)
    except Exception:
        pass
    try:
        register_session(session)
    except Exception:
        pass
    try:
        if session.backend and session.backend != "claude":
            from backend.specs import get as _get
            spec = _get(session.backend)
            from .keys import BACKEND, write_setting
            write_setting(view.settings(), BACKEND, session.backend)
            if getattr(spec, "theme", None):
                view.settings().set("color_scheme", spec.theme)
    except Exception:
        pass
    try:
        session.output.set_name(session.display_name)
    except Exception:
        pass
    reveal_session_bottom(session)
    if focus and not session.working:
        try:
            session._enter_input_with_draft()
        except Exception:
            pass
    return True


def reveal_session_bottom(session) -> None:
    """Scroll to ◎ / EOF and put the caret there. Never wake a sleeping
    session.

    The caret matters: the renderer follows the tail only while the caret
    owner is the draft, so a reveal that left the caret in history (where a
    click or a search had put it) showed a sheet that then stopped scrolling
    with the reply. Coming here from the list or a shortcut means "show me
    the live end".
    """
    out = getattr(session, "output", None)
    view = out.view if out else None
    if not view or not view.is_valid():
        return
    try:
        out.set_caret_owner("draft")
    except Exception:
        pass
    try:
        if out.is_input_mode():
            out.scroll_composer_chrome(force=True)
            try:
                out.restore_draft_caret(force=True)
            except Exception:
                pass
            try:
                # No draft caret to restore: park at the end of the composer.
                if not list(view.sel()) and sublime is not None:
                    end = view.size()
                    view.sel().add(sublime.Region(end, end))
            except Exception:
                pass
            return
    except Exception:
        pass
    try:
        end = view.size()
        if sublime is not None:
            view.sel().clear()
            view.sel().add(sublime.Region(end, end))
        view.show(sublime.Region(end, end), False)
        _x, y = view.text_to_layout(end)
        vh = float(view.viewport_extent()[1])
        vx, _vy = view.viewport_position()
        view.set_viewport_position((float(vx), max(0.0, float(y) - vh + 24.0)), False)
    except Exception:
        try:
            view.show(view.size())
        except Exception:
            pass


def resume_saved(window, row: dict, focus: bool = True) -> bool:
    sid = row.get("session_id")
    if not sid:
        return False
    try:
        live = find_live_by_session_id(sid)
        if live and reveal_live_session(window, live, focus=focus):
            return True
    except Exception:
        pass
    backend = row.get("backend") or "claude"
    s = create_session(window, resume_id=sid, backend=backend, focus=focus)
    name = row.get("name")
    if s and name and name != "(unnamed)":
        s.name = name
        if s.output:
            s.output.set_name(name)
    if s:
        reveal_in_list(window, s)
    return bool(s)


def reveal_in_list(window, session) -> None:
    """Point this window's Sessions list at a session that just became live
    (a history row woke): caret and scroll follow it into CURRENT, focus
    stays where it is."""
    aid = getattr(session, "agent_id", None)
    if window is not None and aid:
        _point_list_at(window, aid)


_last_open = (0.0, None)


def _wake_if_sleeping(session) -> None:
    if session is None or not getattr(session, "is_sleeping", False):
        return
    wake = getattr(session, "wake", None)
    if callable(wake):
        wake()


def open_row(window, row: dict) -> bool:
    if not row:
        return False
    global _last_open
    key = (row.get("session_id"), row.get("agent_id") or row.get("view_id"), row.get("kind"))
    now = time.time()
    if key == _last_open[1] and (now - _last_open[0]) < 0.4:
        return True
    _last_open = (now, key)
    try:
        from ui.host import HostView, is_single_mode
        if is_single_mode():
            if row.get("kind") == "live":
                session = _live_session_for_row(row)
                if session is not None:
                    if getattr(session, "torn_off", False) or row.get("torn_off"):
                        _wake_if_sleeping(session)
                        ok = focus_live(window, row)
                        focus_sheet_soon(window, session)
                        return ok
                    hv = HostView.for_window(window)
                    if hv.bound_session(window) is session:
                        _wake_if_sleeping(session)
                        ok = focus_live(window, row)
                        focus_sheet_soon(window, session)
                        return ok
                    ok = bool(hv.attach(window, session, focus=True))
                    _wake_if_sleeping(session)
                    focus_sheet_soon(window, session)
                    return ok
            return resume_saved(window, row)
    except Exception:
        pass
    if row.get("kind") == "live":
        session = _live_session_for_row(row)
        _wake_if_sleeping(session)
        if focus_live(window, row):
            focus_sheet_soon(window, session)
            return True
    return resume_saved(window, row)


def focus_agent(window, agent_id) -> bool:
    """Show the session an agent id names (Cmd+click on an id in a sheet).

    Live: open it as the list would, in its own window, brought forward. Not
    live: resume it from the saved record (an old alias counts too).
    """
    try:
        from core.agent_ids import PREFIX, canon_agent_id
    except Exception:
        return False
    aid = canon_agent_id(agent_id)
    if not isinstance(aid, str) or not aid.startswith(PREFIX):
        return False
    live = get_session_by_agent_id(aid)
    if live is not None:
        target = getattr(live, "window", None)
        try:
            if target is None or not target.is_valid():
                target = window
        except Exception:
            target = window
        if target is not window:
            try:
                target.bring_to_front()
            except Exception:
                pass
        ok = open_row(target, {"kind": "live", "agent_id": aid,
                               "session_id": getattr(live, "session_id", None)})
        _point_list_at(target, aid)
        return ok
    try:
        from core.agent_ids import canon_agent_ids
        for rec in load_saved_sessions() or []:
            if aid == rec.get("agent_id") or aid in canon_agent_ids(rec.get("agent_id_aliases")):
                return resume_saved(window, rec)
    except Exception:
        pass
    sublime.status_message("Submarine: no session %s" % aid)
    return False


_POINT_RETRY_MS = (150, 400, 900, 1600)


def _point_list_at(window, agent_id, attempt=0) -> None:
    """The window's Sessions list caret onto the session just jumped to.

    A resumed session is only a CURRENT row after the list's next refresh,
    so a miss retries a few times; the first hit stops it.
    """
    session = get_session_by_agent_id(agent_id)
    if session is not None and sync_list_to_session(window, session):
        return
    if attempt < len(_POINT_RETRY_MS) and sublime is not None:
        sublime.set_timeout(
            lambda: _point_list_at(window, agent_id, attempt + 1),
            _POINT_RETRY_MS[attempt])


def _same_view(a, b) -> bool:
    if a is None or b is None:
        return False
    if a is b:
        return True
    try:
        return a.id() == b.id()
    except Exception:
        return False


def follow_current_under_caret(view, force: bool = False) -> bool:
    """Caret on a CURRENT live row → show that session; keep list focus.

    ``force`` is for click-into / on_activated: the list may not be
    ``active_view`` yet, but the caret line still names the session to show.
    """
    if not view or not getattr(view, "is_valid", lambda: False)():
        return False
    try:
        if view.settings().get(WRITING_KEY):
            return False
    except Exception:
        return False
    win = view.window()
    if not win:
        return False
    if not force and not _same_view(win.active_view(), view):
        return False
    if not view.sel():
        return False
    try:
        line = view.rowcol(view.sel()[0].begin())[0] + 1
        raw = view.settings().get(ROWS_KEY) or "[]"
        index = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except Exception:
        return False
    row = row_at_line(index, line)
    if not row or row.get("kind") != "live":
        return False
    return reveal_row(win, row, keep=view)


def focus_sheet_soon(window, session, delay_ms: int = 60) -> None:
    """Enter in the list: keyboard focus moves to the session's sheet, caret
    in the composer, tail in view — after every deferred attach hook (stamps,
    chrome, composer plant at set_timeout(0)) and the list's own refresh
    have run, so nothing can take the focus back or leave the view focused
    but the caret parked outside the composer (keys then did nothing until
    a click).
    """
    if session is None:
        return

    def _go():
        out = getattr(session, "output", None)
        view = getattr(out, "view", None) if out is not None else None
        if view is None:
            return
        try:
            if not view.is_valid():
                return
        except Exception:
            return
        win = None
        try:
            win = view.window() or window
        except Exception:
            win = window
        if win is None:
            return
        try:
            win.focus_view(view)
        except Exception:
            pass
        try:
            out.set_caret_owner("draft")
        except Exception:
            pass
        if not getattr(session, "is_sleeping", False):
            try:
                session._enter_input_with_draft()
            except Exception:
                pass
        try:
            if out.is_input_mode():
                out.focus_composer(force_show=True, steal_focus=True, park_at_end=True)
                try:
                    if not list(view.sel()) and sublime is not None:
                        end = view.size()
                        view.sel().add(sublime.Region(end, end))
                except Exception:
                    pass
                return
        except Exception:
            pass
        reveal_session_bottom(session)

    if sublime is None:
        _go()
        return
    sublime.set_timeout(_go, delay_ms)


def reveal_tail_soon(session, delay_ms: int = 40) -> None:
    """Scroll the session's sheet to its tail once the attach paint settled.

    Attach restores the sheet's saved scroll (surface_restore) and its
    post-paint hooks (stamps, chrome, composer) run on set_timeout(0); a
    reveal from the list or a shortcut means "show me the live end", so the
    tail scroll goes last.
    """
    if session is None:
        return
    if sublime is None:
        reveal_session_bottom(session)
        return
    sublime.set_timeout(lambda: reveal_session_bottom(session), delay_ms)


def reveal_row(window, row: dict, keep=None) -> bool:
    """Bring that session's sheet on screen, at its tail; keep keyboard focus
    on the list."""
    if not row or not window:
        return False
    if keep is None:
        keep = window.active_view()
    ok = False
    try:
        from ui.host import HostView, is_single_mode
        if is_single_mode():
            if row.get("kind") == "live":
                session = _live_session_for_row(row)
                if session is not None:
                    hv = HostView.for_window(window)
                    if getattr(session, "torn_off", False) or row.get("torn_off"):
                        # A torn-off sheet may be a tab behind another file in
                        # its group; a valid view is not a visible one.
                        # reveal_live_session focuses it (no-op if already
                        # active) or recreates it when the view is gone.
                        ok = reveal_live_session(
                            window, session, focus=True, force_sheet=True)
                    elif hv.bound_session(window) is session:
                        # Already on the host: no focus_view / retab, but the
                        # tail is still the point of revealing it.
                        ok = True
                    else:
                        ok = bool(hv.attach(window, session, focus=True))
                    if ok:
                        reveal_tail_soon(session)
            if not ok:
                ok = resume_saved(window, row, focus=True)
            _restore_list_focus(window, keep)
            return ok
    except Exception:
        pass
    if row.get("kind") == "live":
        session = _live_session_for_row(row)
        if session is not None:
            ok = reveal_live_session(
                window, session, focus=True, force_sheet=True)
    if not ok:
        ok = resume_saved(window, row, focus=True)
    _restore_list_focus(window, keep)
    return ok


def _restore_list_focus(window, keep) -> None:
    if not window or keep is None:
        return
    try:
        if keep.is_valid() and not _same_view(window.active_view(), keep):
            window.focus_view(keep)
    except Exception:
        pass


def _live_session_for_row(row: dict):
    if not row:
        return None
    aid = row.get("agent_id")
    if aid:
        session = get_session_by_agent_id(aid)
        if session is not None:
            return session
    sessions = sessions_map()
    vid = row.get("view_id")
    if vid is not None and vid in sessions:
        return sessions.get(vid)
    sid = row.get("session_id")
    if sid:
        try:
            return find_live_by_session_id(sid)
        except Exception:
            pass
        for s in sessions.values():
            if getattr(s, "session_id", None) == sid:
                return s
    return None


def _row_is_current_session(window, row: dict) -> bool:
    """True when this live row is the window's bound / active session."""
    if row.get("bound"):
        return True
    sid = row.get("session_id")
    aid = row.get("agent_id")
    try:
        from main import get_active_session
        s = get_active_session(window) if window else None
    except Exception:
        s = None
    if s is None:
        return False
    if sid and getattr(s, "session_id", None) == sid:
        return True
    if aid and getattr(s, "agent_id", None) == aid:
        return True
    return False


def close_confirm(window, row: dict) -> bool:
    """Ask before Cmd+W close in the Sessions list (every row)."""
    if not row:
        return False
    if sublime is None:
        return True
    name = one_line_title(row.get("name") or "") or row.get("session_id") or "session"
    live = row.get("kind") == "live"
    verb = "Close" if live else "Delete"
    what = "this session" if live else "this session from the list"
    msg = "%s %s?\n\n%s" % (verb, what, name)
    try:
        return bool(sublime.ok_cancel_dialog(msg, verb))
    except Exception:
        return True


def descendant_rows(index: List[dict], row: dict) -> List[dict]:
    """Rows shown under `row` in its section (parent_agent_id chain), deepest
    first so a caller can close leaves before their parents."""
    if not row or not index:
        return []
    root_aid = row.get("agent_id")
    if not root_aid:
        return []
    section = row.get("section")
    by_parent = {}  # type: Dict[str, List[dict]]
    for r in index:
        if r is row or r.get("section") != section:
            continue
        paid = r.get("parent_agent_id")
        if paid:
            by_parent.setdefault(paid, []).append(r)
    out = []  # type: List[Tuple[int, dict]]
    seen = {root_aid}
    frontier = [(root_aid, 0)]
    while frontier:
        aid, depth = frontier.pop()
        for kid in by_parent.get(aid, []):
            kaid = kid.get("agent_id")
            if kaid in seen:
                continue
            out.append((depth + 1, kid))
            if kaid:
                seen.add(kaid)
                frontier.append((kaid, depth + 1))
    out.sort(key=lambda t: t[0], reverse=True)
    return [r for _d, r in out]


def children_confirm(window, row: dict, kids: List[dict]) -> Optional[bool]:
    """Closing a parent: True = close the children too, False = only this
    row, None = cancel. Without a dialog API the children are left alone."""
    if not kids:
        return False
    if sublime is None:
        return False
    name = one_line_title(row.get("name") or "") or row.get("session_id") or "session"
    live = row.get("kind") == "live"
    verb = "Close" if live else "Delete"
    n = len(kids)
    msg = ("%s %s?\n\n%s\n\nIt has %d child session%s. %s them too?"
           % (verb, "this session" if live else "this session from the list",
              name, n, "" if n == 1 else "s", verb))
    try:
        ans = sublime.yes_no_cancel_dialog(msg, "%s all" % verb, "Only this")
    except Exception:
        return False
    if ans == getattr(sublime, "DIALOG_YES", 1):
        return True
    if ans == getattr(sublime, "DIALOG_NO", 2):
        return False
    return None


def starred_confirm(window, row: dict) -> bool:
    """Ask before closing a starred row that is not the current session.

    The bound CURRENT sheet closes like any live session (no extra pin
    dialog). Starred HISTORY and other live sheets still ask, because
    that drop is easy to miss.
    """
    if not row or sublime is None:
        return True
    sid = row.get("session_id")
    if not sid:
        return True
    cwd = ""
    try:
        if window and window.folders():
            cwd = window.folders()[0]
    except Exception:
        cwd = ""
    try:
        starred = load_bookmarks(cwd or None) or set()
    except Exception:
        return True
    if not _pinned(row, starred):
        return True
    if row.get("kind") == "live" and _row_is_current_session(window, row):
        return True
    name = one_line_title(row.get("name") or "") or sid
    live = row.get("kind") == "live"
    verb = "Close" if live else "Delete"
    what = "session" if live else "session from the list"
    msg = ("%s starred %s?\n\n%s\n\n"
           "Unstar it first (s) to keep it out of this question." % (verb, what, name))
    try:
        return bool(sublime.ok_cancel_dialog(msg, verb))
    except Exception:
        return True


def close_row(window, row: dict, remove: Optional[bool] = None) -> bool:
    """Del a list row. Only HISTORY removes the saved resume entry.

    Callers that act on a keystroke ask `starred_confirm` first.

    CURRENT (including starred live): stop the live sheet (tabs) or the
    bound host (single) and keep the save.
    HISTORY (including starred saved): drop the saved row (resume list).
    """
    if not row:
        return False
    if remove is None:
        remove = row.get("section") == "HISTORY"
    sid = row.get("session_id")
    if row.get("kind") == "live":
        session = _live_session_for_row(row)
        if session:
            view = None
            try:
                view = session.output.view if session.output else None
            except Exception:
                view = None
            host_bound = False
            try:
                from ui.host import HostView, is_single_mode
                # Single-mode non-torn-off sessions share the host. Do not
                # require hv._view to already be cached — a miss used to
                # view.close() the host. Unused (query_count 0) still counts.
                if is_single_mode() and not getattr(session, "torn_off", False):
                    host_bound = True
            except Exception:
                host_bound = False
            if host_bound:
                try:
                    from core.registry import default_registry
                    from ui.host import HostView
                    kept = HostView.for_window(window).handoff_host_on_dismiss(
                        window, session)
                    try:
                        session.stop()
                    except Exception:
                        pass
                    if not kept:
                        HostView.for_window(window).detach_current(window)
                    aid = getattr(session, "agent_id", None)
                    if aid:
                        default_registry.by_agent.pop(aid, None)
                except Exception:
                    try:
                        session.stop()
                    except Exception:
                        pass
            else:
                try:
                    session.stop()
                except Exception:
                    pass
                try:
                    if view:
                        unregister_view(view.id())
                    if view and view.is_valid():
                        from .keys import SOFT_CLOSE, write_setting
                        write_setting(view.settings(), SOFT_CLOSE, True)
                        view.close()
                except Exception:
                    pass
            try:
                aid = getattr(session, "agent_id", None)
                bg = getattr(sublime, "_submarine_background", None) or getattr(
                    sublime, "_claude_background", None)
                if aid and isinstance(bg, dict):
                    bg.pop(aid, None)
            except Exception:
                pass
        elif sid:
            try:
                from core.records import SessionStore
                SessionStore().persist_state(sid, "closed")
            except Exception:
                pass
        if remove and sid:
            _forget_row_rows(row, window)
        return True
    if remove and sid:
        return _forget_row_rows(row, window)
    return False


def _forget_row_rows(row: dict, window=None) -> bool:
    """Drop a HISTORY row's resume entries — every id the session was resumed
    under, or the next incarnation would just take its place in the list.

    A pin goes with them: `_include_starred_saved` rebuilds a starred id from
    its bookmark snapshot on the next render, so an explicit delete has to
    clear the window's bookmark state for those ids in the same step.
    """
    ids = row_ids(row)
    dropped = False
    for sid in ids:
        try:
            dropped = bool(remove_saved_session(sid)) or dropped
        except Exception:
            pass
    cwd = ""
    try:
        if window and window.folders():
            cwd = window.folders()[0]
    except Exception:
        cwd = ""
    try:
        starred = set(load_bookmarks(cwd or None) or ())
        if starred.intersection(ids):
            recs = dict(load_bookmark_records(cwd or None) or {})
            for x in ids:
                starred.discard(x)
                recs.pop(x, None)
            save_bookmarks(starred, cwd or None, records=recs)
    except Exception:
        pass
    return dropped


def jsonl_path_for_row(window, row: dict) -> Optional[str]:
    if not row:
        return None
    sid = row.get("session_id")
    backend = row.get("backend") or "claude"
    cwd = row.get("project") or ""
    agent_id = row.get("agent_id") or ""
    if not cwd and window:
        try:
            folders = window.folders()
            if folders:
                cwd = folders[0]
        except Exception:
            cwd = ""
    live = _live_session_for_row(row)
    if live is not None:
        try:
            p = live._find_jsonl_path()
            if p:
                return p
        except Exception:
            pass
        try:
            cwd = live._cwd() or cwd
        except Exception:
            pass
        backend = getattr(live, "backend", None) or backend
        sid = getattr(live, "session_id", None) or sid
        agent_id = getattr(live, "agent_id", None) or agent_id
    try:
        from features.resume import find_session_jsonl
        return find_session_jsonl(sid, backend, cwd, agent_id or "")
    except Exception:
        return None


def open_row_jsonl(window, row: dict, reveal: bool = False) -> bool:
    """Open the session transcript in ST, or reveal it in the file manager."""
    path = jsonl_path_for_row(window, row)
    if not path:
        sublime.status_message("Submarine: no jsonl for this session")
        return False
    if reveal:
        return _reveal_in_file_manager(path)
    if window:
        window.open_file(path)
    return True


def _reveal_in_file_manager(path: str) -> bool:
    import subprocess
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
        return True
    except Exception as e:
        print(f"[Submarine] reveal jsonl: {e}")
        return False


def fork_row(window, row: dict) -> bool:
    """Fork the row's session into a new sheet. True if a session was created."""
    sid = (row or {}).get("session_id")
    if not window or not sid:
        return False
    backend = (row or {}).get("backend") or "claude"
    model = (row or {}).get("model")
    name = ((row or {}).get("name") or "").strip() or "session"
    forked = create_session(
        window, resume_id=sid, fork=True, backend=backend, model=model)
    if not forked:
        return False
    from core.session import fork_session_title
    forked_name = fork_session_title(name)
    forked.name = forked_name
    try:
        forked.output.set_name(forked_name)
    except Exception:
        pass
    sublime.status_message("Forked session: %s" % forked_name)
    return True


def rename_row(window, row: dict, name: str) -> bool:
    """Rename a live session or a history entry."""
    name = (name or "").strip()
    if not row or not name:
        return False
    sid = row.get("session_id")
    if row.get("kind") == "live":
        session = _live_session_for_row(row)
        if session is not None:
            session._set_name(name)
            return True
        if sid:
            return bool(rename_saved_session(sid, name))
        return False
    if sid:
        return bool(rename_saved_session(sid, name))
    return False


class SubmarineSessionJsonlCommand(sublime_plugin.WindowCommand):
    """Open the active session's transcript jsonl (or reveal in Finder)."""

    def run(self, reveal: bool = False):
        s = get_active_session(self.window)
        if not s or not getattr(s, "session_id", None):
            sublime.status_message("Submarine: no session")
            return
        view = None
        try:
            view = s.output.view if s.output else None
        except Exception:
            view = None
        row = {
            "kind": "live",
            "session_id": s.session_id,
            "agent_id": getattr(s, "agent_id", None),
            "backend": getattr(s, "backend", None) or "claude",
            "view_id": view.id() if view and view.is_valid() else None,
            "project": "",
        }
        if open_row_jsonl(self.window, row, reveal=bool(reveal)):
            sublime.status_message(
                "Submarine: revealed jsonl" if reveal else "Submarine: opened jsonl")


class SessionListView:
    def __init__(self, window):
        self.window = window
        self.view = None
        self._create()

    def _apply_chrome(self):
        st = self.view.settings()
        st.set(SETTING, True)
        st.set("command_mode", False)
        st.set("word_wrap", False)
        st.set("gutter", False)
        st.set("line_numbers", False)
        st.set("margin", 4)
        st.set("scroll_past_end", False)
        st.set("highlight_line", True)
        st.set("font_size", 10)
        st.set("font_face", "Menlo")
        st.set("color_scheme", keys.SESSION_LIST_SCHEME)
        self.view.assign_syntax(keys.SESSION_LIST_SYNTAX)

    def _create(self):
        for v in self.window.views():
            if v.settings().get(SETTING):
                self.view = v
                break
        if not self.view:
            self.view = self.window.new_file()
            self.view.set_scratch(True)
            self.view.set_read_only(True)
            # A new sheet lands at the end of the active group; the list is
            # usually kept in its own split, so put it back in its slot.
            try:
                from core.placement import TAB_LIST, apply_view_tab
                apply_view_tab(self.window, self.view, TAB_LIST)
            except Exception as e:
                print("[Submarine] session list tab: %s" % e)
        # Symbol so the list tab is findable among session sheets.
        self.view.set_name("☰ Sessions")
        self._apply_chrome()
        self.refresh(follow=True)
        self.window.focus_view(self.view)

    def select_active(self) -> bool:
        """Put the caret on this window's active session row, if listed."""
        if not self.view or not self.view.is_valid():
            return False
        session = None
        try:
            session = get_active_session(self.window)
        except Exception:
            session = None
        if session is None:
            return False
        try:
            index = json.loads(self.view.settings().get(ROWS_KEY) or "[]")
        except Exception:
            index = []
        if not index:
            return False
        aid = getattr(session, "agent_id", None)
        sid = getattr(session, "session_id", None)
        target = None
        if aid:
            for rec in index:
                if rec.get("agent_id") == aid:
                    target = rec
                    break
        if target is None and sid:
            for rec in index:
                if rec.get("session_id") == sid:
                    target = rec
                    break
        if target is None:
            return False
        try:
            pt = self.view.text_point(max(0, int(target.get("line") or 1) - 1), 0)
            self.view.sel().clear()
            self.view.sel().add(sublime.Region(pt))
            self.view.show(pt)
            return True
        except Exception:
            return False

    def refresh(self, follow: bool = False):
        if not self.view or not self.view.is_valid():
            return
        try:
            vx, vy = self.view.viewport_position()
        except Exception:
            vx, vy = 0.0, 0.0
        cols = view_cols(self.view)
        state_cols = cols  # the fingerprint uses the width before any fallback
        text, index = build_for_window(self.window, cols=cols)
        self.view.settings().set(WRITING_KEY, True)
        cur = self.view.substr(sublime.Region(0, self.view.size()))
        keep_sid = None
        keep_kind = None
        if self.view.sel():
            line = self.view.rowcol(self.view.sel()[0].begin())[0] + 1
            try:
                old = json.loads(self.view.settings().get(ROWS_KEY) or "[]")
            except Exception:
                old = []
            hit = row_at_line(old, line)
            if hit:
                keep_sid = hit.get("session_id")
                keep_kind = hit.get("kind")
        rows_json = json.dumps(index)
        if self.view.settings().get(ROWS_KEY) != rows_json:
            self.view.settings().set(ROWS_KEY, rows_json)
        wrote = False
        if text != cur:
            self._write_list_text(text)
            wrote = True
        self._remember_state(state_cols, index)
        # Wide glyphs (⏸/…) can exceed em; only then drop a column.
        for _ in range(3):
            try:
                if self.view.layout_extent()[0] <= self.view.viewport_extent()[0] + 1:
                    break
            except Exception:
                break
            cols = max(24, cols - 1)
            text, index = build_for_window(self.window, cols=cols)
            rows_json = json.dumps(index)
            if self.view.settings().get(ROWS_KEY) != rows_json:
                self.view.settings().set(ROWS_KEY, rows_json)
            self._write_list_text(text)
            wrote = True
        if not wrote and not follow:
            try:
                self.view.settings().erase(WRITING_KEY)
            except Exception:
                try:
                    self.view.settings().set(WRITING_KEY, False)
                except Exception:
                    pass
            return
        target = None
        if keep_sid:
            for rec in index:
                if rec.get("session_id") == keep_sid and rec.get("kind") == keep_kind:
                    target = rec
                    break
        if target is None and self.view.sel() and self.view.rowcol(
                self.view.sel()[0].begin())[0] < 3 and index:
            target = index[0]
        if target:
            pt = self.view.text_point(max(0, int(target["line"]) - 1), 0)
            self.view.sel().clear()
            self.view.sel().add(sublime.Region(pt))
            if follow:
                self.view.show(pt)
        if not follow:
            try:
                self.view.set_viewport_position((float(vx), float(vy)), False)
            except Exception:
                pass
        try:
            self.view.settings().erase(WRITING_KEY)
        except Exception:
            try:
                self.view.settings().set(WRITING_KEY, False)
            except Exception:
                pass

    def _remember_state(self, cols: int, index: List[dict]) -> None:
        """Snapshot what this render was built from, for `_list_changed`."""
        _stamps[self.view.id()] = _next_stamp_change(index)
        try:
            _fingerprints[self.view.id()] = list_state_key(self.window, cols)
        except Exception:
            pass

    def _write_list_text(self, text: str) -> None:
        view = self.view
        view.set_read_only(False)
        view.run_command("submarine_session_list_set_text", {"text": text})
        view.set_read_only(True)
        # select_all+delete+append left an undo snapshot every poll (~1Hz).
        def _drop_undo(v=view):
            try:
                if v and v.is_valid():
                    v.clear_undo_stack()
            except Exception:
                pass
        if sublime is not None:
            sublime.set_timeout(_drop_undo, 0)


def show_session_list(window) -> Optional[SessionListView]:
    if not window:
        return None
    for v in window.views():
        if v.settings().get(SETTING) and v.is_valid():
            sl = SessionListView.__new__(SessionListView)
            sl.window = window
            sl.view = v
            sl._apply_chrome()
            sl.refresh()
            sl.select_active()
            window.focus_view(v)
            _arm_session_list_poll()
            return sl
    sl = SessionListView(window)
    sl.select_active()
    _arm_session_list_poll()
    return sl


class SessionListClickListener(sublime_plugin.EventListener):
    """Letter keys often fall through to insert. Dclick open is mousemap only.

    Do not also open on Default `drag_select` by=words — word-select begin
    is often the previous line, so one dclick opened the target and a neighbor.
    """

    def on_query_context(self, view, key, operator, operand, match_all):
        if key != keys.SESSION_LIST and key != "claude_session_list":
            return None
        val = bool(view and view.settings().get(SETTING))
        if operator == sublime.OP_EQUAL:
            return val == bool(operand)
        if operator == sublime.OP_NOT_EQUAL:
            return val != bool(operand)
        return val

    def on_text_command(self, view, name, args):
        if not view or not view.settings().get(SETTING):
            return None
        if name in ("left_delete", "right_delete"):
            return ("submarine_session_list_close", {})
        if name != "insert":
            return None
        ch = (args or {}).get("characters") or ""
        if ch == "r":
            return ("submarine_session_list_rename", {})
        if ch == "v":
            return ("submarine_session_list_reveal", {})
        if ch == "s":
            return ("submarine_session_list_star", {})
        if ch == "f":
            return ("submarine_session_list_fork", {})
        if ch == "t":
            return ("submarine_session_list_tear_off", {})
        if ch == "j":
            return ("submarine_session_list_jsonl", {})
        if ch == "J":
            return ("submarine_session_list_jsonl", {"reveal": True})
        if ch in ("\n", "\r"):
            return ("submarine_session_list_open", {})
        return None

    def _schedule_follow(self, view, force=False):
        if not view or not view.settings().get(SETTING):
            return
        if view.settings().get(WRITING_KEY):
            return
        gen = int(view.settings().get(FOLLOW_GEN_KEY) or 0) + 1
        view.settings().set(FOLLOW_GEN_KEY, gen)

        def _go(expected=gen, v=view, forced=force):
            try:
                if not v.is_valid():
                    return
                if int(v.settings().get(FOLLOW_GEN_KEY) or 0) != expected:
                    return
            except Exception:
                return
            follow_current_under_caret(v, force=forced)

        if sublime is not None:
            sublime.set_timeout(_go, 30)
        else:
            _go()

    def on_activated(self, view):
        if view and view.settings().get(SETTING):
            # A list restored with the window, or one left behind by a reload,
            # has no poll armed and renders nothing on its own; focusing it
            # brings it current and starts its clock again.
            _arm_session_list_poll()
            try:
                refresh_session_list(view.window())
            except Exception:
                pass
        self._schedule_follow(view, force=True)

    def on_selection_modified(self, view):
        self._schedule_follow(view)

    def on_post_text_command(self, view, name, args):
        if not view or not view.settings().get(SETTING):
            return
        if name in ("move", "move_to", "move_word", "move_to_bof", "move_to_eof"):
            self._schedule_follow(view)
            return
        if name != "drag_select":
            return
        self._schedule_follow(view, force=True)


# --- what an idle poll may skip ---------------------------------------------
# A Sessions view is refreshed on events (session state changes) and by a
# timer. The timer exists for what no event reports: the elapsed-time column
# aging, a split being resized, or another process rewriting the session store.
# Every tick compares a fingerprint first — the registry, the star records, and
# a stat of `.sessions.json` — and only renders when that changed or an elapsed
# time came due, so a tick with nothing to show costs a couple of reads and a
# tuple compare instead of a rebuild plus a buffer write.
_POLL_MIN_MS = 1000
_POLL_MAX_MS = 8000
_fingerprints = {}  # type: Dict[int, Any]  # list view id -> what it renders
# list view id -> wall clock when its own elapsed-time column next changes.
# Per view: two open lists hold rows of different ages, and one of them
# re-arming a shared deadline used to leave the other's clock frozen.
_stamps = {}  # type: Dict[int, float]
_poll_delay = _POLL_MIN_MS
_poll_stopped = False  # set on plugin unload: this incarnation stops polling
_poll_gen = 0          # the shared generation this incarnation armed under


def _sessions_store_path() -> str:
    try:
        from core.records import default_sessions_path
        return default_sessions_path()
    except Exception:
        return ""


def _store_signature(path: str):
    """(mtime_ns, size) — enough to tell a JSON store was rewritten."""
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    mtime = getattr(st, "st_mtime_ns", None)
    if mtime is None:
        mtime = int(st.st_mtime * 1000000000)
    return (int(mtime), int(st.st_size))


def list_state_key(window, cols: int):
    """Cheap proxy for `build_for_window`: everything a render reads.

    Live rows come from the registry alone, and the history rows only from
    `.sessions.json` — which is rewritten whole on every change, so a stat
    covers it without parsing the file on each tick.
    """
    cwd = ""
    try:
        if window and window.folders():
            cwd = window.folders()[0]
    except Exception:
        cwd = ""
    rows = collect_live(window)
    # Stars and their record snapshots both ride in bookmarks.json, and a
    # snapshot (name, counts) shows on a row whose store entry was pruned.
    try:
        starred = tuple(sorted(load_bookmarks(cwd or None) or ()))
    except Exception:
        starred = ()
    try:
        records = json.dumps(load_bookmark_records(cwd or None) or {},
                             sort_keys=True, default=str)
    except Exception:
        records = ""
    return (
        int(cols),
        cwd,
        tuple(
            (r.get("session_id"), r.get("agent_id"), r.get("parent_agent_id"),
             r.get("view_id"), r.get("name"), r.get("backend"), r.get("model"),
             r.get("status"), r.get("query_count"), r.get("last_access"),
             r.get("last_activity"), r.get("bound"), r.get("torn_off"))
            for r in rows
        ),
        _store_signature(_sessions_store_path()),
        starred,
        records,
    )


def _stamp_due(view_id, now=None) -> bool:
    """Has this view's elapsed-time column aged into a new label?"""
    deadline = _stamps.get(view_id) or 0.0
    if not deadline:
        return False
    return (time.time() if now is None else float(now)) >= deadline


def _soonest_stamp() -> float:
    """When the first open list needs its clock repainted (0.0 for none)."""
    soonest = 0.0
    for vid in _open_list_view_ids():
        deadline = _stamps.get(vid) or 0.0
        if deadline and (not soonest or deadline < soonest):
            soonest = deadline
    return soonest


def _list_changed(window, view) -> bool:
    """Would re-rendering this list show anything new?"""
    try:
        key = list_state_key(window, view_cols(view))
    except Exception:
        return True
    if key != _fingerprints.get(view.id()):
        return True
    return _stamp_due(view.id())


def _place_caret_on_session(view, sid, kind=None) -> None:
    """Put the caret on that session's row after a rewrite (star pin, etc.)."""
    if view is None or not sid:
        return
    try:
        index = json.loads(view.settings().get(ROWS_KEY) or "[]")
    except Exception:
        return
    target = None
    for rec in index:
        if rec.get("session_id") != sid:
            continue
        if kind and rec.get("kind") != kind:
            continue
        target = rec
        break
    if target is None:
        for rec in index:
            if sid in row_ids(rec):
                target = rec
                break
    if not target:
        return
    try:
        pt = view.text_point(max(0, int(target.get("line") or 1) - 1), 0)
        view.sel().clear()
        if sublime is not None:
            view.sel().add(sublime.Region(pt, pt))
        else:
            view.sel().add(pt)
        view.show(pt)
    except Exception:
        pass
    try:
        gen = int(view.settings().get(FOLLOW_GEN_KEY) or 0) + 1
        view.settings().set(FOLLOW_GEN_KEY, gen)
    except Exception:
        pass


def live_agent_ids_in_list_order(window) -> List[str]:
    """CURRENT rows, top to bottom, as the Sessions list shows (or would
    show) them: the order Ctrl+] / Ctrl+[ step through.

    An open list in this window is the authority — its stored row index is
    exactly what the user is looking at. Without one, the same build
    (`collect_live` → `tree_order`, starred pins and status bands included).
    """
    if window is not None:
        try:
            views = list(window.views())
        except Exception:
            views = []
        for v in views:
            try:
                if not (v.settings().get(SETTING) and v.is_valid()):
                    continue
                index = json.loads(v.settings().get(ROWS_KEY) or "[]")
            except Exception:
                continue
            ids = [str(r.get("agent_id")) for r in index
                   if r.get("kind") == "live" and r.get("agent_id")]
            if ids:
                return ids
    cwd = ""
    try:
        if window is not None and window.folders():
            cwd = window.folders()[0]
    except Exception:
        cwd = ""
    try:
        starred = set(load_bookmarks(cwd or None) or ())
    except Exception:
        starred = set()
    rows = tree_order(collect_live(window), starred)
    return [str(r.get("agent_id")) for r in rows if r.get("agent_id")]


def sync_list_to_session(window, session) -> bool:
    """Caret (and scroll) of this window's Sessions list onto `session`'s row.

    For a switch made elsewhere — Ctrl+] / Ctrl+[, a reveal command — so the
    list keeps pointing at what the window shows. True when a row was found.
    """
    if window is None or session is None:
        return False
    sid = getattr(session, "session_id", None) or ""
    aid = getattr(session, "agent_id", None) or ""
    done = False
    try:
        views = list(window.views())
    except Exception:
        return False
    for v in views:
        try:
            if not (v.settings().get(SETTING) and v.is_valid()):
                continue
            index = json.loads(v.settings().get(ROWS_KEY) or "[]")
        except Exception:
            continue
        target = None
        for rec in index:
            if rec.get("kind") != "live":
                continue
            if (aid and rec.get("agent_id") == aid) or (sid and rec.get("session_id") == sid):
                target = rec
                break
        if target is None:
            continue
        try:
            pt = v.text_point(max(0, int(target.get("line") or 1) - 1), 0)
            v.sel().clear()
            v.sel().add(sublime.Region(pt, pt) if sublime is not None else pt)
            v.show(pt)
            gen = int(v.settings().get(FOLLOW_GEN_KEY) or 0) + 1
            v.settings().set(FOLLOW_GEN_KEY, gen)
            done = True
        except Exception:
            pass
    return done


def refresh_session_list(window, force: bool = True) -> bool:
    """Re-render this window's Sessions view; True when it ran.

    Callers that just changed something (a command, a session event) leave
    `force` alone. The poll passes False, so an idle tick skips the rebuild.
    """
    if not window:
        return False
    for v in window.views():
        if v.settings().get(SETTING) and v.is_valid():
            if not force and not _list_changed(window, v):
                return False
            sl = SessionListView.__new__(SessionListView)
            sl.window = window
            sl.view = v
            sl.refresh()
            return True
    return False


def _session_list_open() -> bool:
    try:
        for w in sublime.windows():
            for v in w.views():
                if v.settings().get(SETTING) and v.is_valid():
                    return True
    except Exception:
        pass
    return False


def refresh_all_session_lists(force: bool = True) -> bool:
    """Refresh every open list; True when at least one was re-rendered."""
    changed = False
    try:
        for w in sublime.windows():
            if refresh_session_list(w, force=force):
                changed = True
    except Exception:
        pass
    return changed


_refresh_pending = False
_poll_armed = False


def schedule_session_list_refresh() -> None:
    """Debounced refresh of every open Sessions scratch view.

    State changes (busy/idle/sleep) call this; the poll is only the clock.
    """
    global _refresh_pending, _poll_delay
    if _refresh_pending:
        return
    if not _session_list_open():
        return
    _refresh_pending = True
    _poll_delay = _POLL_MIN_MS

    def _go():
        global _refresh_pending
        _refresh_pending = False
        refresh_all_session_lists()

    sublime.set_timeout(_go, 50)
    _arm_session_list_poll()


def _poll_generation() -> int:
    """A counter on the long-lived `sublime` module, shared by every copy.

    A reload leaves the previous incarnation's pending `set_timeout` callbacks
    alive, and nothing else it holds is visible from outside; this is how a
    stopped chain tells them apart.
    """
    try:
        return int(getattr(sublime, "_submarine_poll_gen", 0) or 0)
    except Exception:
        return 0


def _bump_poll_generation() -> None:
    try:
        sublime._submarine_poll_gen = _poll_generation() + 1
    except Exception:
        pass


def _open_list_view_ids() -> List[int]:
    """Every open Sessions view id, across all windows."""
    out: List[int] = []
    try:
        for w in sublime.windows():
            for v in w.views():
                if v.settings().get(SETTING):
                    out.append(v.id())
    except Exception:
        pass
    return out


def _prune_fingerprints() -> None:
    """Drop memories of views that are gone (closed, or window shut)."""
    live = set(_open_list_view_ids())
    for vid in [k for k in _fingerprints if k not in live]:
        _fingerprints.pop(vid, None)
    for vid in [k for k in _stamps if k not in live]:
        _stamps.pop(vid, None)


def _arm_session_list_poll() -> None:
    global _poll_armed, _poll_delay, _poll_gen
    if _poll_stopped or _poll_armed:
        return
    _poll_armed = True
    _poll_delay = _POLL_MIN_MS
    _poll_gen = _poll_generation()
    _prune_fingerprints()
    sublime.set_timeout(_session_list_poll, _poll_delay)


def stop_session_list_poll() -> None:
    """Stop the poll chain of every copy of this module. `plugin_unloaded` calls
    it: a reload leaves the old incarnation's callbacks pending, and without
    this each one keeps re-rendering the list on its own clock.
    """
    global _poll_stopped, _poll_armed
    _poll_stopped = True
    _poll_armed = False
    _stamps.clear()
    _fingerprints.clear()
    _bump_poll_generation()


def _next_poll_delay() -> int:
    """Back off while nothing moves, but wake for the next elapsed-time change."""
    delay = _poll_delay
    soonest = _soonest_stamp()
    if soonest:
        left = int(max(0.0, soonest - time.time()) * 1000) + 50
        if left < delay:
            delay = left
    return max(_POLL_MIN_MS, min(_POLL_MAX_MS, delay))


def _session_list_poll() -> None:
    global _poll_armed, _poll_delay
    if (_poll_stopped or _poll_gen != _poll_generation()
            or not _session_list_open()):
        _poll_armed = False
        return
    # force=False: rebuild only when the fingerprint or a stamp says so.
    if refresh_all_session_lists(force=False):
        _poll_delay = _POLL_MIN_MS
    else:
        _poll_delay = min(_POLL_MAX_MS, _poll_delay * 2)
    sublime.set_timeout(_session_list_poll, _next_poll_delay())


class SubmarineSessionListSetTextCommand(sublime_plugin.TextCommand):
    """Replace the list buffer in one edit (no select_all/delete undo pair)."""

    def run(self, edit, text=""):
        if not self.view.settings().get(SETTING):
            return
        self.view.replace(edit, sublime.Region(0, self.view.size()), text)


class SubmarineSessionListCloseCommand(sublime_plugin.TextCommand):
    """Delete/close the session under the caret in the Sessions list.

    Delete/Backspace: starred rows ask first (`starred_confirm`).
    Cmd+W (`confirm=True`): always ask, then close the row — not the list view.
    A row with children in its section asks whether to close them as well
    (`children_confirm`); "Only this" leaves them as roots.
    """

    def run(self, edit, confirm=False):
        import json
        if not self.view.settings().get(SETTING):
            return
        raw = self.view.settings().get(ROWS_KEY) or "[]"
        try:
            index = json.loads(raw)
        except Exception:
            index = []
        sel = self.view.sel()
        if not sel:
            return
        line = self.view.rowcol(sel[0].begin())[0] + 1
        row = row_at_line(index, line)
        win = self.view.window()
        if not win or not row:
            return
        name = (row.get("name") or "").strip() or "session"
        list_view = self.view
        kids = descendant_rows(index, row)
        if kids:
            # One dialog covers the Cmd+W confirm and the children question.
            ok = starred_confirm(win, row) if not confirm else True
            if not ok:
                return
            take_kids = children_confirm(win, row, kids)
            if take_kids is None:
                return
            if not take_kids:
                kids = []
        else:
            ok = close_confirm(win, row) if confirm else starred_confirm(win, row)
            if not ok:
                return
        closed_kids = 0
        for kid in kids:  # deepest first
            try:
                if close_row(win, kid):
                    closed_kids += 1
            except Exception:
                pass
        if close_row(win, row):
            refresh_session_list(win)

            def _stay(_v=list_view, _win=win, _line=line):
                if not _v or not _v.is_valid():
                    return
                w = _v.window() or _win
                if not w:
                    return
                w.focus_view(_v)
                try:
                    pt = _v.text_point(max(0, _line - 1), 0)
                    _v.sel().clear()
                    _v.sel().add(pt)
                except Exception:
                    pass

            _stay()
            sublime.set_timeout(_stay, 0)
            if closed_kids:
                sublime.status_message("Submarine: closed {} and {} child session{}".format(
                    name, closed_kids, "" if closed_kids == 1 else "s"))
            else:
                sublime.status_message("Submarine: closed {}".format(name))

    def is_enabled(self):
        return bool(self.view.settings().get(SETTING))


class SubmarineSessionListRenameCommand(sublime_plugin.TextCommand):
    """Rename the session under the caret."""

    def run(self, edit):
        if not self.view.settings().get(SETTING):
            return
        raw = self.view.settings().get(ROWS_KEY) or "[]"
        try:
            index = json.loads(raw)
        except Exception:
            index = []
        sel = self.view.sel()
        if not sel:
            return
        line = self.view.rowcol(sel[0].begin())[0] + 1
        row = row_at_line(index, line)
        win = self.view.window()
        if not win or not row:
            return
        current = (row.get("name") or "").strip()
        if current == "(unnamed)":
            current = ""
        list_view = self.view

        def _done(name, _row=row, _win=win, _view=list_view):
            if not (name or "").strip():
                return
            if rename_row(_win, _row, name.strip()):
                refresh_session_list(_win)
                try:
                    if _view and _view.is_valid():
                        _win.focus_view(_view)
                except Exception:
                    pass
                sublime.status_message("Submarine: renamed")

        win.show_input_panel("Session name:", current, _done, None, None)

    def is_enabled(self):
        return bool(self.view.settings().get(SETTING))


class SubmarineSessionListForkCommand(sublime_plugin.TextCommand):
    """Fork the session under the caret (f)."""

    def run(self, edit):
        if not self.view.settings().get(SETTING):
            return
        raw = self.view.settings().get(ROWS_KEY) or "[]"
        try:
            index = json.loads(raw)
        except Exception:
            index = []
        sel = self.view.sel()
        if not sel:
            return
        line = self.view.rowcol(sel[0].begin())[0] + 1
        row = row_at_line(index, line)
        win = self.view.window()
        if not win or not row:
            return
        if not row.get("session_id"):
            sublime.status_message("Submarine: no session to fork")
            return
        fork_row(win, row)

    def is_enabled(self):
        return bool(self.view.settings().get(SETTING))


class SubmarineSessionListJsonlCommand(sublime_plugin.TextCommand):
    """Open (j) or Finder-reveal (J) the session transcript jsonl."""

    def run(self, edit, reveal: bool = False):
        if not self.view.settings().get(SETTING):
            return
        raw = self.view.settings().get(ROWS_KEY) or "[]"
        try:
            index = json.loads(raw)
        except Exception:
            index = []
        sel = self.view.sel()
        if not sel:
            return
        line = self.view.rowcol(sel[0].begin())[0] + 1
        row = row_at_line(index, line)
        win = self.view.window()
        if not win or not row:
            return
        if open_row_jsonl(win, row, reveal=bool(reveal)):
            if reveal:
                sublime.status_message("Submarine: revealed jsonl")
            else:
                sublime.status_message("Submarine: opened jsonl")

    def is_enabled(self):
        return bool(self.view.settings().get(SETTING))


class SubmarineSessionListStarCommand(sublime_plugin.TextCommand):
    """Toggle bookmark for the session under the caret."""

    def run(self, edit):
        if not self.view.settings().get(SETTING):
            return
        raw = self.view.settings().get(ROWS_KEY) or "[]"
        try:
            index = json.loads(raw)
        except Exception:
            index = []
        sel = self.view.sel()
        if not sel:
            return
        line = self.view.rowcol(sel[0].begin())[0] + 1
        row = row_at_line(index, line)
        win = self.view.window()
        sid = (row or {}).get("session_id")
        if not win or not sid:
            return
        cwd = ""
        try:
            folders = win.folders()
            if folders:
                cwd = folders[0]
        except Exception:
            cwd = ""
        record = {
            "name": row.get("name"),
            "backend": row.get("backend"),
            "project": row.get("project") or cwd,
            "model": row.get("model"),
            "query_count": row.get("query_count"),
            "last_activity": row.get("last_activity"),
            "last_access": row.get("last_access"),
        }
        try:
            starred = set(load_bookmarks(cwd or None) or ())
        except Exception:
            starred = set()
        try:
            recs = dict(load_bookmark_records(cwd or None) or {})
        except Exception:
            recs = {}
        # A pin covers the whole session: every id a row stands for is starred
        # and unstarred together, or an older incarnation would keep it pinned.
        pinned = _pinned(row, starred)
        for x in row_ids(row):
            if pinned:
                starred.discard(x)
                recs.pop(x, None)
            else:
                starred.add(x)
                recs[x] = dict(record)
        save_bookmarks(starred, cwd or None, records=recs)
        now = not pinned
        name = (row.get("name") or "").strip() or sid
        refresh_session_list(win)
        _place_caret_on_session(self.view, sid, kind=row.get("kind"))
        sublime.status_message(
            ("★ Starred: {}" if now else "☆ Unstarred: {}").format(name))

    def is_enabled(self):
        return bool(self.view.settings().get(SETTING))


class SubmarineSessionListRevealCommand(sublime_plugin.TextCommand):
    """Show the session under the caret; keep focus on the Sessions list."""

    def run(self, edit):
        import json
        if not self.view.settings().get(SETTING):
            return
        raw = self.view.settings().get(ROWS_KEY) or "[]"
        try:
            index = json.loads(raw)
        except Exception:
            index = []
        sel = self.view.sel()
        if not sel:
            return
        line = self.view.rowcol(sel[0].begin())[0] + 1
        row = row_at_line(index, line)
        win = self.view.window()
        if not win or not row:
            return
        if reveal_row(win, row):
            try:
                win.focus_view(self.view)
            except Exception:
                pass

    def is_enabled(self):
        return bool(self.view.settings().get(SETTING))


def tear_or_dock_row(window, row):
    """t on a bound row tears off; t on a torn-off row docks."""
    if not row or not window or row.get("kind") != "live":
        return False
    session = _live_session_for_row(row)
    if session is None:
        return False
    from ui.host import HostView, is_single_mode
    if not is_single_mode():
        return False
    hv = HostView.for_window(window)
    if getattr(session, "torn_off", False) or row.get("torn_off"):
        return bool(hv.dock(window, session, focus=True))
    bound = hv.bound_session(window)
    if session is bound:
        return bool(hv.tear_off(window, session, focus=True))
    return False


class SubmarineSessionListTearOffCommand(sublime_plugin.TextCommand):
    """Tear off the bound row, or dock a torn-off row (t)."""

    def run(self, edit):
        if not self.view.settings().get(SETTING):
            return
        raw = self.view.settings().get(ROWS_KEY) or "[]"
        try:
            index = json.loads(raw)
        except Exception:
            index = []
        sel = self.view.sel()
        if not sel:
            return
        line = self.view.rowcol(sel[0].begin())[0] + 1
        row = row_at_line(index, line)
        win = self.view.window()
        if not win or not row:
            return
        if tear_or_dock_row(win, row):
            refresh_session_list(win)

    def is_enabled(self):
        return bool(self.view.settings().get(SETTING))

