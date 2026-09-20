"""Undo / rewind service.

Claude: parse `~/.claude/projects/<slug>/<sid>.jsonl` and resume at the
prior assistant uuid (`resume_session_at`). Synthetic turns tagged
channel/timer/inject (and siblings) stay recognized so undo skips them.

Grok: `rewind_points` then `rewind_execute` with mode=conversation_only.
Never send_wait — callbacks only (UI-thread deadlock).
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, List, Optional, Tuple


SYNTHETIC_TAGS = frozenset({
    "task-notification",
    "channel",
    "subsession",
    "wake",
    "inject",
    "timer",
    "notification",
    "retain",
    "system-reminder",
})

SYNTHETIC_BRACKETS = (
    "[Request interrupted",
    "[retain context]",
    "[Loop]",
)


def is_synthetic_turn(prompt):
    # type: (str) -> bool
    """Detect prompts that aren't real user messages.

    KEEP channel/timer/inject tags recognized so Undo stays clean on old
    transcripts (removal-map decision).
    """
    if not prompt:
        return True
    first = prompt.lstrip().split("\n", 1)[0]
    if first.startswith("<") and ">" in first:
        tag = first[1:first.index(">")].split()[0].lstrip("/")
        if tag in SYNTHETIC_TAGS:
            return True
    if any(first.startswith(p) for p in SYNTHETIC_BRACKETS):
        return True
    return False


def claude_project_slug(cwd):
    # type: (str) -> str
    return (cwd or "").replace("/", "-").lstrip("-")


def find_claude_jsonl(session_id, cwd=""):
    # type: (str, str) -> Optional[str]
    if not session_id:
        return None
    fname = "%s.jsonl" % session_id
    projects_dir = os.path.expanduser("~/.claude/projects")
    if cwd:
        exact = os.path.join(projects_dir, claude_project_slug(cwd), fname)
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


def find_session_jsonl(session_id, backend="claude", cwd=""):
    # type: (str, str, str) -> Optional[str]
    backend = (backend or "claude").lower()
    if backend == "claude":
        return find_claude_jsonl(session_id, cwd)
    return None


def read_claude_turns(jsonl_path):
    # type: (str) -> List[Tuple[str, Optional[str]]]
    """Return [(prompt, prev_assistant_uuid)] for each user turn."""
    if not jsonl_path or not os.path.isfile(jsonl_path):
        return []
    turns = []  # type: List[Tuple[str, Optional[str]]]
    last_assistant_uuid = None  # type: Optional[str]
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("isSidechain") or entry.get("isMeta"):
                    continue
                etype = entry.get("type")
                if etype == "assistant":
                    uid = entry.get("uuid")
                    if uid:
                        last_assistant_uuid = uid
                elif etype == "user":
                    msg = entry.get("message", {}) or {}
                    content = msg.get("content", [])
                    has_tool_result = (
                        isinstance(content, list)
                        and any(
                            isinstance(b, dict) and b.get("type") == "tool_result"
                            for b in content
                        )
                    )
                    if has_tool_result:
                        continue
                    prompt = ""
                    if isinstance(content, str):
                        prompt = content
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                prompt += block.get("text", "")
                    turns.append((prompt, last_assistant_uuid))
    except OSError:
        return []
    return turns


def last_prompt_span(content):
    # type: (str) -> Optional[Tuple[int, int]]
    """Buffer range of the last `◎ … ▶` turn through EOF, or None."""
    spans = prompt_spans(content)
    if not spans:
        return None
    return spans[-1][0], len(content)


def prompt_spans(content):
    # type: (str) -> List[Tuple[int, int]]
    """Start offsets of each submitted `◎ … ▶` line (leading newline included)."""
    import re
    if not content:
        return []
    out = []  # type: List[Tuple[int, int]]
    for m in re.finditer(r"(?m)^◎ .+? ▶", content):
        start = m.start()
        if start > 0 and content[start - 1] == "\n":
            start -= 1
        out.append((start, m.end()))
    return out


def prompt_index_span(content, prompt_index):
    # type: (str, int) -> Optional[Tuple[int, int]]
    """Range from the given ◎ turn through EOF (sublime-claude Grok strip)."""
    spans = prompt_spans(content)
    if not spans:
        return None
    try:
        idx = int(prompt_index)
    except (TypeError, ValueError):
        idx = -1
    if idx < 0 or idx >= len(spans):
        start = spans[-1][0]
    else:
        start = spans[idx][0]
    return start, len(content)


def find_rewind_point(turns, pending_resume_at=None):
    # type: (List[Tuple[str, Optional[str]]], Optional[str]) -> Tuple[Optional[str], str]
    """(uuid, undone_prompt) or (None, ''). Skips synthetic turns."""
    if not turns:
        return None, ""
    if pending_resume_at:
        for i, (_prompt, asst_uuid) in enumerate(turns):
            if asst_uuid == pending_resume_at:
                j = i - 1
                while j >= 0 and is_synthetic_turn(turns[j][0]):
                    j -= 1
                if j < 1:
                    return None, ""
                undone_prompt = turns[j][0]
                rewind_to = turns[j][1]
                if not rewind_to:
                    return None, ""
                return rewind_to, undone_prompt
        return None, ""
    idx = len(turns) - 1
    while idx >= 0 and is_synthetic_turn(turns[idx][0]):
        idx -= 1
    if idx < 1:
        return None, ""
    undone_prompt = turns[idx][0]
    rewind_to = turns[idx][1]
    if not rewind_to:
        return None, ""
    return rewind_to, undone_prompt


def turns_for_undo(turns):
    # type: (List[Tuple[str, Optional[str]]]) -> List[Tuple[str, str, str]]
    """[(label, rewind_id, draft_prompt)] newest first."""
    result = []  # type: List[Tuple[str, str, str]]
    for i, (prompt, prev_asst_uuid) in enumerate(turns):
        if not prev_asst_uuid:
            continue
        if is_synthetic_turn(prompt):
            continue
        first_line = prompt.split("\n")[0][:72]
        label = "%s — %s" % (i + 1, first_line if first_line else "(empty)")
        result.append((label, prev_asst_uuid, prompt))
    result.reverse()
    return result


class RewindService:
    """Claude jsonl undo + Grok async rewind_points / rewind_execute."""

    def __init__(
        self,
        send,  # type: Callable[..., bool]
        on_apply_claude,  # type: Callable[[str, str], None]
        on_apply_grok,  # type: Callable[[int, str], None]
        on_fail=None,  # type: Optional[Callable[[str], None]]
        scheduler=None,  # type: Any
        jsonl_finder=None,  # type: Optional[Callable[[], Optional[str]]]
    ):
        self.send = send
        self.on_apply_claude = on_apply_claude
        self.on_apply_grok = on_apply_grok
        self.on_fail = on_fail or (lambda _m: None)
        self.scheduler = scheduler
        self.jsonl_finder = jsonl_finder
        self._grok_busy = False

    def get_turns_for_undo(self, backend, session_id, cwd="", pending_resume_at=None):
        # type: (str, Optional[str], str, Optional[str]) -> list
        if (backend or "") == "grok":
            # Sync list must not send_wait (UI deadlock). Command path uses
            # show_grok_undo_panel / fetch_grok_points.
            return []
        path = None
        if self.jsonl_finder is not None:
            path = self.jsonl_finder()
        if not path:
            path = find_claude_jsonl(session_id or "", cwd)
        return turns_for_undo(read_claude_turns(path or ""))

    def undo_last(self, backend, session_id, cwd="", pending_resume_at=None):
        # type: (str, Optional[str], str, Optional[str]) -> bool
        if not session_id:
            return False
        if (backend or "") == "grok":
            self.grok_undo_async()
            return True
        path = None
        if self.jsonl_finder is not None:
            path = self.jsonl_finder()
        if not path:
            path = find_claude_jsonl(session_id, cwd)
        rewind_id, undone = find_rewind_point(
            read_claude_turns(path or ""), pending_resume_at)
        if not rewind_id:
            self.on_fail("no rewind point found")
            return False
        self.on_apply_claude(rewind_id, undone)
        return True

    def grok_undo_async(self, prompt_index=None, draft_prompt=""):
        # type: (Optional[int], str) -> None
        if self._grok_busy:
            return
        self._grok_busy = True

        def _fail(msg):
            self._grok_busy = False
            self.on_fail(msg)

        def _execute(idx, draft):
            state = {"done": False}

            def on_exec(resp):
                if state["done"]:
                    return
                state["done"] = True
                if not isinstance(resp, dict):
                    resp = {}
                if "error" in resp:
                    err = resp["error"]
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    _fail(msg or "execute error")
                    return
                result = resp if "draft_prompt" in resp or "ok" in resp else (
                    resp.get("result") or resp)
                if not isinstance(result, dict):
                    result = {}
                if result.get("ok") is False:
                    # Neither the disk cut nor the ACP rewind happened: the
                    # history still holds the later turns, so do not restart
                    # the session as if it were rewound.
                    _fail(str(result.get("error") or "rewind failed"))
                    return
                d = (result.get("draft_prompt") or draft or "").strip()
                self._grok_busy = False
                self.on_apply_grok(idx, d)

            ok = self.send(
                "rewind_execute",
                {"prompt_index": idx, "mode": "conversation_only"},
                on_exec,
            )
            if not ok:
                _fail("bridge send failed")
                return
            if self.scheduler is not None:
                def _timeout():
                    if state["done"]:
                        return
                    state["done"] = True
                    _fail("timeout waiting for bridge (45s)")
                self.scheduler.call_later(45000, _timeout)

        if prompt_index is not None:
            try:
                idx = int(prompt_index)
            except (TypeError, ValueError):
                _fail("bad prompt_index %r" % (prompt_index,))
                return
            _execute(idx, draft_prompt or "")
            return

        def on_points(resp):
            if not isinstance(resp, dict):
                resp = {}
            if "error" in resp:
                err = resp["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                _fail(msg or "points error")
                return
            result = resp if "points" in resp else (resp.get("result") or resp)
            if not isinstance(result, dict):
                result = {}
            points = result.get("points") or []
            if not points:
                _fail("no rewind points")
                return
            best = None
            for p in points:
                if not isinstance(p, dict):
                    continue
                try:
                    i = int(p.get("prompt_index"))
                except (TypeError, ValueError):
                    continue
                if best is None or i > best[0]:
                    best = (i, (p.get("prompt_preview") or "").strip())
            if best is None:
                _fail("no valid rewind points")
                return
            _execute(best[0], best[1])

        if not self.send("rewind_points", {}, on_points):
            _fail("bridge send failed")

    def fetch_grok_points(self, on_turns):
        # type: (Callable[[list], None]) -> bool
        """Async rewind_points → [(label, idx, preview)] newest first."""

        def on_points(resp):
            if not isinstance(resp, dict):
                resp = {}
            if "error" in resp:
                err = resp["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                self.on_fail(msg or "points error")
                on_turns([])
                return
            result = resp if "points" in resp else (resp.get("result") or resp)
            if not isinstance(result, dict):
                result = {}
            points = result.get("points") or []
            turns = []
            for p in points:
                if not isinstance(p, dict):
                    continue
                try:
                    idx = int(p.get("prompt_index"))
                except (TypeError, ValueError):
                    continue
                preview = (p.get("prompt_preview") or "").strip()
                first = preview.split("\n")[0][:72] if preview else "(empty)"
                files = " · files" if p.get("has_file_changes") else ""
                label = "%s — %s%s" % (idx, first, files)
                turns.append((label, idx, preview))
            turns.reverse()
            on_turns(turns)

        return bool(self.send("rewind_points", {}, on_points))
