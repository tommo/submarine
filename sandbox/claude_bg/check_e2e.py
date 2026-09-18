#!/usr/bin/env python3
"""Live claude-code background-bash e2e: what the bridge really puts on the wire.

The host's ⚙ (background) management for the claude backend is built on
assumptions nobody re-checked against a real session: that the launch ack is
recognizable, that `task_started` binds to the tool_use that spawned it, and
that the turn's `result` arrives while the task is still running.

This drives the real `bridge/claude_main.py` (Claude Agent SDK) in a throwaway
sandbox cwd, with a prompt that forces `Bash(run_in_background: true)`, and
records every notification. The capture is written to
`fixtures/cc_bg_wire.jsonl`; `check_host_gate.py` replays it through the real
host gate.

    python3 sandbox/claude_bg/check_e2e.py                  # model 'haiku'
    python3 sandbox/claude_bg/check_e2e.py --clean-env      # ignore the shell's
                                                            # ANTHROPIC_* overrides
    python3 sandbox/claude_bg/check_e2e.py --model claude-haiku-4-5

Env note: `ANTHROPIC_DEFAULT_HAIKU_MODEL` (this shell: deepseek) decides what
"haiku" resolves to. Background bash is a CLI-side feature, so the provider does
not change the wire shapes; `--clean-env` is for confirming with real Anthropic.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_BRIDGE = os.path.join(_ROOT, "bridge")
FIXTURE = os.path.join(_HERE, "fixtures", "cc_bg_wire.jsonl")

MARK = "CC_BG_DONE"
FIXTURES = {
    "after": os.path.join(_HERE, "fixtures", "cc_bg_wire.jsonl"),
    "during": os.path.join(_HERE, "fixtures", "cc_bg_during.jsonl"),
}

PROMPT = """Do not call EnterPlanMode, ExitPlanMode, Task, or Subagent.

Step 1: Call the Bash tool exactly once with run_in_background set to true and description "sandbox bg sleep":
sleep {sleep_s}; echo {mark}

Step 2: The sleep is in the background. Immediately after the Bash launch ack (do not wait for the sleep), reply with exactly one short line:
SANDBOX_LAUNCHED
Then stop and end your turn.
"""

# `--mode during` keeps the turn open until after the background job ends, so the
# plugin's question is answered: does the completion arrive as a task_notification
# when a turn is live, or is `session/…`-style task_updated all we ever get?
PROMPT_DURING = """Do not call EnterPlanMode, ExitPlanMode, Task, or Subagent.

Step 1: Call the Bash tool exactly once with run_in_background set to true and description "sandbox bg sleep":
sleep {sleep_s}; echo {mark}

Step 2: Then call the Bash tool once in the FOREGROUND (no run_in_background), command:
sleep {hold_s}

