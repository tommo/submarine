"""MCP tool catalog + router. Sublime-free.

A single TOOL_TABLE drives:
- tools/list schemas (stdio MCP server)
- codegen the socket server execs
- exec_globals names (advertised == callable)

Importable by tests and by mcp/server.py (path insert). Plugin 3.8-safe.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple


# ─── Name normalization ───────────────────────────────────────────────────────

_PREFIXES = (
    "mcp__submarine__",
    "mcp__sublime__",
    "submarine__",
    "sublime__",
)


def normalize_mcp_tool_name(name: str) -> str:
    """Strip Grok/Claude MCP prefixes so tools/call names resolve.

    Accepts ``mcp__submarine__X``, ``submarine__X``, ``mcp__sublime__X``,
    ``sublime__X`` → bare ``X``. Grok sends the ``__`` forms.
    """
    n = (name or "").strip()
    if not n:
        return n
    low = n.lower()
    for prefix in _PREFIXES:
        if low.startswith(prefix):
            return n[len(prefix):]
    if n.startswith("mcp__"):
        parts = n.split("__", 2)
        if len(parts) == 3 and parts[2]:
            return parts[2]
    if "__" in n:
        left, right = n.split("__", 1)
        if left and right and left.isidentifier() and not left.startswith("mcp"):
            return right
    return n


def parse_tool_call(method: str, params: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Parse an MCP tools/call request. Returns (normalized_name, arguments)."""
    if method != "tools/call":
        raise ValueError("Invalid method: %s" % method)
    tool_name = params.get("name") or params.get("toolName") or params.get("tool")
    if not tool_name:
        raise ValueError("Missing tool name")
    arguments = params.get("arguments") or params.get("input") or {}
    if not isinstance(arguments, dict):
        arguments = {}
    return normalize_mcp_tool_name(str(tool_name)), arguments


# ─── Codegen helpers ──────────────────────────────────────────────────────────

Codegen = Callable[[Dict[str, Any]], str]


def _simple(func_name: str) -> Codegen:
    def handler(args: Dict[str, Any]) -> str:
        return "return %s()" % func_name
    return handler


def _kwargs(
    func_name: str,
    *param_names: str,
    required: Optional[List[str]] = None,
    always: Optional[List[str]] = None,
    bools: Optional[List[str]] = None,
) -> Codegen:
    """Generate ``return func(k=v, ...)``.

    ``always`` keys are emitted even when missing (as None).
    ``bools`` keys are coerced with ``bool(...)`` and default False.
    """
    required = list(required or [])
    always = list(always or [])
    bools = list(bools or [])

    def handler(args: Dict[str, Any]) -> str:
        parts = []  # type: List[str]
        for param in param_names:
            if param in bools:
                parts.append("%s=%r" % (param, bool(args.get(param, False))))
                continue
            if param in args and args[param] is not None:
                parts.append("%s=%r" % (param, args[param]))
            elif param in required:
                raise ValueError("Missing required parameter: %s" % param)
            elif param in always:
                parts.append("%s=%r" % (param, args.get(param)))
        return "return %s(%s)" % (func_name, ", ".join(parts))

    return handler


def _eval_codegen(args: Dict[str, Any]) -> str:
    return args.get("code") or ""


def _tool_file_codegen(args: Dict[str, Any]) -> str:
    return args.get("name") or ""


def _spawn_codegen(args: Dict[str, Any]) -> str:
    parts = ["prompt=%r" % (args.get("prompt") or "")]
    for key in (
        "name", "profile", "backend", "model",
        "fork_from_agent_id", "_caller_agent_id",
    ):
        if args.get(key) is not None:
            parts.append("%s=%r" % (key, args[key]))
    parts.append("fork_current=%r" % bool(args.get("fork_current", False)))
    parts.append("wait_for_completion=%r" % bool(args.get("wait_for_completion", False)))
    return "return spawn_session(%s)" % ", ".join(parts)


def _send_codegen(args: Dict[str, Any]) -> str:
    parts = ["prompt=%r" % (args.get("prompt") or "")]
    for key in ("agent_id", "session_id", "name"):
        if args.get(key):
            parts.append("%s=%r" % (key, args[key]))
    if args.get("_caller_agent_id") is not None:
        parts.append("_caller_agent_id=%r" % args["_caller_agent_id"])
    return "return send_to_session(%s)" % ", ".join(parts)


def _read_session_codegen(args: Dict[str, Any]) -> str:
    parts = []  # type: List[str]
    if args.get("agent_id"):
        parts.append("agent_id=%r" % args["agent_id"])
    parts.append("lines=%r" % args.get("lines"))
    return "return read_session_output(%s)" % ", ".join(parts)


