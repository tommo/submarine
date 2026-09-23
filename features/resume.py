"""Load a short tail of a saved transcript to paint when reopening history.

Grok needs one extra step: its CLI mints a new session id per resume, so the
turns of a resumed session sit in the directory of an earlier id. The saved
records that share an `agent_id` are that chain (see `grok_chat_paths`).
"""
from __future__ import annotations

import glob
import json
import os
import re

from core.rewind import is_synthetic_turn
from typing import Dict, List, Optional


MIN_CHARS = 500
MAX_TURNS = 8

_USER_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
# Last-turn prompts that are a nudge, not the session's real title/context.
_NUDGE_PROMPTS = frozenset({
    "resume", "continue", "ok", "okay", "yes", "y", "go",
    "go on", "keep going", "next", "and", "more",
})


_TASK_SUMMARY = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.S)
_TASK_BLOCK = re.compile(r"<task-notification>", re.I)


def synthetic_label(text: str) -> str:
    """The sheet's ⚙ line for a host-injected prompt (a task notification, a
    wake, a channel message): what the live turn showed instead of the raw
    tag block, so a resumed transcript reads the same way."""
    t = (text or "").strip()
    n = len(_TASK_BLOCK.findall(t))
    if n:
        summaries = [" ".join(m.split()) for m in _TASK_SUMMARY.findall(t)]
        label = "⚙ %d task notification%s" % (n, "" if n == 1 else "s")
        if summaries:
            tail = "; ".join(summaries)
            if len(tail) > 160:
                tail = tail[:159] + "…"
            label += ": " + tail
        return label
    first = t.split("\n", 1)[0].strip()
    if first.startswith("["):
        return first[:120]                       # "[Request interrupted by user]"
    if first.startswith("<") and ">" in first:
        tag = first[1:first.index(">")].split()[0].lstrip("/")
        body = t.split("\n", 1)[1].strip() if "\n" in t else first[first.index(">") + 1:]
        body = re.sub(r"</?[a-z_-]+[^>]*>", "", body)          # strip the tags
        body = " ".join(body.split())
        label = "⚙ %s" % tag
        if body:
            label += ": " + (body[:119] + "…" if len(body) > 120 else body)
        return label
    return "⚙ " + first[:120]


def display_prompt(raw: str) -> str:
    """What a resumed turn's ◎ line shows: the user's text, or the ⚙ label
    the live sheet used for a host-injected prompt — never the raw tag block."""
    text = (raw or "").strip()
    if not text:
        return ""
    m = _USER_QUERY.search(text)
    if m:
        return m.group(1).strip()
    if is_synthetic_turn(text):
        return synthetic_label(text)
    return text


