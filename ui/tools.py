"""Tool row model→text, background gate, host-control hide, todo side effects."""
from __future__ import annotations

from typing import Optional

from core.background import SHELL_BG as _CORE_SHELL_BG
from core.background import SUBAGENT_BG as _CORE_SUBAGENT_BG
from plat.constants import TOOL_STATUS_SYMBOLS

from .formatters import (
    extract_media_path,
    format_tool_detail,
    is_media_tool_name,
    _read_image_path,
)
from .models import (
    BACKGROUND,
    DONE,
    ERROR,
    PENDING,
    TodoItem,
    ToolCall,
    _open_todos,
    _todo_status_norm,
)

SYMBOLS = dict(TOOL_STATUS_SYMBOLS)  # pending ☐ / done ✔ / error ✘ / background ⚙

# Host/plumbing tools — tracked for lifecycle but never shown in the buffer.
_HOST_CONTROL_TOOLS = frozenset({
    "quick_done",
    "mcp__sublime__quick_done",
    "sublime__quick_done",
    "mcp__submarine__quick_done",
    "submarine__quick_done",
})

# Canonical host list: core/background.py SHELL_BG ∪ SUBAGENT_BG.
SHELL_BG = frozenset(_CORE_SHELL_BG) | frozenset(_CORE_SUBAGENT_BG)


def is_host_control_tool(name: str) -> bool:
    """Hide quick_done plumbing (never render as a tool row)."""
    n = (name or "").strip()
    if not n:
        return False
    if n in _HOST_CONTROL_TOOLS:
        return True
    low = n.lower()
    return low.endswith("quick_done") or "quick_done" in low


def may_background(name: str) -> bool:
    """True only for the background-gate allowlist."""
    return (name or "") in SHELL_BG


def same_modal_tool(a: str, b: str) -> bool:
    def _n(x: str) -> str:
        s = (x or "").strip()
        if s in ("ask_user", "AskUserQuestion", "ask_user_question", "AskUser"):
            return "ask_user"
        if s in ("ExitPlanMode", "exit_plan_mode", "exitPlanMode"):
            return "ExitPlanMode"
        if s in ("EnterPlanMode", "enter_plan_mode", "enterPlanMode"):
            return "EnterPlanMode"
        return s
    return bool(a and b and _n(a) == _n(b))


def format_tool_row(view, tool: ToolCall) -> str:
    """`  {SYMBOL} {name}{detail}\\n` — empty string for host-control tools."""
    if is_host_control_tool(tool.name):
        return ""
    symbol = SYMBOLS.get(tool.status, "☐")
    detail = format_tool_detail(view, tool)
    return "  %s %s%s\n" % (symbol, tool.name, detail)


def todos_from_raw_list(raw: list) -> list:
    """Normalize Claude/Grok/Kimi todo dicts → TodoItem list."""
    out = []
    for t in raw or []:
        if not isinstance(t, dict):
            continue
        content = (
            t.get("content") or t.get("title") or t.get("text")
            or t.get("activeForm") or t.get("active_form")
            or t.get("subject") or ""
        )
        content = str(content).strip()
        if not content:
            continue
        out.append(TodoItem(
            content=content,
            status=_todo_status_norm(t.get("status", "pending")),
            id=str(t.get("id") or t.get("taskId") or t.get("task_id") or ""),
        ))
    return out


def apply_tool_side_effects(conv, name: str, tool_input: dict, session=None) -> None:
    """TodoWrite / Task* / goal tool side effects on the live conversation."""
    tool_input = tool_input or {}
    if name == "TodoWrite" or (
            isinstance(name, str)
            and name.lower() in ("todowrite", "todolist", "todo")):
        raw = (
            tool_input.get("todos") or tool_input.get("items")
            or tool_input.get("tasks") or []
        )
        if isinstance(raw, list) and raw:
            parsed = todos_from_raw_list(raw)
            merge = tool_input.get("merge") is True
            if merge and conv.todos:
                by_id = {t.id: t for t in conv.todos if t.id}
                for t in parsed:
                    if t.id and t.id in by_id:
                        by_id[t.id].content = t.content or by_id[t.id].content
                        by_id[t.id].status = t.status
                    elif t.id:
                        by_id[t.id] = t
                    else:
                        conv.todos.append(t)
                kept = []
                seen = set()
                for t in conv.todos:
                    if t.id and t.id in by_id:
                        if t.id not in seen:
                            kept.append(by_id[t.id])
                            seen.add(t.id)
                    elif not t.id:
                        kept.append(t)
                for tid, t in by_id.items():
                    if tid not in seen:
                        kept.append(t)
                conv.todos = kept
            else:
                conv.todos = parsed
            conv.todos = _open_todos(conv.todos)
            conv.todos_all_done = not conv.todos
    elif name == "TaskCreate":
        subject = tool_input.get("subject", "") or tool_input.get("description", "")
        if subject:
            conv.todos.append(TodoItem(content=subject, status="pending"))
            conv.todos_all_done = False
    elif name == "TaskUpdate":
        tid = tool_input.get("taskId", "")
        new_status = tool_input.get("status")
        new_subject = tool_input.get("subject")
        if tid:
            st = _todo_status_norm(new_status) if new_status else ""
            if st in ("deleted", "cancelled", "canceled"):
                conv.todos = [t for t in conv.todos if t.id != tid]
                if not _open_todos(conv.todos):
                    conv.todos_all_done = True
            else:
                for todo in conv.todos:
                    if todo.id == tid:
                        if new_status:
                            todo.status = st or new_status
                        if new_subject:
                            todo.content = new_subject
                        break
                if not _open_todos(conv.todos):
                    conv.todos_all_done = True
    elif name == "update_goal" or (
            isinstance(name, str) and name.endswith("update_goal")):
        if session is not None and hasattr(session, "apply_goal_update"):
            blocked = (tool_input.get("blocked_reason") or "").strip()
            msg = (tool_input.get("message") or "").strip()
            completed = tool_input.get("completed") is True
            try:
                session.apply_goal_update(
                    message=msg, completed=completed, blocked_reason=blocked)
            except Exception as e:
                print("[Submarine] update_goal drain: %s" % e)
    elif name == "goal_verdict" or (
            isinstance(name, str) and name.endswith("goal_verdict")):
        if session is not None and hasattr(session, "apply_goal_verdict"):
            try:
                session.apply_goal_verdict(
                    achieved=tool_input.get("achieved") is True,
                    evidence=tool_input.get("evidence"),
                    gaps=tool_input.get("gaps"),
                    message=(tool_input.get("message") or "").strip(),
                )
            except Exception as e:
                print("[Submarine] goal_verdict drain: %s" % e)