def _read_session_edits_codegen(args: Dict[str, Any]) -> str:
    parts = []  # type: List[str]
    if args.get("agent_id"):
        parts.append("agent_id=%r" % args["agent_id"])
    parts.append("offset=%r" % args.get("offset", 0))
    parts.append("limit=%r" % args.get("limit", 10))
    if args.get("file_path"):
        parts.append("file_path=%r" % args["file_path"])
    return "return read_session_edits(%s)" % ", ".join(parts)


def _lsp_codegen(args: Dict[str, Any]) -> str:
    return "return lsp(cmd=%r)" % (args.get("cmd") or "")


def _find_file_codegen(args: Dict[str, Any]) -> str:
    return "return find_file(%r, %r, %r)" % (
        args.get("query") or "",
        args.get("pattern"),
        args.get("limit", 20),
    )


def _get_symbols_codegen(args: Dict[str, Any]) -> str:
    return "return get_symbols(%r, %r, %r)" % (
        args.get("query") or "",
        args.get("file_path"),
        args.get("limit", 10),
    )


def _read_view_codegen(args: Dict[str, Any]) -> str:
    return (
        "return read_view(%r, %r, %s, %s, %r, %r)"
        % (
            args.get("file_path"),
            args.get("view_name"),
            args.get("head"),
            args.get("tail"),
            args.get("grep"),
            args.get("grep_i"),
        )
    )


def _read_image_codegen(args: Dict[str, Any]) -> str:
    path = args.get("path") or args.get("file_path") or args.get("target_file") or ""
    return "return read_image(path=%r, max_edge=%r)" % (path, args.get("max_edge"))


def _update_goal_codegen(args: Dict[str, Any]) -> str:
    return (
        "return update_goal(message=%r, completed=%r, blocked_reason=%r)"
        % (
            args.get("message") or "",
            bool(args.get("completed", False)),
            args.get("blocked_reason") or "",
        )
    )


def _goal_verdict_codegen(args: Dict[str, Any]) -> str:
    return (
        "return goal_verdict(achieved=%r, evidence=%r, gaps=%r, message=%r)"
        % (
            bool(args.get("achieved", False)),
            args.get("evidence"),
            args.get("gaps"),
            args.get("message") or "",
        )
    )


def _quick_done_codegen(args: Dict[str, Any]) -> str:
    return "return quick_done(status=%r, message=%r)" % (
        args.get("status") or "completed",
        args.get("message") or "",
    )


def _wait_codegen(args: Dict[str, Any]) -> str:
    return (
        "return wait_for_subsession(subsession_id=%r, agent_id=%r, wake_prompt=%r)"
        % (args.get("subsession_id"), args.get("agent_id"), args.get("wake_prompt") or "")
    )


def _signal_codegen(args: Dict[str, Any]) -> str:
    return "return signal_complete(session_id=%r, result_summary=%r)" % (
        args.get("session_id"),
        args.get("result_summary"),
    )


def _set_timer_codegen(args: Dict[str, Any]) -> str:
    if args.get("seconds") is None:
        raise ValueError("Missing required parameter: seconds")
    if args.get("wake_prompt") is None:
        raise ValueError("Missing required parameter: wake_prompt")
    return "return set_timer(seconds=%r, wake_prompt=%r)" % (
        args.get("seconds"),
        args.get("wake_prompt"),
    )


def _cancel_timer_codegen(args: Dict[str, Any]) -> str:
    if args.get("timer_id") is not None:
        return "return cancel_timer(timer_id=%r)" % args.get("timer_id")
    return "return cancel_timer()"


def _write_artifact_codegen(args: Dict[str, Any]) -> str:
    parts = [
        "name=%r" % (args.get("name") or ""),
        "content=%r" % (args.get("content") if args.get("content") is not None else ""),
        "mode=%r" % (args.get("mode") or "write"),
    ]
    for key in ("title", "summary", "auto_open", "_caller_agent_id"):
        if args.get(key) is not None:
            parts.append("%s=%r" % (key, args[key]))
    return "return write_artifact(%s)" % ", ".join(parts)


def _edit_artifact_codegen(args: Dict[str, Any]) -> str:
    if not args.get("path"):
        raise ValueError("Missing required parameter: path")
    if not args.get("op"):
        raise ValueError("Missing required parameter: op")
    parts = [
        "path=%r" % args.get("path"),
        "op=%r" % args.get("op"),
    ]
    for key in (
        "old", "new", "offset", "length", "heading", "note",
        "_caller_agent_id",
    ):
        if args.get(key) is not None:
            parts.append("%s=%r" % (key, args[key]))
    return "return edit_artifact(%s)" % ", ".join(parts)


