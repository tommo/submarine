#!/usr/bin/env python3
"""Replay a captured claude background-bash wire through the REAL host gate.

Fixture: `fixtures/cc_bg_wire.jsonl` (written by `check_e2e.py`). It is the
notification stream of one real session — Bash(run_in_background) → launch ack →
task_started → turn result → the task's completion → the CLI's own follow-up
turn — and it is fed, in order, into the shipped `BridgeEventRouter` +
`BackgroundTaskGate` + `SubmarineOutputView` (headless) so the assertions are
about host behaviour, not about a model.

What a user sees, and what this checks:

  1. the ⚙ row must still be there after the launch ack (the job is running);
  2. it must still be there after the turn's own `result` (the job outlives it);
  3. the task's completion must flip ⚙ → ✓ with what the job printed, and
     must NOT start a turn: the CLI already tells the model itself;
  4. the CLI's follow-up turn (`injected_turn` … `result{origin}`) is adopted
     as one turn of the sheet and closed by its tagged result;
  5. a repeated completion event must not flip anything twice.

    python3 sandbox/claude_bg/check_host_gate.py
    python3 sandbox/claude_bg/check_host_gate.py --fixture path.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for p in (_HERE, _ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

FIXTURES = {
    # turn ended while the job ran (the common case)
    "after": os.path.join(_HERE, "fixtures", "cc_bg_wire.jsonl"),
    # a foreground command kept the turn live past the job's completion
    "during": os.path.join(_HERE, "fixtures", "cc_bg_during.jsonl"),
}
FIXTURE = FIXTURES["after"]
SLEEP_OUTPUT = "CC_BG_DONE\n"


def read_sandbox_output(path):
    """The captured job's log is gone with its temp dir: stand in the text."""
    return SLEEP_OUTPUT


class _Settings(object):
    def __init__(self):
        self._d = {}

    def get(self, k, d=None):
        return self._d[k] if k in self._d else d

    def set(self, k, v):
        self._d[k] = v

    def erase(self, k):
        self._d.pop(k, None)


class _Window(object):
    """Just enough window for the headless output view."""

    def __init__(self):
        self._settings = _Settings()
        self._views = []

    def id(self):
        return 1

    def settings(self):
        return self._settings

    def folders(self):
        return []

    def views(self):
        return list(self._views)

    def active_view(self):
        return None

    def focus_view(self, v):
        pass

    def new_file(self):
        return None


class Sched(object):
    def __init__(self):
        self.pending = []

    def call_later(self, ms, fn):
        self.pending.append((ms, fn))
        return len(self.pending)

    def fire_due(self, max_ms=None):
        due = [p for p in self.pending if max_ms is None or p[0] <= max_ms]
        self.pending = [p for p in self.pending if p not in due]
        for _ms, fn in due:
            fn()
        return len(due)


class Chrome(object):
    def connecting_banner(self, *a, **k):
        pass

    def __getattr__(self, name):
        return lambda *a, **k: None


def build(read_output):
    from core.background import BackgroundTaskGate
    from core.events import BridgeEventRouter
    from core.turn import TurnController
    from ui.view import SubmarineOutputView

    sched = Sched()
    out = SubmarineOutputView(_Window())
    out.prompt("sandbox prompt")           # opens a conversation (state only)
    turn = TurnController(sched)
    queries = []
    sends = []

    surfaced = []
    holder = {}

    def send_poll(cb):
        # The bridge answers with what the CLI still reports running; a job
        # the host tracks is running until its completion event arrives.
        sends.append("poll")
        cb({"running": sorted(holder["bg"].task_tool_map)})
        return True

    bg = BackgroundTaskGate(
        turn, sched, out, backend="claude",
        send_poll=send_poll,
        on_surface=lambda: surfaced.append(1),
        read_output_file=read_output,
    )
    router = BridgeEventRouter(
        out, Chrome(), turn, sched, bg,
        send=lambda *a, **k: True, backend="claude",
        on_query=lambda body, display=None: queries.append((body, display)),
        on_phase=None,
    )
    holder["bg"] = bg
    return {"out": out, "turn": turn, "sched": sched, "bg": bg,
            "router": router, "queries": queries, "sends": sends,
            "surfaced": surfaced}


def statuses(h):
    return [getattr(t, "status", None) for t in h["out"].active_background_tools()]


def replay(fixture, read_output):
    h = build(read_output)
    steps = []
    for rec in fixture:
        if rec.get("kind") != "notify":
            continue
        payload = rec["payload"]
        typ = payload.get("type")
        sub = payload.get("subtype")
        dispatch(h, payload)
        h["sched"].fire_due()
        label = "%s/%s" % (typ, sub) if sub else str(typ)
        steps.append((sec(rec), label, {
            "bg_rows": statuses(h),
            "queries": len(h["queries"]),
            "turn_busy": h["turn"].busy,
        }))
    return h, steps


