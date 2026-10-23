"""opencode ACP bridge — thin adapter over AcpBridge.

Spawns `opencode acp` (or `$OPENCODE_BIN acp`) over stdio.

opencode does not report the ACP `modes` / `models` blocks on session/new;
it returns a `configOptions` array instead (id=model, id=mode). We translate
those into the shapes AcpBridge expects so mode/model switching, the plan-mode
banner and the picker catalog all work unchanged. The setters themselves
(`session/set_mode`, `session/set_model`) are supported natively.
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import Dict, List, Optional

_BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_BRIDGE_DIR)
sys.path.insert(0, _BRIDGE_DIR)
sys.path.insert(0, _PLUGIN_DIR)

from acp_base import AcpBridge, run_bridge  # noqa: E402
from acp.tools import KIND_TO_NAME  # noqa: E402

# MCP servers the bridge injects on session/new (see _collect_mcp_servers).
# opencode namespaces their tools `{server}_{tool}` with a single underscore.
_INJECTED_MCP_SERVERS = ("submarine", "irr")


def resolve_opencode_bin() -> str:
    # Mirrors backend/opencode.py — the bridge runs without the plugin package.
    return (
        (os.environ.get("OPENCODE_BIN") or "").strip()
        or shutil.which("opencode")
        or os.path.expanduser("~/.local/bin/opencode")
    )


class OpencodeBridge(AcpBridge):
    BACKEND_NAME = "opencode"
    DEFAULT_MODEL = ""  # let opencode pick its configured default
    LOG_PATH = os.path.join(
        os.environ.get("TMPDIR")
        or os.environ.get("TEMP")
        or os.environ.get("TMP")
        or "/tmp",
        "opencode_bridge.log",
    )

    # opencode session modes: build | plan (agents may add more; apply_mode
    # clamps to whatever session/new advertised).
    PERM_TO_MODE = {
        "default": "build",
        "acceptEdits": "build",
        "auto": "build",
        "bypassPermissions": "build",
        "dontAsk": "build",
        "plan": "plan",
    }
    MODE_TO_PERM = {
        "build": "default",
        "plan": "plan",
    }

    TOOL_TO_CANONICAL = {
        "read": "Read",
        "write": "Write",
        "edit": "Edit",
        "patch": "Edit",
        "multiedit": "Edit",
        "bash": "Bash",
        "grep": "Grep",
        "glob": "Glob",
        "list": "Glob",
        "ls": "Glob",
        "webfetch": "WebFetch",
        "websearch": "WebSearch",
        "todowrite": "TodoWrite",
        "todoread": "TodoWrite",
        "task": "Task",
        "agent": "Task",
        "skill": "Skill",
        "question": "AskUserQuestion",
        "invalid": "Bash",
    }

    def agent_argv(self) -> List[str]:
        return [resolve_opencode_bin() or "opencode", "acp",
                "--cwd", self.cwd or os.getcwd()]

    def spawn_env(self) -> Optional[Dict[str, str]]:
        env = dict(os.environ)
        # opencode installs to ~/.local/bin by default; keep it findable for
        # child tools spawned by the agent.
        local_bin = os.path.expanduser("~/.local/bin")
        path = env.get("PATH") or ""
        if os.path.isdir(local_bin) and local_bin not in path.split(os.pathsep):
            env["PATH"] = local_bin + os.pathsep + path
        return env

    def usage_from_prompt_result(self, result: dict) -> Optional[dict]:
        """opencode reports tokens on the session/prompt result itself.

        The base hook only looks at `_meta` (Grok's shape); opencode sends
        `usage` top-level and leaves `_meta` empty, so the context meter got
        nothing.
        """
        usage = (result or {}).get("usage") or {}
        keys = ("inputTokens", "outputTokens", "cachedReadTokens",
                "reasoningTokens", "totalTokens")
        if not isinstance(usage, dict) or not any(k in usage for k in keys):
            return super().usage_from_prompt_result(result)

        def _tok(key: str) -> int:
            try:
                return int(usage.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        return {
            "input_tokens": _tok("inputTokens"),
            "output_tokens": _tok("outputTokens"),
            "cache_read_input_tokens": _tok("cachedReadTokens"),
            "reasoning_tokens": _tok("reasoningTokens"),
            "total_tokens": _tok("totalTokens"),
            "model": self.model or None,
        }

    @staticmethod
    def _canonical_mcp_name(name: str) -> str:
        """`submarine_read_view` → `mcp__submarine__read_view` ("" if not MCP).

        The formatters key off Claude's `mcp__{server}__{tool}` convention
        (ui/formatters._mcp_short_name), so opencode's rows would render raw.
        """
        for server in _INJECTED_MCP_SERVERS:
            prefix = server + "_"
            if name.startswith(prefix) and not name.startswith(prefix + "_"):
                bare = name[len(prefix):]
                if bare:
                    return "mcp__%s__%s" % (server, bare)
        return ""

    def _normalize_tool_name(self, upd: dict) -> str:
        """The opening tool_call's name wins; opencode's later titles are prose.

        The opening tool_call carries title="bash", but every follow-up update
        replaces it with the command line ("echo hi", or "ls" — which would
        even map to Glob). The base handler overwrites the call's name with
        whatever this returns, so the name cached on the open call must win
        before any title parsing. The subagent tool likewise opens as
        title="task" kind="think" and then swaps the title for the task
        description; terminal updates drop `kind` entirely.
        """
        call = self._call(self._resolve_tool_id(upd.get("toolCallId")))
        if call is not None and call.name and call.name != "tool":
            return call.name
        raw = upd.get("rawInput") or {}
        if isinstance(raw, dict):
            for key in ("tool", "name", "toolName"):
                mapped = self._map_agent_tool_id(raw.get(key) or "")
                if mapped:
                    return mapped
        title = (upd.get("title") or "").strip()
        if title:
            mapped = (self._canonical_mcp_name(title)
                      or self.TOOL_TO_CANONICAL.get(title.lower()))
            if mapped:
                return mapped
            if "__" in title:
                return title
        mapped_kind = KIND_TO_NAME.get((upd.get("kind") or "").lower())
        if mapped_kind:
            return mapped_kind
        return super()._normalize_tool_name(upd)

    # ── configOptions → modes/models ───────────────────────────────────
    @staticmethod
    def _choices(opt: dict) -> List[dict]:
        return [c for c in opt.get("options") or []
                if isinstance(c, dict) and c.get("value")]

    def _ingest_session_result(self, result: dict) -> None:
        opts = result.get("configOptions")
        if isinstance(opts, list) and opts:
            result = dict(result)
            for opt in opts:
                if not isinstance(opt, dict):
                    continue
                oid = str(opt.get("id") or opt.get("category") or "")
                choices = self._choices(opt)
                if oid == "model" and not result.get("models"):
                    result["models"] = {
                        "availableModels": [
                            {"modelId": c["value"],
                             "name": c.get("name") or c["value"]}
                            for c in choices
                        ],
                        "currentModelId": opt.get("currentValue") or "",
                    }
                elif oid == "mode" and not result.get("modes"):
                    result["modes"] = {
                        "availableModes": [
                            {"id": c["value"],
                             "name": c.get("name") or c["value"],
                             "description": c.get("description") or ""}
                            for c in choices
                        ],
                        "currentModeId": opt.get("currentValue") or "",
                    }
            self.file_log(
                "configOptions → modes=%s models=%d"
                % (
                    [m.get("id") for m in
                     (result.get("modes") or {}).get("availableModes", [])],
                    len((result.get("models") or {}).get("availableModels", [])),
                )
            )
        super()._ingest_session_result(result)

    def normalize_model(self, model: Optional[str]) -> str:
        """Keep only opencode ids (`provider/model`).

        Backend switching / global default_model can hand us a Claude id like
        "opus"; sending that to session/set_model errors the session. Falling
        back to "" leaves opencode on its own configured default (apply_model
        skips an empty model).
        """
        mid = (model or "").strip()
        if mid and "/" not in mid:
            self.file_log("ignoring non-opencode model id %r" % mid)
            return ""
        return mid


def main() -> None:
    run_bridge(OpencodeBridge())


if __name__ == "__main__":
    main()