def _read_artifact_codegen(args: Dict[str, Any]) -> str:
    if not args.get("path"):
        raise ValueError("Missing required parameter: path")
    parts = ["path=%r" % args.get("path")]
    if args.get("offset") is not None:
        parts.append("offset=%r" % args.get("offset"))
    if args.get("limit") is not None:
        parts.append("limit=%r" % args.get("limit"))
    if args.get("_caller_agent_id") is not None:
        parts.append("_caller_agent_id=%r" % args["_caller_agent_id"])
    return "return read_artifact(%s)" % ", ".join(parts)


def _list_artifacts_codegen(args: Dict[str, Any]) -> str:
    parts = []  # type: List[str]
    parts.append("scope=%r" % (args.get("scope") or "self"))
    if args.get("_caller_agent_id") is not None:
        parts.append("_caller_agent_id=%r" % args["_caller_agent_id"])
    return "return list_artifacts(%s)" % ", ".join(parts)


# ─── Catalog (single source of truth) ─────────────────────────────────────────
# Each entry: description, schema (MCP inputSchema), codegen, optional flags:
#   local=True  — handled in the stdio process (no socket); still in both tables
#   gated=True  — advertised only with --enable-read-image

_EMPTY_SCHEMA = {"type": "object", "properties": {}}  # type: Dict[str, Any]


