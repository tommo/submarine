"""Tool-name / input normalization so plugin formatters stay Claude-only.

Invariants: KIND_TO_NAME + INPUT_KEY_MAP + peel of Grok use_tool
(report extra / §9 tools). Repaint/suppress gates prevent duplicate
☐ rows and lifecycle-title noise.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

# Shared ACP ToolKind → Claude formatter names.
KIND_TO_NAME = {
    "read": "Read",
    "edit": "Edit",
    "write": "Write",
    "execute": "Bash",
    "search": "Grep",
    "glob": "Glob",
    "list": "Glob",
    "fetch": "WebFetch",
    "delete": "Bash",
    "move": "Bash",
    "think": "Thinking",
    "other": "",
}

# Agent / ACP rawInput keys → Claude tool_formatters input shape only.
# Formatters stay Claude-only (file_path, pattern, command, …); all agent
# quirks are normalized here before the plugin sees the tool_use.
INPUT_KEY_MAP = {
    "filePath": "file_path",
    "filepath": "file_path",
    "target_file": "file_path",
    "targetFile": "file_path",
    "oldString": "old_string",
    "newString": "new_string",
    "oldText": "old_string",
    "newText": "new_string",  # Edit; Write also copies to content below
    "contents": "content",  # Grok write body alias
    "oldText": "old_string",
    "newText": "new_string",
    "old_str": "old_string",
    "new_str": "new_string",
    "unifiedDiff": "unified_diff",
    "notebookPath": "notebook_path",
    "subagentType": "subagent_type",
    "replaceAll": "replace_all",
    "target_directory": "pattern",   # list_dir → Glob expects pattern
    "targetDirectory": "pattern",
}


class ToolsMixin:
    # Claude formatter names we accept as-is (never treat freeform title
    # prose like "Smoke-test subagent harness" as a tool id).
    _CANONICAL_NAMES = frozenset({
        "Read", "Write", "Edit", "Bash", "Glob", "Grep",
        "WebSearch", "WebFetch", "TodoWrite", "Task", "TaskGet", "Subagent",
        "TaskCreate", "TaskUpdate", "TaskList", "NotebookEdit",
        "Skill", "EnterPlanMode", "ExitPlanMode", "ask_user",
        "Thinking",
    })

    # Kimi (and others) emit lifecycle *titles* as tool_call rows — e.g.
    # title="Starting" → was kept as PascalCase tool id → "✔ Starting".
    _LIFECYCLE_TOOL_NOISE = frozenset({
        "starting", "started", "start", "loading", "loaded", "load",
        "thinking", "thought", "working", "processing", "waiting",
        "initializing", "initialize", "init", "preparing", "prepare",
        "running", "done", "finished", "complete", "completed",
        "idle", "ready", "pending", "progress", "status", "update",
        "beginning", "ending", "end", "stop", "stopped", "cancel",
        "cancelled", "canceled", "continue", "continuing",
    })

    def _map_agent_tool_id(self, name: str) -> Optional[str]:
        """Map an agent tool id / variant → Claude formatter name, or None."""
        if not name or not isinstance(name, str):
            return None
        n = name.strip()
        if not n:
            return None
        mapped = self.TOOL_TO_CANONICAL.get(n) or self.TOOL_TO_CANONICAL.get(n.lower())
        if mapped:
            return mapped
        # ReadFile / WriteFile / ListDir → strip trailing File/Dir noise
        if n.endswith("File") and len(n) > 4:
            base = n[:-4]
            if base in self._CANONICAL_NAMES:
                return base
        if n.endswith("Dir") and len(n) > 3:
            # ListDir → Glob (directory listing uses Glob formatter)
            mapped = self.TOOL_TO_CANONICAL.get(n[:-3]) or self.TOOL_TO_CANONICAL.get(
                n[:-3].lower())
            if mapped:
                return mapped
            if n in ("ListDir", "list_dir"):
                return "Glob"
        if n in self._CANONICAL_NAMES:
            return n
        return None

    def _is_lifecycle_tool_noise(self, text: str) -> bool:
        """True for status titles like 'Starting' / 'Working…' (not real tools)."""
        if not text or not isinstance(text, str):
            return False
        t = text.strip().lower().rstrip(".…! ")
        if not t:
            return False
        if t in self._LIFECYCLE_TOOL_NOISE:
            return True
        first = t.split()[0].strip("`'\"*") if t else ""
        return first in self._LIFECYCLE_TOOL_NOISE

    def _title_looks_like_tool_id(self, first: str) -> bool:
        """Accept multi-hump CamelCase (EnterPlanMode) / snake_case; reject Starting."""
        if not first or not first.isascii():
            return False
        if first in self._CANONICAL_NAMES or self._map_agent_tool_id(first):
            return True
        if "_" in first and first.replace("_", "").isalnum():
            return True  # snake_case machine id
        if first[0].isupper() and first.isalnum():
            # Multi-hump: ReadFile, TodoWrite, ExitPlanMode (≥2 capitals)
            if sum(1 for c in first if c.isupper()) >= 2:
                return True
        return False

    @staticmethod
    def _same_modal_tool(a: str, b: str) -> bool:
        """ask_user / ExitPlanMode aliases that must share one UI row."""
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

    def _resolve_tool_id(self, tid: Optional[str]) -> Optional[str]:
        """Map aliased secondary toolCallId → primary open row id."""
        if not tid:
            return tid
        aliases = getattr(self, "_tool_id_alias", None) or {}
        return aliases.get(tid, tid)

    def _tool_update_has_substance(self, upd: dict) -> bool:
        """True if rawInput/locations/content-JSON look like a real tool call."""
        raw = upd.get("rawInput") or {}
        if isinstance(raw, dict) and raw:
            for k in (
                "command", "path", "file_path", "target_file", "query",
                "pattern", "content", "old_string", "new_string", "tool",
                "name", "toolName", "variant", "tool_name", "tool_input",
                "arguments", "prompt", "description", "todos",
            ):
                v = raw.get(k)
                if v is not None and v != "" and v != {} and v != []:
                    return True
        if self._parse_content_args_json(upd):
            return True
        locs = upd.get("locations") or []
        if isinstance(locs, list) and any(
                isinstance(x, dict) and x.get("path") for x in locs):
            return True
        return False

    def _should_suppress_tool_row(self, upd: dict, tool_name: str) -> bool:
        """Drop Kimi lifecycle tool_call noise (✔ Starting)."""
        if self._is_subagent_output_poll(upd, tool_name):
            return True
        title = (upd.get("title") or "").strip()
        name = (tool_name or "").strip()
        # Real mapped / mcp tools always keep
        if name and name not in ("tool",):
            if (name in self._CANONICAL_NAMES
                    or self._map_agent_tool_id(name)
                    or name.startswith("mcp__")
                    or name.startswith("submarine__")
                    or name.startswith("sublime__")
                    or "__" in name):
                if not self._is_lifecycle_tool_noise(name):
                    return False
        # Lifecycle title with no real args → suppress
        if self._is_lifecycle_tool_noise(title) or self._is_lifecycle_tool_noise(name):
            if not self._tool_update_has_substance(upd):
                return True
        # Bare single-hump PascalCase name that isn't a known tool
        if (name and name[0].isupper() and name.isalpha()
                and name not in self._CANONICAL_NAMES
                and not self._map_agent_tool_id(name)
                and sum(1 for c in name if c.isupper()) < 2
                and not self._tool_update_has_substance(upd)):
            return True
        return False

    _USE_TOOL_WRAPPERS = frozenset({
        "use_tool", "UseTool", "CallMcpTool", "call_mcp_tool",
    })

    def _peel_use_tool_name(self, upd: dict) -> Optional[str]:
        """Grok MCP is use_tool{tool_name, tool_input}. Do not keep the wrapper."""
        raw = upd.get("rawInput") if isinstance(upd.get("rawInput"), dict) else {}
        inner = raw.get("tool_name") or raw.get("toolName")
        if not isinstance(inner, str) or not inner.strip():
            return None
        inner = inner.strip()
        if inner in self._USE_TOOL_WRAPPERS:
            return None
        mapped = self._map_agent_tool_id(inner)
        if mapped:
            return mapped
        if inner.startswith("submarine__"):
            short = inner[len("submarine__"):]
            return self._map_agent_tool_id(short) or short
        if inner.startswith("sublime__"):
            short = inner[len("sublime__"):]
            return self._map_agent_tool_id(short) or short
        if inner.startswith("mcp__"):
            return inner
        return inner

    def _normalize_tool_name(self, upd: dict) -> str:
        peeled = self._peel_use_tool_name(upd)
        if peeled:
            return peeled

        # 1) Grok advertises the real tool id on _meta.x.ai/tool.name — prefer it.
        # Skip when that name is the use_tool wrapper (inner is in rawInput).
        meta = upd.get("_meta") or {}
        if isinstance(meta, dict):
            xai = meta.get("x.ai/tool") or meta.get("xai_tool") or {}
            if isinstance(xai, dict):
                xn = xai.get("name") or ""
                if xn not in self._USE_TOOL_WRAPPERS:
                    mapped = self._map_agent_tool_id(xn)
                    if mapped:
                        return mapped

        # 2) rawInput tool / name / toolName / variant (e.g. variant=Task, ReadFile)
        raw = upd.get("rawInput") or {}
        if isinstance(raw, dict):
            for key in ("tool", "name", "toolName", "variant"):
                val = raw.get(key) or ""
                if val in self._USE_TOOL_WRAPPERS:
                    continue
                mapped = self._map_agent_tool_id(val)
                if mapped:
                    return mapped

        # 3) title — tool id, decorated prose, or PascalCase agent names (Kimi).
        # Bare: "spawn_subagent", "list_dir", "TodoList", "AskUserQuestion".
        # Official kimi-cli: "ToolName: key_arg" (session.py get_title).
        # Decorated: "Read `/path`", "Running: grep…", "Asking user questions".
        # Do NOT use first word of free prose / lifecycle ("Starting", "Working").
        title = upd.get("title")
        if isinstance(title, str) and title.strip():
            t = title.strip()
            # Lifecycle titles are never tool ids (Kimi "Starting" spam)
            if self._is_lifecycle_tool_noise(t) and not self._tool_update_has_substance(upd):
                mapped_kind = KIND_TO_NAME.get((upd.get("kind") or "").lower())
                return mapped_kind or "tool"
            mapped = self._map_agent_tool_id(t)
            if mapped:
                return mapped
            # "Read: /path" / "Bash: ls" / "Agent: Implement …" (kimi-cli title)
            if ":" in t:
                head = t.split(":", 1)[0].strip()
                mapped = self._map_agent_tool_id(head)
                if mapped:
                    return mapped
                if head in self._CANONICAL_NAMES:
                    return head
            # Human-prefixed activity titles (Kimi streams these often)
            low = t.lower()
            # TaskOutput polls: "Reading output of task bash-…" — NOT a file Read
            # (was mis-mapped → "⚙ Read (background)" when bg gates misfired).
            if (
                "reading output of task" in low
                or low.startswith("taskoutput")
                or low.startswith("task output")
                or low.startswith("taskget")
            ):
                return "TaskGet"
            if low.startswith(("reading ", "read ")):
                return "Read"
            if low.startswith(("writing ", "write ", "wrote ")):
                return "Write"
            if low.startswith(("editing ", "edit ", "applying ")):
                return "Edit"
            if low.startswith(("running:", "running ", "execute ", "executing ")):
                # "Running" alone is lifecycle noise; "Running: cmd" is Bash
                if low.startswith("running:") or len(t.split()) > 1:
                    return "Bash"
            if low.startswith("launching ") and "agent" in low:
                return "Subagent"
            if (
                low.startswith("asking ")
                or low.startswith("ask:")
                or low.startswith("ask ")
                or "question" in low
            ):
                return "ask_user"
            if low.startswith("todo") or "todolist" in low.replace(" ", ""):
                return "TodoWrite"
            first = t.split()[0].strip("`'\"*")
            mapped = self._map_agent_tool_id(first)
            if mapped:
                return mapped
            # PascalCase / CamelCase / snake_case only when it looks like a tool id
            if first and first not in self._USE_TOOL_WRAPPERS and self._title_looks_like_tool_id(first):
                mapped = self._map_agent_tool_id(first)
                if mapped:
                    return mapped
                if first in self._CANONICAL_NAMES:
                    return first
                if "_" in first or sum(1 for c in first if c.isupper()) >= 2:
                    return first
            # snake_case / lowercase machine id without map entry
            if (first and first.isascii() and first.replace("_", "").isalnum()
                    and ("_" in first or first.islower())
                    and first not in self._USE_TOOL_WRAPPERS
                    and not self._is_lifecycle_tool_noise(first)):
                return first

        mapped = KIND_TO_NAME.get((upd.get("kind") or "").lower())
        if mapped:
            return mapped
        return "tool"

    def _normalize_tool_input(self, raw: Any, tool_name: str = "") -> dict:
        """Map agent rawInput → Claude formatter keys only."""
        if not isinstance(raw, dict):
            return {}
        # Grok UseTool / MCP wrapper: {tool_name, tool_input:{path:…}} — peel
        # so formatters see the real args (read_image path, etc.).
        nested = raw.get("tool_input") or raw.get("arguments") or raw.get("input")
        if isinstance(nested, dict) and (
                raw.get("variant") in ("UseTool", "use_tool")
                or raw.get("tool_name")
                or raw.get("name")
                or tool_name in (
                    "use_tool", "CallMcpTool", "call_mcp_tool",
                    "read_image", "mcp__submarine__read_image",
                    "mcp__sublime__read_image")
                or (isinstance(tool_name, str) and (
                    tool_name.startswith("submarine__")
                    or tool_name.startswith("sublime__")
                    or tool_name.startswith("mcp__submarine__")
                    or tool_name.startswith("mcp__sublime__")))):
            # Prefer nested args when present; keep outer keys only as fallback
            peeled = dict(nested)
            for k, v in raw.items():
                if k in ("tool_input", "arguments", "input", "tool_name",
                         "name", "variant", "server"):
                    continue
                peeled.setdefault(k, v)
            raw = peeled
        out: dict = {}
        for k, v in raw.items():
            # Grep search root stays as path (Claude Grep also uses path);
            # don't collapse it into file_path.
            if k == "path" and tool_name in ("Grep", "Glob"):
                out["path"] = v
                continue
            if k == "path" and tool_name in ("Read", "Write", "Edit"):
                out["file_path"] = v
                continue
            if k == "path" and (
                    tool_name in (
                        "read_image", "mcp__submarine__read_image",
                        "mcp__sublime__read_image")
                    or (isinstance(tool_name, str)
                        and tool_name.endswith("read_image"))):
                # Keep as path — formatter + MCP expect path, not file_path
                out["path"] = v
                continue
            if k == "path":
                # Default: file path for file tools
                out["file_path"] = v
                continue
            out[INPUT_KEY_MAP.get(k, k)] = v
        # list_dir / Glob: pattern is the display field for Claude Glob formatter
        if tool_name == "Glob" and not out.get("pattern"):
            out["pattern"] = out.get("path") or out.get("file_path") or ""
        # WebSearch-shaped tools: ensure query
        if tool_name == "WebSearch" and not out.get("query"):
            out["query"] = out.get("pattern") or out.get("q") or ""
        # Write: Grok/ACP may only set new_string (diff) or contents
        if tool_name == "Write" and not out.get("content"):
            for alt in ("contents", "new_string", "newText", "text", "body"):
                if out.get(alt):
                    out["content"] = out[alt]
                    break
        return out

    def _reclassify_read_dir(
            self, tool_name: str, tool_input: Optional[dict]) -> tuple:
        """Kimi Read on a directory is a listing — show as Glob, not Read lines."""
        if tool_name != "Read":
            return tool_name, tool_input or {}
        inp = tool_input or {}
        path = inp.get("file_path") or inp.get("path") or ""
        if not path or not os.path.isdir(path):
            return tool_name, inp
        return "Glob", {"pattern": path, "path": path}

    def _should_repaint_tool(
            self, tid: Optional[str], upd: dict, enriched: dict) -> bool:
        """True when a tool_call_update is worth re-sending to the plugin UI.

        kimi-cli ToolCallProgress re-sends full args every delta (session.py
        _send_tool_call_part). Re-painting each char floods the transcript.
        """
        if not tid:
            return True
        title = (upd.get("title") or "").strip()
        call = self._ensure_call(tid)
        prev_title = call.title or ""
        if title and title != prev_title:
            call.title = title
            # Prefer titles that gained a subtitle ("Bash: cmd") or Agent label
            if ":" in title or len(title) > len(prev_title) + 2:
                return True
        prev = call.input
        # Full rawInput or complete content JSON → paint once usable
        if isinstance(upd.get("rawInput"), dict) and upd.get("rawInput"):
            if not prev or any(
                    enriched.get(k) and enriched.get(k) != prev.get(k)
                    for k in ("file_path", "path", "command", "pattern",
                              "description", "query", "content",
                              "old_string", "new_string", "unified_diff")):
                return True
        if self._parse_content_args_json(upd):
            if not prev:
                return True
            # Only if a display-critical field newly appeared or grew a lot
            for k in ("file_path", "path", "command", "pattern", "description",
                      "old_string", "new_string"):
                a, b = str(prev.get(k) or ""), str(enriched.get(k) or "")
                if b and (not a or len(b) > len(a) + 8):
                    return True
        return False

    def _parse_content_args_json(self, upd: dict) -> dict:
        """Parse tool args JSON from ACP content blocks (kimi-cli official).

        ToolCallStart / ToolCallProgress put accumulated args as::
          content: [{type: "content", content: {type: "text", text: "{...}"}}]
        Partial streams fail json.loads — return {}.
        """
        for block in (upd.get("content") or []):
            if not isinstance(block, dict):
                continue
            text = None
            if block.get("type") == "content":
                inner = block.get("content")
                if isinstance(inner, dict):
                    text = inner.get("text")
                elif isinstance(inner, str):
                    text = inner
            elif block.get("type") == "text":
                text = block.get("text")
            if not isinstance(text, str):
                continue
            s = text.strip()
            if not s.startswith("{"):
                continue
            try:
                data = json.loads(s)
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
            if isinstance(data, dict) and data:
                return data
        return {}

    def _tool_input_from_update(self, upd: dict, tool_name: str = "") -> dict:
        """Claude-formatter-ready input from rawInput + content JSON + title.

        kimi-cli (github.com/MoonshotAI/kimi-cli acp/session.py) streams args
        as content text JSON, not only rawInput.
        """
        out = self._normalize_tool_input(upd.get("rawInput") or {}, tool_name)
        # Official ACP: full args JSON in content blocks
        if not out:
            content_args = self._parse_content_args_json(upd)
            if content_args:
                out = self._normalize_tool_input(content_args, tool_name)
        else:
            # Merge content keys as fill-ins when rawInput sparse
            content_args = self._parse_content_args_json(upd)
            if content_args:
                extra = self._normalize_tool_input(content_args, tool_name)
                for k, v in extra.items():
                    if v and not out.get(k):
                        out[k] = v
        for loc in (upd.get("locations") or []):
            if not isinstance(loc, dict) or not loc.get("path"):
                continue
            if tool_name in ("Grep", "Glob"):
                out.setdefault("path", loc["path"])
                if tool_name == "Glob":
                    out.setdefault("pattern", loc["path"])
            else:
                out.setdefault("file_path", loc["path"])
            break
        title = upd.get("title") or ""
        # Title often embeds path: Read `/abs/path`
        if not out.get("file_path") and not out.get("pattern"):
            if isinstance(title, str) and "`" in title:
                try:
                    path = title.split("`")[1]
                    if path:
                        if tool_name == "Glob":
                            out.setdefault("pattern", path)
                        elif tool_name == "Grep":
                            out.setdefault("path", path)
                        else:
                            out.setdefault("file_path", path)
                except IndexError:
                    pass
        # kimi-cli get_title: "ToolName: key_arg" when args partial
        if isinstance(title, str) and ":" in title:
            _, _, sub = title.partition(":")
            sub = sub.strip()
            if sub:
                if tool_name == "Bash" and not out.get("command"):
                    out["command"] = sub
                elif tool_name in ("Read", "Write", "Edit") and not out.get(
                        "file_path"):
                    out["file_path"] = sub.split()[0] if sub else sub
                elif tool_name in ("Grep", "Glob") and not out.get("pattern"):
                    out["pattern"] = sub
                elif tool_name == "Task" and not out.get("description"):
                    out["description"] = sub[:200]
        # Task / Kimi Agent: description in rawInput or title
        # ("Launching coder agent: Implement render.playground…")
        if tool_name in ("Task", "Subagent"):
            if not out.get("description"):
                for k in ("description", "prompt", "task"):
                    v = out.get(k)
                    if isinstance(v, str) and v.strip():
                        # Prefer short description; prompt is often huge
                        if k == "prompt" and len(v) > 120:
                            out["description"] = v.strip().split("\n", 1)[0][:100]
                        else:
                            out["description"] = v.strip()[:200]
                        break
            if isinstance(title, str):
                t = title.strip()
                if t and not out.get("description"):
                    low = t.lower()
                    # Strip "Launching coder agent: " prefix (Kimi)
                    for prefix in (
                        "launching coder agent:",
                        "launching agent:",
                        "launching explore agent:",
                        "running agent:",
                    ):
                        if low.startswith(prefix):
                            t = t[len(prefix):].strip()
                            low = t.lower()
                            break
                    if t and low not in (
                            "task", "agent", "agentswarm", "spawn_subagent",
                            "spawn subagent"):
                        out["description"] = t[:200]
                # "Launching coder agent: …" → subagent_type=coder
                if not out.get("subagent_type") and isinstance(title, str):
                    m = re.search(
                        r"\b(coder|explore|general|reviewer|plan)\b",
                        title, flags=re.I)
                    if m:
                        out["subagent_type"] = m.group(1).lower()
            if not out.get("subagent_type"):
                st = (
                    out.get("subagentType")
                    or out.get("subagent_type")
                    or out.get("type")
                    or out.get("agent_type")
                    or ""
                )
                if st:
                    out["subagent_type"] = str(st)
        # TaskGet / get_command_or_subagent_output: task_ids → taskId
        if tool_name == "TaskGet" and not out.get("taskId"):
            ids = out.get("task_ids") or out.get("taskIds") or []
            if isinstance(ids, list) and ids:
                out["taskId"] = str(ids[0])
            elif out.get("task_id"):
                out["taskId"] = str(out["task_id"])
        # Grok: content [{type:diff, oldText, newText}]. Kimi never sends this
        # (JSON-drip args + completed text "Replaced 1 occurrence").
        diff = self._extract_diff_input(upd)
        if diff:
            for k, v in diff.items():
                if v is not None and v != "" and not out.get(k):
                    out[k] = v
        return out

    def _edit_ui_input(self, inp: dict, tool_name: str) -> dict:
        """Formatter payload: path + unified_diff. Kimi old/new can be huge."""
        if tool_name not in ("Edit", "Write") or not isinstance(inp, dict):
            return inp or {}
        out = dict(inp)
        old = out.get("old_string") or ""
        new = out.get("new_string") or ""
        if tool_name == "Edit" and not out.get("unified_diff") and (old or new):
            path = (
                out.get("file_path") or out.get("path")
                or out.get("target_file") or ""
            )
            out["unified_diff"] = self._snippet_unified_diff(
                str(old), str(new), str(path), max_chars=8000)
        # Plugin Edit formatter prefers unified_diff; drop bulky bodies so
        # the host JSON-RPC line is not enormous (drip of a whole function).
        if tool_name == "Edit" and out.get("unified_diff"):
            for k in ("old_string", "new_string"):
                v = out.get(k)
                if isinstance(v, str) and len(v) > 2000:
                    out.pop(k, None)
        return out

    @staticmethod
    def _extract_tool_content(upd: dict, tool_name: str = "") -> str:
        out: list = []
        for block in (upd.get("content") or []):
            if not isinstance(block, dict):
                continue
            inner = block.get("content") if block.get("type") == "content" else block
            if isinstance(inner, dict):
                if inner.get("type") == "text" and inner.get("text"):
                    out.append(inner["text"])
                elif inner.get("type") == "diff" and inner.get("newText"):
                    out.append(inner["newText"])
        if out:
            return "\n".join(out)
        raw = upd.get("rawOutput")
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            result = raw.get("Result") or raw.get("result")
            if isinstance(result, dict):
                body = result.get("output") or result.get("stdout") or ""
                if body:
                    return str(body)
            if tool_name == "Bash":
                stdout = raw.get("stdout") or ""
                stderr = raw.get("stderr") or ""
                joined = stdout + (("\n" + stderr) if stderr.strip() else "")
                if joined:
                    return joined
            try:
                return json.dumps(raw, ensure_ascii=False, indent=2)
            except Exception:
                return str(raw)
        return str(raw)

    def _soften_image_read_fail(
            self, text: str, tool_input: Optional[dict],
            tool_name: str) -> Optional[tuple]:
        """Rewrite Grok image read_file fails so UI is not red FAILED.

        Returns (new_text, is_error) or None if not this case.
        """
        t = (text or "").strip()
        low = t.lower()
        if "cannot read binary" not in low and "binary file" not in low:
            return None
        path = ""
        if isinstance(tool_input, dict):
            path = (
                tool_input.get("file_path")
                or tool_input.get("target_file")
                or tool_input.get("path")
                or ""
            )
        if not path and ":" in t:
            # "Cannot read binary file: /abs/path.png"
            path = t.split(":", 1)[-1].strip()
        path_l = (path or "").lower()
        is_img = any(path_l.endswith(e) for e in self._IMAGE_EXTS)
        if not is_img and tool_name not in ("Read", "read_file", "ReadFile"):
            return None
        if not is_img and "cannot read binary" not in low:
            return None
        # Still soften when path missing but message is the binary-file stock error
        # on a Read tool (Grok image reads).
        if not is_img and tool_name not in ("Read", "read_file", "ReadFile", ""):
            return None
        if not is_img and not path:
            # generic binary fail — leave as error
            return None
        if not is_img:
            return None
        note = (
            f"Image on disk: {path}\n"
            f"read_file cannot load pixels over ACP. For vision call "
            f"use_tool with tool_name=\"submarine__read_image\" and "
            f"tool_input={{\"path\": {path!r}}} "
            f"(search_tool query=\"read_image\" if needed). "
            f"image_edit/image_gen can take this path directly."
        )
        self.file_log(
            f"soften image read fail → non-error UI for {path!r}")
        return note, False

    @staticmethod
    def _extract_diff_input(upd: dict) -> Optional[dict]:
        for block in (upd.get("content") or []):
            if not isinstance(block, dict):
                continue
            inner = block.get("content") if block.get("type") == "content" else block
            if isinstance(inner, dict) and inner.get("type") == "diff":
                out: dict = {}
                if inner.get("path"):
                    out["file_path"] = inner["path"]
                if inner.get("oldText") is not None:
                    out["old_string"] = inner["oldText"]
                if inner.get("newText") is not None:
                    # Edit formatter uses new_string; Write size uses content.
                    out["new_string"] = inner["newText"]
                    out["content"] = inner["newText"]
                if out:
                    return out
        return None
