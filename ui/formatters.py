"""Per-tool detail formatter registry.

Merged from old tool_formatters.py + tool_formatters_sublime.py.
mcp__sublime__* aliases kept alongside mcp__submarine__*.
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ToolCall

MEDIA_TOOLS = frozenset({
    "image_gen", "image_edit", "image_to_video", "reference_to_video", "video_gen",
    "read_image", "ReadImage",
    "mcp__sublime__read_image", "sublime__read_image",
    "mcp__submarine__read_image", "submarine__read_image",
})
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv")
MEDIA_EXTS = IMAGE_EXTS + VIDEO_EXTS


def is_media_tool_name(name: str) -> bool:
    n = (name or "").strip()
    if n in MEDIA_TOOLS:
        return True
    low = n.lower().replace("-", "_")
    return low.endswith("read_image") or low.endswith("readimage")


X_SEARCH_TOOLS = frozenset({
    "x_keyword_search", "x_semantic_search", "x_user_search", "x_thread_fetch",
})


def extract_media_path(
        result: Optional[str],
        tool_input: Optional[dict] = None,
        cwd: Optional[str] = None,
) -> Optional[str]:
    candidates = []

    def _collect_from_dict(d: dict) -> None:
        if not isinstance(d, dict):
            return
        for k in ("path", "file_path", "output_path", "image", "filename",
                  "target_file"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                candidates.append(v.strip())
        imgs = d.get("images")
        if isinstance(imgs, list):
            for v in imgs:
                if isinstance(v, str) and v.strip():
                    candidates.append(v.strip())
        for nk in ("tool_input", "arguments", "input", "args"):
            nested = d.get(nk)
            if isinstance(nested, dict):
                _collect_from_dict(nested)
            elif isinstance(nested, str) and nested.strip().startswith("{"):
                try:
                    obj = json.loads(nested)
                    if isinstance(obj, dict):
                        _collect_from_dict(obj)
                except Exception:
                    pass

    if isinstance(tool_input, dict):
        _collect_from_dict(tool_input)
        mp = tool_input.get("_media_path")
        if isinstance(mp, str) and mp.strip():
            candidates.insert(0, mp.strip())

    text = (result or "").strip()
    if text:
        for m in re.finditer(r"path=([^\s,)]+)", text):
            candidates.append(m.group(1))
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                _collect_from_dict(obj)
            elif isinstance(obj, str):
                candidates.append(obj)
        except Exception:
            pass
        for m in re.finditer(r'\{[^{}]*"(?:path|file_path|filename)"[^{}]*\}', text):
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    _collect_from_dict(obj)
            except Exception:
                pass
        for m in re.finditer(
                r'(?:/|~/)[^\s"\']+?\.(?:png|jpe?g|webp|gif|bmp|mp4|webm|mov|mkv)\b',
                text, re.I):
            candidates.append(m.group(0))
        for m in re.finditer(
                r'(?:images|videos|captures)/\S+?\.(?:png|jpe?g|webp|gif|bmp|mp4|webm|mov|mkv)\b',
                text, re.I):
            candidates.append(m.group(0))

    def _resolve(c: str) -> Optional[str]:
        p = os.path.expanduser(c)
        if os.path.isfile(p):
            return os.path.abspath(p)
        if cwd and not os.path.isabs(p):
            joined = os.path.join(cwd, p)
            if os.path.isfile(joined):
                return os.path.abspath(joined)
        return None

    for c in candidates:
        hit = _resolve(c)
        if hit:
            return hit
    for c in candidates:
        p = os.path.expanduser(c)
        if os.path.isabs(p) and p.lower().endswith(MEDIA_EXTS):
            return p
    for c in candidates:
        if c.lower().endswith(MEDIA_EXTS):
            p = os.path.expanduser(c)
            if cwd and not os.path.isabs(p):
                return os.path.abspath(os.path.join(cwd, p))
            return p
    return None


def media_display_path(path: str) -> str:
    if not path:
        return ""
    norm = path.replace("\\", "/")
    for marker in ("/images/", "/videos/"):
        idx = norm.find(marker)
        if idx >= 0:
            return norm[idx + 1:]
    if norm.startswith(("images/", "videos/")):
        return norm
    return os.path.basename(path)


def is_image_path(path: str) -> bool:
    return bool(path) and path.lower().endswith(IMAGE_EXTS)


def is_video_path(path: str) -> bool:
    return bool(path) and path.lower().endswith(VIDEO_EXTS)


def _clip(s: str, n: int = 70) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _tool_input(tool) -> dict:
    return tool.tool_input if isinstance(tool.tool_input, dict) else {}


def _join_bits(*parts) -> str:
    bits = [p for p in parts if p]
    return (": " + " · ".join(bits)) if bits else ""


def _basename(path: str) -> str:
    if not path:
        return ""
    return os.path.basename(str(path).rstrip("/")) or str(path)


def _mcp_short_name(name: str) -> str:
    n = (name or "").strip()
    for prefix in ("mcp__submarine__", "mcp__sublime__", "submarine__", "sublime__"):
        if n.startswith(prefix):
            return n[len(prefix):]
    if n.startswith("mcp__") and n.count("__") >= 2:
        return n
    return n


def _media_path_suffix(tool) -> str:
    path = None
    if isinstance(tool.tool_input, dict):
        path = tool.tool_input.get("_media_path") or tool.tool_input.get("path")
    if not path:
        path = extract_media_path(tool.result, tool.tool_input)
    if not path:
        return ""
    if tool.status in ("done", "error"):
        return " → %s" % media_display_path(path)
    return ""


def _image_gen(view, tool) -> str:
    prompt = _clip(tool.tool_input.get("prompt", "") if tool.tool_input else "")
    out = (": %s" % prompt) if prompt else ""
    return out + _media_path_suffix(tool)


def _image_edit(view, tool) -> str:
    prompt = _clip(tool.tool_input.get("prompt", "") if tool.tool_input else "")
    out = (": %s" % prompt) if prompt else ": edit"
    out += _media_path_suffix(tool)
    if not extract_media_path(getattr(tool, "result", None), tool.tool_input):
        try:
            from .session_api import get_session_for_view
            v = getattr(view, "view", None)
            sess = get_session_for_view(v) if v else None
            et = getattr(sess, "edit_target", None) if sess else None
            if et:
                out += " · target %s" % (media_display_path(et) or et)
        except Exception:
            pass
    return out


def _image_to_video(view, tool) -> str:
    prompt = _clip(tool.tool_input.get("prompt", "") if tool.tool_input else "")
    out = (": %s" % prompt) if prompt else ": animate"
    return out + _media_path_suffix(tool)


def _reference_to_video(view, tool) -> str:
    prompt = _clip(tool.tool_input.get("prompt", "") if tool.tool_input else "")
    out = (": %s" % prompt) if prompt else ": refs→video"
    return out + _media_path_suffix(tool)


def _video_gen(view, tool) -> str:
    prompt = _clip(tool.tool_input.get("prompt", "") if tool.tool_input else "")
    out = (": %s" % prompt) if prompt else ""
    return out + _media_path_suffix(tool)


def _x_search(view, tool) -> str:
    inp = tool.tool_input or {}
    q = inp.get("query") or inp.get("q") or ""
    out = (": %s" % _clip(str(q), 80)) if q else ""
    if tool.result and tool.status == "done":
        out += view._format_x_search_result(tool.result)
    return out


def _x_user_search(view, tool) -> str:
    inp = tool.tool_input or {}
    q = inp.get("query") or inp.get("q") or inp.get("username") or ""
    out = (": %s" % _clip(str(q), 60)) if q else ""
    if tool.result and tool.status == "done":
        out += view._format_x_search_result(tool.result)
    return out


def _x_thread_fetch(view, tool) -> str:
    inp = tool.tool_input or {}
    tid = inp.get("post_id") or inp.get("tweet_id") or inp.get("id") or ""
    out = (": #%s" % tid) if tid else ""
    if tool.result and tool.status == "done":
        out += view._format_x_search_result(tool.result)
    return out


def _truncate_one_line(text: str, max_len: int) -> str:
    s = (text or "").strip()
    if max_len <= 0 or len(s) <= max_len:
        return s
    if max_len <= 1:
        return "…"
    return s[: max_len - 1] + "…"


def _bash(view, tool) -> str:
    cmd = tool.tool_input.get("command", "") or tool.tool_input.get("description", "")
    if "\n" in cmd:
        cmd = " ⏎ ".join(s.strip() for s in cmd.splitlines() if s.strip())
    if getattr(tool, "status", None) == "background":
        cmd = _truncate_one_line(cmd, 72)
    else:
        cmd = _truncate_one_line(cmd, 160)
    out = (": %s" % cmd) if cmd else ""
    if tool.result and tool.status in ("done", "error"):
        out += view._format_bash_result(tool.result)
    return out


def _read(view, tool) -> str:
    inp = tool.tool_input or {}
    path = inp.get("file_path") or inp.get("path") or inp.get("target_file") or ""
    out = (": %s" % path) if path else ""
    if tool.result and tool.status == "done":
        if path and os.path.isdir(path):
            out += view._format_glob_result(tool.result)
        else:
            out += view._format_read_result(tool.result)
    return out


def _read_image_path(tool) -> str:
    inp = tool.tool_input if isinstance(tool.tool_input, dict) else {}
    nested = inp.get("tool_input") or inp.get("arguments") or inp.get("input")
    if isinstance(nested, dict):
        path = (
            nested.get("path") or nested.get("file_path")
            or nested.get("target_file") or ""
        )
    else:
        path = ""
    if not path:
        path = inp.get("path") or inp.get("file_path") or inp.get("target_file") or ""
    if not path and tool.result:
        m = re.search(r"path=([^\s,)]+)", str(tool.result))
        if m:
            path = m.group(1)
    return path or ""


def _read_image(view, tool) -> str:
    path = None
    if isinstance(tool.tool_input, dict):
        path = tool.tool_input.get("_media_path")
    if not path:
        path = _read_image_path(tool) or extract_media_path(
            getattr(tool, "result", None), tool.tool_input)
    if path and isinstance(tool.tool_input, dict) and not tool.tool_input.get("_media_path"):
        tool.tool_input["_media_path"] = path
        tool.tool_input.setdefault("path", path)
    out = ""
    if path:
        label = media_display_path(path)
        if not label or label == os.path.basename(path):
            parts = path.replace("\\", "/").rstrip("/").split("/")
            label = "/".join(parts[-2:]) if len(parts) >= 2 else (
                parts[-1] if parts else path)
        out = ": %s" % label
    if tool.status == "error" and tool.result:
        out += " ✗ %s" % _clip(str(tool.result), 60)
    return out


def _scheduler_create(view, tool) -> str:
    inp = tool.tool_input or {}
    interval = inp.get("interval") or inp.get("cron") or ""
    prompt = _clip(str(inp.get("prompt") or ""), 50)
    bits = []
    if interval:
        bits.append(str(interval))
    if prompt:
        bits.append(prompt)
    out = (": " + " · ".join(bits)) if bits else ""
    if tool.status == "done":
        out += " ↻ armed"
    elif tool.status == "error" and tool.result:
        out += " ✗ %s" % _clip(str(tool.result), 40)
    return out


def _scheduler_list(view, tool) -> str:
    if tool.status == "done" and tool.result:
        return ": %s" % _clip(str(tool.result), 60)
    return ""


def _scheduler_delete(view, tool) -> str:
    inp = tool.tool_input or {}
    tid = inp.get("id") or inp.get("task_id") or ""
    out = (": %s" % tid) if tid else ""
    if tool.status == "done":
        out += " ✓ cancelled"
    return out


def _edit(view, tool) -> str:
    file_path = tool.tool_input.get("file_path", "")
    if tool.status == "error":
        return ": %s" % file_path
    old = tool.tool_input.get("old_string", "")
    new = tool.tool_input.get("new_string", "")
    unified = tool.tool_input.get("unified_diff", "")
    if unified:
        diff_str = view._format_unified_diff(unified)
        line_num = view._extract_diff_line_num(unified)
    else:
        diff_str = view._format_edit_diff(old, new)
        line_num = view._find_line_number(file_path, old, new)
    out = (": %s:%s" % (file_path, line_num)) if line_num else (": %s" % file_path)
    if diff_str:
        out += diff_str
    return out


def _write(view, tool) -> str:
    inp = tool.tool_input or {}
    path = (
        inp.get("file_path") or inp.get("path") or inp.get("target_file")
        or inp.get("filePath") or ""
    )
    content = (
        inp.get("content") or inp.get("contents") or inp.get("new_string")
        or inp.get("newText") or ""
    )
    if not isinstance(content, str):
        content = str(content) if content is not None else ""
    out = (": %s" % path) if path else ""
    nbytes = 0
    lines = 0
    if content:
        lines = len(content.splitlines()) or (1 if content else 0)
        nbytes = len(content.encode("utf-8", "replace"))
    elif tool.status == "done" and path and os.path.isfile(path):
        try:
            nbytes = os.path.getsize(path)
            with open(path, "rb") as f:
                data = f.read()
            lines = data.count(b"\n")
            if data and not data.endswith(b"\n"):
                lines += 1
        except OSError:
            pass
    if nbytes or lines:
        size = ("%.1f KB" % (nbytes / 1024.0)) if nbytes >= 1024 else ("%d B" % nbytes)
        out += " → %d lines, %s" % (lines, size)
    return out


def _glob(view, tool) -> str:
    out = ": %s" % tool.tool_input.get("pattern", "")
    if tool.result and tool.status == "done":
        out += view._format_glob_result(tool.result)
    return out


def _grep(view, tool) -> str:
    out = ": %s" % tool.tool_input.get("pattern", "")
    if tool.result and tool.status == "done":
        out += view._format_grep_result(tool.result)
    return out


def _websearch(view, tool) -> str:
    q = tool.tool_input.get("query", "") if tool.tool_input else ""
    out = (": %s" % q) if q else ""
    if tool.result and tool.status in ("done", "error"):
        out += view._format_websearch_result(tool.result)
    return out


def _search_tool(view, tool) -> str:
    q = tool.tool_input.get("query", "")
    return (": %s" % q) if q else ""


def _update_goal(view, tool) -> str:
    inp = tool.tool_input or {}
    if inp.get("blocked_reason"):
        return ": blocked — %s" % inp["blocked_reason"]
    if inp.get("completed") is True:
        msg = (inp.get("message") or "").strip()
        return (": completed — %s" % msg) if msg else ": completed"
    msg = (inp.get("message") or "").strip()
    return (": %s" % msg) if msg else ": progress"


def _goal_verdict(view, tool) -> str:
    inp = tool.tool_input or {}
    if inp.get("achieved") is True:
        n = len(inp.get("evidence") or []) if isinstance(inp.get("evidence"), list) else 0
        return (": achieved (evidence×%d)" % n) if n else ": achieved"
    gaps = inp.get("gaps") or []
    if isinstance(gaps, list) and gaps:
        return ": not_achieved — %s" % gaps[0]
    return ": not_achieved"


def _webfetch(view, tool) -> str:
    return ": %s" % tool.tool_input.get("url", "")


def _task(view, tool) -> str:
    inp = tool.tool_input or {}
    sub = (
        inp.get("subagent_type") or inp.get("subagentType")
        or inp.get("type") or inp.get("agent_type") or ""
    )
    desc = inp.get("description") or inp.get("prompt") or ""
    if not isinstance(desc, str):
        desc = str(desc) if desc else ""
    if desc:
        desc = desc.strip().split("\n", 1)[0].strip()
        if len(desc) > 80:
            desc = desc[:77] + "…"
    if sub and desc:
        return ": %s — %s" % (sub, desc)
    if sub or desc:
        return ": %s" % (sub or desc)
    if tool.status in ("pending", "running", "in_progress"):
        return ": subagent…"
    return ""


def _notebook_edit(view, tool) -> str:
    return ": %s" % tool.tool_input.get("notebook_path", "")


def _todo_write(view, tool) -> str:
    todos = (
        tool.tool_input.get("todos") or tool.tool_input.get("items")
        or tool.tool_input.get("tasks") or []
    )
    count = len(todos) if isinstance(todos, list) else "?"
    return ": %s task%s" % (count, "" if count == 1 else "s")


def _task_create(view, tool) -> str:
    subject = tool.tool_input.get("subject", "") or tool.tool_input.get("description", "")
    return (": %s" % subject) if subject else ""


def _task_update(view, tool) -> str:
    tid = tool.tool_input.get("taskId", "")
    status = tool.tool_input.get("status")
    subject = tool.tool_input.get("subject")
    bits = []
    if tid:
        bits.append("#%s" % tid)
    if status:
        bits.append(status)
    if subject:
        bits.append(subject)
    return (": %s" % " ".join(bits)) if bits else ""


def _task_list(view, tool) -> str:
    return ""


def _task_get(view, tool) -> str:
    inp = tool.tool_input or {}
    tid = inp.get("taskId") or inp.get("task_id") or ""
    if not tid:
        ids = inp.get("task_ids") or inp.get("taskIds") or []
        if isinstance(ids, list) and ids:
            tid = ids[0]
    block = inp.get("block")
    bits = []
    if tid:
        bits.append("#%s" % tid)
    if block:
        bits.append("wait")
    if bits:
        return ": %s" % " ".join(str(b) for b in bits)
    if tool.status in ("pending", "running", "in_progress"):
        return ": waiting…"
    return ""


def _ask_user(view, tool) -> str:
    inp = tool.tool_input or {}
    question = inp.get("question") or ""
    if not question:
        qs = inp.get("questions") or []
        if isinstance(qs, list) and qs:
            first = qs[0]
            if isinstance(first, dict):
                question = first.get("question") or first.get("header") or ""
            elif first:
                question = str(first)
    out = (": %s" % question) if question else ""
    if tool.result and tool.status == "done":
        out += view._format_ask_user_result(tool.result, question)
    return out


def _skill(view, tool) -> str:
    return ": %s" % tool.tool_input.get("skill", "")


def _enter_plan_mode(view, tool) -> str:
    return ": entering plan mode..."


def _exit_plan_mode(view, tool) -> str:
    st = getattr(tool, "status", None)
    if st == "done":
        return ""
    if st == "error":
        return ": rejected"
    allowed = (tool.tool_input or {}).get("allowedPrompts", [])
    if allowed:
        return ": %d requested permissions" % len(allowed)
    return ": awaiting approval..."


def _find_file(view, tool) -> str:
    inp = _tool_input(tool)
    q = inp.get("query") or ""
    pat = inp.get("pattern") or ""
    lim = inp.get("limit")
    bits = [_clip(str(q), 50)] if q else []
    if pat:
        bits.append("glob %s" % pat)
    if lim:
        bits.append("limit %s" % lim)
    out = _join_bits(*bits)
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _get_symbols(view, tool) -> str:
    inp = _tool_input(tool)
    q = inp.get("query") or ""
    if isinstance(q, list):
        q = ", ".join(str(x) for x in q[:5])
    fp = inp.get("file_path") or ""
    bits = [_clip(str(q), 50)] if q else []
    if fp:
        bits.append(_basename(fp))
    out = _join_bits(*bits)
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _goto_symbol(view, tool) -> str:
    inp = _tool_input(tool)
    q = inp.get("query") or ""
    out = _join_bits(_clip(str(q), 60)) if q else ""
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _read_view(view, tool) -> str:
    inp = _tool_input(tool)
    path = inp.get("file_path") or inp.get("path") or inp.get("view_name") or ""
    bits = []
    if path:
        bits.append(_basename(path) if "/" in str(path) or "\\" in str(path)
                    else _clip(str(path), 40))
    if inp.get("head"):
        bits.append("head %s" % inp["head"])
    if inp.get("tail"):
        bits.append("tail %s" % inp["tail"])
    if inp.get("grep"):
        bits.append("grep %s" % _clip(str(inp["grep"]), 30))
    if inp.get("grep_i"):
        bits.append("grep -i %s" % _clip(str(inp["grep_i"]), 30))
    out = _join_bits(*bits)
    if tool.status == "done" and tool.result:
        text = str(tool.result)
        n = text.count("\n") + (1 if text.strip() else 0)
        if n > 1 or len(text) > 80:
            out += " → %d lines" % n
        else:
            out += view._format_mcp_result(tool.result)
    return out


def _get_window_summary(view, tool) -> str:
    if tool.status == "done" and tool.result:
        return view._format_mcp_result(tool.result) or ": window"
    return ": window"


def _list_backends(view, tool) -> str:
    if tool.status == "done" and tool.result:
        return view._format_mcp_result(tool.result) or ""
    return ""


def _list_profiles(view, tool) -> str:
    if tool.status == "done" and tool.result:
        return view._format_mcp_result(tool.result) or ""
    return ""


def _spawn_session(view, tool) -> str:
    inp = _tool_input(tool)
    name = inp.get("name") or ""
    backend = inp.get("backend") or ""
    profile = inp.get("profile") or ""
    prompt = inp.get("prompt") or ""
    bits = []
    if name:
        bits.append(str(name)[:30])
    if backend and backend != "claude":
        bits.append(str(backend))
    if profile:
        bits.append("profile=%s" % profile)
    if inp.get("fork_current"):
        bits.append("fork:self")
    fa = inp.get("fork_from_agent_id")
    if fa:
        bits.append("fork:%s" % fa)
    ff = inp.get("fork_from_view_id")
    if ff is not None and not fa:
        bits.append("fork:view:%s" % ff)
    if prompt:
        bits.append(_clip(str(prompt), 45))
    out = _join_bits(*bits)
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _send_to_session(view, tool) -> str:
    inp = _tool_input(tool)
    aid = inp.get("agent_id")
    vid = inp.get("view_id")
    prompt = inp.get("prompt") or ""
    bits = []
    if aid:
        bits.append(str(aid))
    elif vid is not None:
        bits.append("view %s" % vid)
    if prompt:
        bits.append(_clip(str(prompt), 50))
    return _join_bits(*bits)


def _list_sessions(view, tool) -> str:
    if tool.status == "done" and tool.result:
        return view._format_mcp_result(tool.result) or ""
    return ""


def _read_session_output(view, tool) -> str:
    inp = _tool_input(tool)
    vid = inp.get("view_id")
    lines = inp.get("lines")
    bits = []
    if vid is not None:
        bits.append("view %s" % vid)
    if lines:
        bits.append("%s lines" % lines)
    out = _join_bits(*bits)
    if tool.status == "done" and tool.result:
        r = tool.result
        if isinstance(r, dict):
            cs = r.get("context_summary") or (r.get("context_budget") or {}).get("summary")
            if cs:
                bits.append(str(cs))
                out = _join_bits(*bits)
        out += view._format_mcp_result(tool.result)
    return out


def _list_profile_docs(view, tool) -> str:
    if tool.status == "done" and tool.result:
        return view._format_mcp_result(tool.result) or ""
    return ""


def _read_profile_doc(view, tool) -> str:
    inp = _tool_input(tool)
    path = inp.get("path") or ""
    out = _join_bits(path) if path else ""
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _lsp(view, tool) -> str:
    inp = _tool_input(tool)
    cmd = inp.get("cmd") or inp.get("command") or ""
    out = _join_bits(_clip(str(cmd), 70)) if cmd else ""
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _sublime_eval(view, tool) -> str:
    inp = _tool_input(tool)
    code = inp.get("code") or ""
    out = _join_bits(_clip(str(code), 60)) if code else ""
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _sublime_tool(view, tool) -> str:
    inp = _tool_input(tool)
    name = inp.get("name") or ""
    return _join_bits(name) if name else ""


def _list_tools(view, tool) -> str:
    if tool.status == "done" and tool.result:
        return view._format_mcp_result(tool.result) or ""
    return ""


def _session_info(view, tool) -> str:
    if tool.status == "done" and tool.result:
        return view._format_mcp_result(tool.result) or ""
    return ""


def _signal_complete(view, tool) -> str:
    inp = _tool_input(tool)
    sid = inp.get("session_id")
    summary = inp.get("result_summary") or ""
    bits = []
    if sid is not None:
        bits.append("session %s" % sid)
    else:
        bits.append("self")
    if summary:
        bits.append(_clip(str(summary), 45))
    if tool.status == "done" and isinstance(tool.result, dict):
        if tool.result.get("deferred") or tool.result.get("status") == "queued":
            bits.append("defer→parent")
        cs = tool.result.get("context_summary") or (
            (tool.result.get("context_budget") or {}).get("summary")
        )
        if cs:
            bits.append(str(cs))
    return _join_bits(*bits)


def _wait_for_subsession(view, tool) -> str:
    inp = _tool_input(tool)
    sid = inp.get("subsession_id") or ""
    wake = inp.get("wake_prompt") or ""
    bits = []
    if sid:
        bits.append(_clip(str(sid), 24))
    if wake:
        bits.append(_clip(str(wake), 40))
    return _join_bits(*bits)


def _garage_search(view, tool) -> str:
    inp = _tool_input(tool)
    q = inp.get("query") or ""
    out = _join_bits(_clip(str(q), 55)) if q else ""
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _mcp_call_args_fallback(tool) -> str:
    inp = _tool_input(tool)
    if not inp:
        return ""
    prefer = (
        "query", "cmd", "command", "path", "file_path", "prompt", "name",
        "view_id", "code", "pattern", "seconds", "notification_type",
        "subsession_id", "session_id", "wake_prompt", "backend", "profile",
    )
    bits = []
    seen = set()
    for k in prefer:
        if k not in inp or k in seen:
            continue
        v = inp[k]
        if v is None or v == "" or v == {} or v == []:
            continue
        seen.add(k)
        if k in ("path", "file_path"):
            bits.append(_basename(str(v)))
        else:
            bits.append(_clip(str(v), 40))
        if len(bits) >= 3:
            break
    if not bits:
        for k, v in list(inp.items())[:3]:
            if v is None or v == "" or str(k).startswith("_"):
                continue
            bits.append("%s=%s" % (k, _clip(str(v), 28)))
    return _join_bits(*bits)


SUBLIME_MCP_FORMATTERS = {
    "get_window_summary": _get_window_summary,
    "find_file": _find_file,
    "get_symbols": _get_symbols,
    "goto_symbol": _goto_symbol,
    "read_view": _read_view,
    "read_image": _read_image,
    "list_backends": _list_backends,
    "list_profiles": _list_profiles,
    "spawn_session": _spawn_session,
    "send_to_session": _send_to_session,
    "list_sessions": _list_sessions,
    "session_info": _session_info,
    "read_session_output": _read_session_output,
    "list_profile_docs": _list_profile_docs,
    "read_profile_doc": _read_profile_doc,
    "lsp": _lsp,
    "sublime_eval": _sublime_eval,
    "sublime_tool": _sublime_tool,
    "list_tools": _list_tools,
    "signal_complete": _signal_complete,
    "wait_for_subsession": _wait_for_subsession,
    "garage_search": _garage_search,
    "ask_user": _ask_user,
}  # type: Dict[str, Callable]


def _register_mcp_aliases(registry: Dict[str, Callable]) -> None:
    for short, fmt in SUBLIME_MCP_FORMATTERS.items():
        registry.setdefault(short, fmt)
        registry.setdefault("mcp__submarine__%s" % short, fmt)
        registry.setdefault("submarine__%s" % short, fmt)
        registry.setdefault("mcp__sublime__%s" % short, fmt)
        registry.setdefault("sublime__%s" % short, fmt)


TOOL_FORMATTERS = {
    "Bash": _bash,
    "Read": _read,
    "read_image": _read_image,
    "Edit": _edit,
    "Write": _write,
    "Glob": _glob,
    "Grep": _grep,
    "WebSearch": _websearch,
    "search_tool": _search_tool,
    "update_goal": _update_goal,
    "goal_verdict": _goal_verdict,
    "WebFetch": _webfetch,
    "Task": _task,
    "Agent": _task,
    "AgentSwarm": _task,
    "NotebookEdit": _notebook_edit,
    "TodoWrite": _todo_write,
    "TaskCreate": _task_create,
    "TaskUpdate": _task_update,
    "TaskList": _task_list,
    "TaskGet": _task_get,
    "TaskOutput": _task_get,
    "ask_user": _ask_user,
    "Skill": _skill,
    "EnterPlanMode": _enter_plan_mode,
    "ExitPlanMode": _exit_plan_mode,
    "image_gen": _image_gen,
    "image_edit": _image_edit,
    "image_to_video": _image_to_video,
    "reference_to_video": _reference_to_video,
    "video_gen": _video_gen,
    "x_keyword_search": _x_search,
    "x_semantic_search": _x_search,
    "x_user_search": _x_user_search,
    "x_thread_fetch": _x_thread_fetch,
    "scheduler_create": _scheduler_create,
    "CronCreate": _scheduler_create,
    "scheduler_list": _scheduler_list,
    "CronList": _scheduler_list,
    "scheduler_delete": _scheduler_delete,
    "CronDelete": _scheduler_delete,
}  # type: Dict[str, Callable]
_register_mcp_aliases(TOOL_FORMATTERS)


def _unwrap_use_tool(tool):
    name = tool.name or ""
    inp = _tool_input(tool)
    nested = inp.get("tool_input") or inp.get("arguments") or inp.get("input")
    has_nested_args = isinstance(nested, dict)
    is_wrapper_name = name in ("use_tool", "CallMcpTool", "call_mcp_tool")
    is_use_tool_shape = bool(
        has_nested_args and (inp.get("tool_name") or inp.get("name")
                             or inp.get("variant") == "UseTool"
                             or inp.get("variant") == "use_tool"))
    if not is_wrapper_name and not is_use_tool_shape:
        return tool
    inner_name = inp.get("tool_name") or inp.get("name") or name
    if has_nested_args:
        inner_input = nested
    elif any(k in inp for k in (
            "query", "path", "cmd", "command", "prompt", "file_path",
            "code", "view_id", "seconds", "target_file")):
        inner_input = {k: v for k, v in inp.items()
                       if k not in ("tool_name", "name", "server", "variant")}
    else:
        inner_input = inp
    short = _mcp_short_name(str(inner_name))
    if short in SUBLIME_MCP_FORMATTERS:
        display = "mcp__submarine__%s" % short
    else:
        display = str(inner_name)
    try:
        from .models import ToolCall as _ToolCall
        ctor = _ToolCall
    except Exception:
        ctor = type(tool)
    return ctor(
        name=display,
        tool_input=inner_input if isinstance(inner_input, dict) else {},
        status=getattr(tool, "status", "pending"),
        result=getattr(tool, "result", None),
        id=getattr(tool, "id", None),
    )


def format_tool_detail(view, tool) -> str:
    t = _unwrap_use_tool(tool)
    short = _mcp_short_name(t.name)
    fmt = (
        TOOL_FORMATTERS.get(t.name)
        or SUBLIME_MCP_FORMATTERS.get(short)
        or TOOL_FORMATTERS.get(short)
    )
    detail = fmt(view, t) if fmt is not None else ""

    is_host_mcp = (
        t.name.startswith("mcp__submarine__")
        or t.name.startswith("mcp__sublime__")
        or t.name.startswith("submarine__")
        or t.name.startswith("sublime__")
        or short in SUBLIME_MCP_FORMATTERS
    )
    is_any_mcp = (
        is_host_mcp
        or t.name.startswith("mcp__")
        or (isinstance(t.name, str) and t.name.startswith("mcp_"))
    )
    if not detail and is_host_mcp:
        detail = _mcp_call_args_fallback(t)
        if t.result and t.status == "done":
            detail += view._format_mcp_result(t.result)
    elif not detail and is_any_mcp:
        detail = _mcp_call_args_fallback(t)
        if t.result and t.status in ("done", "error"):
            detail += view._format_mcp_result(t.result)
    elif (
        not fmt
        and t.name.startswith("mcp__")
        and t.result
        and t.status == "done"
    ):
        detail = _mcp_call_args_fallback(t) + view._format_mcp_result(t.result)

    if tool.status == "background":
        detail += " (background)"
    return detail
