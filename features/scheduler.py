"""Host wake scheduler — one table for set_timer + /loop banners.

Host-local timers plus /loop banners; one table for every wake.
Survives package reload by hanging the table on a module-global the
entry point (and sublime module, when present) preserves.

Tokens from Scheduler.call_later cannot be cancelled — generation
guards drop stale fires.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional, Union

MIN_TIMER_S = 60
IDLE_POLL_MS = 500

# Process-global table. Re-import keeps this binding if the module object
# is preserved; attach_table() also re-homes it on sublime / a bag.
_TABLE = {
    "wakes": {},  # timer_id -> rec
    "by_session": {},  # id(session) or session_id -> timer_id
    "gen": 0,
}  # type: Dict[str, Any]


def _sublime():
    try:
        import sublime  # type: ignore
        return sublime
    except Exception:
        return None


def attach_table(bag: Optional[dict] = None) -> dict:
    """Re-home the wake table on a surviving object (sublime / plugin bag)."""
    global _TABLE
    sub = _sublime()
    if bag is None and sub is not None:
        bag = getattr(sub, "_submarine_scheduler", None)
        if not isinstance(bag, dict) or "wakes" not in bag:
            bag = _TABLE
            try:
                sub._submarine_scheduler = bag  # type: ignore[attr-defined]
            except Exception:
                pass
        else:
            _TABLE = bag
            return _TABLE
    if isinstance(bag, dict) and "wakes" in bag:
        _TABLE = bag
    return _TABLE


def _table() -> dict:
    return attach_table()


def _session_key(session: Any) -> str:
    sid = getattr(session, "session_id", None) or getattr(session, "resume_id", None)
    if sid:
        return "sid:%s" % sid
    vid = getattr(session, "view_id", None)
    if vid is not None:
        return "view:%s" % vid
    return "obj:%s" % id(session)


def _call_later(session: Any, ms: int, fn) -> None:
    sched = getattr(session, "scheduler", None)
    if sched is not None and hasattr(sched, "call_later"):
        sched.call_later(int(ms), fn)
        return
    sub = _sublime()
    if sub is not None:
        sub.set_timeout(fn, int(ms))


def _is_working(session: Any) -> bool:
    try:
        return bool(getattr(session, "working", False))
    except Exception:
        return False


def _query(session: Any, prompt: str) -> None:
    session.query(prompt)


def set_timer(seconds: Union[int, float], wake_prompt: str, session: Any) -> str:
    """Arm a one-shot wake. Clamps to min 60s. One pending wake per session."""
    try:
        delay = float(seconds)
    except (TypeError, ValueError):
        delay = float(MIN_TIMER_S)
    if delay < MIN_TIMER_S:
        delay = float(MIN_TIMER_S)
    prompt = (wake_prompt or "").strip()
    tbl = _table()
    key = _session_key(session)
    # Wake-storm invariant: replace any pending wake for this session.
    prev = tbl["by_session"].get(key)
    if prev and prev in tbl["wakes"]:
        tbl["wakes"][prev]["cancelled"] = True
        tbl["wakes"].pop(prev, None)
    timer_id = uuid.uuid4().hex[:12]
    tbl["gen"] = int(tbl.get("gen") or 0) + 1
    gen = tbl["gen"]
    rec = {
        "timer_id": timer_id,
        "session": session,
        "session_key": key,
        "wake_prompt": prompt,
        "fire_at": time.time() + delay,
        "gen": gen,
        "cancelled": False,
        "kind": "timer",
    }
    tbl["wakes"][timer_id] = rec
    tbl["by_session"][key] = timer_id
    _arm_fire(session, rec, delay)
    _paint_banner(session, rec["fire_at"])
    return timer_id


def cancel_timer(timer_id_or_session: Any = None) -> int:
    """Cancel one timer_id, or every pending wake for a session object.

    ``cancel_timer("all")`` is not a session — pass the session object for
    all-for-session. A string is treated as a timer_id.
    """
    tbl = _table()
    n = 0
    if timer_id_or_session is None:
        return 0
    if isinstance(timer_id_or_session, str):
        rec = tbl["wakes"].pop(timer_id_or_session, None)
        if rec:
            rec["cancelled"] = True
            key = rec.get("session_key")
            if key and tbl["by_session"].get(key) == timer_id_or_session:
                tbl["by_session"].pop(key, None)
            _paint_banner(rec.get("session"), None)
            n = 1
        return n
    # session object → cancel all for that session
    return cancel_all_for_session(timer_id_or_session)


def cancel_all_for_session(session: Any) -> int:
    tbl = _table()
    key = _session_key(session)
    tid = tbl["by_session"].pop(key, None)
    n = 0
    if tid and tid in tbl["wakes"]:
        rec = tbl["wakes"].pop(tid)
        rec["cancelled"] = True
        n = 1
    # Also drop any rec still pointing at this session object
    drop = [
        tid for tid, rec in list(tbl["wakes"].items())
        if rec.get("session") is session or rec.get("session_key") == key
    ]
    for tid in drop:
        rec = tbl["wakes"].pop(tid, None)
        if rec:
            rec["cancelled"] = True
            n += 1
    _paint_banner(session, None)
    return n


def accept_loop_scheduled(session: Any, fire_at: Optional[float]) -> None:
    """Route bridge ``loop_scheduled {fire_at}`` into the host table + banner."""
    session.next_wake_at = fire_at
    session.is_looping = bool(fire_at)
    tbl = _table()
    key = _session_key(session)
    if not fire_at:
        tid = tbl["by_session"].get(key)
        if tid and tbl["wakes"].get(tid, {}).get("kind") == "loop":
            cancel_timer(tid)
        _paint_banner(session, None)
        return
    prev = tbl["by_session"].get(key)
    if prev and prev in tbl["wakes"]:
        tbl["wakes"][prev]["cancelled"] = True
        tbl["wakes"].pop(prev, None)
    timer_id = "loop-%s" % uuid.uuid4().hex[:8]
    tbl["gen"] = int(tbl.get("gen") or 0) + 1
    rec = {
        "timer_id": timer_id,
        "session": session,
        "session_key": key,
        "wake_prompt": "",
        "fire_at": float(fire_at),
        "gen": tbl["gen"],
        "cancelled": False,
        "kind": "loop",
    }
    tbl["wakes"][timer_id] = rec
    tbl["by_session"][key] = timer_id
    _paint_banner(session, float(fire_at))


def cancel_loop(session: Any) -> None:
    """Banner Stop: cancel host row + bridge ``cancel_loop`` RPC."""
    cancel_all_for_session(session)
    try:
        session.is_looping = False
        session.next_wake_at = None
    except Exception:
        pass
    send = getattr(session, "_send", None)
    if callable(send):
        try:
            send("cancel_loop", {})
        except Exception:
            pass
    _paint_banner(session, None)


def eta_text(fire_at: Optional[float], now: Optional[float] = None) -> str:
    """``↻ cron · next at HH:MM · ~Xm`` — banner always says cron."""
    if not fire_at:
        return ""
    now = now if now is not None else time.time()
    remain = max(0, int(float(fire_at) - now))
    try:
        local = time.localtime(float(fire_at))
        hhmm = time.strftime("%H:%M", local)
    except Exception:
        hhmm = "??:??"
    if remain >= 3600:
        approx = "~%dh" % (remain // 3600)
    elif remain >= 60:
        approx = "~%dm" % (remain // 60)
    else:
        approx = "~%ds" % remain
    return "↻ cron · next at %s · %s" % (hhmm, approx)


def attach_scheduler(session: Any) -> Any:
    """Wrap ``_accept_loop`` so bridge loop_scheduled hits this table."""
    if getattr(session, "_scheduler_attached", False):
        return session
    session._scheduler_attached = True
    def _accept_loop(fire_at, s=session):
        accept_loop_scheduled(s, fire_at)

    session._accept_loop = _accept_loop
    session.cancel_scheduled_loop = lambda s=session: cancel_loop(s)
    return session


def _arm_fire(session: Any, rec: dict, delay_s: float) -> None:
    delay_ms = max(0, int(delay_s * 1000))
    tid = rec["timer_id"]
    gen = rec["gen"]

    def _due(s=session, timer_id=tid, g=gen):
        _on_due(s, timer_id, g)

    _call_later(session, delay_ms, _due)


def _on_due(session: Any, timer_id: str, gen: int) -> None:
    tbl = _table()
    rec = tbl["wakes"].get(timer_id)
    if rec is None or rec.get("cancelled") or rec.get("gen") != gen:
        return
    if rec.get("kind") == "loop":
        # Bridge owns the actual inject; we only keep banner state.
        return
    if _is_working(session):
        def _retry(s=session, tid=timer_id, g=gen):
            _on_due(s, tid, g)
        _call_later(session, IDLE_POLL_MS, _retry)
        return
    rec["cancelled"] = True
    tbl["wakes"].pop(timer_id, None)
    key = rec.get("session_key")
    if key and tbl["by_session"].get(key) == timer_id:
        tbl["by_session"].pop(key, None)
    _paint_banner(session, None)
    prompt = rec.get("wake_prompt") or ""
    if prompt:
        try:
            _query(session, prompt)
        except Exception:
            pass


def _paint_banner(session: Any, fire_at: Optional[float]) -> None:
    try:
        session.next_wake_at = fire_at
        session.is_looping = bool(fire_at)
    except Exception:
        pass
    chrome = getattr(session, "chrome", None)
    if chrome is not None and hasattr(chrome, "wakeup_banner"):
        try:
            chrome.wakeup_banner(fire_at)
        except Exception:
            pass


def pending_for_session(session: Any) -> Optional[dict]:
    tbl = _table()
    tid = tbl["by_session"].get(_session_key(session))
    if not tid:
        return None
    return tbl["wakes"].get(tid)