TOOL_TABLE = {
    "get_window_summary": {
        "description": (
            "Get editor state: open files (with dirty/size), active file with "
            "selection, project folders, layout."
        ),
        "schema": _EMPTY_SCHEMA,
        "codegen": _simple("get_window_summary"),
    },
    "find_file": {
        "description": (
            "Fuzzy find files by partial name. Scores: exact > starts with > "
            "contains > path contains > fuzzy."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Partial filename to search for"},
                "pattern": {"type": "string", "description": "Optional glob pattern to filter first (e.g. '*.py')"},
                "limit": {"type": "number", "description": "Max results (default 20)"},
            },
            "required": ["query"],
        },
        "codegen": _find_file_codegen,
    },
    "get_symbols": {
        "description": (
            "Fast project-wide symbol search — find classes, functions, methods, "
            "variables by exact or partial name. Use this FIRST to locate "
            "definitions before reading files. Accepts single symbol, "
            "comma-separated list, or JSON array for batch lookup. Uses "
            "Sublime's index first, then a lightweight partial-match project "
            "scan when needed."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "query": {
                    "description": (
                        "Symbol name(s) or partial name(s) to find: string, "
                        "comma-separated, or JSON array. Examples: 'MyClass', "
                        "'handle_req', 'handle_request,process_event', "
                        "'[\"foo\", \"bar\"]'"
                    ),
                },
                "file_path": {"type": "string", "description": "Optional: limit search to specific file"},
                "limit": {"type": "number", "description": "Max results per symbol (default 10)"},
            },
            "required": ["query"],
        },
        "codegen": _get_symbols_codegen,
    },
    "goto_symbol": {
        "description": "Navigate to a symbol definition in Sublime Text",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Symbol name to navigate to"},
            },
            "required": ["query"],
        },
        "codegen": _kwargs("goto_symbol", "query", required=["query"]),
    },
    "read_view": {
        "description": (
            "Read content from any view (file buffer or scratch) in Sublime Text. "
            "Specify either file_path for file buffers or view_name for scratch "
            "buffers. Supports head/tail/grep filtering."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "File path to read (absolute or relative to project)"},
                "view_name": {"type": "string", "description": "View name for scratch buffers (e.g. output panels)"},
                "head": {"type": "integer", "description": "Read first N lines"},
                "tail": {"type": "integer", "description": "Read last N lines"},
                "grep": {"type": "string", "description": "Filter lines matching regex pattern (case-sensitive)"},
                "grep_i": {"type": "string", "description": "Filter lines matching regex pattern (case-insensitive)"},
            },
        },
        "codegen": _read_view_codegen,
    },
    "read_image": {
        "description": (
            "VISION: load a local image file (PNG/JPEG/WebP/GIF screenshots, "
            "UI renders, app captures) so the model can see pixels. Grok: call "
            "via use_tool tool_name=\"submarine__read_image\" "
            "tool_input={\"path\":\"/abs/file.png\"}; if unknown, search_tool "
            "query=\"read_image\" first. Never use read_file on images "
            "(ACP text FS → 'Cannot read binary file')."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the image file"},
                "file_path": {"type": "string", "description": "Alias for path"},
                "max_edge": {
                    "type": "number",
                    "description": "Optional max long-edge pixels before shrink (default 1600)",
                },
            },
            "required": ["path"],
        },
        "codegen": _read_image_codegen,
        "local": True,
        "gated": True,
    },
    "list_backends": {
        "description": (
            "List backends available for spawn_session's `backend` argument — "
            "built-ins (claude, codex, grok, kimi, pi) plus any custom "
            "Anthropic-compatible providers, each with live availability "
            "(auth/CLI resolved), kind, bridge family, and models. Call this "
            "before spawn_session to pick a valid backend instead of guessing. "
            "Also reports the default backend and the fork-family rule "
            "(fork_current only works within the same bridge family)."
        ),
        "schema": _EMPTY_SCHEMA,
        "codegen": _simple("list_backends"),
    },
    "list_profiles": {
        "description": (
            "List available session profiles. Profiles configure model/context "
            "for different use cases."
        ),
        "schema": _EMPTY_SCHEMA,
        "codegen": _simple("list_profiles"),
    },
    "spawn_session": {
        "description": (
            'Spawn a subsession. Returns stable agent_id.\n'
            "\n"
            'This is the default sidecar when the user says "sidecar" or '
            '"SUBLIME sidecar" (not grok/kimi/codex CLI). Named CLI drivers '
            "still use those CLIs.\n"
            "\n"
            "ALWAYS address workers by agent_id.\n"
            "Reuse warm sheets: list_sessions first; send_to_session(agent_id=…) an\n"
            "idle/sleeping child. spawn_session only if none fit.\n"
            "Honor the user's model: pass model= (and matching backend) from their\n"
            "request; list_backends if unsure. Do not substitute a default. Reuse only\n"
            "a warm child that already has that model.\n"
            "\n"
            "Workflow for base context then workers:\n"
            "  1) spawn_session(prompt=…, name=\"explorer\", backend=X, model=Y)  # returns agent_id\n"
            "  2) spawn workers with fork_from_agent_id=<explorer agent_id>  # keeps X/Y unless model= set\n"
            "\n"
            "In the child prompt require: (a) project knowledge first (list_profile_docs,\n"
            "irr, existing code); (b) finish with MCP signal_complete as its own\n"
            "last tool step.\n"
            "\n"
            "Fork rules:\n"
            "  - fork_current: fork THIS (caller) session\n"
            "  - fork_from_agent_id: fork any open session (preferred)\n"
            "\n"
            "Host appends signal_complete reminder. Parent linkage uses parent_agent_id.\n"
            "Child reports done via MCP signal_complete only — not send_to_session(parent)."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Task for the child (host adds signal_complete reminder)."},
                "name": {"type": "string", "description": "Optional: name for the session"},
                "profile": {"type": "string", "description": "Optional: profile name from list_profiles"},
                "backend": {
                    "type": "string",
                    "description": "Optional: backend (claude, codex, grok, …). When forking, defaults to source.",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Optional: submodel id from list_backends models "
                        "(e.g. deepseek-v4-flash-vision-exp). Forking without "
                        "this keeps the source session's model."
                    ),
                },
                "fork_current": {"type": "boolean", "description": "Fork caller's history into the child (default false)."},
                "fork_from_agent_id": {
                    "type": "string",
                    "description": "Fork that session by stable agent_id.",
                },
                "wait_for_completion": {
                    "type": "boolean",
                    "description": "Optional: wait for prompt to finish (default false).",
                },
            },
            "required": ["prompt"],
        },
        "codegen": _spawn_codegen,
    },
    "send_to_session": {
        "description": (
            "Send a message to another session — any window. Address it by "
            "agent_id (preferred), session_id, or its exact name (a unique "
            "name; an ambiguous one returns the candidates). Find peers with "
            "list_sessions(scope=\"all\"). Direct mail only — mid-task steer, "
            "questions, follow-ups.\n"
            "\n"
            "Do NOT use this to tell a parent that a spawned child is done. "
            "That is signal_complete (parent wait_for_subsession / host inject). "
            "Not for parent/child session completion. "
            "Mailing the same summary AND signaling completion duplicates the "
            "parent's turn.\n"
            "\n"
            "The target sees a [from agent <your agent_id>] header (or "
            "[from user] if no caller session). Reply with "
            "send_to_session(agent_id=that id). Sleeping workers auto-wake. "
            "Mid-turn: queued (sent=true); do not retry the same prompt. "
            "Prefer reuse over spawn."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Stable id from spawn_session / list_sessions"},
                "session_id": {"type": "string", "description": "The target's session_id (if you have that instead)"},
                "name": {"type": "string", "description": "The target's exact session name (must be unique)"},
                "prompt": {"type": "string", "description": "Message to send"},
            },
            "required": ["prompt"],
        },
        "codegen": _send_codegen,
    },
    "list_sessions": {
        "description": (
            "List sessions with agent_id, sleeping/working, context_budget. "
            "Default scope \"children\": your subsessions. scope \"all\": "
            "every live session in every window (name, window, project, "
            "backend) — to find a peer to send_to_session. Use agent_id for "
            "send_to_session / fork_from_agent_id."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["children", "all"],
                          "description": "children (default) or all"},
            },
        },
        "codegen": lambda args: (
            "return list_sessions(scope=%r)" % (args.get("scope") or "children")),
    },
    "read_session_edits": {
        "description": (
            "Read Edit/Write diffs from a subsession transcript (not the full "
            "chat).\n"
            "\n"
            "Use after spawn_session / list_sessions. Prefer agent_id. Page "
            "with offset/limit (default limit 10, max 40). Optional file_path "
            "filters to one file (suffix ok).\n"
            "\n"
            "Returns {total, offset, limit, count, has_more, "
            "edits:[{i, tool, file_path, line, status, diff}]}. "
            "If has_more, call again with offset += count.\n"
            "\n"
            "Complementary to read_artifact: this pages the session's edit "
            "journal (what the agent Edit/Wrote). Artifacts are a durable "
            "report store — use read_artifact for named reports."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Stable agent_id from spawn_session / list_sessions",
                },
                "offset": {
                    "type": "integer",
                    "description": "Skip this many edits (default 0)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max edits to return (default 10, max 40)",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional: only this file (exact or suffix)",
                },
            },
            "required": [],
        },
        "codegen": _read_session_edits_codegen,
    },
    "read_session_output": {
        "description": (
            "Read subsession output by agent_id. Also returns "
            "context_budget/headroom for continue vs fork strategy."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Stable agent_id"},
                "lines": {"type": "integer", "description": "Number of lines from end (default: all)"},
            },
            "required": [],
        },
        "codegen": _read_session_codegen,
    },
    "list_profile_docs": {
        "description": (
            "List documentation files available from your session's profile. "
            "These are project-specific docs configured in the profile's "
            "preload_docs patterns. Use read_profile_doc to read their contents."
        ),
        "schema": _EMPTY_SCHEMA,
        "codegen": _simple("list_profile_docs"),
    },
    "read_profile_doc": {
        "description": (
            "Read a documentation file from your session's profile docset. "
            "Use list_profile_docs to see available files. Path is relative "
            "to project root."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to the doc file (from list_profile_docs)"},
            },
            "required": ["path"],
        },
        "codegen": _kwargs("read_profile_doc", "path", required=["path"]),
    },
    "lsp": {
        "description": (
            "Language server integration - get type info, definitions, "
            "references, diagnostics from running LSP servers. Commands:\n"
            "- hover <file> <line> <col>         → type info and docs at position\n"
            "- definition <file> <line> <col>    → jump to symbol definition\n"
            "- references <file> <line> <col>    → find all usages of symbol\n"
            "- symbols <file>                    → list all symbols in file\n"
            "- workspace_symbols <query>         → search symbols across project\n"
            "- diagnostics [file]                → errors/warnings (default: active file)\n"
            "\n"
            "Line and col are 0-based. File can be a path or view name.\n"
            "\n"
            "Examples:\n"
            "  lsp(\"hover /path/to/file.py 42 10\")\n"
            "  lsp(\"definition /path/to/file.py 42 10\")\n"
            "  lsp(\"references /path/to/file.py 42 10\")\n"
            "  lsp(\"symbols /path/to/file.py\")\n"
            "  lsp(\"workspace_symbols MyClass\")\n"
            "  lsp(\"diagnostics\")"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "Command string"},
            },
            "required": ["cmd"],
        },
        "codegen": _lsp_codegen,
    },
    "sublime_eval": {
        "description": (
            "Execute custom Python code in Sublime Text's context.\n"
            "\n"
            "Available modules: sublime, sublime_plugin, plus every named MCP "
            "tool function. Use 'return <value>' to return results.\n"
            "\n"
            "For simple operations, prefer the dedicated tools above."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
            },
            "required": ["code"],
        },
        "codegen": _eval_codegen,
    },
    "sublime_tool": {
        "description": "Run a saved tool from .claude/sublime_tools/<name>.py",
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Tool name (without .py)"},
            },
            "required": ["name"],
        },
        "codegen": _tool_file_codegen,
    },
    "list_tools": {
        "description": "List saved tools in .claude/sublime_tools/ with descriptions",
        "schema": _EMPTY_SCHEMA,
        "codegen": _simple("list_tools"),
    },
    "quick_done": {
        "description": (
            "End this one-shot Quick reply (Quick Agent only).\n"
            "\n"
            "- completed: done — host stops the agent; user's next message starts a fresh session\n"
            "- blocked: need more from the user (why in message)\n"
            "- closed: user asked leave/close/dismiss — host hides Quick\n"
            "Do not use update_goal."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "completed | blocked | closed",
                    "enum": ["completed", "blocked", "closed"],
                },
                "message": {
                    "type": "string",
                    "description": "Optional short verdict / blocked reason / goodbye",
                },
            },
            "required": ["status"],
        },
        "codegen": _quick_done_codegen,
    },
    "update_goal": {
        "description": (
            "Report progress on the host-owned goal (requires user /goal first).\n"
            "\n"
            "- message only: progress note\n"
            "- completed=true: claim done — host re-verifies; do not assume accepted\n"
            "- blocked_reason: after multiple failed attempts (3× pauses the goal)\n"
            "\n"
            "Do not invent a goal; user activates with /goal <objective>."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Progress note or completion summary"},
                "completed": {"type": "boolean", "description": "True only when objective is fully achieved"},
                "blocked_reason": {
                    "type": "string",
                    "description": "Why the goal cannot proceed after real attempts",
                },
            },
        },
        "codegen": _update_goal_codegen,
    },
    "goal_verdict": {
        "description": (
            "Submit the host skeptic verdict during goal verify turns only.\n"
            "\n"
            "Use after inspecting evidence with real tools. Host re-validates evidence[]:\n"
            "\n"
            "- achieved=true only if every plan criterion is proven\n"
            "- evidence[] must cite on-disk files (logs/captures under evidence/) that exist\n"
            "- narrative-only lines (\"Structure:…\", \"API:…\") are rejected\n"
            "- visual/render goals need ≥1 image capture + ≥2 grounded lines — green tests alone fail\n"
            "- message must not launder residuals/stubs as \"non-blockers\" with gaps=[]\n"
            "- naming fraud (feature name ≠ implementation) → achieved=false with gaps\n"
            "- achieved=false with gaps[] (default / fail-closed)\n"
            "- Do not use for normal implementer progress (use update_goal instead)"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "achieved": {
                    "type": "boolean",
                    "description": "True only if fully proven against the plan with on-disk artifacts",
                },
                "evidence": {
                    "description": (
                        "Proof lines that include real paths (evidence/*.log, "
                        "evidence/*.png). Host checks files exist. Not prose theater."
                    ),
                    "oneOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "string"},
                    ],
                },
                "gaps": {
                    "description": (
                        "What's still missing or misnamed (required when "
                        "achieved=false). Residuals go here, not in message."
                    ),
                    "oneOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "string"},
                    ],
                },
                "message": {
                    "type": "string",
                    "description": "One-line summary; do not list non-blocker residuals while claiming achieved",
                },
            },
            "required": ["achieved"],
        },
        "codegen": _goal_verdict_codegen,
    },
    "session_info": {
        "description": (
            "Identity for THIS Sublime session (MCP is bound to your sheet).\n"
            "\n"
            "Returns agent_id, parent_agent_id, sleeping, backend, "
            "context_budget. Do NOT search files for parent — call this. "
            "Parent routing for signal_complete is automatic."
        ),
        "schema": _EMPTY_SCHEMA,
        "codegen": _simple("session_info"),
    },
    "signal_complete": {
        "description": (
            "Signal that this spawned subsession has completed.\n"
            "\n"
            "This IS the parent notification (wait_for_subsession / host inject). "
            "Do NOT also send_to_session the parent with the same result_summary "
            "— that doubles the parent's next turn.\n"
            "\n"
            "ONLY for spawn_session children. Host looks up parent from this "
            "sheet — do NOT search for parent ids. Omit session_id. Host attaches "
            "context_budget and notifies the parent only after *this* turn is idle. "
            "Finish your summary text, then call signal_complete alone — not in "
            "parallel with other tools.\n"
            "\n"
            "Example:\n"
            "  signal_complete(result_summary=\"Task done. Files: … Findings: …\")"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": (
                        "Optional. Defaults to this session's agent_id "
                        "(MCP --agent-id). Omit unless overriding."
                    ),
                },
                "result_summary": {
                    "type": "string",
                    "description": "Brief summary of what was accomplished (full work product for parent)",
                },
            },
            "required": [],
        },
        "codegen": _signal_codegen,
    },
    "wait_for_subsession": {
        "description": (
            "Wait for a child agent to complete (host-local; fires on child's signal_complete).\n"
            "\n"
            "This is the subscribe path. Do NOT also send_to_session yourself the same "
            "wake_prompt — you will run that text twice.\n"
            "\n"
            "Prefer agent_id from spawn_session (stable).\n"
            "\n"
            "Example:\n"
            "  r = spawn_session(prompt=\"Design solution\", name=\"architect\")\n"
            "  wait_for_subsession(agent_id=r['agent_id'], wake_prompt=\"Architect done — review.\")\n"
            "\n"
            "Child must signal_complete when finished (not send_to_session you). Returns wait_id."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Stable agent_id from spawn_session (preferred)",
                },
                "subsession_id": {
                    "type": "string",
                    "description": "Alias for agent_id (same for new spawns)",
                },
                "wake_prompt": {
                    "type": "string",
                    "description": "Prompt to inject when child completes",
                },
            },
            "required": ["wake_prompt"],
        },
        "codegen": _wait_codegen,
    },
    "set_timer": {
        "description": (
            "Set a host-local timer to wake this session after N seconds "
            "(clamped to a 60s minimum). One pending wake per session.\n"
            "\n"
            "Example:\n"
            "  set_timer(seconds=300, wake_prompt=\"⏰ 5 minutes elapsed!\")\n"
            "\n"
            "Returns timer_id for cancellation via cancel_timer()."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "integer", "description": "Seconds until the session is woken"},
                "wake_prompt": {"type": "string", "description": "Prompt to inject when the timer fires"},
            },
            "required": ["seconds", "wake_prompt"],
        },
        "codegen": _set_timer_codegen,
    },
    "cancel_timer": {
        "description": (
            "Cancel a host-local timer. Pass timer_id from set_timer, or omit "
            "to cancel every pending timer for this session."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "timer_id": {
                    "type": "string",
                    "description": "Timer id from set_timer. Omit to cancel all for this session.",
                },
            },
        },
        "codegen": _cancel_timer_codegen,
    },
    "write_artifact": {
        "description": (
            "Write a report/analysis/walkthrough to the agent artifact store. "
            "THIS WRITE IS THE OUTPUT — do not also print the report into the "
            "transcript. The session gets a compact card (name, size, summary); "
            "the file is the source of truth.\n"
            "\n"
            "Prefer this over dumping long text into the session. Reply with "
            "the returned path and a short summary (signal_complete payload).\n"
            "\n"
            "Surgical edits: prefer the agent's native Edit/Write on the "
            "returned path. Use edit_artifact only when you have no file-edit "
            "tool (MCP-only callers) or need byte-range / heading ops.\n"
            "\n"
            "If read_artifact journal_tail shows op=external after your last "
            "write, the user edited the file — re-read before rewriting.\n"
            "\n"
            "Returns {path, bytes} — not the content."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "File name/slug (e.g. auth-analysis). Sanitized; .md default.",
                },
                "content": {
                    "type": "string",
                    "description": "Full file contents (mode=write) or text to append (mode=append).",
                },
                "mode": {
                    "type": "string",
                    "description": "write (create/rewrite, default) or append.",
                },
                "title": {
                    "type": "string",
                    "description": "Optional display title for the card/index.",
                },
                "summary": {
                    "type": "string",
                    "description": "One-line summary shown on the transcript card.",
                },
                "auto_open": {
                    "description": (
                        "Override artifacts_auto_open for this call: true/false "
                        "or first/always/never."
                    ),
                },
            },
            "required": ["name", "content"],
        },
        "codegen": _write_artifact_codegen,
    },
    "edit_artifact": {
        "description": (
            "Edit an existing artifact (MCP-only callers / precise ops). "
            "Prefer native Edit on the artifact path for surgical changes.\n"
            "\n"
            "ops:\n"
            "  replace_text   old, new          unique match; fails on 0 or 2+\n"
            "  replace_range  offset, length, new   byte-exact (pairs with read_artifact)\n"
            "  insert_at      offset, new\n"
            "  delete_range   offset, length\n"
            "  replace_section heading, new     markdown body under ## heading\n"
            "  append         new\n"
            "\n"
            "If journal_tail shows external (user) edits after your last write, "
            "re-read before rewriting. Do not also print the file into the transcript.\n"
            "\n"
            "Returns {path, bytes, op}."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute artifact path from write_artifact / list_artifacts.",
                },
                "op": {
                    "type": "string",
                    "description": (
                        "replace_text | replace_range | insert_at | "
                        "delete_range | replace_section | append"
                    ),
                },
                "old": {"type": "string", "description": "replace_text: exact unique substring"},
                "new": {
                    "type": "string",
                    "description": "Replacement / insert / append / section body",
                },
                "offset": {
                    "type": "integer",
                    "description": "Byte offset (replace_range / insert_at / delete_range)",
                },
                "length": {
                    "type": "integer",
                    "description": "Byte length (replace_range / delete_range)",
                },
                "heading": {
                    "type": "string",
                    "description": "Markdown heading title (with or without ##) for replace_section",
                },
                "note": {
                    "type": "string",
                    "description": "One-liner stored on the journal event",
                },
            },
            "required": ["path", "op"],
        },
        "codegen": _edit_artifact_codegen,
    },
    "read_artifact": {
        "description": (
            "Read an artifact with byte-accurate offset/limit paging. "
            "Prefer artifact paths over read_session_output for anything "
            "longer than a screen — session output is a rendered, truncated "
            "buffer and dies when the view is detached.\n"
            "\n"
            "Returns {content, offset, total_bytes, truncated, journal_tail}. "
            "journal_tail is recent change events (create/rewrite/append/edit/"
            "external). If you see op=external after your last write, the user "
            "annotated the file — re-read before rewriting."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute artifact path",
                },
                "offset": {
                    "type": "integer",
                    "description": "Byte offset (default 0)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max bytes to return (default 20000)",
                },
            },
            "required": ["path"],
        },
        "codegen": _read_artifact_codegen,
    },
    "list_artifacts": {
        "description": (
            "List artifact index entries (MRU). scope=self (default) is this "
            "agent's files; scope=all is every owner. Any agent may then "
            "read_artifact any path (flat trust)."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "description": "self (default) or all",
                },
            },
        },
        "codegen": _list_artifacts_codegen,
    },
}  # type: Dict[str, Dict[str, Any]]