def _flatten(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return content.get("text") or ""
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                if b.get("type") == "text" or "text" in b:
                    parts.append(b.get("text") or "")
        return "\n".join(p for p in parts if p)
    return ""


def _new_turn(prompt: str) -> dict:
    # `events` is the turn as it happened: ("text", str) / ("tool", name) in
    # transcript order. `reply` and `tools` are the flattened views of it
    # (summaries, the CLI, `select_preview`'s length check) — the painter
    # uses `events`, because a turn is text *between* tool calls, not a pile
    # of tool names followed by every word the agent said.
    return {"prompt": prompt or "", "reply": "", "tools": [], "events": []}


def _turn_text(cur: dict, text) -> None:
    text = text if isinstance(text, str) else ("" if text is None else str(text))
    if not text:
        return
    events = cur.setdefault("events", [])
    if events and events[-1][0] == "text":
        # The same stretch of text (a streamed part): glue as is.
        cur["reply"] += text
        events[-1] = ("text", events[-1][1] + text)
        return
    # Text after a tool call is a new paragraph in the flattened reply too —
    # glued, "…the layout):Now fix the two issues" read as one line.
    if cur["reply"] and not cur["reply"].endswith("\n\n"):
        cur["reply"] = cur["reply"].rstrip("\n") + "\n\n"
    cur["reply"] += text.lstrip("\n") if cur["reply"] else text
    events.append(("text", text))


def _turn_tool(cur: dict, name) -> None:
    name = str(name or "").strip()
    if not name:
        return
    cur["tools"].append(name)
    cur.setdefault("events", []).append(("tool", name))


def select_preview(turns: List[dict], min_chars: int = MIN_CHARS,
                   max_turns: int = MAX_TURNS) -> List[dict]:
    """Last turn, plus earlier ones if the tail is too short."""
    if not turns:
        return []
    out: List[dict] = []
    total = 0
    for t in reversed(turns):
        out.append(t)
        total += len(t.get("prompt") or "") + len(t.get("reply") or "")
        if total >= min_chars or len(out) >= max_turns:
            break
    out.reverse()
    if out and len(out) < max_turns and len(turns) > len(out):
        head = display_prompt(out[0].get("prompt") or "").strip().lower()
        if head in _NUDGE_PROMPTS:
            earlier = turns[-(len(out) + 1)]
            out.insert(0, earlier)
    return out


def parse_claude_jsonl(path: str) -> List[dict]:
    turns: List[dict] = []
    cur = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("isMeta") or rec.get("isSidechain"):
                    continue
                et = rec.get("type")
                if et == "user":
                    msg = rec.get("message") or {}
                    content = msg.get("content", [])
                    if isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in content
                    ):
                        continue
                    prompt = _flatten(content)
                    if prompt:
                        cur = _new_turn(prompt)
                        turns.append(cur)
                elif et == "assistant" and cur is not None:
                    msg = rec.get("message") or {}
                    content = msg.get("content", [])
                    if isinstance(content, str):
                        _turn_text(cur, content)
                    elif isinstance(content, list):
                        for b in content:
                            if not isinstance(b, dict):
                                continue
                            if b.get("type") == "text":
                                _turn_text(cur, b.get("text") or "")
                            elif b.get("type") == "tool_use" and b.get("name"):
                                _turn_tool(cur, b["name"])
    except OSError:
        return []
    return turns


def parse_grok_chat(path: str) -> List[dict]:
    turns: List[dict] = []
    cur = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                et = rec.get("type")
                if et == "user":
                    prompt = _flatten(rec.get("content"))
                    if not prompt or _skip_synthetic_prompt(prompt):
                        # Keep cur so the following assistant tools stay on
                        # the real user turn, not a fake ◎ system-reminder.
                        continue
                    cur = _new_turn(prompt)
                    turns.append(cur)
                elif et == "assistant" and cur is not None:
                    _turn_text(cur, _flatten(rec.get("content")))
                    for tc in rec.get("tool_calls") or []:
                        if isinstance(tc, dict) and tc.get("name"):
                            _turn_tool(cur, tc["name"])
    except OSError:
        return []
    return turns


def _kimi_prompt_text(rec: dict) -> str:
    inp = rec.get("input")
    if isinstance(inp, list):
        return _flatten(inp)
    if isinstance(inp, dict):
        return _flatten(inp)
    if isinstance(inp, str):
        return inp.strip()
    return ""


def _skip_synthetic_prompt(text: str, origin: str = "") -> bool:
    """Host/agent injects, not a real ◎ user turn (Grok stores these as type=user)."""
    if origin == "task":
        return True
    t = (text or "").lstrip()
    if t.startswith((
        "<system-reminder>",
        "<task-notification>",
        "<notification",
        "<user_info>",
    )):
        return True
    # Reminder after other wrappers
    if "<system-reminder>" in t[:200]:
        return True
    return False


def _kimi_skip_prompt(text: str, origin: str) -> bool:
    return _skip_synthetic_prompt(text, origin)


