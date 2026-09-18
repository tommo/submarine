#!/usr/bin/env python3
"""Replay a captured claude background-bash wire through the REAL host gate.

Fixture: `fixtures/cc_bg_wire.jsonl` (written by `check_e2e.py`). It is the
notification stream of one real session — Bash(run_in_background) → launch ack →
task_started → turn result → the task's completion — and it is fed, in order,
into the shipped `BridgeEventRouter` + `BackgroundTaskGate` + `SubmarineOutputView`
(headless) so the assertions are about host behaviour, not about a model.

What a user sees, and what this checks:

  1. the ⚙ row must still be there after the launch ack (the job is running);
  2. it must still be there after the turn's own `result` (the job outlives it);
  3. the task's completion must flip ⚙ → ✓ AND tell the user: one
     `<task-notification>` turn carrying what the job printed;
  4. a repeated completion event must not produce a second notification turn.

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

    bg = BackgroundTaskGate(
        turn, sched, out, backend="claude",
        send_poll=lambda cb: (sends.append("poll"), cb({"running": []}), True)[-1],
        on_query=lambda body, display: queries.append((body, display)),
        on_surface=lambda: queries.append(("SURFACE", "")),
        read_output_file=read_output,
    )
    router = BridgeEventRouter(
        out, Chrome(), turn, sched, bg,
        send=lambda *a, **k: True, backend="claude",
        on_query=lambda body, display=None: queries.append((body, display)),
        on_phase=None,
    )
    return {"out": out, "turn": turn, "sched": sched, "bg": bg,
            "router": router, "queries": queries, "sends": sends}


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
        if typ == "result":
            pass
        h["router"].dispatch("message", payload)
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
    """The host's `_on_done`: the query RPC returned, the turn idles, and a
    buffer held while busy is flushed."""
    h["turn"].end_live()
    h["sched"].fire_due()
    if h["bg"].pending_notifications:
        h["bg"].flush()
    h["sched"].fire_due()


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

    def feed(rec, label):
        h["router"].dispatch("message", rec["payload"])
        h["sched"].fire_due()
        print("   %8.2fs  %-26s bg_rows=%s queries=%d busy=%s"
              % (sec(rec), label, statuses(h), len(h["queries"]),
                 h["turn"].busy))

    use = find(fixture, "tool_use")
    ack = find(fixture, "tool_result")
    started = find(fixture, "system", "task_started")
    result = find(fixture, "result")
    done = find(fixture, "system", "task_updated")
    notif = find(fixture, "system", "task_notification")

    if not use or not ack or not started or not result:
        return ["fixture is missing a step: tool_use=%s ack=%s task_started=%s "
                "result=%s" % (bool(use), bool(ack), bool(started), bool(result))]

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
    # land before or after the turn's own result).
    completed = False
    n_before = len(h["queries"])
    for rec in fixture:
        payload = rec["payload"]
        typ, sub = payload.get("type"), payload.get("subtype")
        if typ not in ("tool_use", "tool_result", "result", "system"):
            continue
        if typ == "system" and sub not in (
                "task_started", "task_updated", "task_notification"):
            continue
        if typ == "tool_use" and not payload.get("background"):
            # foreground rows are noise for this test; the gate still sees them
            pass
        feed(rec, "%s/%s" % (typ, sub) if sub else str(typ))
        if typ == "tool_use" and payload.get("background"):
            if len(statuses(h)) != 1:
                fails.append("the Bash row did not open as a background (⚙) row: "
                             "%s" % statuses(h))
        elif typ == "system" and sub == "task_started":
            if payload.get("data", {}).get("task_id") == bg_task and \
                    h["bg"].task_tool_map.get(bg_task) != use["payload"].get("id"):
                fails.append("task_started did not bind task_id -> tool_use_id")
        elif typ == "tool_result" and payload.get("tool_use_id") == use["payload"].get("id"):
            if not statuses(h):
                fails.append(
                    "BUG: the launch ack closed the ⚙ row — a running background "
                    "task looks finished the instant it starts, so the ⚙ strip is "
                    "empty and there is nothing left to reconcile. ack text was: %r"
                    % (str(payload.get("content"))[:120],))
        elif typ == "result" and not completed:
            if not statuses(h):
                fails.append("BUG: the turn's own result closed the ⚙ row while "
                             "the task was still running")
        elif typ == "system" and sub in ("task_updated", "task_notification"):
            data = payload.get("data") or {}
            if data.get("task_id") == bg_task and \
                    (data.get("status") or (data.get("patch") or {}).get("status")) \
                    in ("completed", "failed", "cancelled", "canceled", "error"):
                completed = True
        elif typ == "result":
            pass

    # The completion. Whatever the CLI calls it, the host must surface it once —
    # but only once the turn is over: while it is busy the gate holds the buffer
    # and `_on_done` retries the flush, which is what `settle` emulates.
    settle(h)
    if not h["queries"]:
        fails.append(
            "BUG: the task completed and the user was told nothing — no "
            "notification turn. (This CLI reports a background bash completion "
            "as task_updated{status:completed}, and the host only flips the row.)")
    else:
        if len(h["queries"]) != n_before + 1:
            fails.append("expected exactly one notification turn, got %d: %s"
                         % (len(h["queries"]) - n_before,
                            [q[0][:60] for q in h["queries"][n_before:]]))
        body, _display = h["queries"][n_before]
        if "CC_BG_DONE" not in body:
            fails.append("the notification turn does not carry the job's output "
                         "(looked for 'CC_BG_DONE')")
        if not body.startswith("<task-notification>"):
            fails.append("notification body is not a <task-notification> block: "
                         "%r" % body[:80])
        for tid in fg_ids:
            if tid in body:
                fails.append("a FOREGROUND task (%s) produced a notification: %r"
                             % (tid, body[:120]))
        print("   notification body: %r" % body[:200])

    if statuses(h):
        fails.append("the ⚙ row survived completion: %s" % statuses(h))

    # A repeated completion event (SDK re-send / poll + notification) must not
    # start a second turn.
    for rec in fixture:
        payload = rec["payload"]
        if payload.get("type") == "system" and payload.get("subtype") in (
                "task_updated", "task_notification"):
            h["router"].dispatch("message", payload)
    settle(h)
    if len(h["queries"]) != n_before + 1:
        fails.append("a repeated completion event produced another notification "
                     "turn (%d -> %d)" % (n_before + 1, len(h["queries"])))

    # Fallback: completion event lost entirely -> the poll's `running` list must
    # clear the stale ⚙ rather than leave it forever.
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
    # the CLI. It was already answered inside the turn — no ⚙, no wake.
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
            if h3["queries"]:
                fails.append("a foreground command produced a notification turn: "
                             "%s" % (h3["queries"],))

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
