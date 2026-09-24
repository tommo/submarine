#!/usr/bin/env python3
"""
Bridge process between Sublime Text (Python 3.8) and Claude Agent SDK (Python 3.10+).
Communicates via JSON-RPC over stdio.
"""
import asyncio
import datetime
import json
import os
import re
import sys
import time
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional

# Import shared utilities
sys.path.insert(0, str(Path(__file__).parent))
from support.settings import load_project_settings
from support.log import get_bridge_logger, ContextLogger
from support.const import BRIDGE_BUFFER_SIZE


# Initialize logger
_logger = get_bridge_logger()

# Set env var so child processes (bash commands) can detect they're running under Claude agent
os.environ["CLAUDE_AGENT"] = "1"
# Tool output is captured as plain text (not a TTY). Force monochrome so
# CLIs do not emit ESC sequences into Bash tool results.
os.environ["NO_COLOR"] = "1"
os.environ["FORCE_COLOR"] = "0"
os.environ["CLICOLOR"] = "0"
os.environ["CLICOLOR_FORCE"] = "0"
if not os.environ.get("TERM") or os.environ.get("TERM", "").startswith("xterm"):
    os.environ["TERM"] = "dumb"
os.environ.pop("COLORTERM", None)

# task_updated.patch.status values that mean the task ended. (The SDK dropped
# the old `is_backgrounded` patch flag for a {status, end_time} patch.)
_TASK_TERMINAL = ("completed", "failed", "cancelled", "canceled",
                  "error", "errored", "aborted", "timeout", "crashed",
                  "killed", "stopped")


# ── cron: the agent's built-in CronCreate is inert under the SDK (no REPL idle
# loop fires it), so the bridge shadows those jobs and fires them itself. ──────
def _cron_field_match(value: int, field: str, vmin: int, vmax: int) -> bool:
    """Match one cron field (supports *, lists, ranges, and */step or a-b/step)."""
    if field == "*":
        return True
    for part in field.split(","):
        part = part.strip()
        step = 1
        base = part
        if "/" in part:
            base, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                continue
        if base == "*":
            lo, hi = vmin, vmax
        elif "-" in base:
            a, b = base.split("-", 1)
            lo, hi = int(a), int(b)
        else:
            lo = hi = int(base)
        if lo <= value <= hi and (value - lo) % max(step, 1) == 0:
            return True
    return False


def _cron_next_fire(expr: str, after_ts: float):
    """Next wall-clock time (epoch secs) the 5-field local-time cron matches
    strictly after `after_ts`, or None if malformed / no match within a year."""
    parts = expr.split()
    if len(parts) != 5:
        return None
    fmin, fhour, fdom, fmon, fdow = parts
    try:
        dt = (datetime.datetime.fromtimestamp(after_ts)
              .replace(second=0, microsecond=0) + datetime.timedelta(minutes=1))
        for _ in range(366 * 24 * 60):
            dow = dt.isoweekday() % 7  # cron: 0=Sun .. 6=Sat
            dom_ok = _cron_field_match(dt.day, fdom, 1, 31)
            dow_ok = _cron_field_match(dow, fdow, 0, 6)
            # standard cron quirk: restricted DoM *and* DoW → OR them
            day_ok = (dom_ok or dow_ok) if (fdom != "*" and fdow != "*") else (dom_ok and dow_ok)
            if (day_ok
                    and _cron_field_match(dt.minute, fmin, 0, 59)
                    and _cron_field_match(dt.hour, fhour, 0, 23)
                    and _cron_field_match(dt.month, fmon, 1, 12)):
                return dt.timestamp()
            dt += datetime.timedelta(minutes=1)
    except (ValueError, OverflowError):
        return None
    return None

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    UserMessage,
    SystemMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ThinkingBlock,
    PermissionResultAllow,
    PermissionResultDeny,
)


def serialize(obj: Any) -> Any:
    """Serialize SDK objects to JSON-compatible dicts."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: serialize(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [serialize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: serialize(v) for k, v in obj.items()}
    return obj


from rpc_helpers import send, send_error, send_result, send_notification


class Bridge:
    def __init__(self):
        self.client: ClaudeSDKClient | None = None
        self.options: ClaudeAgentOptions | None = None
        self.running = True
        self.pending_permissions: dict[int, asyncio.Future] = {}
        self.pending_questions: dict[int, asyncio.Future] = {}  # For AskUserQuestion
        self.pending_plan_approvals: dict[int, asyncio.Future] = {}  # For plan mode
        self.permission_id = 0
        self.question_id = 0
        self.plan_id = 0
        self.interrupted = False  # Set by interrupt(); the reader skips turn content until the closer
        self.query_id: int | None = None  # Track active query for inject_message
        self.cwd: str | None = None  # Current working directory (set by initialize)

        # One persistent reader owns the SDK stream (see _read_stream). In
        # streaming-input mode the CLI interleaves the turns we send with turns
        # it starts on its own — the follow-up for a finished background task
        # (`origin.kind == "task-notification"`), scheduled prompts, peer
        # messages. Every message is attributed to one of:
        #   _host_query  — the host's query RPC: {"id", "done", "owned",
        #                  "absorbed"}; `owned` flips when the CLI echoes our
        #                  prompt back (--replay-user-messages, origin human)
        #                  as a turn of its own, `absorbed` when that echo
        #                  lands inside a running CLI turn — the CLI attaches
        #                  a prompt sent mid-turn to that turn, and that
        #                  turn's result is then the only closer ours gets;
        #   _injected    — a turn the CLI started by itself, announced to the
        #                  host with `injected_turn` and closed by its own
        #                  ResultMessage (same origin).
        self._reader_task: asyncio.Task | None = None
        self._host_query: dict | None = None
        self._injected = False
        self._echo_seen = False
        # A query was closed without its ResultMessage (interrupt drain
        # timeout): the late result must not close the next query.
        self._stray_result_pending = False
        # task_notification summaries since the last turn start: the label of
        # the injected turn's prompt row.
        self._bg_summaries: list[str] = []
        # Live background task ids (`background_tasks_changed`), for the
        # host's reconcile poll.
        self._running_tasks: set[str] = set()
        self._bg_tool_use_ids: set[str] = set()

        # Cron jobs the agent scheduled via CronCreate (inert under the SDK), which
        # we shadow + fire ourselves. job_id -> {cron, prompt, recurring, next_fire}.
        self._crons: dict[str, dict] = {}
        self._pending_cron: dict[str, dict] = {}  # tool_use_id -> job, until its result lands the CLI id
        self._cron_monitor_task: Optional[asyncio.Task] = None
        self._wake_tasks: set = set()  # one-shot ScheduleWakeup timers (/loop dynamic)
        self._pending_wake: Optional[dict] = None  # {prompt, fire_at} mirror of the live wake, for persistence

    async def handle_request(self, req: dict) -> None:
        id = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})

        try:
            if method == "initialize":
                await self.initialize(id, params)
            elif method == "query":
                await self.query(id, params)
            elif method == "interrupt":
                await self.interrupt(id)
            elif method == "shutdown":
                await self.shutdown(id)
            elif method == "permission_response":
                await self.handle_permission_response(id, params)
            elif method == "question_response":
                await self.handle_question_response(id, params)
            elif method == "plan_response":
                await self.handle_plan_response(id, params)
            elif method == "cancel_pending":
                await self.cancel_pending(id)
            elif method == "inject_message":
                await self.inject_message(id, params)
            elif method == "get_history":
                await self.get_history(id)
            elif method == "set_model":
                model = params.get("model")
                if model and self.client:
                    await self.client.set_model(model)
                max_ctx = params.get("max_context_tokens")
                if max_ctx:
                    os.environ["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(max_ctx)
                send_result(id, {"ok": True})
            elif method == "set_effort":
                await self.set_effort(id, params)
            elif method == "stop_task":
                await self.stop_task(id, params)
            elif method == "set_permission_mode":
                mode = params.get("mode")
                if mode and self.client:
                    await self.client.set_permission_mode(mode)
                send_result(id, {"ok": True})
            elif method == "poll_bg_tasks":
                await self.poll_bg_tasks(id)
            elif method == "cancel_loop":
                await self.cancel_loop(id, params)
            elif method == "clear":
                await self.clear(id, params)
            else:
                send_error(id, -32601, f"Method not found: {method}")
        except Exception as e:
            send_error(id, -32000, str(e))

    async def initialize(self, id: int, params: dict) -> None:
        """Initialize the Claude SDK client."""
        resume_id = params.get("resume")
        fork_session = params.get("fork_session", False)
        cwd = params.get("cwd")
        agent_id = params.get("agent_id")
        self.cwd = cwd  # Store for later use (e.g., in can_use_tool)
        self._agent_id = agent_id  # Store for spawn_session to pass to subsessions
        # Vision MCP tool — default off for Claude/SDK; host may opt in.
        self._mcp_enable_read_image = bool(params.get("mcp_enable_read_image", False))
        # Quick Agent: narrow MCP surface (only quick_done auto-allowed).
        self._quick_mode = bool(params.get("quick_mode", False))

        # Generate a proper UUID for Claude CLI (--session-id requires valid UUID format)
        # For fresh sessions, generate new UUID; for resume, use existing resume_id
        if resume_id:
            session_id = resume_id
        else:
            session_id = str(uuid.uuid4())
        self._session_id = session_id

        # Change to project directory so SDK finds CLAUDE.md etc.
        if cwd and os.path.isdir(cwd):
            os.chdir(cwd)

        # Load MCP servers, agents, and plugins from project settings
        mcp_servers = self._load_mcp_servers(cwd)
        agents = self._load_agents(cwd)
        plugins = self._load_plugins(cwd)
        settings = load_project_settings(cwd)

        self.kanban_base_url = settings.get("kanban_base_url", "http://localhost:5050")
        _logger.info(f"Kanban base URL: {self.kanban_base_url}")

        _logger.info(f"initialize: params={params}")
        _logger.info(f"  resume_id={resume_id}, fork={fork_session}, resume_session_at={params.get('resume_session_at')}, cwd={cwd}, actual_cwd={os.getcwd()}")
        _logger.info(f"  mcp_servers={list(mcp_servers.keys()) if mcp_servers else None}")
        _logger.info(f"  agents={list(agents.keys()) if agents else None}")
        _logger.info(f"  plugins={plugins}")
        _logger.info(f"  subsession_id={params.get('subsession_id')}")
        # Diagnostic: log relevant env (masked) so we know what the bridge subprocess
        # actually inherited. Helps diagnose deepseek subsession issues.
        def _mask_env(k, v):
            if any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                return f"<set:{len(v)}b>" if v else "<empty>"
            return v
        env_view = {k: _mask_env(k, os.environ.get(k, "")) for k in
                    ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
                     "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
                     "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                     "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
                     "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                     "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK")}
        _logger.info(f"  env (masked) = {env_view}")

        # Build the addon text (session info, project addon, subsession guide).
        # Profile-supplied system_prompt is treated as a full replacement
        # (user intent — they're overriding Claude Code's default prompt).
        # append_system_prompt (Quick Agent) keeps the claude_code preset so
        # CLAUDE.md / project instructions still load via setting_sources.
        # Otherwise we use the `claude_code` preset with `append`, which
        # preserves Claude Code's <env> block (cwd, additional working dirs,
        # platform, date, etc.) — losing that was why add_dirs looked broken.
        profile_system_prompt = params.get("system_prompt", "")
        append_system_prompt = (params.get("append_system_prompt") or "").strip()
        addon = settings.get("system_prompt_addon")

        session_id_info = f"sublime.{session_id}"
        agent_id_info = agent_id or session_id
        parent_agent_id = params.get("parent_agent_id")
        sidecar_rule = ""
        try:
            from sidecar_skill import RULE as sidecar_rule
        except Exception:
            sidecar_rule = (
                'Unqualified "sidecar" means SUBLIME SIDECAR: MCP spawn_session, '
                "not grok/kimi/codex CLI."
            )
        session_guide = f"""