def parse_kimi_wire(path: str) -> List[dict]:
    """Kimi agents/main/wire.jsonl → turns (user prompt + text + tools)."""
    turns: List[dict] = []
    cur = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = rec.get("type")
                if kind == "turn.cancel":
                    # Rejected prompt (agent_busy) — drop the empty turn.
                    if cur is not None and not cur.get("reply") and not cur.get("tools"):
                        turns.pop()
                        cur = turns[-1] if turns else None
                    continue
                if kind == "turn.prompt":
                    text = _kimi_prompt_text(rec)
                    origin = str((rec.get("origin") or {}).get("kind") or "")
                    if not text or _kimi_skip_prompt(text, origin):
                        continue
                    cur = _new_turn(text)
                    turns.append(cur)
                    continue
                if kind != "context.append_loop_event" or cur is None:
                    continue
                ev = rec.get("event") if isinstance(rec.get("event"), dict) else {}
                et = ev.get("type")
                if et == "tool.call" and ev.get("name"):
                    _turn_tool(cur, ev["name"])
                elif et == "content.part":
                    part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
                    if part.get("type") == "text" and part.get("text"):
                        _turn_text(cur, part["text"])
    except OSError:
        return []
    return turns


def find_kimi_wire(session_id: str, cwd: str = "") -> Optional[str]:
    if not session_id:
        return None
    root = os.path.expanduser("~/.kimi-code/sessions")
    if not os.path.isdir(root):
        return None
    names = [session_id]
    if not session_id.startswith("session_"):
        names.append("session_" + session_id)
    try:
        for wd in os.listdir(root):
            base = os.path.join(root, wd)
            if not os.path.isdir(base):
                continue
            for name in names:
                cand = os.path.join(base, name, "agents", "main", "wire.jsonl")
                if os.path.isfile(cand):
                    return cand
    except OSError:
        return None
    return None


def grok_sessions_root() -> str:
    return os.path.expanduser("~/.grok/sessions")


def _saved_session_rows() -> List[dict]:
    try:
        from core.records import load_saved_sessions
        return load_saved_sessions() or []
    except Exception:
        return []


def _grok_chain_ids(session_id: str, agent_id: str) -> List[str]:
    """This session's id and the ids it was resumed under, oldest first.

    The grok CLI mints a NEW session id on every resume, so a resumed session's
    own `chat_history.jsonl` starts empty and the earlier turns stay in an
    earlier id's directory. Saved records sharing an `agent_id` are that chain;
    when there is none (a fork carries a fresh agent_id, a session reopened
    without one carries none) the resumed id's own record still names the agent
    it belongs to.
    """
    if not session_id:
        return []
    rows = _saved_session_rows()

    def chain(aid: str) -> List[dict]:
        if not aid:
            return []
        return [r for r in rows
                if (r.get("agent_id") or "") == aid and r.get("session_id")]

    members = chain(agent_id)
    if not members:
        for r in rows:
            if r.get("session_id") == session_id:
                members = chain(r.get("agent_id") or "")
                break
    if not members:
        return [session_id]
    try:
        members.sort(key=lambda r: float(r.get("last_activity") or 0))
    except (TypeError, ValueError):
        pass
    ids = []
    for r in members:
        sid = r.get("session_id")
        if sid and sid not in ids:
            ids.append(sid)
    if session_id not in ids:
        ids.append(session_id)
    return ids


def _grok_chat_for_id(session_id: str, cwd: str = "") -> Optional[str]:
    if not session_id:
        return None
    root = grok_sessions_root()
    if cwd:
        enc = cwd.replace("/", "%2F")
        cand = os.path.join(root, enc, session_id, "chat_history.jsonl")
        if os.path.isfile(cand):
            return cand
    if not os.path.isdir(root):
        return None
    try:
        for name in os.listdir(root):
            cand = os.path.join(root, name, session_id, "chat_history.jsonl")
            if os.path.isfile(cand):
                return cand
    except OSError:
        return None
    return None


def find_grok_chat(session_id: str, cwd: str = "") -> Optional[str]:
    """`chat_history.jsonl` for one session id."""
    return _grok_chat_for_id(session_id, cwd)