# Derived views — tests assert these two cover exactly the same names.
TOOL_SCHEMAS = {name: spec["schema"] for name, spec in TOOL_TABLE.items()}
TOOL_CODEGEN = {name: spec["codegen"] for name, spec in TOOL_TABLE.items()}

# tools/call names that get --agent-id injected as _caller_agent_id / session_id.
CALLER_INJECT_TOOLS = frozenset((
    "spawn_session",
    "send_to_session",
    "signal_complete",
    "write_artifact",
    "edit_artifact",
    "read_artifact",
    "list_artifacts",
))

# Host-only debug ops (socket op=debug). Never in tools/list or TOOL_TABLE.
DEBUG_OPS = frozenset((
    "ping", "snapshot", "sessions", "composer", "log",
    "dispatch", "reload", "goal",
))


def catalog_names() -> FrozenSet[str]:
    return frozenset(TOOL_TABLE)


def exec_global_names() -> FrozenSet[str]:
    """Names the socket sandbox must bind.

    Same set as the catalog minus local-only tools (read_image is served
    by the stdio process, never exec'd on the Sublime thread).
    """
    return frozenset(
        name for name, spec in TOOL_TABLE.items() if not spec.get("local")
    )


def is_local_tool(name: str) -> bool:
    spec = TOOL_TABLE.get(normalize_mcp_tool_name(name))
    return bool(spec and spec.get("local"))