def sync_todos_from_task_result(conv, name: str, tool: ToolCall, result: str) -> None:
    """Parse Task* result text and merge into conv.todos."""
    import json
    parsed_items = []
    for match in re_find_json_items(result):
        parsed_items.append(match)
    if not parsed_items:
        parsed_items = parse_task_result_lines(result)
    if not parsed_items:
        return
    if name == "TaskList":
        conv.todos = _open_todos([
            TodoItem(
                content=it.get("subject") or it.get("content") or "",
                status=_todo_status_norm(it.get("status") or "pending"),
                id=str(it.get("id") or ""),
            )
            for it in parsed_items if (it.get("subject") or it.get("content"))
        ])
        conv.todos_all_done = not conv.todos
    elif name == "TaskCreate":
        new = parsed_items[-1]
        new_id = str(new.get("id") or "")
        new_subject = (new.get("subject") or tool.tool_input.get("subject") or "").strip()
        if new_id:
            idless = [t for t in conv.todos if not t.id]
            target = next((t for t in idless
                           if new_subject and t.content.strip() == new_subject), None)
            if target is None and idless:
                target = idless[-1]
            if target is not None:
                target.id = new_id
                if new_subject:
                    target.content = new_subject
    elif name == "TaskGet":
        new = parsed_items[-1]
        tid = str(new.get("id") or "")
        if tid:
            for todo in conv.todos:
                if todo.id == tid:
                    if new.get("subject"):
                        todo.content = new["subject"]
                    if new.get("status"):
                        todo.status = new["status"]
                    break


def re_find_json_items(result: str) -> list:
    import json
    import re
    parsed = []
    for match in re.finditer(r'\{[^{}]*"(?:id|subject|status)"[^{}]*\}', result or ""):
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict) and ("id" in obj or "subject" in obj):
                parsed.append(obj)
        except Exception:
            pass
    return parsed


def parse_task_result_lines(result: str) -> list:
    import re
    parsed = []
    for line in (result or "").splitlines():
        m = re.match(r'\s*#(\d+)\s+\[(\w+)\]\s+(.+)', line)
        if m:
            parsed.append({
                "id": m.group(1), "status": m.group(2),
                "subject": m.group(3).strip(),
            })
            continue
        m_t = re.search(r'[Tt]ask\s+#(\d+)', line)
        if m_t:
            sub_m = re.search(r':\s*(.+?)\s*$', line)
            parsed.append({
                "id": m_t.group(1),
                "subject": sub_m.group(1).strip() if sub_m else "",
                "status": "pending",
            })
            continue
        m_id = re.search(r'\b(?:id|taskId)["\s:=]+([^\s,"\}]+)', line)
        m_sub = re.search(r'\bsubject["\s:=]+([^,"\}]+)', line)
        m_st = re.search(r'\bstatus["\s:=]+([^\s,"\}]+)', line)
        if m_id or m_sub:
            parsed.append({
                "id": m_id.group(1).strip() if m_id else "",
                "subject": (m_sub.group(1).strip() if m_sub else ""),
                "status": (m_st.group(1).strip() if m_st else "pending"),
            })
    return parsed


def stash_media_path(tool: ToolCall, result: Optional[str], cwd: Optional[str]) -> None:
    if not is_media_tool_name(tool.name):
        return
    path = extract_media_path(result, tool.tool_input, cwd=cwd)
    if not path and isinstance(tool.tool_input, dict):
        path = _read_image_path(tool) or None
        if path and cwd and not os_isabs_join(path, cwd):
            pass
        if path and cwd:
            import os
            if not os.path.isabs(path):
                joined = os.path.join(cwd, path)
                if os.path.isfile(joined):
                    path = joined
    if path:
        if not isinstance(tool.tool_input, dict):
            tool.tool_input = {}
        tool.tool_input["_media_path"] = path
        tool.tool_input.setdefault("path", path)


def os_isabs_join(path, cwd):
    import os
    return os.path.isabs(path)