def grok_chat_paths(session_id: str, cwd: str = "",
                    agent_id: str = "") -> List[str]:
    """`chat_history.jsonl` for the resumed id and its chain, oldest first."""
    paths: List[str] = []
    for sid in _grok_chain_ids(session_id, agent_id):
        path = find_grok_chat(sid, cwd)
        if path and path not in paths:
            paths.append(path)
    return paths


def find_grok_history(session_id: str, cwd: str = "",
                      agent_id: str = "") -> Optional[str]:
    """The chain file that holds the turns — the old one for a fresh resume."""
    paths = grok_chat_paths(session_id, cwd, agent_id)
    if not paths:
        return None
    for path in reversed(paths):
        if parse_grok_chat(path):
            return path
    return paths[-1]


def _cwd_of_chat(path: str) -> str:
    """The project directory a grok session directory is filed under."""
    if not path:
        return ""
    enc = os.path.basename(os.path.dirname(os.path.dirname(path)))
    try:
        from urllib.parse import unquote
        out = unquote(enc)
    except Exception:
        out = enc
    return out if os.path.isabs(out) else ""


def grok_session_cwd(session_id: str, cwd: str = "",
                     agent_id: str = "") -> str:
    """Directory the grok CLI filed `session_id` under, "" when unknown.

    An id the CLI never filed (a fork's fresh id) falls back to the directory of
    the chain member that does hold the conversation.
    """
    path = find_grok_chat(session_id, cwd) or find_grok_history(
        session_id, cwd, agent_id)
    return _cwd_of_chat(path)


def transcript_cwd(backend: str, session_id: str, cwd: str = "",
                   agent_id: str = "") -> str:
    """The directory a backend filed this session's transcript under.

    Grok scopes `session/load` to the cwd: resuming a session from another
    project fails with FS_NOT_FOUND and the CLI silently opens a fresh one with
    no prior turns. The session's own directory is then the one to send, not the
    window's project.
    """
    if (backend or "").lower() != "grok":
        return ""
    return grok_session_cwd(session_id, cwd, agent_id)


def find_claude_jsonl(session_id: str, cwd: str = "") -> Optional[str]:
    if not session_id:
        return None
    fname = f"{session_id}.jsonl"
    projects_dir = os.path.expanduser("~/.claude/projects")
    if cwd:
        key = cwd.replace("/", "-").lstrip("-")
        exact = os.path.join(projects_dir, key, fname)
        if os.path.isfile(exact):
            return exact
    if not os.path.isdir(projects_dir):
        return None
    try:
        for d in os.listdir(projects_dir):
            cand = os.path.join(projects_dir, d, fname)
            if os.path.isfile(cand):
                return cand
    except OSError:
        return None
    return None


# Codex keeps one rollout per thread:
#   <CODEX_HOME>/sessions/YYYY/MM/DD/rollout-<ts>-<threadId>.jsonl
# Records are {type, payload, timestamp}. The real prompt is
# event_msg/user_message; response_item messages with role user/developer are
# AGENTS.md and <environment_context> injections, not turns.
_CODEX_INJECTIONS = (
    "# AGENTS.md",
    "<environment_context>",
    "<user_instructions>",
    "<permissions instructions>",
    "<INSTRUCTIONS>",
)


def codex_sessions_root() -> str:
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.join(home, "sessions")


def _codex_skip_prompt(text: str) -> bool:
    t = (text or "").lstrip()
    if not t:
        return True
    if t.startswith(_CODEX_INJECTIONS):
        return True
    if "<environment_context>" in t[:400]:
        return True
    return _skip_synthetic_prompt(t)


def _codex_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return _flatten(content)
    parts = []
    for b in content:
        if isinstance(b, dict):
            parts.append(b.get("text") or "")
        elif isinstance(b, str):
            parts.append(b)
    return "\n".join(p for p in parts if p)


