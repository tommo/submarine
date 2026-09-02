"""Auto-allow patterns and session/request_permission.

Invariants: ExitPlanMode always shows plan UI, even under
bypassPermissions (§9.19); never auto-allow AskUser (§9.24);
--always-approve only for bypassPermissions (§9.36).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import List, Optional

_BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_DIR)

from rpc_helpers import send_notification  # noqa: E402


class PermissionsMixin:
    # Tools auto-approved under acceptEdits (file + search), matching Claude
    # Code's "accept edits" posture — Bash still prompts unless listed in
    # allowed_tools / autoAllowedMcpTools. Read-only research tools (WebSearch,
    # search_tool, Grep, …) are included so ACP sessions don't freeze waiting
    # on a permission UI for every search.
    ACCEPT_EDITS_TOOLS = frozenset({
        "Read", "Write", "Edit", "Glob", "Grep", "TodoWrite", "NotebookEdit",
        "WebSearch", "WebFetch", "search_tool", "update_goal",
        "read_image", "mcp__submarine__read_image", "mcp__sublime__read_image",
        "x_keyword_search", "x_semantic_search", "x_user_search",
        "x_thread_fetch",
        # Enter plan is free; ExitPlanMode is NEVER auto-allowed (see below).
        "EnterPlanMode",
    })

    # Must show plan approval UI — never auto-allow under acceptEdits/bypass.
    PLAN_EXIT_TOOLS = frozenset({
        "ExitPlanMode", "exit_plan_mode", "exitPlanMode",
        "submit_plan", "SubmitPlan", "exit-plan-mode",
    })

    # Read-only tools safe in plan mode without prompting.
    PLAN_READONLY_TOOLS = frozenset({
        "Read", "Glob", "Grep", "WebFetch", "WebSearch", "TodoWrite",
        "search_tool", "update_goal",
        "read_image", "mcp__submarine__read_image", "mcp__sublime__read_image",
        "x_keyword_search", "x_semantic_search", "x_user_search",
        "x_thread_fetch",
        "scheduler_list", "CronList",
        "EnterPlanMode",
    })

    # ACP / Grok tool kinds that are read-only research (from toolCall.kind or
    # _meta x.ai/tool.kind).
    READONLY_KINDS = frozenset({
        "read", "search", "fetch", "think", "search_tool", "grep", "glob",
    })

    def _reload_auto_allow_patterns(self) -> None:
        """Load autoAllowedMcpTools (+ permissions.allow) from project settings."""
        patterns: List[str] = []
        try:
            from support.settings import load_project_settings  # type: ignore
            settings = load_project_settings(self.cwd) or {}
            raw = settings.get("autoAllowedMcpTools") or []
            if isinstance(raw, list):
                patterns = [str(p) for p in raw if p]
        except Exception as e:
            self.log(f"load auto-allow patterns failed: {e}")
        self._auto_allow_patterns = patterns

    def _parse_permission_pattern(self, pattern: str):
        """Parse 'Tool' or 'Tool(specifier)' → (tool_name, specifier|None)."""
        if "(" in pattern and pattern.endswith(")"):
            i = pattern.index("(")
            return pattern[:i], pattern[i + 1:-1]
        return pattern, None

    def _match_permission_pattern(self, tool_name: str, tool_input: dict,
                                   pattern: str) -> bool:
        """Match tool use against an auto-allow pattern (Claude/plugin shape)."""
        import fnmatch
        parsed_tool, specifier = self._parse_permission_pattern(pattern)
        if not fnmatch.fnmatch(tool_name, parsed_tool):
            return False
        if specifier is None:
            return True
        if tool_name == "Bash":
            command = tool_input.get("command") or ""
            if not command:
                return False
            if specifier.endswith(":*"):
                prefix = specifier[:-2]
                return command.strip().startswith(prefix) or any(
                    w.startswith(prefix) for w in command.replace("|", " ")
                    .replace("&&", " ").split()
                )
            if any(c in specifier for c in "*?["):
                return fnmatch.fnmatch(command, specifier)
            return command == specifier or specifier in command.split()
        if tool_name in ("Read", "Write", "Edit"):
            file_path = tool_input.get("file_path") or ""
            if not file_path:
                return False
            if specifier.endswith("/"):
                return file_path.startswith(specifier) or (
                    os.path.dirname(file_path) + "/" == specifier)
            if any(c in specifier for c in "*?["):
                return fnmatch.fnmatch(file_path, specifier)
            if specifier.endswith(":*"):
                return file_path.startswith(specifier[:-2])
            return file_path == specifier or (
                os.path.dirname(file_path) == os.path.dirname(specifier.rstrip("/")))
        if tool_name == "Skill":
            return (tool_input.get("skill") or "") == specifier
        match_value = (
            tool_input.get("pattern")
            or tool_input.get("url")
            or tool_input.get("command")
            or tool_input.get("path")
            or tool_input.get("query")
            or ""
        )
        if not match_value:
            return False
        if specifier.endswith(":*"):
            return str(match_value).startswith(specifier[:-2])
        if any(c in specifier for c in "*?["):
            return fnmatch.fnmatch(str(match_value), specifier)
        return str(match_value) == specifier

    def _tool_is_readonly(self, tool_call: dict, tool_name: str) -> bool:
        """True for research/read tools (Grok marks these via kind / _meta)."""
        if tool_name in self.PLAN_READONLY_TOOLS or tool_name in (
                "WebSearch", "WebFetch", "search_tool", "Grep", "Glob", "Read"):
            return True
        meta = (tool_call.get("_meta") or {}).get("x.ai/tool") or {}
        if meta.get("read_only") is True:
            return True
        kind = (
            (tool_call.get("kind") or "")
            or (meta.get("kind") or "")
            or ""
        ).lower()
        if kind in self.READONLY_KINDS:
            return True
        # MCP tools often look like server__tool; treat common search names as RO.
        low = tool_name.lower()
        if any(s in low for s in ("search", "grep", "fetch", "read", "list")):
            if not any(s in low for s in (
                    "write", "edit", "delete", "run", "exec", "bash", "shell")):
                return True
        return False

    def _is_plan_exit_tool(self, tool_name: str, tool_call: Optional[dict] = None) -> bool:
        """True for ExitPlanMode / submit_plan (any agent naming)."""
        if not tool_name:
            tool_name = ""
        if tool_name in self.PLAN_EXIT_TOOLS or tool_name == "ExitPlanMode":
            return True
        low = (tool_name or "").lower().replace("_", "").replace("-", "")
        if low in ("exitplanmode", "submitplan"):
            return True
        if "exitplan" in low or low.endswith("planmode") and "exit" in low:
            return True
        # Title / rawInput hints (Kimi sometimes only sets title)
        tc = tool_call or {}
        title = (tc.get("title") or "")
        if isinstance(title, str):
            tlow = title.lower().replace(" ", "").replace("_", "")
            if "exitplan" in tlow or tlow in ("submitplan", "exitplanmode"):
                return True
        raw = tc.get("rawInput") or {}
        if isinstance(raw, dict):
            for k in ("name", "tool", "variant", "toolName"):
                v = raw.get(k)
                if isinstance(v, str) and self._is_plan_exit_tool(v, {}):
                    return True
        return False

    def _permission_decision(self, tool_name: str,
                              tool_input: dict,
                              tool_call: Optional[dict] = None) -> Optional[bool]:
        """Apply plugin permission rules.

        Returns True (auto-allow), False (auto-deny), or None (ask user).
        """
        mode = self.permission_mode or "default"
        tool_call = tool_call or {}

        # Plan exit always needs the plan approval UI — never auto-allow,
        # even under bypassPermissions (Claude bridge parity).
        if self._is_plan_exit_tool(tool_name, tool_call):
            return None

        # AskUserQuestion is a choice UI, not a Y/N allow. Auto-allowing it
        # always selects the first option (Option A / q0_opt_0).
        if tool_name in (
                "AskUserQuestion", "ask_user", "ask_user_question", "AskUser"):
            return None

        # Full bypass — same as Claude bypassPermissions / --always-approve.
        if mode == "bypassPermissions":
            return True

        # Built-in submarine MCP tools are always trusted (Claude bridge parity).
        # Also accept legacy mcp__sublime__* prefixes from older settings.
        if (tool_name.startswith("mcp__submarine__")
                or tool_name.startswith("mcp__sublime__")):
            return True

        # Project / user auto-allow patterns (Always button + permissions.allow).
        # Never pattern-auto-allow plan exit (handled above).
        for pattern in self._auto_allow_patterns:
            if self._match_permission_pattern(tool_name, tool_input, pattern):
                self.file_log(
                    f"permission auto-allow {tool_name} via pattern {pattern!r}")
                return True

        # acceptEdits: auto-approve file edits + read-only research (search/grep
        # /web). Bash and mutating MCP still prompt unless allowlisted.
        if mode in ("acceptEdits", "auto"):
            if tool_name in self.ACCEPT_EDITS_TOOLS:
                return True
            if tool_name in self.allowed_tools:
                return True
            if self._tool_is_readonly(tool_call, tool_name):
                return True
            return None

        # plan: read-only tools ok; mutating tools denied without UI spam.
        if mode == "plan":
            if tool_name in self.PLAN_READONLY_TOOLS:
                return True
            if self._tool_is_readonly(tool_call, tool_name):
                return True
            if tool_name in ("Write", "Edit", "Bash", "NotebookEdit"):
                self.file_log(
                    f"permission auto-deny {tool_name} in plan mode")
                return False
            return None

        # default: prompt for everything (except patterns / sublime above).
        # Bare allowed_tools in default mode still prompt — matches session.py
        # which clears allowed_tools when mode is default.
        return None

    async def _acp_request_permission(self, params: dict) -> dict:
        tool_call = params.get("toolCall") or {}
        options = params.get("options") or []
        # kimi-cli OSS optionIds: approve | approve_for_session | reject
        # (session.py _handle_approval_request). Also kind-based allow_*.
        allow_once = next((o.get("optionId") for o in options
                           if isinstance(o, dict)
                           and (o.get("kind") == "allow_once"
                                or o.get("optionId") in ("approve", "approve_once"))), None)
        allow_always = next((o.get("optionId") for o in options
                             if isinstance(o, dict)
                             and (o.get("kind") == "allow_always"
                                  or o.get("optionId") in (
                                      "approve_for_session", "approve_always"))), None)
        reject_once = next((o.get("optionId") for o in options
                            if isinstance(o, dict)
                            and (o.get("kind") == "reject_once"
                                 or o.get("optionId") in ("reject", "reject_once"))), None)
        reject_always = next((o.get("optionId") for o in options
                              if isinstance(o, dict)
                              and o.get("kind") == "reject_always"), None)
        allow_id = allow_once or allow_always or next(
            (o.get("optionId") for o in options
             if isinstance(o, dict)
             and (o.get("kind") or "").startswith("allow")), None)
        reject_id = reject_once or reject_always or next(
            (o.get("optionId") for o in options
             if isinstance(o, dict)
             and (o.get("kind") or "").startswith("reject")), None)
        # AskUserQuestion is q0_opt_* + q0_skip — do not cancel before that
        # path just because generic approve/reject ids are missing.

        # Prefer Grok's embedded tool name from _meta when present.
        meta_tool = ((tool_call.get("_meta") or {}).get("x.ai/tool") or {})
        if meta_tool.get("name") and not (tool_call.get("rawInput") or {}).get("tool"):
            # Inject so _normalize_tool_name can map it.
            raw = dict(tool_call.get("rawInput") or {})
            raw.setdefault("name", meta_tool["name"])
            tool_call = dict(tool_call)
            tool_call["rawInput"] = raw
            if meta_tool.get("kind") and not tool_call.get("kind"):
                tool_call["kind"] = meta_tool["kind"]

        tool_name = self._normalize_tool_name(tool_call)
        tool_input = self._tool_input_from_update(tool_call, tool_name)
        # Recover rawInput.questions from earlier tool_call_update (permission
        # payload often only has title + truncated content).
        tid = tool_call.get("toolCallId")
        prev_call = self._call(tid) if tid else None
        if prev_call:
            for k, v in prev_call.input.items():
                tool_input.setdefault(k, v)

        # EnterPlanMode: notify host, allow (Claude bridge parity).
        if tool_name in ("EnterPlanMode", "enter_plan_mode", "EnterPlan"):
            send_notification("plan_mode_enter", {})
            self._in_plan_mode = True
            return {"outcome": {
                "outcome": "selected", "optionId": allow_id,
            }}

        # ExitPlanMode / submit plan: full plan approval UI (never generic Y/N
        # and never auto-allow). Kimi/Grok ACP both hit this path.
        if self._is_plan_exit_tool(tool_name, tool_call):
            self.file_log(
                f"permission → plan approval UI for {tool_name!r} "
                f"input_keys={list(tool_input.keys())}")
            try:
                plan_result = await self._handle_acp_exit_plan_permission(
                    tool_input, tool_call)
            except Exception as e:
                self.file_log(f"exit plan permission UI failed: {e}")
                plan_result = False
            if plan_result is True:
                return {"outcome": {
                    "outcome": "selected", "optionId": allow_id,
                }}
            # Reject or cancel
            return {"outcome": {
                "outcome": "selected", "optionId": reject_id,
            }}

        # Kimi AskUserQuestion: choices are permission options (q0_opt_0…).
        # Never auto-allow — that always picks Option A (first allow_once).
        if self._is_ask_user_permission(tool_name, options, tool_call):
            self.file_log(
                f"permission → question UI for {tool_name!r} "
                f"n_options={len(options)}")
            try:
                return await self._handle_acp_ask_user_permission(
                    tool_call, options, tool_input)
            except Exception as e:
                self.file_log(f"ask_user permission UI failed: {e}")
                return {"outcome": {"outcome": "cancelled"}}

        if allow_id is None or reject_id is None:
            self.file_log(
                f"permission request missing options: {json.dumps(options)[:300]}")
            return {"outcome": {"outcome": "cancelled"}}

        # Always re-read project auto-allows — UI "Always" persists them live.
        self._reload_auto_allow_patterns()
        decision = self._permission_decision(tool_name, tool_input, tool_call)
        self.file_log(
            f"permission {tool_name} mode={self.permission_mode} "
            f"decision={decision!r} input_keys={list(tool_input.keys())}")
        if decision is True:
            return {"outcome": {
                "outcome": "selected", "optionId": allow_id,
            }}
        if decision is False:
            return {"outcome": {
                "outcome": "selected", "optionId": reject_id,
            }}

        # Ask Sublime (Y/N/S/A — Always patterns persist via output.py).
        self.permission_id += 1
        pid = self.permission_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_permissions[pid] = fut
        send_notification("permission_request", {
            "id": pid,
            "tool": tool_name,
            "input": tool_input,
        })
        try:
            answer = await fut
        except Exception:
            return {"outcome": {"outcome": "cancelled"}}
        allowed = isinstance(answer, dict) and answer.get("kind") == "approved"
        always = isinstance(answer, dict) and bool(answer.get("always"))
        if allowed:
            # Prefer allow_always when user chose Always and the agent offered it.
            oid = (allow_always if always and allow_always else allow_id) or allow_id
            return {"outcome": {"outcome": "selected", "optionId": oid}}
        oid = (reject_always if always and reject_always else reject_id) or reject_id
        return {"outcome": {"outcome": "selected", "optionId": oid}}