## Session Info

Session ID: {session_id_info}
Agent ID: {agent_id_info}
"""
        if sidecar_rule:
            session_guide += "\n%s\n" % sidecar_rule
        if parent_agent_id:
            session_guide += f"Parent Agent ID: {parent_agent_id}\n"

        subsession_id = params.get("subsession_id")
        self._subsession_id = subsession_id  # Store for signal_complete tool
        subsession_guide = ""
        if subsession_id:
            subsession_guide = (
                f"\nYou are subsession {subsession_id} (agent_id={agent_id_info}). "
                f"When done, call MCP signal_complete(result_summary=…) — not prose alone.\n"
            )

        if profile_system_prompt:
            # Profile wants full replacement; concatenate our pieces onto it.
            parts = [profile_system_prompt]
            if addon:
                parts.append(addon)
            parts.append(session_guide)
            if subsession_guide:
                parts.append(subsession_guide)
            system_prompt_value = "\n\n".join(parts)
        else:
            # Keep Claude Code's default prompt (env, cwd, add-dirs) and append ours.
            append_parts = []
            if append_system_prompt:
                append_parts.append(append_system_prompt)
            if addon:
                append_parts.append(addon)
            append_parts.append(session_guide)
            if subsession_guide:
                append_parts.append(subsession_guide)
            system_prompt_value = {
                "type": "preset",
                "preset": "claude_code",
                "append": "\n\n".join(append_parts),
            }

        options_dict = {
            "allowed_tools": params.get("allowed_tools", []),
            "permission_mode": params.get("permission_mode", "default"),
            "cwd": cwd,
            "system_prompt": system_prompt_value,
            "can_use_tool": self.can_use_tool,
            "resume": resume_id,
            "fork_session": fork_session,
            "setting_sources": ["user", "project"],
            "max_buffer_size": 100 * 1024 * 1024,  # 100MB for large images/files
            "include_partial_messages": True,
            "cli_path": "claude",
        }

        # Profile config: model, betas, effort
        if params.get("model"):
            options_dict["model"] = params["model"]
        if params.get("betas"):
            options_dict["betas"] = params["betas"]
        effort = params.get("effort")
        if effort:
            options_dict["effort"] = effort

        # Sandbox settings from project config
        sandbox = self._load_sandbox_settings(cwd)
        if sandbox:
            options_dict["sandbox"] = sandbox
            with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                f.write(f"  sandbox enabled: {sandbox}\n")

        # Add MCP servers if found
        if mcp_servers:
            options_dict["mcp_servers"] = mcp_servers

        # Add agents if found
        if agents:
            options_dict["agents"] = agents

        # Add plugins if found
        if plugins:
            options_dict["plugins"] = plugins

        # Additional working directories. MUST go through ClaudeAgentOptions.add_dirs,
        # not extra_args — extra_args is typed `dict[str, str | None]` and the SDK
        # does `str(value)` on it, so a list becomes "['/path1', '/path2']" passed
        # as ONE bad --add-dir arg. The proper add_dirs path emits --add-dir once
        # per path. Also: always pass them, regardless of fresh-vs-resume.
        additional_dirs = params.get("additional_dirs", [])
        if additional_dirs:
            options_dict["add_dirs"] = list(additional_dirs)

        # The CLI echoes each user turn it starts (origin `human` for ours,
        # `task-notification` for its own): that echo is how the reader tells
        # our turn from one the CLI injected. See _read_stream.
        extra_args: dict = {"replay-user-messages": None}
        if not resume_id:
            # Fresh session: specify session_id upfront via CLI arg so we don't
            # have to wait for the first ResultMessage to learn it.
            extra_args["session-id"] = session_id
        else:
            resume_session_at = params.get("resume_session_at")
            if resume_session_at:
                extra_args["resume-session-at"] = resume_session_at
        if extra_args:
            options_dict["extra_args"] = extra_args

        self.options = ClaudeAgentOptions(**options_dict)
        self.client = ClaudeSDKClient(options=self.options)

        try:
            _logger.info(f"  connecting SDK with model={options_dict.get('model')!r} "
                         f"resume={options_dict.get('resume')!r} "
                         f"add_dirs={options_dict.get('add_dirs')} "
                         f"extra_args={options_dict.get('extra_args')}")
            await self.client.connect()
            _logger.info("  SDK connect OK")
        except Exception as e:
            error_msg = str(e)
            _logger.error(f"SDK connect FAILED: {type(e).__name__}: {error_msg}")

            # If session not found or command failed during resume, retry without resume
            # The SDK wraps the actual error, so we check for common patterns
            is_session_error = (
                "No conversation found" in error_msg or
                ("Command failed" in error_msg and resume_id)
            )
            if is_session_error and resume_id:
                # If rewind failed, retry resume without rewind point
                if "extra_args" in options_dict and "resume-session-at" in options_dict.get("extra_args", {}):
                    with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                        f.write(f"resume-session-at failed, retrying plain resume: {error_msg}\n")
                    del options_dict["extra_args"]["resume-session-at"]
                    self.options = ClaudeAgentOptions(**options_dict)
                    self.client = ClaudeSDKClient(options=self.options)
                    await self.client.connect()
                else:
                    raise
            else:
                raise
        self._start_reader()

        send_result(id, {
            "status": "initialized",
            "session_id": session_id,
            "mcp_servers": list(mcp_servers.keys()) if mcp_servers else [],
            "agents": list(agents.keys()) if agents else [],
        })

        # Re-arm any wake/crons this session had pending before a restart.
        self._restore_loop_state()


    def _load_mcp_servers(self, cwd: str) -> dict:
        """Return built-in submarine MCP server only.

        Other MCP servers are loaded by SDK via setting_sources: ["user", "project"].
        """
        servers = {}

        # Always include the built-in submarine MCP server
        bridge_dir = os.path.dirname(os.path.abspath(__file__))
        plugin_dir = os.path.dirname(bridge_dir)
        mcp_server_path = os.path.join(plugin_dir, "mcp", "server.py")

        if os.path.exists(mcp_server_path):
            # Pass agent_id so MCP server can inject it into spawn_session calls
            args = [mcp_server_path]
            if self._agent_id:
                args.append(f"--agent-id={self._agent_id}")
            if getattr(self, "_mcp_enable_read_image", False):
                args.append("--enable-read-image")
            servers["submarine"] = {
                "command": sys.executable,  # Use same python as bridge
                "args": args,
            }

        if servers:
            with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                f.write(f"  injected MCP servers: {list(servers.keys())}\n")
        return servers

    def _load_sandbox_settings(self, cwd: str) -> dict:
        """Load sandbox settings from project config."""
        settings = load_project_settings(cwd)
        sandbox_config = settings.get("sandbox", {})

        if not sandbox_config.get("enabled"):
            return None

        sandbox = {
            "enabled": True,
            "auto_allow_bash_if_sandboxed": sandbox_config.get("autoAllowBashIfSandboxed", False),
        }

        # Excluded commands (bypass sandbox)
        if "excludedCommands" in sandbox_config:
            sandbox["excluded_commands"] = sandbox_config["excludedCommands"]

        # Allow model to request unsandboxed execution
        if sandbox_config.get("allowUnsandboxedCommands"):
            sandbox["allow_unsandboxed_commands"] = True

        # Network settings
        network = sandbox_config.get("network", {})
        if network:
            sandbox["network"] = {}
            if network.get("allowLocalBinding"):
                sandbox["network"]["allow_local_binding"] = True
            if network.get("allowUnixSockets"):
                sandbox["network"]["allow_unix_sockets"] = network["allowUnixSockets"]
            if network.get("allowAllUnixSockets"):
                sandbox["network"]["allow_all_unix_sockets"] = True

        return sandbox

    def _load_agents(self, cwd: str) -> dict:
        """Return empty dict - agents loaded by SDK via setting_sources."""
        # SDK loads agents from ~/.claude/settings.json and .claude/settings.json
        return {}

    def _load_plugins(self, cwd: str) -> list:
        """Return empty list - plugins loaded by SDK via setting_sources.

        SDK loads plugins from ~/.claude/settings.json and .claude/settings.json.
        """
        return []

    def _parse_permission_pattern(self, pattern: str) -> tuple[str, str | None]:
        """Parse permission pattern into (tool_name, specifier).

        Formats:
            "Bash" -> ("Bash", None)
            "Bash(git:*)" -> ("Bash", "git:*")
            "Read(/src/**)" -> ("Read", "/src/**")
        """
        if '(' in pattern and pattern.endswith(')'):
            paren_idx = pattern.index('(')
            tool_name = pattern[:paren_idx]
            specifier = pattern[paren_idx + 1:-1]
            return tool_name, specifier
        return pattern, None

    def _extract_bash_commands(self, command: str) -> list[str]:
        """Extract individual command names from a bash command string.

        Handles:
        - Command chains: cmd1 && cmd2, cmd1 || cmd2, cmd1 ; cmd2
        - Pipes: cmd1 | cmd2
        - Environment variables: FOO=bar cmd
        - Subshells: $(cmd), `cmd`

        Returns list of command names (e.g., ["cd", "git", "npm"])
        """
        import re
        import shlex

        commands = []

        # Split on command separators: &&, ||, ;, |, but not inside quotes
        # Simple approach: split on these patterns
        parts = re.split(r'\s*(?:&&|\|\||;|\|)\s*', command)

        for part in parts:
            part = part.strip()
            if not part:
                continue

            # Skip subshell wrappers
            part = re.sub(r'^\$\(|\)$|^`|`$', '', part).strip()

            # Skip leading environment variable assignments (VAR=value)
            while part and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=\S*\s+', part):
                part = re.sub(r'^[A-Za-z_][A-Za-z0-9_]*=\S*\s+', '', part)

            if not part:
                continue

            # Extract first word as command name
            try:
                tokens = shlex.split(part)
                if tokens:
                    cmd = tokens[0]
                    # Handle path prefixes like /usr/bin/git -> git
                    if '/' in cmd:
                        cmd = cmd.split('/')[-1]
                    commands.append(cmd)
            except ValueError:
                # shlex parsing failed, try simple split
                words = part.split()
                if words:
                    cmd = words[0]
                    if '/' in cmd:
                        cmd = cmd.split('/')[-1]
                    commands.append(cmd)

        return commands

    def _match_permission_pattern(self, tool_name: str, tool_input: dict, pattern: str) -> bool:
        """Check if tool use matches a permission pattern.

        Supports:
            - Simple tool match: "Bash" matches any Bash command
            - Prefix match: "Bash(git:*)" matches commands starting with "git"
            - Exact match: "Bash(git status)" matches exactly "git status"
            - Glob match: "Read(/src/**/*.py)" matches files under /src/ ending in .py
        """
        import fnmatch

        parsed_tool, specifier = self._parse_permission_pattern(pattern)

        # Tool name must match (supports wildcards like mcp__*__)
        if not fnmatch.fnmatch(tool_name, parsed_tool):
            return False

        # No specifier = match all uses of this tool
        if specifier is None:
            return True

        # Special handling for Bash - extract and match individual commands
        if tool_name == "Bash":
            full_command = tool_input.get("command", "")
            if not full_command:
                return False

            # Extract individual command names from the bash string
            cmd_names = self._extract_bash_commands(full_command)

            # Handle prefix match with :* suffix
            if specifier.endswith(":*"):
                prefix = specifier[:-2]
                # Match if ANY command starts with prefix OR full command starts with prefix
                if full_command.startswith(prefix):
                    return True
                return any(cmd.startswith(prefix) for cmd in cmd_names)

            # Handle glob/fnmatch patterns
            if any(c in specifier for c in ['*', '?', '[']):
                # Match against full command OR any individual command
                if fnmatch.fnmatch(full_command, specifier):
                    return True
                return any(fnmatch.fnmatch(cmd, specifier) for cmd in cmd_names)

            # Exact match - check full command OR any individual command name
            if full_command == specifier:
                return True
            return specifier in cmd_names

        # Special handling for Read/Write/Edit - directory-based permissions
        # Like Claude CLI: permission granted for a file extends to its directory
        if tool_name in ("Read", "Write", "Edit"):
            file_path = tool_input.get("file_path", "")
            if not file_path:
                return False

            # Handle glob patterns (e.g., /src/**/*.py)
            if any(c in specifier for c in ['*', '?', '[']):
                return fnmatch.fnmatch(file_path, specifier)

            # Handle prefix match with :* suffix
            if specifier.endswith(":*"):
                prefix = specifier[:-2]
                return file_path.startswith(prefix)

            # Directory-based permission: if specifier is a file path,
            # allow access to any file in the same directory
            # e.g., pattern "/src/foo.py" allows "/src/bar.py"
            specifier_dir = os.path.dirname(specifier.rstrip('/'))
            file_dir = os.path.dirname(file_path)

            # If specifier looks like a directory (ends with /), match files within
            if specifier.endswith('/'):
                return file_path.startswith(specifier)

            # Same directory = allowed
            if specifier_dir and file_dir == specifier_dir:
                return True

            # Exact match still works
            return file_path == specifier

        # Get the value to match against based on tool type
        match_value = None
        if tool_name in ("Glob", "Grep"):
            match_value = tool_input.get("pattern", "")
        elif tool_name == "WebFetch":
            match_value = tool_input.get("url", "")
        elif tool_name == "Skill":
            match_value = tool_input.get("skill", "")
        else:
            # For other tools, try common field names
            match_value = tool_input.get("command") or tool_input.get("path") or tool_input.get("query", "")

        if not match_value:
            return False

        # Handle prefix match with :* suffix (like Claude Code)
        if specifier.endswith(":*"):
            prefix = specifier[:-2]
            return match_value.startswith(prefix)

        # Handle glob/fnmatch patterns
        if any(c in specifier for c in ['*', '?', '[']):
            return fnmatch.fnmatch(match_value, specifier)

        # Exact match
        return match_value == specifier

    def _validate_bash_command(self, command: str) -> tuple[bool, str]:
        """Validate bash command for dangerous patterns.

        Returns: (is_safe, warning_message)
        """
        import re

        # Check for rm -rf with potentially dangerous paths
        rm_pattern = r'\brm\s+(-[rf]{1,2}\s+|-[a-z]*[rf][a-z]*\s+)'
        if re.search(rm_pattern, command):
            # Extract the path being deleted
            # Match: rm -rf <path> or rm -f -r <path>, etc.
            path_match = re.search(rm_pattern + r'([^\s;&|]+)', command)
            if path_match:
                path = path_match.group(2)

                # Dangerous: relative paths that could delete parent dirs
                if '..' in path:
                    return False, f"Dangerous rm command with parent directory reference: {path}"

                # Dangerous: deleting from root or home
                if path.startswith('/') and path.count('/') <= 3:
                    return False, f"Dangerous rm command targeting high-level directory: {path}"

                # Dangerous: wildcards in critical locations
                if '*' in path and path.count('/') <= 4:
                    return False, f"Dangerous rm command with wildcards in shallow path: {path}"

                # Check for deletion of entire project directories
                critical_dirs = ['node', 'src', 'lib', 'app', 'dist', 'build']
                path_parts = path.rstrip('/').split('/')
                if path_parts and path_parts[-1] in critical_dirs and '/' not in path:
                    return False, f"Dangerous: attempting to delete entire '{path_parts[-1]}' directory"

        return True, ""

    async def can_use_tool(self, tool_name: str, tool_input: dict, context=None):
        """Handle permission request - ask Sublime for approval."""
        # Handle AskUserQuestion - show UI and collect answers
        if tool_name == "AskUserQuestion":
            return await self._handle_ask_user_question(tool_input)

        # Handle EnterPlanMode - notify Sublime and auto-allow
        if tool_name == "EnterPlanMode":
            send_notification("plan_mode_enter", {})
            return PermissionResultAllow(updated_input=tool_input)

        # Handle ExitPlanMode - wait for user approval
        if tool_name == "ExitPlanMode":
            return await self._handle_exit_plan_mode(tool_input)

        # Auto-allow built-in submarine MCP tools — except Quick Agent, which only
        # needs quick_done. Full MCP made agents thrash on get_window_summary.
        # Also accept legacy mcp__sublime__* prefixes from older settings.
        if (tool_name.startswith("mcp__submarine__")
                or tool_name.startswith("mcp__sublime__")):
            if getattr(self, "_quick_mode", False):
                short = tool_name.split("__")[-1] if "__" in tool_name else tool_name
                if short != "quick_done" and tool_name not in (
                        "mcp__submarine__quick_done", "mcp__sublime__quick_done"):
                    return PermissionResultDeny(
                        message=(
                            "Quick Agent: only mcp__submarine__quick_done is "
                            "allowed. Use Read/Grep/Glob for files; attached "
                            "context has the focused path — do not call "
                            f"{tool_name}."
                        ))
            return PermissionResultAllow(updated_input=tool_input)

        # Validate Bash commands for dangerous patterns
        if tool_name == "Bash" and "command" in tool_input:
            is_safe, warning = self._validate_bash_command(tool_input["command"])
            if not is_safe:
                with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                    f.write(f"BLOCKED dangerous Bash command: {warning}\n")
                    f.write(f"  Command: {tool_input['command']}\n")
                return PermissionResultDeny(message=f"Blocked dangerous command: {warning}")

        # Check auto-allowed tools from settings
        settings = load_project_settings(self.cwd)
        auto_allowed = settings.get("autoAllowedMcpTools", [])

        # Check if tool matches any auto-allow pattern (supports fine-grained patterns)
        for pattern in auto_allowed:
            if self._match_permission_pattern(tool_name, tool_input, pattern):
                with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                    f.write(f"can_use_tool: auto-allowed {tool_name} (matched pattern: {pattern})\n")
                return PermissionResultAllow(updated_input=tool_input)

        self.permission_id += 1
        pid = self.permission_id

        with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
            f.write(f"can_use_tool: tool={tool_name}, pid={pid}, input={str(tool_input)[:100]}\n")

        # Create a future to wait for the response
        future = asyncio.get_event_loop().create_future()
        self.pending_permissions[pid] = future

        # Send permission request to Sublime
        send_notification("permission_request", {
            "id": pid,
            "tool": tool_name,
            "input": tool_input,
        })

        # Wait for response from Sublime
        try:
            allowed = await asyncio.wait_for(future, timeout=3600)  # 1 hour timeout
            with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                f.write(f"can_use_tool returning: pid={pid}, allowed={allowed}\n")
            if allowed:
                return PermissionResultAllow(updated_input=tool_input)
            else:
                return PermissionResultDeny(message="User denied permission")
        except asyncio.TimeoutError:
            return PermissionResultDeny(message="Permission request timed out")
        finally:
            self.pending_permissions.pop(pid, None)

    async def handle_permission_response(self, id: int, params: dict) -> None:
        """Handle permission response from Sublime."""
        pid = params.get("id")
        allow = params.get("allow", False)

        with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
            f.write(f"permission_response: pid={pid}, allow={allow}\n")

        if pid in self.pending_permissions:
            future = self.pending_permissions[pid]
            future.set_result(allow)
        else:
            with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                f.write(f"  -> WARNING: pid {pid} not found in pending!\n")

        send_result(id, {"status": "ok"})

    async def _handle_ask_user_question(self, tool_input: dict):
        """Handle AskUserQuestion tool - show UI and collect answers."""
        questions = tool_input.get("questions", [])
        if not questions:
            return PermissionResultAllow(updated_input=tool_input)

        self.permission_id += 1
        qid = self.permission_id

        with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
            f.write(f"AskUserQuestion: qid={qid}, questions={len(questions)}\n")

        future = asyncio.get_event_loop().create_future()
        self.pending_questions[qid] = future

        send_notification("question_request", {
            "id": qid,
            "questions": questions,
        })

        try:
            answers = await future
            with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                f.write(f"AskUserQuestion response: qid={qid}, answers={answers}\n")

            if answers is None:
                return PermissionResultDeny(message="User cancelled")

            updated_input = {"questions": questions, "answers": answers}
            return PermissionResultAllow(updated_input=updated_input)
        finally:
            self.pending_questions.pop(qid, None)

    async def handle_question_response(self, id: int, params: dict) -> None:
        """Handle question response from Sublime."""
        qid = params.get("id")
        answers = params.get("answers")

        with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
            f.write(f"question_response: qid={qid}, answers={answers}\n")

        if qid in self.pending_questions:
            self.pending_questions[qid].set_result(answers)
        else:
            with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                f.write(f"  -> WARNING: qid {qid} not found!\n")

        send_result(id, {"status": "ok"})

    async def _handle_exit_plan_mode(self, tool_input: dict):
        """Handle ExitPlanMode — shared plan approval UI (base.request_plan_approval).

        On approve, re-send tool_input with the plan file content as of the
        user's response (Claude Code: user edits to the plan are applied).
        """
        from base import request_plan_approval

        tool_input = dict(tool_input or {})
        with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
            f.write(f"ExitPlanMode: input={str(tool_input)[:200]}\n")

        result = await request_plan_approval(self, tool_input, timeout=3600)

        with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
            f.write(f"ExitPlanMode response: {result!r}\n")

        if not result:
            return PermissionResultDeny(message="User wants to continue planning")

        approved = result.get("approved")
        plan_text = result.get("plan") or ""
        if plan_text:
            # Claude ExitPlanMode input uses `plan` for the document body.
            tool_input = dict(tool_input)
            tool_input["plan"] = plan_text
            if result.get("planFilePath"):
                tool_input.setdefault("planFilePath", result["planFilePath"])

        if approved is True:
            return PermissionResultAllow(updated_input=tool_input)
        if approved is False:
            return PermissionResultDeny(message="Plan rejected by user")
        return PermissionResultDeny(message="User wants to continue planning")

    async def handle_plan_response(self, id: int, params: dict) -> None:
        """Handle plan approval response from Sublime."""
        from base import resolve_plan_response

        payload = resolve_plan_response(self, params)
        with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
            f.write(
                f"plan_response: pid={params.get('id')}, "
                f"approved={payload.get('approved') if isinstance(payload, dict) else payload}, "
                f"plan_chars={len((payload or {}).get('plan') or '') if isinstance(payload, dict) else 0}\n"
            )
        send_result(id, {"status": "ok"})

    def _build_content_with_images(self, prompt: str, images: list) -> list:
        """Build Claude content array with text and images.

        Args:
            prompt: Text prompt
            images: List of {"mime_type": str, "data": str} dicts

        Returns:
            Content array for Claude API
        """
        content = []
        # Add images first (Claude prefers images before text)
        for img in images:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img["mime_type"],
                    "data": img["data"]
                }
            })
        # Add text
        content.append({"type": "text", "text": prompt})
        return content

    # ── stream ownership ───────────────────────────────────────────────────
    def _start_reader(self) -> None:
        """One reader per connection. It never stops between queries: the
        CLI's own turns (background-task follow-ups) arrive while we are idle,
        and a reader that only ran during query() dropped them on the floor
        — then the host re-told the model about the same job."""
        self._stop_reader()
        self._host_query = None
        self._injected = False
        self._echo_seen = False
        self._stray_result_pending = False
        self._bg_summaries = []
        self._running_tasks = set()
        self._reader_task = asyncio.create_task(self._read_stream())

    def _stop_reader(self) -> None:
        task = self._reader_task
        self._reader_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _read_stream(self) -> None:
        ended = None  # type: Exception | None
        try:
            async for message in self.client.receive_messages():
                try:
                    await self._route(message)
                except Exception as e:
                    _logger.error(f"route {type(message).__name__}: {e}")
            ended = RuntimeError("Command failed: the SDK stream ended")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _logger.error(f"stream reader ended: {type(e).__name__}: {e}")
            ended = e
        # A query waiting on this stream must not hang forever.
        hq = self._host_query
        if hq is not None and not hq["done"].done():
            hq["done"].set_exception(ended)

    @staticmethod
    def _origin_kind(message: Any) -> str | None:
        origin = getattr(message, "origin", None)
        if isinstance(origin, dict):
            kind = origin.get("kind")
            return str(kind) if kind else None
        return None

    @staticmethod
    def _is_prompt_echo(message: Any) -> bool:
        content = message.content
        if isinstance(content, str):
            return True
        if isinstance(content, list):
            return not any(isinstance(b, ToolResultBlock) for b in content)
        return False

    def _begin_injected(self, kind: str) -> None:
        self._injected = True
        self.interrupted = False
        summaries = [x for x in self._bg_summaries if x][-5:]
        self._bg_summaries = []
        send_notification("injected_turn", {
            "origin": kind,
            "summaries": summaries,
        })

    def _finish_host_query(self, status: str) -> None:
        hq = self._host_query
        if hq is None:
            return
        self._host_query = None
        send_result(hq["id"], {"status": status})
        if not hq["done"].done():
            hq["done"].set_result(status)

    async def _route(self, message: Any) -> None:
        """Attribute one stream message to the host's query or to a turn the
        CLI started by itself, and forward it accordingly."""
        if isinstance(message, SystemMessage):
            data = message.data or {}
            if message.subtype == "task_notification":
                summary = data.get("summary")
                if summary:
                    self._bg_summaries.append(str(summary))
                self._running_tasks.discard(data.get("task_id", ""))
            elif message.subtype == "task_started":
                if data.get("is_backgrounded") is not False:
                    self._running_tasks.add(data.get("task_id", ""))
            elif message.subtype == "task_updated":
                if (data.get("patch") or {}).get("status", "") in _TASK_TERMINAL:
                    self._running_tasks.discard(data.get("task_id", ""))
            elif message.subtype == "background_tasks_changed":
                tasks = data.get("tasks")
                if isinstance(tasks, list):
                    self._running_tasks = set(
                        str(t.get("task_id")) for t in tasks
                        if isinstance(t, dict) and t.get("task_id"))
            await self.emit_message(message)
            return

        # A subagent's own stream (Agent/Task, foreground or background) —
        # its prompt, text, thinking, tool calls and results. The parent shows
        # the Agent row and gets the report as that tool's result; the rest
        # was a wall of someone else's work in the sheet. Its prompt would
        # also read as our echo below, and its usage as our context.
        if getattr(message, "parent_tool_use_id", None):
            return

        kind = self._origin_kind(message)
        hq = self._host_query

        if isinstance(message, UserMessage) and self._is_prompt_echo(message):
            # A user-turn echo (--replay-user-messages), not a tool result.
            if kind in (None, "human"):
                self._echo_seen = True
                self._bg_summaries = []
                if hq is not None:
                    if self._injected:
                        # Delivered into the CLI's own running turn
                        # (queue-operation "absorbed_mid_turn"): no result of
                        # its own will ever come. That turn's closes ours.
                        hq["absorbed"] = True
                    else:
                        hq["owned"] = True
                    # Our prompt reached the model: whatever the old turn
                    # still owed has come and gone.
                    self._stray_result_pending = False
                return
            if self._injected or (hq is not None and hq["owned"]):
                # A completion absorbed into the running turn: the CLI
                # attaches the notification as a user message mid-turn
                # (queued_command, "absorbed_mid_turn"). Same turn, not a
                # new one — the model already has it, the row is flipped by
                # the task_notification that preceded it.
                return
            if hq is not None and not self._echo_seen:
                return
            self._begin_injected(kind)
            return

        if isinstance(message, ResultMessage):
            if self._injected and kind != "human":
                self._injected = False
                if hq is not None and hq.get("absorbed"):
                    # The host's prompt rode inside this turn; to the host
                    # the whole thing was its query. Close it as such.
                    await self.emit_message(message)
                    self._finish_host_query(
                        "interrupted" if self.interrupted else "complete")
                    return
                await self.emit_message(message, origin=kind or "task-notification")
                self.interrupted = False
                return
            if hq is not None:
                if self._stray_result_pending and not hq["owned"]:
                    self._stray_result_pending = False
                    _logger.info("late result of a closed query dropped")
                    return
                if kind not in (None, "human") and self._echo_seen and not hq["owned"]:
                    # The CLI ran a turn of its own ahead of ours before our
                    # echo arrived; its content already went out as ours.
                    # Close it as what it was; our result is still to come.
                    await self.emit_message(message, origin=kind)
                    return
                await self.emit_message(message)
                # Our closer while an "injected" turn was still open: the
                # attribution was wrong (content before our echo). Ours ends
                # here either way; nothing else is owed a closer.
                self._injected = False
                self._finish_host_query(
                    "interrupted" if self.interrupted else "complete")
                return
            self._stray_result_pending = False
            _logger.info(f"stray result dropped (origin={kind})")
            return

        # Turn content: StreamEvent, AssistantMessage, UserMessage(tool results).
        if self._injected:
            if self.interrupted:
                return
            await self.emit_message(message)
            return
        if hq is not None and (hq["owned"] or not self._echo_seen):
            if self.interrupted:
                return
            await self.emit_message(message)
            return
        self._begin_injected("task-notification")
        await self.emit_message(message)

    async def query(self, id: int, params: dict) -> None:
        """Send a query and stream responses."""
        if not self.client:
            send_error(id, -32002, "Not initialized")
            return

        prompt = params.get("prompt", "")
        images = params.get("images", [])

        # A query still open (its closer never came) must not block this one.
        if self._host_query is not None:
            self._finish_host_query("interrupted")

        self.interrupted = False  # Reset at start of query
        self._got_first_delta = False
        self.query_id = id  # Store for inject_message to know query is active
        self._bg_summaries = []
        done = asyncio.get_event_loop().create_future()
        self._host_query = {"id": id, "done": done, "owned": False,
                            "absorbed": False}

        if images:
            content = self._build_content_with_images(prompt, images)
        else:
            content = prompt

        async def message_stream():
            # `origin: human` marks the turn as ours on its echo and on its
            # ResultMessage (the CLI's own turns say `task-notification`).
            yield {
                "type": "user",
                "message": {"role": "user", "content": content},
                "parent_tool_use_id": None,
                "origin": {"kind": "human"},
            }

        try:
            await self.client.query(message_stream())
            await done
        except asyncio.CancelledError:
            if self._host_query is not None and self._host_query["id"] == id:
                self._finish_host_query("interrupted")
        except Exception as e:
            error_msg = str(e)
            # Enrich with exception type + HTTP status code (APIStatusError carries
            # these on attributes) so provider 401/429/500 errors are diagnosable
            # from the log/console instead of just a stringified message.
            exc_type = type(e).__name__
            status_code = getattr(e, "status_code", None)
            request_id = getattr(e, "request_id", None) or getattr(getattr(e, "response", None), "headers", None)
            diag = f"{exc_type}: {error_msg}"
            if status_code is not None:
                diag += f" [HTTP {status_code}]"
            if request_id is not None:
                # request_id may be a Headers Mapping — best-effort string.
                try:
                    diag += f" req_id={str(request_id)[:120]}"
                except Exception:
                    pass
            with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                f.write(f"query error: {diag}\n")
                import traceback as _tb
                f.write(_tb.format_exc() + "\n")
            error_msg = diag
            if self._host_query is not None and self._host_query["id"] == id:
                self._host_query = None
            # Check for session-related errors
            is_session_error = (
                "No conversation found" in error_msg or
                "Command failed" in error_msg or
                "exit code" in error_msg
            )
            if is_session_error:
                send_error(id, -32003, f"Session error: {error_msg}. Try restarting the session.")
            else:
                send_error(id, -32000, f"Query failed: {error_msg}")
        finally:
            self.query_id = None

    async def emit_message(self, message: Any, origin: str | None = None) -> None:
        """Emit a message notification. `origin` tags a ResultMessage that
        closes a turn the CLI started by itself."""
        if isinstance(message, StreamEvent):
            event = message.event
            etype = event.get("type")
            if etype == "content_block_start":
                self._got_first_delta = False
            elif etype == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta["text"]
                    if not self._got_first_delta:
                        text = text.lstrip('\n')
                        self._got_first_delta = True
                    if text:
                        send_notification("message", {
                            "type": "text_delta",
                            "text": text,
                        })
            return

        if isinstance(message, AssistantMessage):
            if message.usage:
                send_notification("message", {
                    "type": "turn_usage",
                    "usage": message.usage,
                })
            for block in message.content:
                if isinstance(block, TextBlock):
                    # Text was already streamed via StreamEvent text_deltas — skip
                    pass
                elif isinstance(block, ToolUseBlock):
                    if block.name in ("CronCreate", "CronDelete"):
                        self._handle_cron_tooluse(block)
                    elif block.name == "ScheduleWakeup":
                        self._handle_schedule_wakeup(block)
                    tool_input = block.input or {}
                    # Workflow runs in the background (returns a task id immediately,
                    # finishes via a later task-notification) — treat it like an
                    # explicit run_in_background tool so it gets the persistent ⚙
                    # rendering + completion wake instead of looking foreground.
                    is_bg = bool(tool_input.get("run_in_background") if isinstance(tool_input, dict) else False) \
                        or block.name == "Workflow"
                    if is_bg:
                        self._bg_tool_use_ids.add(block.id)
                    send_notification("message", {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": tool_input,
                        "background": is_bg,
                    })
                elif isinstance(block, ToolResultBlock):
                    if block.tool_use_id in self._pending_cron:
                        self._finalize_cron_from_result(block)
                    send_notification("message", {
                        "type": "tool_result",
                        "tool_use_id": block.tool_use_id,
                        "content": block.content,
                        "is_error": block.is_error,
                    })
                elif isinstance(block, ThinkingBlock):
                    send_notification("message", {
                        "type": "thinking",
                        "thinking": block.thinking,
                    })
        elif isinstance(message, UserMessage):
            # UserMessage contains tool results
            content = message.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        # Tool results arrive here (UserMessage), NOT via the
                        # AssistantMessage path — finalize a pending cron now.
                        if block.tool_use_id in self._pending_cron:
                            self._finalize_cron_from_result(block)
                        send_notification("message", {
                            "type": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": block.content if hasattr(block, 'content') else None,
                            "is_error": block.is_error,
                        })
        elif isinstance(message, ResultMessage):
            result_params = {
                "type": "result",
                "session_id": message.session_id,
                "duration_ms": message.duration_ms,
                "is_error": message.is_error,
                "num_turns": message.num_turns,
                "total_cost_usd": message.total_cost_usd,
                # Distinguish a manual interrupt from a real error: the SDK still
                # emits a ResultMessage (is_error=True) on interrupt, so without
                # this the plugin's _on_msg_result would print a "turn failed"
                # hint for a user-initiated interrupt.
                "status": "interrupted" if self.interrupted else "complete",
            }
            if origin:
                result_params["origin"] = origin
            if message.usage:
                result_params["usage"] = message.usage
            if message.stop_reason:
                result_params["stop_reason"] = message.stop_reason
            send_notification("message", result_params)
        elif isinstance(message, SystemMessage):
            if message.subtype not in ("thinking_tokens", "status"):
                with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                    f.write(f"SystemMessage: subtype={message.subtype}, data={message.data}\n")
            send_notification("message", {
                "type": "system",
                "subtype": message.subtype,
                "data": message.data,
            })

    # Levels the CLI's `effortLevel` setting accepts. `max` is a start flag
    # only (--effort max): through apply_flag_settings it is silently dropped
    # and the flag layer's value is cleared, falling back to userSettings.
    LIVE_EFFORT = ("low", "medium", "high", "xhigh")

    async def stop_task(self, id: int, params: dict) -> None:
        """Stop one background task (a backgrounded Bash, an Agent, a
        Workflow) and leave the turn alone. The CLI confirms with a
        `task_notification` of status `stopped`, which closes the ⚙ row."""
        task_id = str(params.get("task_id") or "").strip()
        if not task_id:
            send_error(id, -32602, "task_id is required")
            return
        stop = getattr(self.client, "stop_task", None) if self.client else None
        if not callable(stop):
            send_error(id, -32000, "this SDK cannot stop a single task")
            return
        try:
            await stop(task_id)
        except Exception as e:
            send_error(id, -32000, f"stop_task failed: {e}")
            return
        send_result(id, {"ok": True, "task_id": task_id})

    async def set_effort(self, id: int, params: dict) -> None:
        """Change effort for the running session, no restart.

        Uses the CLI's `apply_flag_settings` control request — the same
        session-scoped layer `--settings` feeds — and reads the result back
        from `get_settings` (`applied.effort` is what the next API request
        uses). `live: false` means the caller has to restart for it.
        """
        effort = str(params.get("effort") or "").strip()
        if effort not in self.LIVE_EFFORT:
            send_result(id, {"ok": False, "live": False,
                             "reason": "%s needs a restart" % (effort or "that level")})
            return
        query = getattr(self.client, "_query", None) if self.client else None
        control = getattr(query, "_send_control_request", None)
        if not callable(control):
            send_result(id, {"ok": False, "live": False,
                             "reason": "this SDK cannot change effort live"})
            return
        try:
            await control({"subtype": "apply_flag_settings",
                           "settings": {"effortLevel": effort}})
            got = await control({"subtype": "get_settings"})
            applied = str(((got or {}).get("applied") or {}).get("effort") or "")
        except Exception as e:
            send_result(id, {"ok": False, "live": False, "reason": str(e)})
            return
        if applied and applied != effort:
            send_result(id, {"ok": False, "live": False, "applied": applied,
                             "reason": "the CLI kept %s" % applied})
            return
        # A later /clear rebuilds the client from these options.
        if self.options is not None:
            try:
                self.options.effort = effort
            except Exception:
                pass
        send_result(id, {"ok": True, "live": True, "applied": applied or effort})

    async def interrupt(self, id: int) -> None:
        """Interrupt the running turn — ours or one the CLI started itself."""
        hq = self._host_query
        active = hq is not None or self._injected
        with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
            f.write(f"interrupt: called, host_query={hq is not None} injected={self._injected}\n")
        if active:
            self.interrupted = True  # The reader skips turn content until the closer
            with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                f.write(f"interrupt: sending to SDK\n")
            await self.client.interrupt()
            # Cancel any pending permission requests
            for pid, future in list(self.pending_permissions.items()):
                if not future.done():
                    future.set_result(False)  # Deny pending permissions
            self.pending_permissions.clear()
            # Also release pending question / plan-approval futures. Their tool
            # handlers (AskUserQuestion, ExitPlanMode) are awaiting these plain
            # asyncio futures, which the SDK interrupt can't unblock — so without
            # this the task sits blocked and interrupt stalls the full 5s drain
            # timeout before cancelling (the "halt during a question").
            for qid, future in list(self.pending_questions.items()):
                if not future.done():
                    future.set_result(None)  # → handler returns "User cancelled"
            self.pending_questions.clear()
            for pid, future in list(self.pending_plan_approvals.items()):
                if not future.done():
                    future.set_result(False)
            self.pending_plan_approvals.clear()
            # Let the turn drain to its ResultMessage (it should be quick).
            if hq is not None:
                with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                    f.write(f"interrupt: waiting for the turn to drain\n")
                try:
                    await asyncio.wait_for(asyncio.shield(hq["done"]), timeout=5.0)
                except asyncio.TimeoutError:
                    with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                        f.write(f"interrupt: drain timeout, closing the query\n")
                    if self._host_query is hq:
                        self._stray_result_pending = True
                        self._finish_host_query("interrupted")
                except Exception as e:
                    with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                        f.write(f"interrupt: drain error: {e}\n")
        send_result(id, {"status": "interrupted"})

    async def clear(self, id: int, params: dict) -> None:
        """Harness /clear: new SDK session, same process and options."""
        if self._host_query is not None or self._injected:
            self.interrupted = True
            try:
                if self.client:
                    await self.client.interrupt()
            except Exception as e:
                _logger.info(f"clear: interrupt before new session: {e}")
            self._finish_host_query("interrupted")
            self._injected = False
        self._stop_reader()
        old = getattr(self, "_session_id", None)
        if self.client:
            try:
                await self.client.disconnect()
            except Exception as e:
                _logger.info(f"clear: disconnect: {e}")
            self.client = None
        session_id = str(uuid.uuid4())
        self._session_id = session_id
        if self.options is None:
            send_error(id, -32000, "session not initialized")
            return
        opts = dict(getattr(self.options, "__dict__", {}) or {})
        opts["resume"] = None
        extra = dict(opts.get("extra_args") or {})
        extra.pop("resume-session-at", None)
        extra["session-id"] = session_id
        opts["extra_args"] = extra
        opts["fork_session"] = False
        fields = getattr(ClaudeAgentOptions, "__dataclass_fields__", None)
        if fields:
            opts = {k: v for k, v in opts.items() if k in fields}
        self.options = ClaudeAgentOptions(**opts)
        self.client = ClaudeSDKClient(options=self.options)
        await self.client.connect()
        self._start_reader()
        _logger.info(f"clear: {old} → {session_id}")
        send_result(id, {
            "ok": True,
            "session_id": session_id,
            "sessionId": session_id,
        })

    async def cancel_pending(self, id: int) -> None:
        """Cancel all pending permission/question requests."""
        count = 0
        for pid, future in list(self.pending_permissions.items()):
            if not future.done():
                future.set_result(False)  # Deny
                count += 1
        self.pending_permissions.clear()

        for qid, future in list(self.pending_questions.items()):
            if not future.done():
                future.set_result(None)  # Cancel
                count += 1
        self.pending_questions.clear()

        with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
            f.write(f"cancel_pending: cancelled {count} requests\n")
        send_result(id, {"status": "ok", "cancelled": count})

    async def inject_message(self, id: int, params: dict) -> None:
        """Deliver a user message into the running turn — the host's query or
        a turn the CLI started itself (the CLI attaches it mid-turn). With no
        turn running there is nothing to inject into: the host keeps the
        message queued and sends it as a query when its turn closes."""
        message = params.get("message", "")
        if not message:
            send_error(id, -32602, "Missing message parameter")
            return

        with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
            f.write(f"inject_message: {message[:60]}...\n")

        if self._host_query is None and not self._injected:
            with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                f.write(f"  no turn running, not injected\n")
            send_result(id, {"status": "idle"})
            return

        try:
            await self.client.query(message)
            send_result(id, {"status": "ok"})
        except Exception as e:
            with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                f.write(f"  inject failed: {e}\n")
            send_result(id, {"status": "idle", "error": str(e)})

    # ── cron (bridge-owned; agent's CronCreate is inert under the SDK) ───────
    def _handle_cron_tooluse(self, block) -> None:
        inp = block.input or {}
        if block.name == "CronCreate":
            expr = (inp.get("cron") or "").strip()
            prompt = inp.get("prompt") or ""
            if expr and prompt:
                # Finalize once the tool_result lands so we can key on the CLI's
                # own job id (what the agent will pass to CronDelete).
                self._pending_cron[block.id] = {
                    "cron": expr, "prompt": prompt,
                    "recurring": bool(inp.get("recurring", True)),
                }
        elif block.name == "CronDelete":
            jid = inp.get("id")
            if jid and self._crons.pop(jid, None) is not None:
                self._save_loop_state()
                with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                    f.write(f"[cron] deleted {jid}\n")

    def _finalize_cron_from_result(self, block) -> None:
        pend = self._pending_cron.pop(block.tool_use_id, None)
        if not pend:
            return
        text = block.content if isinstance(block.content, str) else str(block.content)
        m = re.search(r"\bjob\s+([0-9a-fA-F]{6,})", text)
        jid = m.group(1) if m else uuid.uuid4().hex[:8]
        nxt = _cron_next_fire(pend["cron"], time.time())
        if nxt is None:
            with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                f.write(f"[cron] bad expr, not scheduled: {pend['cron']!r}\n")
            return
        pend["next_fire"] = nxt
        self._crons[jid] = pend
        self._ensure_cron_monitor()
        self._save_loop_state()
        send_notification("loop_scheduled", {"fire_at": nxt})  # plugin wakeup hint
        with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
            f.write(f"[cron] registered {jid} cron={pend['cron']!r} recurring={pend['recurring']} "
                    f"next={datetime.datetime.fromtimestamp(nxt).isoformat()}\n")

    def _ensure_cron_monitor(self) -> None:
        if self._cron_monitor_task is None or self._cron_monitor_task.done():
            self._cron_monitor_task = asyncio.create_task(self._cron_monitor())

    async def _cron_monitor(self) -> None:
        """Tick while jobs exist; on a due job, wake the agent via the same
        notification_wake the plugin already turns into a real query turn."""
        try:
            while self.running and self._crons:
                await asyncio.sleep(15)
                now = time.time()
                for jid, job in list(self._crons.items()):
                    if now < job["next_fire"]:
                        continue
                    with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
                        f.write(f"[cron] fire {jid} -> wake: {job['prompt'][:60]!r}\n")
                    send_notification("notification_wake", {
                        "wake_prompt": job["prompt"],
                        "display_message": "⏰ " + job["prompt"].split("\n", 1)[0][:60],
                    })
                    if job["recurring"]:
                        nxt = _cron_next_fire(job["cron"], now)
                        if nxt:
                            job["next_fire"] = nxt
                            send_notification("loop_scheduled", {"fire_at": nxt})  # next-wakeup hint
                        else:
                            self._crons.pop(jid, None)
                    else:
                        self._crons.pop(jid, None)
                    self._save_loop_state()
        finally:
            self._cron_monitor_task = None

    def _handle_schedule_wakeup(self, block) -> None:
        """ScheduleWakeup is a harness /loop-dynamic tool — inert under the SDK
        (no idle loop re-invokes it). Shadow it with a one-shot timer that fires
        the same notification_wake the cron path uses, so self-paced loops continue."""
        inp = block.input or {}
        prompt = inp.get("prompt") or ""
        try:
            delay = float(inp.get("delaySeconds") or 0)
        except (TypeError, ValueError):
            delay = 0
        if not prompt or delay <= 0:
            return
        delay = max(60.0, min(delay, 3600.0))  # runtime clamp, matches the tool spec
        # Only one pending wake may be live. The agent re-arms each turn and may
        # call ScheduleWakeup several times in quick succession; without this,
        # the timers pile up — each fires, re-invokes, re-arms → a wake storm.
        for t in list(self._wake_tasks):
            t.cancel()
        self._wake_tasks.clear()
        task = asyncio.create_task(self._fire_wake_after(delay, prompt))
        self._wake_tasks.add(task)
        task.add_done_callback(self._wake_tasks.discard)
        fire_at = time.time() + delay
        self._pending_wake = {"prompt": prompt, "fire_at": fire_at}
        self._save_loop_state()
        with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
            f.write(f"[wake] scheduled in {delay:.0f}s -> {prompt[:60]!r}\n")
        # Tell the plugin the exact fire time so it can show the wakeup hint.
        send_notification("loop_scheduled", {"fire_at": fire_at})

    async def _fire_wake_after(self, delay: float, prompt: str) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if not self.running:
            return
        with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
            f.write(f"[wake] fire -> {prompt[:60]!r}\n")
        self._pending_wake = None
        self._save_loop_state()
        send_notification("loop_scheduled", {"fire_at": None})  # one-shot done; agent re-arms
        send_notification("notification_wake", {
            "wake_prompt": prompt,
            "display_message": "⏰ " + prompt.split("\n", 1)[0][:60],
        })

    async def cancel_loop(self, id: int, params: dict = None) -> None:
        """User cancelled scheduled wakeup / cron from the plugin banner."""
        n_cron = len(self._crons)
        n_wake = len(self._wake_tasks)
        self._crons.clear()
        for t in list(self._wake_tasks):
            try:
                t.cancel()
            except Exception:
                pass
        self._wake_tasks.clear()
        self._pending_wake = None
        self._pending_cron.clear()
        try:
            self._save_loop_state()
        except Exception:
            pass
        try:
            self._clear_loop_state()
        except Exception:
            pass
        send_notification("loop_scheduled", {"fire_at": None})
        with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
            f.write(f"[loop] cancel_loop crons={n_cron} wakes={n_wake}\n")
        send_result(id, {"ok": True, "crons": n_cron, "wakes": n_wake})

    # ── loop persistence: survive bridge/Sublime restarts ─────────────────────
    def _loop_state_path(self) -> str:
        d = os.path.expanduser("~/.submarine/loops")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{self._session_id}.json")

    def _save_loop_state(self) -> None:
        """Persist pending wake + crons so a restarted bridge can re-arm them."""
        try:
            crons = {jid: {k: j[k] for k in ("cron", "prompt", "recurring", "next_fire")}
                     for jid, j in self._crons.items()}
            if not self._pending_wake and not crons:
                self._clear_loop_state()
                return
            with open(self._loop_state_path(), "w") as f:
                json.dump({"wake": self._pending_wake, "crons": crons}, f)
        except Exception as e:
            _logger.warning(f"loop persist failed: {e}")

    def _clear_loop_state(self) -> None:
        try:
            p = self._loop_state_path()
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    def _restore_loop_state(self) -> None:
        """On startup, re-arm any persisted wake/crons (survives restarts)."""
        try:
            p = self._loop_state_path()
            if not os.path.exists(p):
                return
            with open(p) as f:
                state = json.load(f)
        except Exception:
            return
        now = time.time()
        for jid, j in (state.get("crons") or {}).items():
            nxt = _cron_next_fire(j["cron"], now)
            if nxt is None:
                continue
            self._crons[jid] = {"cron": j["cron"], "prompt": j["prompt"],
                                "recurring": j.get("recurring", True), "next_fire": nxt}
        if self._crons:
            self._ensure_cron_monitor()
        w = state.get("wake")
        if w and w.get("prompt"):
            delay = max(2.0, (w.get("fire_at") or now) - now)  # missed during downtime → fire shortly
            self._pending_wake = {"prompt": w["prompt"], "fire_at": now + delay}
            task = asyncio.create_task(self._fire_wake_after(delay, w["prompt"]))
            self._wake_tasks.add(task)
            task.add_done_callback(self._wake_tasks.discard)
        fires = [j["next_fire"] for j in self._crons.values()]
        if self._pending_wake:
            fires.append(self._pending_wake["fire_at"])
        if fires:
            send_notification("loop_scheduled", {"fire_at": min(fires)})
        with open(os.path.join(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "submarine_bridge.log"), "a") as f:
            f.write(f"[loop] restored {len(self._crons)} cron(s), wake={bool(self._pending_wake)}\n")

    async def poll_bg_tasks(self, rpc_id: int) -> None:
        """Host reconcile poll: which background tasks the CLI still reports
        running (`background_tasks_changed`). The stream itself is read
        continuously by _read_stream, so there is nothing to drain here."""
        running = sorted(t for t in self._running_tasks if t)
        send_result(rpc_id, {"pending": len(running), "checked": 0,
                             "running": running})

    async def get_history(self, id: int) -> None:
        """Get conversation history from the SDK."""
        if not self.client:
            send_error(id, -32002, "Client not initialized")
            return

        try:
            # Try to access SDK's internal conversation state
            # The SDK stores messages internally for context
            messages = []

            # Check if client has a messages/history attribute
            if hasattr(self.client, '_messages'):
                messages = serialize(self.client._messages)
            elif hasattr(self.client, 'messages'):
                messages = serialize(self.client.messages)
            elif hasattr(self.client, 'conversation'):
                messages = serialize(self.client.conversation)
            else:
                # Fallback: return what we know
                send_result(id, {
                    "messages": [],
                    "note": "SDK conversation history not accessible via standard API"
                })
                return

            send_result(id, {"messages": messages})
        except Exception as e:
            send_error(id, -32000, f"Failed to get history: {str(e)}")

    async def shutdown(self, id: int) -> None:
        """Shutdown the bridge."""
        self._stop_reader()
        if self.client:
            await self.client.disconnect()

        send_result(id, {"status": "shutdown"})
        self.running = False

    async def run(self) -> None:
        """Main loop - read JSON-RPC from stdin."""
        # Immediate startup log
        sys.stderr.write("=== BRIDGE STARTING (thread-based stdin reader) ===\n")
        sys.stderr.flush()

        import threading
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _read_stdin():
            try:
                while True:
                    line = sys.stdin.buffer.readline()
                    loop.call_soon_threadsafe(queue.put_nowait, line)
                    if not line:
                        break
            except Exception:
                loop.call_soon_threadsafe(queue.put_nowait, b'')

        threading.Thread(target=_read_stdin, daemon=True).start()

        sys.stderr.write("=== Bridge stdin reader started (thread-based) ===\n")
        sys.stderr.flush()

        while self.running:
            try:
                line = await queue.get()
                if not line:
                    break
                req = json.loads(line.decode())
                # Don't await - handle requests concurrently so permission responses
                # can be processed while a query is running
                asyncio.create_task(self.handle_request(req))
            except json.JSONDecodeError as e:
                send_error(None, -32700, f"Parse error: {e}")
                sys.stderr.write(f"Fatal error in message reader: Failed to decode JSON: {e}\n")
                sys.stderr.flush()
            except Exception as e:
                send_error(None, -32000, f"Internal error: {e}")
                sys.stderr.write(f"!!! EXCEPTION TYPE: {type(e).__module__}.{type(e).__name__} !!!\n")
                sys.stderr.write(f"!!! EXCEPTION MESSAGE: {e} !!!\n")
                sys.stderr.write(f"Fatal error in message reader: {e}\n")
                sys.stderr.flush()
                import traceback
                traceback.print_exc(file=sys.stderr)


async def main():
    bridge = Bridge()
    await bridge.run()


if __name__ == "__main__":
    asyncio.run(main())