def parse_codex_rollout(path: str) -> List[dict]:
    """Codex rollout jsonl → turns (user prompt + assistant text + tools).

    Two on-disk generations: older rollouts carry event_msg/user_message plus
    response_item messages, newer ones (cli 0.15x) carry event_msg/
    item_completed items. Exactly one shape is present per file; the newer
    one wins when both appear.
    """
    old_turns: List[dict] = []
    new_turns: List[dict] = []
    cur_old = None
    cur_new = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = rec.get("payload")
                if not isinstance(payload, dict):
                    continue
                rtype = rec.get("type")
                ptype = payload.get("type")
                if rtype == "event_msg":
                    if ptype == "user_message":
                        text = payload.get("message") or ""
                        if _codex_skip_prompt(text):
                            continue
                        cur_old = _new_turn(text)
                        old_turns.append(cur_old)
                    elif ptype == "item_completed":
                        cur_new = _codex_item_turn(
                            payload.get("item"), cur_new, new_turns)
                    elif ptype == "task_complete":
                        # Rollouts without assistant response items — the
                        # closer carries the turn's final text.
                        last = payload.get("last_agent_message")
                        for cur in (cur_old, cur_new):
                            if cur is not None and last and not cur["reply"]:
                                _turn_text(cur, str(last))
                    continue
                if rtype != "response_item":
                    continue
                if ptype == "message":
                    if payload.get("role") != "assistant" or cur_old is None:
                        continue
                    _turn_text(cur_old, _codex_text(payload.get("content")))
                elif ptype in ("function_call", "custom_tool_call"):
                    name = payload.get("name")
                    if name and cur_old is not None:
                        _turn_tool(cur_old, name)
    except OSError:
        return []
    return new_turns or old_turns


def _codex_item_turn(item, cur, turns: List[dict]):
    """Fold one newer-format item_completed into `cur`. Returns the new cur."""
    if not isinstance(item, dict):
        return cur
    itype = item.get("type")
    if itype == "UserMessage":
        text = _codex_text(item.get("content"))
        if _codex_skip_prompt(text):
            return cur
        cur = _new_turn(text)
        turns.append(cur)
        return cur
    if cur is None:
        return cur
    if itype == "AgentMessage":
        _turn_text(cur, _codex_text(item.get("content")))
    elif itype == "CommandExecution":
        _turn_tool(cur, "exec_command")
    elif itype == "McpToolCall":
        server = item.get("server")
        tool = item.get("tool")
        name = ("%s.%s" % (server, tool)) if server and tool else (tool or "")
        _turn_tool(cur, name)
    return cur