def is_gated_tool(name: str) -> bool:
    spec = TOOL_TABLE.get(normalize_mcp_tool_name(name))
    return bool(spec and spec.get("gated"))


def list_tool_descriptors(enable_read_image: bool = False) -> List[Dict[str, Any]]:
    """MCP tools/list payload, generated from TOOL_TABLE.

    ``read_image`` is advertised only when ``enable_read_image`` is true.
    """
    tools = []  # type: List[Dict[str, Any]]
    for name, spec in TOOL_TABLE.items():
        if spec.get("gated") and not enable_read_image:
            continue
        tools.append({
            "name": name,
            "description": spec["description"],
            "inputSchema": spec["schema"],
        })
    return tools


def route(tool_name: str, args: Dict[str, Any]) -> str:
    """Generate the Python the socket server execs for ``tool_name``."""
    name = normalize_mcp_tool_name(tool_name)
    spec = TOOL_TABLE.get(name)
    if spec is None:
        raise ValueError("Unknown tool: %s" % tool_name)
    codegen = spec["codegen"]
    return codegen(args or {})


def has_tool(tool_name: str) -> bool:
    name = normalize_mcp_tool_name(tool_name)
    return name in TOOL_TABLE


class ToolRouter:
    """Thin wrapper so callers can share one object (matches the old API)."""

    def route(self, tool_name: str, args: Dict[str, Any]) -> str:
        return route(tool_name, args)

    def has_tool(self, tool_name: str) -> bool:
        return has_tool(tool_name)


def create_router() -> ToolRouter:
    return ToolRouter()