Step 3: After that foreground sleep returns, reply with exactly one line:
SANDBOX_DURING
"""


class Bridge(object):
    """NDJSON JSON-RPC client for one bridge subprocess."""

    def __init__(self, argv, cwd, env):
        self.proc = subprocess.Popen(
            argv, cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        self._id = 0
        self._lock = threading.Lock()
        self._results = {}
        self.events = []          # (t, kind, payload)
        self.t0 = time.time()
        self.stderr = []
        self.closed = False
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _now(self):
        return round(time.time() - self.t0, 3)

    def _note(self, kind, payload):
        with self._lock:
            self.events.append((self._now(), kind, payload))

    def _read(self):
        for line in iter(self.proc.stdout.readline, ""):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self._note("garbage", {"line": line[:200]})
                continue
            if msg.get("method"):
                params = msg.get("params") or {}
                self._note("notify", params)
            elif msg.get("id") is not None:
                with self._lock:
                    self._results[msg["id"]] = msg

    def _read_err(self):
        for line in iter(self.proc.stderr.readline, ""):
            line = line.rstrip()
            if line:
                self.stderr.append(line[-300:])
                if len(self.stderr) > 40:
                    del self.stderr[0]

    def request(self, method, params, timeout=60.0):
        self._id += 1
        rid = self._id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        self._note("request", {"method": method, "id": rid})
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except Exception as e:
            return {"error": {"message": "write failed: %s" % e}}
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                hit = self._results.pop(rid, None)
            if hit is not None:
                return hit
            if self.proc.poll() is not None:
                time.sleep(0.3)
                with self._lock:
                    hit = self._results.pop(rid, None)
                return hit or {"error": {"message": "bridge exited"}}
            time.sleep(0.05)
        return {"error": {"message": "timeout waiting for %s" % method}}

    def wait_for(self, predicate, timeout=60.0, poll=0.2):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for _, kind, payload in self.events:
                    if kind == "notify" and predicate(payload):
                        return payload
            time.sleep(poll)
        return None

    def close(self):
        try:
            self.request("shutdown", {}, timeout=10)
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def _env(clean):
    env = dict(os.environ)
    if clean:
        for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
                  "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
                  "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                  "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
                  "DEEPSEEK_API_KEY"):
            env.pop(k, None)
    # The bridge is a subprocess of a plugin we are NOT running: no MCP socket.
    env["SUBMARINE_SANDBOX"] = "cc_bg"
    return env


def classify(events):
    """Bridge-side view of one background bash: what arrived, in what order."""
    out = {
        "bg_tool_use": None,      # tool_use with background=true (Bash)
        "bg_tool_result": None,   # its launch ack text
        "task_started": [],       # system/task_started
        "task_updated": [],
        "task_notification": [],
        "turn_results": [],       # message/result (turn end)
        "text": [],
        "order": [],
    }
    pending_ack = None
    for t, kind, payload in events:
        if kind != "notify":
            continue
        typ = payload.get("type")
        if typ == "tool_use":
            out["order"].append((t, "tool_use", payload.get("name")))
            if payload.get("background"):
                out["bg_tool_use"] = payload
                pending_ack = payload.get("id")
        elif typ == "tool_result":
            out["order"].append((t, "tool_result", ""))
            if payload.get("tool_use_id") == pending_ack:
                content = payload.get("content")
                if isinstance(content, list):
                    content = "\n".join(
                        str(c.get("text") if isinstance(c, dict) else c)
                        for c in content)
                out["bg_tool_result"] = {
                    "text": str(content or "")[:400],
                    "is_error": bool(payload.get("is_error")),
                }
        elif typ == "system":
            sub = payload.get("subtype") or ""
            data = payload.get("data") or {}
            if sub == "task_started":
                out["task_started"].append(data)
                out["order"].append((t, "task_started", data.get("task_id")))
            elif sub == "task_updated":
                out["task_updated"].append(data)
                out["order"].append((t, "task_updated", data.get("patch")))
            elif sub == "task_notification":
                out["task_notification"].append(data)
                out["order"].append(
                    (t, "task_notification", data.get("status")))
        elif typ == "result":
            out["turn_results"].append(payload)
            out["order"].append((t, "result", payload.get("status")))
        elif typ == "text_delta":
            out["text"].append(payload.get("text") or "")
    return out


def report(r, bridge, sleep_s, model, mode="after"):
    print("model requested: %s" % model)
    print("resolved: ANTHROPIC_DEFAULT_HAIKU_MODEL=%s ANTHROPIC_MODEL=%s"
          % (os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "<unset>"),
             os.environ.get("ANTHROPIC_MODEL", "<unset>")))
    print("\n--- wire order (t, kind, detail)")
    for row in r["order"]:
        print("   %8.2fs  %-18s %s" % (row[0], row[1], row[2]))
    print("\n--- background tool_use")
    tu = r["bg_tool_use"]
    if tu:
        inp = tu.get("input") or {}
        print("   name=%s id=%s run_in_background=%s"
              % (tu.get("name"), tu.get("id"), inp.get("run_in_background")))
    print("--- launch ack (tool_result)")
    ack = r["bg_tool_result"]
    print("   %s" % json.dumps(ack, ensure_ascii=False)[:500])
    print("--- task_started")
    for d in r["task_started"]:
        print("   %s" % json.dumps(
            {k: d.get(k) for k in
             ("task_id", "tool_use_id", "task_type", "is_backgrounded",
              "description", "summary")}, ensure_ascii=False)[:400])
    print("--- task_notification")
    for d in r["task_notification"]:
        print("   %s" % json.dumps(
            {k: d.get(k) for k in
             ("task_id", "tool_use_id", "status", "summary", "output_file")},
            ensure_ascii=False)[:400])
    print("--- turn results: %d" % len(r["turn_results"]))
    print("--- text: %s" % json.dumps("".join(r["text"])[:300], ensure_ascii=False))

    fails = []
    if not tu:
        fails.append("no Bash tool_use with background=true: the model did not "
                     "launch a background task (prompt/model issue)")
    if not r["task_started"]:
        fails.append("no task_started system message")
    else:
        started = r["task_started"][0]
        if not started.get("tool_use_id"):
            fails.append("task_started carries NO tool_use_id: the host cannot "
                         "bind the task to its ⚙ row")
        elif tu and started.get("tool_use_id") != tu.get("id"):
            fails.append("task_started.tool_use_id=%s != tool_use.id=%s"
                         % (started.get("tool_use_id"), tu.get("id")))
    # The bridge-side contract the host depends on: a completion event must
    # reach the plugin, as either task_notification (turn still live) or a
    # terminal task_updated (turn already over — the only thing this CLI sends).
    terminal = [d for d in r["task_updated"]
                if (d.get("patch") or {}).get("status", "") in (
                    "completed", "failed", "cancelled", "canceled", "error")]
    print("--- task_updated: %s" % json.dumps(
        [d.get("patch") for d in r["task_updated"]], ensure_ascii=False)[:300])
    if not r["task_notification"] and not terminal:
        fails.append("no completion event reached the plugin (neither "
                     "task_notification nor a terminal task_updated)")
    if not r["turn_results"]:
        fails.append("no turn result: the model did not end its turn")
    t_note = None
    for t, k, d in r["order"]:
        if k == "task_notification":
            t_note = t
            break
    if r["turn_results"] and t_note is not None:
        t_result = [t for t, k, _d in r["order"] if k == "result"][0]
        print("\nresult at %.2fs, first task_notification at %.2fs"
              % (t_result, t_note))
        if mode == "after" and t_note < t_result:
            fails.append("after-mode: task_notification arrived before the turn "
                         "result — the sleep finished inside the turn, so the "
                         "post-result drain was not exercised")
        if mode == "during" and t_note > t_result:
            fails.append("during-mode: the foreground hold did not keep the turn "
                         "open past the job's completion")
    if bridge.stderr:
        tail = [l for l in bridge.stderr if "Traceback" in l or "Error" in l]
        if tail:
            print("\n--- bridge stderr (errors)")
            for l in tail[-6:]:
                print("   %s" % l)
    return fails


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--sleep", type=int, default=25,
                    help="seconds the background job sleeps")
    ap.add_argument("--mode", choices=("after", "during"), default="after",
                    help="after: the turn ends while the job runs (the common "
                         "case); during: a foreground sleep keeps the turn live "
                         "past the job's completion")
    ap.add_argument("--hold", type=int, default=0,
                    help="during: seconds of foreground sleep (default sleep+8)")
    ap.add_argument("--timeout", type=float, default=240.0,
                    help="seconds to wait for the completion notification")
    ap.add_argument("--clean-env", action="store_true",
                    help="strip the shell's ANTHROPIC_* overrides")
    ap.add_argument("--keep", action="store_true",
                    help="keep the sandbox dir")
    ap.add_argument("--no-fixture", action="store_true")
    ap.add_argument("--no-host", action="store_true",
                    help="skip the host-gate replay of this capture")
    args = ap.parse_args(argv)

    sandbox = tempfile.mkdtemp(prefix="cc_bg_sandbox_")
    print("sandbox: %s" % sandbox)
    bridge = Bridge([sys.executable, os.path.join(_BRIDGE, "claude_main.py")],
                    cwd=sandbox, env=_env(args.clean_env))
    try:
        init = bridge.request("initialize", {
            "cwd": sandbox,
            "model": args.model,
            "permission_mode": "bypassPermissions",
            "allowed_tools": [],
            "agent_id": "cc-bg-sandbox",
        }, timeout=180)
        if init.get("error"):
            print("initialize FAILED: %s" % json.dumps(init)[:800])
            return 2
        print("initialized: %s" % json.dumps(init.get("result") or {})[:400])

        if args.mode == "during":
            hold = args.hold or (args.sleep + 8)
            prompt = PROMPT_DURING.format(
                sleep_s=args.sleep, hold_s=hold, mark=MARK)
        else:
            prompt = PROMPT.format(sleep_s=args.sleep, mark=MARK)
        res = bridge.request("query", {"prompt": prompt}, timeout=args.timeout)
        if res.get("error"):
            print("query error: %s" % json.dumps(res)[:400])

        # Wait for the completion. Which message carries it depends on whether
        # the turn outlived the job: task_notification, or a terminal
        # task_updated (all the CLI sends once the turn has ended).
        def _completed(p):
            if p.get("type") != "system":
                return False
            data = p.get("data") or {}
            if p.get("subtype") == "task_notification":
                return bool(data.get("status"))
            return (p.get("subtype") == "task_updated"
                    and (data.get("patch") or {}).get("status") in (
                        "completed", "failed", "cancelled", "canceled", "error"))

        note = bridge.wait_for(_completed, timeout=args.timeout)
        if note is None:
            print("!! no completion event within %.0fs" % args.timeout)
        time.sleep(1.5)

        poll = bridge.request("poll_bg_tasks", {}, timeout=30)
        print("poll_bg_tasks -> %s" % json.dumps(poll.get("result") or poll)[:300])

        r = classify(bridge.events)
        fails = report(r, bridge, args.sleep, args.model, args.mode)

        fixture = FIXTURES[args.mode]
        if not args.no_fixture:
            os.makedirs(os.path.dirname(fixture), exist_ok=True)
            with open(fixture, "w", encoding="utf-8") as f:
                for t, kind, payload in bridge.events:
                    f.write(json.dumps(
                        {"t": t, "kind": kind, "payload": payload},
                        ensure_ascii=False) + "\n")
            print("\ncapture -> %s" % fixture)

        if not args.no_host:
            print("\n--- host replay of this capture")
            sys.path.insert(0, _HERE)
            import check_host_gate as hg
            host_fails = hg.check(
                [{"t": t, "kind": k, "payload": p} for t, k, p in bridge.events
                 if k == "notify"],
                read_output=hg.read_sandbox_output)
            if host_fails:
                fails.extend("host: %s" % f for f in host_fails)

        if fails:
            print("\nE2E FAILS:")
            for f in fails:
                print(" - %s" % f)
            return 1
        print("\nbridge side matches the host's assumptions")
        return 0
    finally:
        bridge.close()
        if not args.keep:
            shutil.rmtree(sandbox, ignore_errors=True)
        else:
            print("kept sandbox: %s" % sandbox)


if __name__ == "__main__":
    raise SystemExit(main())
