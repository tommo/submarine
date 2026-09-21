#!/usr/bin/env python3
"""CLI client for managing live Submarine sessions from outside the editor.

Usage:
  python3 submarine_sessions.py list [--scope all|window|children] [--json]
  python3 submarine_sessions.py view REF [--mode tail|text|edits] [--turns N]
  python3 submarine_sessions.py chat REF "prompt" [--wait] [--queue POLICY]
  python3 submarine_sessions.py interrupt REF
  python3 submarine_sessions.py --manual        # the full manual
  python3 submarine_sessions.py help            # short usage

REF is an agent_id, a session_id, a view id, or a unique session name.

`chat --wait` sends the prompt, waits for the turn to finish, then asks for the
transcript tail and prints the reply, so one command shows the answer.

Requires Sublime Text running with Submarine loaded (socket up).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time

DEFAULT_TIMEOUT = 30.0
WAIT_TIMEOUT = 600.0


def _socket_path(override: str = "") -> str:
    if override:
        return override
    try:
        from plat.constants import MCP_SOCKET_PATH
        return MCP_SOCKET_PATH
    except Exception:
        import tempfile
        return os.path.join(tempfile.gettempdir(), "submarine_mcp.sock")


def send(req: dict, timeout: float = DEFAULT_TIMEOUT, path: str = "") -> dict:
    """One newline-terminated JSON request per connection (socket protocol)."""
    sock_path = _socket_path(path)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(sock_path)
        sock.sendall((json.dumps(req) + "\n").encode())
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            if b"\n" in chunk:
                break
        sock.close()
        if not data.strip():
            return {"error": "empty response from Sublime"}
        return json.loads(data.decode())
    except FileNotFoundError:
        return {"error": "socket missing: %s" % sock_path,
                "hint": "Launch Sublime Text with Submarine loaded, then retry."}
    except socket.timeout:
        return {"error": "timed out after %.0fs (the prompt may still be "
                         "running; use `view` to follow it)" % timeout}
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}


def call(action: str, timeout: float = DEFAULT_TIMEOUT, path: str = "",
         caller: dict = None, **fields) -> dict:
    """Send one `op:"sessions"` request and return its envelope."""
    req = {"op": "sessions", "action": action}
    req.update({k: v for k, v in fields.items() if v is not None})
    req["caller"] = caller or {"kind": "cli", "pid": os.getpid(), "cwd": os.getcwd()}
    resp = send(req, timeout=timeout, path=path)
    if not isinstance(resp, dict):
        return {"ok": False, "error": "malformed response", "raw": resp}
    body = resp.get("result")
    if isinstance(body, dict):
        out = dict(body)
        if resp.get("error") and not out.get("error"):
            out["error"] = resp["error"]
        return out
    if resp.get("error"):
        out = {"ok": False, "error": resp["error"]}
        if resp.get("hint"):
            out["hint"] = resp["hint"]
        return out
    if body is None:
        return {"ok": False,
                "error": "Sublime answered nothing for op:sessions",
                "hint": "reload the Submarine package (the op needs the new code)"}
    return {"ok": False, "error": "unexpected response: %s" % json.dumps(resp)[:200]}


# ─── rendering ──────────────────────────────────────────────────────────────


def _ago(ts) -> str:
    try:
        secs = max(0, int(time.time() - float(ts)))
    except (TypeError, ValueError):
        return "-"
    for limit, div, unit in ((60, 1, "s"), (3600, 60, "m"),
                             (86400, 3600, "h"), (10 ** 9, 86400, "d")):
        if secs < limit:
            return "%d%s" % (secs // div, unit)
    return "?"


def _window_of(row: dict) -> str:
    """The row's parent window id as text, or "" when it has none.

    `0` is a valid window id, so the test is None/""-based, not truthiness.
    """
    window = (row.get("view") or {}).get("window")
    if window is None or window == "":
        return ""
    return str(window)


def _window_label(key: str, rows: list) -> str:
    if not key:
        return "No window (%d)" % len(rows)
    project = next((str((r.get("view") or {}).get("project")) for r in rows
                    if (r.get("view") or {}).get("project")), "")
    return "Window %s%s (%d)" % (key, " — %s" % project if project else "", len(rows))


def _window_groups(rows: list) -> list:
    """Rows grouped by parent window, windowless rows last.

    The window is what tells two same-named sessions apart, so the human table
    heads each group instead of printing one flat list. Row order inside a group
    is the caller's (last access desc), live rows first; JSON output stays flat.
    """
    by_key = {}
    for row in rows:
        by_key.setdefault(_window_of(row), []).append(row)

    def _sort_key(key):
        if not key:
            return (2, 0, "")
        return (0, int(key), "") if key.isdigit() else (1, 0, key)

    groups = []
    for key in sorted(by_key, key=_sort_key):
        group = by_key[key]
        groups.append({
            "label": _window_label(key, group),
            "rows": ([r for r in group if r.get("kind") == "live"] +
                     [r for r in group if r.get("kind") != "live"]),
        })
    return groups


def _row_line(r: dict) -> str:
    view = r.get("view") or {}
    agent = str(r.get("agent_id") or "")
    sid = str(r.get("session_id") or "")
    return ("%-9s %-22s %-7s %-6s %-6s %-5s %-4s %s"
            % (str(r.get("state") or "")[:9],
               (str(r.get("name") or "(unnamed)"))[:22],
               str(r.get("backend") or "")[:7],
               (str(view.get("view_id")) if view.get("bound") else "-")[:6],
               _window_of(r)[:6] or "-",
               str(r.get("query_count") if r.get("query_count") is not None else "-")[:5],
               _ago(r.get("last_access"))[:4],
               ("%s %s" % (agent[:26], sid[:14])).strip()))


def render_list(body: dict) -> str:
    rows = body.get("sessions") or []
    if not rows:
        return "no sessions"
    head = ("%-9s %-22s %-7s %-6s %-6s %-5s %-4s %s"
            % ("STATE", "NAME", "BACKEND", "VIEW", "WINDOW", "Q", "AGE", "AGENT / SESSION"))
    lines = [head, "-" * len(head)]
    for group in _window_groups(rows):
        lines.append("")
        lines.append(group["label"])
        lines.extend("  " + _row_line(r) for r in group["rows"])
    lines.append("")
    lines.append("%d session(s) — %s" % (
        int(body.get("count") or len(rows)),
        ", ".join("%s=%d" % (k, v) for k, v in sorted((body.get("states") or {}).items()))))
    return "\n".join(lines)


def render_view(body: dict) -> str:
    turns = body.get("turns") or []
    lines = ["%s (%s) — %d turn(s) on disk%s" % (
        (body.get("session") or {}).get("name") or "session",
        (body.get("session") or {}).get("backend") or "?",
        int(body.get("turn_count") or 0),
        ", showing %d" % len(turns) if len(turns) < int(body.get("turn_count") or 0) else "")]
    path = body.get("transcript")
    if path:
        lines.append(path)
    for t in turns:
        lines.append("")
        lines.append("▌ %s" % str(t.get("prompt") or "").strip())
        if t.get("tools"):
            lines.append("  ⚙ %s" % ", ".join(str(x) for x in t["tools"]))
        reply = str(t.get("reply") or "").strip()
        if reply:
            lines.append(reply)
        if t.get("reply_truncated") or t.get("prompt_truncated"):
            lines.append("  … (truncated)")
    return "\n".join(lines)


def render_edits(body: dict) -> str:
    edits = body.get("edits") or []
    if not edits:
        return "no edits"
    lines = ["%d of %d edit(s)" % (len(edits), int(body.get("total") or len(edits)))]
    for e in edits:
        lines.append("  %-6s %s%s" % (str(e.get("tool") or "?"),
                                      str(e.get("file_path") or "?"),
                                      (" :%s" % e.get("line")) if e.get("line") else ""))
    return "\n".join(lines)


def render_chat(body: dict) -> str:
    session = body.get("session") or {}
    label = "%s (%s)" % (session.get("name") or "(unnamed)",
                         str(session.get("agent_id") or "")[:20])
    action = str(body.get("action") or "?")
    verb = {"sent": "sent", "queued": "queued behind the turn",
            "woke": "woke, prompt delivered once connected",
            "send_now": "interrupting and sending", "pending": "accepted"}.get(action, action)
    out = ["%s → %s" % (verb, label)]
    if body.get("duplicate"):
        out.append("(duplicate idem key: not sent again)")
    return "\n".join(out)


def render_interrupt(body: dict) -> str:
    session = body.get("session") or {}
    phase = body.get("turn_phase") or "?"
    lines = ["%s: %s" % (session.get("name") or "(unnamed)",
                         "interrupted" if body.get("interrupted") else "nothing running")]
    lines.append("turn_phase=%s settling=%s user_cancelled=%s"
                 % (phase, bool(body.get("settling")), bool(body.get("user_cancelled"))))
    if body.get("settling"):
        lines.append("the bridge acknowledge is still outstanding; the host's "
                     "settle timer reconciles it if it never arrives")
    return "\n".join(lines)


RENDERERS = {
    "list": render_list,
    "view": lambda b: render_edits(b) if b.get("mode") == "edits"
    else (b.get("text") if b.get("mode") == "text" else render_view(b)),
    "chat": render_chat,
    "interrupt": render_interrupt,
}


def _fail(env: dict) -> int:
    err = env.get("error") or "unknown error"
    data = env.get("data") if isinstance(env.get("data"), dict) else {}
    sys.stderr.write("error: %s\n" % err)
    code = data.get("code")
    if code:
        sys.stderr.write("  code: %s\n" % code)
    for cand in (data.get("candidates") or [])[:12]:
        sys.stderr.write("  candidate: %s  %s  %s  %s\n" % (
            cand.get("state"), cand.get("name"), cand.get("backend"),
            cand.get("agent_id")))
    hint = data.get("hint") or env.get("hint")
    if hint:
        sys.stderr.write("  hint: %s\n" % hint)
    return 1


# ─── commands ───────────────────────────────────────────────────────────────


def cmd_list(args) -> int:
    env = call("list", timeout=args.timeout, path=args.socket,
               scope=args.scope, parent=args.parent, window=args.window)
    if not env.get("ok"):
        return _fail(env)
    body = env.get("data") or {}
    print(json.dumps(body, indent=2) if args.json else render_list(body))
    return 0


def cmd_view(args) -> int:
    env = call("view", timeout=args.timeout, path=args.socket, ref=args.ref,
               mode=args.mode, turns=args.turns, max_chars=args.max_chars,
               offset=args.offset, limit=args.limit, file_path=args.file)
    if not env.get("ok"):
        return _fail(env)
    body = env.get("data") or {}
    print(json.dumps(body, indent=2) if args.json else RENDERERS["view"](body))
    return 0


def cmd_chat(args) -> int:
    env = call("chat", timeout=args.timeout, path=args.socket, ref=args.ref,
               prompt=args.prompt, queue=args.queue, wait=args.wait, idem=args.idem)
    if not env.get("ok"):
        return _fail(env)
    body = env.get("data") or {}
    print(json.dumps(body, indent=2) if args.json else render_chat(body))
    if not args.wait:
        return 0
    # The hand-off answers before the turn ends, so the reply is a second call.
    follow = call("view", timeout=args.wait_timeout, path=args.socket,
                  ref=args.ref, mode="tail", turns=1)
    if not follow.get("ok"):
        return _fail(follow)
    tail = follow.get("data") or {}
    if args.json:
        print(json.dumps(tail, indent=2))
        return 0
    turns = tail.get("turns") or []
    if not turns:
        print("(no turn in the transcript yet)")
        return 0
    reply = str(turns[-1].get("reply") or "").strip()
    print("\n%s" % (reply or "(the turn produced no reply text)"))
    return 0


def cmd_interrupt(args) -> int:
    env = call("interrupt", timeout=args.timeout, path=args.socket, ref=args.ref)
    if not env.get("ok"):
        return _fail(env)
    body = env.get("data") or {}
    print(json.dumps(body, indent=2) if args.json else render_interrupt(body))
    return 0


def render_pending(body: dict) -> str:
    modals = body.get("modals") or []
    if not modals:
        return "nothing pending"
    out = []
    for m in modals:
        kind, p = m.get("kind"), m.get("payload") or {}
        if kind == "question":
            q = p.get("question") or {}
            out.append("? %s (%d of %d)  qid %s" % (q.get("header") or "Question",
                       int(p.get("current_idx") or 0) + 1, p.get("total") or 1, p.get("qid")))
            out.append("  %s" % q.get("question", ""))
            for i, o in enumerate(q.get("options") or []):
                label = o.get("label", o) if isinstance(o, dict) else o
                desc = o.get("description", "") if isinstance(o, dict) else ""
                out.append("  %d. %s%s" % (i + 1, label, ("  — " + desc) if desc else ""))
            if q.get("multiSelect"):
                out.append("  (multiSelect: answer --options 1,3)")
        elif kind == "permission":
            inp = p.get("tool_input") or {}
            head = inp.get("command") or inp.get("file_path") or inp.get("path") or ""
            out.append("! allow %s?  id %s%s" % (p.get("tool"), p.get("id"),
                       ("  " + str(head)[:120]) if head else ""))
        elif kind == "plan":
            out.append("plan needs approval  id %s  %s" % (p.get("id"), p.get("plan_file") or ""))
        else:
            out.append("%s pending" % kind)
    return "\n".join(out)


def cmd_pending(args) -> int:
    env = call("pending", timeout=args.timeout, path=args.socket, ref=args.ref)
    if not env.get("ok"):
        return _fail(env)
    body = env.get("data") or {}
    print(json.dumps(body, indent=2) if args.json else render_pending(body))
    return 0


def cmd_answer(args) -> int:
    fields = {"kind": args.kind}
    if args.kind == "question":
        if args.text:
            fields["text"] = args.text
        elif args.options:
            fields["options"] = [x.strip() for x in args.options.split(",") if x.strip()]
        elif args.option:
            fields["option"] = args.option
        else:
            print("error: give an option number/label, --options 1,3 or --text", file=sys.stderr)
            return 2
        if args.id is not None:
            fields["qid"] = args.id
    else:
        if not args.option:
            print("error: give the response (allow/deny/allow_session/allow_all, "
                  "approve/reject)", file=sys.stderr)
            return 2
        fields["response"] = args.option
        if args.id is not None:
            fields["id"] = args.id
    env = call("answer", timeout=args.timeout, path=args.socket, ref=args.ref, **fields)
    if not env.get("ok"):
        return _fail(env)
    body = env.get("data") or {}
    if args.json:
        print(json.dumps(body, indent=2))
    else:
        print("answered %s" % body.get("answered"))
        rest = render_pending(body)
        if rest != "nothing pending":
            print("still pending:\n" + rest)
    return 0


def cmd_backends(args) -> int:
    env = call("backends", timeout=args.timeout, path=args.socket)
    if not env.get("ok"):
        return _fail(env)
    body = env.get("data") or {}
    if args.json:
        print(json.dumps(body, indent=2))
        return 0
    for b in body.get("backends") or []:
        models = ", ".join(m[0] for m in (b.get("models") or []) if m)
        print("%-10s %-22s %s%s" % (b.get("name"), b.get("label") or "",
                                    "" if b.get("available", True) else "(unavailable) ",
                                    models))
    print()
    for w in body.get("windows") or []:
        print("window %-4s %s  (%s session%s)" % (w.get("id"), w.get("project") or "no folder",
                                                  w.get("sessions"), "" if w.get("sessions") == 1 else "s"))
    print("default: %s" % body.get("default"))
    return 0


def cmd_create(args) -> int:
    fields = {"backend": args.backend, "model": args.model, "name": args.name,
              "prompt": args.prompt, "idem": args.idem}
    if args.window is not None:
        fields["window"] = args.window
    if args.project:
        fields["project"] = args.project
    env = call("create", timeout=max(args.timeout, WAIT_TIMEOUT if args.prompt else args.timeout),
               path=args.socket, **{k: v for k, v in fields.items() if v is not None})
    if not env.get("ok"):
        return _fail(env)
    body = env.get("data") or {}
    if args.json:
        print(json.dumps(body, indent=2))
        return 0
    ref = body.get("session") or {}
    print("created %s  %s  backend=%s%s" % (ref.get("name") or "(unnamed)", ref.get("agent_id"),
                                            body.get("backend"),
                                            ("  prompt: " + render_chat(body)) if body.get("action") else ""))
    return 0


def cmd_rename(args) -> int:
    env = call("rename", timeout=args.timeout, path=args.socket, ref=args.ref, name=args.name)
    if not env.get("ok"):
        return _fail(env)
    body = env.get("data") or {}
    print(json.dumps(body, indent=2) if args.json else "renamed: %s → %s" % (body.get("from"), body.get("name")))
    return 0


def cmd_close(args) -> int:
    env = call("close", timeout=args.timeout, path=args.socket, ref=args.ref, remove=bool(args.remove))
    if not env.get("ok"):
        return _fail(env)
    body = env.get("data") or {}
    if args.json:
        print(json.dumps(body, indent=2))
    else:
        ref = body.get("session") or {}
        what = "closed" if body.get("closed") else "not running"
        if body.get("removed"):
            what += ", removed from history"
        print("%s: %s" % (ref.get("name") or ref.get("agent_id"), what))
    return 0


def cmd_clear(args) -> int:
    env = call("clear", timeout=args.timeout, path=args.socket, ref=args.ref,
               keep_last=not args.all)
    if not env.get("ok"):
        return _fail(env)
    body = env.get("data") or {}
    print(json.dumps(body, indent=2) if args.json else
          "cleared %s (%s)" % ((body.get("session") or {}).get("name"),
                               "kept last round" if body.get("keep_last") else "all"))
    return 0


MANUAL_NAME = "session-control.md"


def manual_path() -> str:
    """`docs/session-control.md` in the plugin tree.

    realpath: the shim is normally reached through a symlink on PATH.
    """
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    return os.path.join(root, "docs", MANUAL_NAME)


def manual_text() -> str:
    """The manual, ready for a terminal: fences dropped, rest verbatim."""
    try:
        with open(manual_path(), "r", encoding="utf-8") as f:
            body = f.read()
    except Exception as e:
        return ("manual not readable: %s\n\nExpected it at:\n  %s"
                % (e, manual_path()))
    return "\n".join(ln for ln in body.splitlines() if not ln.startswith("```"))


def cmd_manual(args) -> int:
    print(manual_text())
    return 0


def cmd_help(args) -> int:
    build_parser().print_help()   # the epilog points at --manual
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="submarine_sessions",
        description="Manage live Submarine sessions from outside Sublime.",
        epilog="Full manual: submarine_sessions --manual")
    p.add_argument("--socket", default="", help="socket path override")
    p.add_argument("--json", action="store_true", help="raw JSON envelope")
    p.add_argument("--manual", action="store_true",
                   help="print the full manual (no socket needed)")
    sub = p.add_subparsers(dest="cmd")

    def common(sp, timeout=DEFAULT_TIMEOUT):
        sp.add_argument("--timeout", type=float, default=timeout,
                        help="seconds to wait for the socket reply")
        # Repeated on the subcommands: `list --json` is how it gets typed.
        sp.add_argument("--json", action="store_true", help="raw JSON envelope")
        sp.add_argument("--manual", action="store_true", help="print the manual")
        return sp

    sp = common(sub.add_parser("list", help="live sessions plus saved rows"))
    sp.add_argument("--scope", default="all", choices=("all", "window", "children"))
    sp.add_argument("--parent", default=None, help="parent ref for --scope children")
    sp.add_argument("--window", default=None, help="window id for --scope window")
    sp.set_defaults(func=cmd_list)

    sp = common(sub.add_parser("view", help="read a session"))
    sp.add_argument("ref")
    sp.add_argument("--mode", default="tail", choices=("tail", "text", "edits"))
    sp.add_argument("--turns", type=int, default=3)
    sp.add_argument("--max-chars", dest="max_chars", type=int, default=20000)
    sp.add_argument("--offset", type=int, default=0)
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--file", default=None)
    sp.set_defaults(func=cmd_view)

    sp = common(sub.add_parser("chat", help="send a prompt to a session"))
    sp.add_argument("ref")
    sp.add_argument("prompt")
    sp.add_argument("--queue", default="queue", choices=("queue", "interrupt", "reject"))
    sp.add_argument("--wait", action="store_true", help="wait, then print the reply")
    sp.add_argument("--wait-timeout", dest="wait_timeout", type=float, default=WAIT_TIMEOUT)
    sp.add_argument("--idem", default=None, help="replay key: resend-safe")
    sp.set_defaults(func=cmd_chat)

    sp = common(sub.add_parser("interrupt", help="cancel the current turn"))
    sp.add_argument("ref")
    sp.set_defaults(func=cmd_interrupt)

    sp = common(sub.add_parser("pending", help="what the sheet is waiting on"))
    sp.add_argument("ref")
    sp.set_defaults(func=cmd_pending)

    sp = common(sub.add_parser("answer", help="answer a question / permission / plan"))
    sp.add_argument("ref")
    sp.add_argument("kind", choices=("question", "permission", "plan"))
    sp.add_argument("option", nargs="?", default=None,
                    help="option number or label (question); allow/deny/allow_session/"
                         "allow_all (permission); approve/reject (plan)")
    sp.add_argument("--options", default=None, help="multiSelect: 1,3 or labels")
    sp.add_argument("--text", default=None, help="free-text answer")
    sp.add_argument("--id", default=None, help="qid / permission id / plan id guard")
    sp.set_defaults(func=cmd_answer)

    sp = common(sub.add_parser("backends", help="backends and windows a new session can use"))
    sp.set_defaults(func=cmd_backends)

    sp = common(sub.add_parser("create", help="start a new session in a Sublime window"))
    sp.add_argument("--backend", default=None, help="claude | grok | kimi | codex | a provider name")
    sp.add_argument("--model", default=None, help="model alias for that backend")
    sp.add_argument("--name", default=None)
    sp.add_argument("--window", default=None, help="window id (see backends)")
    sp.add_argument("--project", default=None, help="window by project folder")
    sp.add_argument("--prompt", default=None, help="first prompt, delivered once the bridge is up")
    sp.add_argument("--idem", default=None, help="replay key for the prompt")
    sp.set_defaults(func=cmd_create)

    sp = common(sub.add_parser("rename", help="rename a session (live or in history)"))
    sp.add_argument("ref")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_rename)

    sp = common(sub.add_parser("close", help="stop a live session; --remove drops a history row"))
    sp.add_argument("ref")
    sp.add_argument("--remove", action="store_true", help="also drop the saved row")
    sp.set_defaults(func=cmd_close)

    sp = common(sub.add_parser("clear", help="clear the sheet, keeping the last round (--all wipes)"))
    sp.add_argument("ref")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(func=cmd_clear)

    sub.add_parser("help", help="short usage").set_defaults(func=cmd_help)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "manual", False):
        return cmd_manual(args)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