def sec(rec):
    return rec.get("t", 0.0)


def settle(h):
    """The host's `_on_done`: the query RPC returned, the turn idles."""
    h["turn"].end_live()
    h["sched"].fire_due()


def dispatch(h, payload):
    method = payload.get("_method")
    if method:
        h["router"].dispatch(method, {k: v for k, v in payload.items()
                                      if k != "_method"})
    else:
        h["router"].dispatch("message", payload)


def find(fixture, typ, sub=None):
    for rec in fixture:
        payload = rec.get("payload") or {}
        if payload.get("type") == typ and (sub is None or payload.get("subtype") == sub):
            return rec
    return None


def check(fixture, read_output=__import__("builtins").str):
    fails = []
    h = build(read_output)

    # The host sends the query RPC before any of these notifications arrive.
    h["turn"].begin_query()

    def feed(rec, label, burst_end=True):
        dispatch(h, rec["payload"])
        if burst_end:
            # Timers (the completion debounce, the poll) run between bursts
            # of the capture, not inside one: task_updated and
            # task_notification land in the same tick.
            h["sched"].fire_due()
        print("   %8.2fs  %-26s bg_rows=%s surfaced=%d busy=%s"
              % (sec(rec), label, statuses(h), len(h["surfaced"]),
                 h["turn"].busy))

    use = find(fixture, "tool_use")
    ack = find(fixture, "tool_result")
    started = find(fixture, "system", "task_started")
    result = find(fixture, "result")

    if not use or not ack or not started or not result:
        return ["fixture is missing a step: tool_use=%s ack=%s task_started=%s "
                "result=%s" % (bool(use), bool(ack), bool(started), bool(result))]

    bg_tool = use["payload"].get("id")
    bg_task = started["payload"]["data"].get("task_id")
    fg_ids = set()
    for rec in fixture:
        data = ((rec.get("payload") or {}).get("data") or {})
        if (rec.get("payload") or {}).get("subtype") == "task_started" \
                and data.get("is_backgrounded") is False:
            fg_ids.add(data.get("task_id"))

    print("--- replay (bg task %s, foreground tasks %s)" % (bg_task, sorted(fg_ids)))
    # Feed the whole stream in capture order — the order is part of what is
    # being tested (task_updated and task_notification race; the completion can
    # land before or after the turn's own result; the CLI's follow-up turn
    # comes after both).
    completed = False
    injected_open = False
    records = []
    for rec in fixture:
        payload = rec["payload"]
        typ, sub = payload.get("type"), payload.get("subtype")
        method = payload.get("_method")
        if method and method != "injected_turn":
            continue
        if not method and typ not in ("tool_use", "tool_result", "result", "system"):
            continue
        if typ == "system" and sub not in (
                "task_started", "task_updated", "task_notification"):
            continue
        records.append(rec)
    for i, rec in enumerate(records):
        payload = rec["payload"]
        typ, sub = payload.get("type"), payload.get("subtype")
        method = payload.get("_method")
        nxt = records[i + 1] if i + 1 < len(records) else None
        burst_end = nxt is None or (sec(nxt) - sec(rec)) > 0.25
        label = method or ("%s/%s" % (typ, sub) if sub else str(typ))
        feed(rec, label, burst_end)
        if method == "injected_turn":
            injected_open = True
            if not h["turn"].busy:
                fails.append("injected_turn did not make the session busy")
            cur = h["out"].current
            text = getattr(cur, "prompt", None) or getattr(cur, "text", "") or ""
            if not str(text).startswith("⚙"):
                fails.append("injected turn has no ⚙ prompt row: %r" % (text,))
        elif typ == "tool_use" and payload.get("background"):
            if len(statuses(h)) != 1:
                fails.append("the Bash row did not open as a background (⚙) row: "
                             "%s" % statuses(h))
        elif typ == "system" and sub == "task_started":
            if payload.get("data", {}).get("task_id") == bg_task and \
                    h["bg"].task_tool_map.get(bg_task) != bg_tool:
                fails.append("task_started did not bind task_id -> tool_use_id")
        elif typ == "tool_result" and payload.get("tool_use_id") == bg_tool:
            if not statuses(h):
                fails.append(
                    "BUG: the launch ack closed the ⚙ row — a running background "
                    "task looks finished the instant it starts, so the ⚙ strip is "
                    "empty and there is nothing left to reconcile. ack text was: %r"
                    % (str(payload.get("content"))[:120],))
        elif typ == "result" and payload.get("origin"):
            if not injected_open:
                fails.append("a tagged result arrived with no injected_turn before it")
            injected_open = False
            if h["turn"].busy:
                fails.append("the injected turn's result did not idle the session")
        elif typ == "result":
            if not completed and not statuses(h):
                fails.append("BUG: the turn's own result closed the ⚙ row while "
                             "the task was still running")
            settle(h)
        elif typ == "system" and sub in ("task_updated", "task_notification"):
            data = payload.get("data") or {}
            if data.get("task_id") == bg_task and \
                    (data.get("status") or (data.get("patch") or {}).get("status")) \
                    in ("completed", "failed", "cancelled", "canceled", "error"):
                completed = True

    # The completion: the row flips once, with the job's output, and no turn
    # is started by the host — the CLI runs its own (adopted above).
    if h["queries"]:
        fails.append("BUG: the host queried the model about the completion: %s"
                     % ([q[0][:60] for q in h["queries"]],))
    if statuses(h):
        fails.append("the ⚙ row survived completion: %s" % statuses(h))
    row = h["out"].find_tool_by_id(bg_tool)
    if row is None:
        fails.append("the background row is gone after completion")
    else:
        res = str(getattr(row, "result", "") or "")
        if "CC_BG_DONE" not in res:
            fails.append("the finished row does not carry the job's output "
                         "(looked for 'CC_BG_DONE'): %r" % res[:120])
        print("   row result: %r" % res[:160])
    if len(h["surfaced"]) != 1:
        fails.append("expected exactly one surface (unread/hints) for the "
                     "completion, got %d" % len(h["surfaced"]))

    # A repeated completion event (SDK re-send / poll + notification) must not
    # flip or surface anything again.
    for rec in fixture:
        payload = rec["payload"]
        if payload.get("type") == "system" and payload.get("subtype") in (
                "task_updated", "task_notification"):
            h["router"].dispatch("message", payload)
    h["sched"].fire_due()
    if len(h["surfaced"]) != 1 or h["queries"]:
        fails.append("a repeated completion event surfaced again "
                     "(surfaced=%d queries=%d)" % (len(h["surfaced"]),
                                                   len(h["queries"])))

    # Fallback: completion event lost entirely -> the poll's `running` list must
    # clear the stale ⚙ rather than leave it forever, and never start a turn.
    h2 = build(read_output)
    h2["turn"].begin_query()
    h2["router"].dispatch("message", use["payload"])
    h2["router"].dispatch("message", started["payload"])
    h2["router"].dispatch("message", ack["payload"])
    h2["bg"].reconcile(running=[])
    h2["sched"].fire_due()
    if statuses(h2):
        fails.append("reconcile(running=[]) left a stale ⚙ row: %s"
                     % statuses(h2))
    if h2["queries"]:
        fails.append("reconcile for claude started a turn with no completion "
                     "payload: %s" % (h2["queries"],))

    # `during`: a foreground Bash also gets task_started/task_notification from
    # the CLI. It was already answered inside the turn — no ⚙, nothing to show.
    for rec in fixture:
        payload = rec["payload"]
        data = payload.get("data") or {}
        if payload.get("subtype") == "task_started" \
                and data.get("is_backgrounded") is False \
                and data.get("task_id") in fg_ids:
            h3 = build(read_output)
            h3["router"].dispatch("message", payload)
            h3["router"].dispatch("message", {
                "type": "system", "subtype": "task_notification",
                "data": {"task_id": data.get("task_id"),
                         "tool_use_id": data.get("tool_use_id"),
                         "status": "completed",
                         "summary": data.get("description") or "foreground"},
            })
            h3["sched"].fire_due()
            if h3["queries"] or h3["surfaced"]:
                fails.append("a foreground command's completion surfaced: "
                             "queries=%s surfaced=%d" % (h3["queries"],
                                                          len(h3["surfaced"])))

    return fails


def load(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", default=None,
                    help="one capture (default: every fixtures/*.jsonl)")
    args = ap.parse_args(argv)
    paths = [args.fixture] if args.fixture else [
        p for p in (FIXTURES["after"], FIXTURES["during"]) if os.path.isfile(p)]
    if not paths:
        print("no fixtures — run sandbox/claude_bg/check_e2e.py first",
              file=sys.stderr)
        return 2
    bad = 0
    for path in paths:
        fixture = load(path)
        print("\n=== %s (%d records)" % (path, len(fixture)))
        fails = check(fixture, read_output=read_sandbox_output)
        if fails:
            bad += 1
            print("HOST GATE FAILS:")
            for f in fails:
                print(" - %s" % f)
        else:
            print("host gate handles this capture correctly")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