def _codex_rollout_cwd(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            rec = json.loads(f.readline().strip())
        payload = rec.get("payload") or {}
        return str(payload.get("cwd") or "").rstrip("/")
    except Exception:
        return ""


def find_codex_rollout(session_id: str, cwd: str = "") -> Optional[str]:
    """Newest rollout for a thread id, preferring one recorded in `cwd`."""
    if not session_id:
        return None
    root = codex_sessions_root()
    if not os.path.isdir(root):
        return None
    try:
        pattern = os.path.join(root, "**", "rollout-*%s.jsonl" % session_id)
        matches = [p for p in glob.glob(pattern, recursive=True)
                   if os.path.isfile(p)]
    except Exception:
        return None
    if not matches:
        return None
    try:
        matches.sort(key=lambda p: os.path.getmtime(p) or 0, reverse=True)
    except OSError:
        pass
    if cwd:
        want = (cwd or "").rstrip("/")
        for p in matches:
            if _codex_rollout_cwd(p) == want:
                return p
    return matches[0]


def find_session_jsonl(session_id: str, backend: str = "",
                       cwd: str = "", agent_id: str = "") -> Optional[str]:
    """Backend transcript: grok chat_history, kimi wire, codex rollout,
    claude projects jsonl."""
    backend = (backend or "claude").lower()
    if backend == "grok":
        return find_grok_history(session_id, cwd, agent_id)
    if backend == "kimi":
        return find_kimi_wire(session_id, cwd)
    if backend == "codex":
        return find_codex_rollout(session_id, cwd)
    return find_claude_jsonl(session_id, cwd)


def load_turns(session_id: str, backend: str, cwd: str = "",
               claude_jsonl: str = "", agent_id: str = "") -> List[dict]:
    """Turns for a session. A grok session is read across its whole resume chain,
    so a reopened session shows the turns it had before it was resumed."""
    backend = (backend or "claude").lower()
    if backend == "grok":
        turns: List[dict] = []
        for path in grok_chat_paths(session_id, cwd, agent_id):
            turns.extend(parse_grok_chat(path))
        return turns
    if backend == "kimi":
        path = find_kimi_wire(session_id, cwd)
        return parse_kimi_wire(path) if path else []
    if backend == "codex":
        path = find_codex_rollout(session_id, cwd)
        return parse_codex_rollout(path) if path else []
    path = claude_jsonl if claude_jsonl and os.path.isfile(claude_jsonl) else find_claude_jsonl(session_id, cwd)
    return parse_claude_jsonl(path) if path else []


_MAX_TOOL_LINES = 12


def _tool_lines(names: List[str]) -> List[str]:
    """One line per tool, `×N` only for a genuine consecutive run."""
    collapsed: List[list] = []
    for n in names:
        if collapsed and collapsed[-1][0] == n:
            collapsed[-1][1] += 1
        else:
            collapsed.append([n, 1])
    shown = collapsed[:_MAX_TOOL_LINES]
    lines = [f"⚙ {n}" + (f" ×{c}" if c > 1 else "") for n, c in shown]
    extra = len(collapsed) - len(shown)
    if extra > 0:
        lines.append(f"⚙ … +{extra} more")
    return lines


def format_turn_body(turn: dict) -> str:
    """The turn as it ran: text and tool calls in transcript order.

    A turn is the agent talking *between* its tool calls. Pooling every tool
    name into one block above the whole reply (what this used to do) reordered
    the turn and merged calls that were pages apart into `×N` runs that never
    happened. Only calls that really followed each other collapse.
    """
    events = turn.get("events")
    if not events:
        # Parsed before `events` existed (or by a caller that builds the
        # flattened shape): tools first, then the text, as before.
        tools = turn.get("tools") or []
        parts = []
        if tools:
            parts.append("\n".join(_tool_lines([str(t) for t in tools])))
        reply = (turn.get("reply") or "").rstrip()
        if reply:
            parts.append(reply)
        return "\n\n".join(parts)

    parts: List[str] = []
    run: List[str] = []

    def _flush_tools():
        if run:
            parts.append("\n".join(_tool_lines(run)))
            del run[:]

    for kind, value in events:
        if kind == "tool":
            run.append(str(value))
            continue
        _flush_tools()
        text = (value or "").strip("\n").rstrip()
        if text:
            parts.append(text)
    _flush_tools()
    return "\n\n".join(parts)


def paint_resume_preview(session) -> bool:
    """Paint last turn(s) into a newly reopened history view. True if painted.

    Forks reuse the parent `resume_id` transcript so the new sheet is not blank.
    Undo reconnect must not repaint the stripped turn from JSONL.
    """
    if getattr(session, "quick_mode", False):
        return False
    if getattr(session, "_park_composer_after_init", False):
        return False
    if not getattr(session, "resume_id", None):
        return False
    out = getattr(session, "output", None)
    if out is None:
        return False
    if list(getattr(out, "conversations", None) or []):
        return False
    cur = getattr(out, "current", None)
    if cur is not None and (getattr(cur, "prompt", None) or getattr(cur, "events", None)):
        return False
    view = getattr(out, "view", None)
    if view is not None:
        try:
            import sublime
            existing = view.substr(sublime.Region(0, view.size())) or ""
        except Exception:
            try:
                existing = view.substr(None) or ""
            except Exception:
                existing = ""
        if "◎ " in existing and " ▶" in existing:
            return False
    fork = bool(getattr(session, "fork", False))
    resume_id = getattr(session, "resume_id", None)
    live_id = getattr(session, "session_id", None)
    # On-disk history belongs to the RESUMED session, not to whatever id the
    # backend just handed back. _on_init has already overwritten session_id,
    # and on the resume-fallback path that is a brand-new session with no
    # transcript of its own — keying on it painted nothing for a reopened
    # closed session even though resume_id's transcript was on disk.
    sid = resume_id or live_id or ""
    backend = getattr(session, "backend", None) or "claude"
    cwd = ""
    try:
        cwd = session._cwd() if hasattr(session, "_cwd") else (getattr(session, "cwd", None) or "")
    except Exception:
        cwd = getattr(session, "cwd", None) or ""
    jsonl = ""
    # Only hand load_turns a path resolved from the live id when that id IS
    # the transcript owner; otherwise let it resolve from `sid`.
    if not fork and sid and sid == live_id:
        try:
            finder = getattr(session, "_find_jsonl_path", None)
            if callable(finder):
                jsonl = finder() or ""
        except Exception:
            jsonl = ""
    turns = load_turns(sid, backend, cwd, jsonl,
                       agent_id=getattr(session, "agent_id", None) or "")
    chosen = select_preview(turns)
    if not chosen:
        return False
    for t in chosen:
        prompt = display_prompt(t.get("prompt") or "") or "(turn)"
        out.prompt(prompt)
        body = format_turn_body(t)
        if body:
            out.text(body if body.endswith("\n") else body + "\n")
        out.meta(0)
    return True


def _ensure_resume_input(session, n=0):
    if getattr(session, "working", False):
        return
    out = getattr(session, "output", None)
    try:
        if out is not None and getattr(out, "is_input_mode", lambda: False)():
            return
    except Exception:
        pass
    try:
        session._enter_input_if_idle()
    except Exception:
        pass
    sched = getattr(session, "scheduler", None)
    if n < 8 and sched is not None:
        sched.call_later(120, lambda: _ensure_resume_input(session, n + 1))


def scroll_to_tail(session, delay_ms: int = 30) -> None:
    """Land a re-opened sheet on its tail instead of on its oldest line.

    A resume paints the transcript into a sheet whose viewport is still at the
    top, so the session opens showing the start of old history. Chrome only: a
    viewless session no-ops, and the deferred call lets Sublime lay the new text
    out first. Already-at-the-tail sheets are left alone (the user may have
    scrolled there, and a short sheet has nothing to scroll).
    """
    out = getattr(session, "output", None)
    composer = getattr(out, "composer", None) if out is not None else None
    sched = getattr(session, "scheduler", None)
    if composer is None or sched is None:
        return

    def _do():
        sheet = getattr(out, "sheet", None)
        try:
            if sheet is not None and sheet.is_following_tail():
                return
        except Exception:
            pass
        try:
            composer.scroll_to_end(force=True)
        except Exception:
            pass

    sched.call_later(max(0, int(delay_ms)), _do)


def _on_init_paint(session, result):
    if isinstance(result, dict) and result.get("error"):
        return
    if getattr(session, "_park_composer_after_init", False):
        return
    try:
        paint_resume_preview(session)
    except Exception as e:
        try:
            from plat.log import log_plugin
            log_plugin("resume preview: %s" % e)
        except Exception:
            print("[Submarine] resume preview: %s" % e)
    if not getattr(session, "resume_id", None):
        return
    scroll_to_tail(session)
    if getattr(session, "quick_mode", False):
        return
    sched = getattr(session, "scheduler", None)
    if sched is not None:
        sched.call_later(200, lambda: _ensure_resume_input(session, 0))


def attach_resume_preview(session):
    if getattr(session, "_resume_preview_attached", False):
        return session
    session._resume_preview_attached = True
    session.on_init.append(lambda s, result: _on_init_paint(s, result))
    return session
