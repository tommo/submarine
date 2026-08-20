"""Plugin initialize/shutdown/clear plus ACP session/new|load.

Invariants: MCP servers must use env:[] not env:{} (§9.34); mode
mapping is agent-specific and clamped to advertised ids (§9.37);
ACP fork is a new session with a log line only (§9.38).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from typing import Any, Dict, List, Optional

_BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_DIR)

from rpc_helpers import send_error, send_notification, send_result  # noqa: E402


class SessionMixin:
    def permission_mode_to_agent_mode(self, permission_mode: Optional[str]) -> str:
        if not permission_mode:
            return self.PERM_TO_MODE.get("default", "")
        return self.PERM_TO_MODE.get(
            permission_mode, self.PERM_TO_MODE.get("default", permission_mode or ""))

    def agent_mode_to_permission_mode(self, mode: str) -> str:
        return self.MODE_TO_PERM.get(mode, mode)

    async def apply_model(self) -> None:
        """Push self.model (and effort, if any) to the live session."""
        if not self.session_id or not self.model:
            return
        requested = self.model
        try:
            result = await self._send_acp(
                "session/set_model", self.set_model_params()) or {}
            # Grok: {_meta: {model: {Ok: id}}} ; others may return currentModelId
            current = result.get("currentModelId")
            if not current:
                meta = result.get("_meta") or {}
                model_meta = meta.get("model") or {}
                if isinstance(model_meta, dict):
                    current = model_meta.get("Ok") or model_meta.get("ok")
            nxt = self.resolve_applied_model(requested, current)
            if nxt:
                self.model = nxt
        except Exception as e:
            self.log(f"session/set_model({self.model}) failed: {e}")

    def _advertised_mode_ids(self) -> List[str]:
        """modeIds from session/new|load modes.availableModes (if any)."""
        ids: List[str] = []
        for m in self._available_modes or []:
            if isinstance(m, dict) and m.get("id"):
                ids.append(str(m["id"]))
            elif isinstance(m, str):
                ids.append(m)
        return ids

    def _resolve_set_mode_id(self) -> str:
        """Map self.agent_mode to a modeId the agent actually advertises.

        Open-source kimi-cli only advertises ``default`` (set_session_mode
        asserts mode_id == "default"). Kimi Code may advertise
        default|plan|auto|yolo. Never send Claude-only ids like acceptEdits.
        """
        want = (self.agent_mode or "default").strip()
        advertised = self._advertised_mode_ids()
        if not advertised:
            # No list yet — only send safe universal id
            if want in ("default", "plan", "auto", "yolo"):
                return want
            return "default"
        if want in advertised:
            return want
        # Fallbacks when host wants acceptEdits/bypass but agent has other names
        for cand in (
            want,
            "yolo" if want in ("acceptEdits", "auto") else "",
            "auto" if want in ("bypassPermissions", "dontAsk") else "",
            "plan" if want == "plan" else "",
            "default",
        ):
            if cand and cand in advertised:
                return cand
        return advertised[0]

    async def apply_mode(self) -> None:
        """Push mode via session/set_mode (only if agent advertises modes)."""
        if not self.session_id:
            return
        mode_id = self._resolve_set_mode_id()
        # kimi-cli OSS: only "default" is valid; skip no-op churn
        advertised = self._advertised_mode_ids()
        if advertised == ["default"] and mode_id == "default":
            self.agent_mode = "default"
            return
        if advertised and mode_id not in advertised:
            self.file_log(
                f"session/set_mode skip unknown modeId={mode_id!r} "
                f"advertised={advertised}")
            return
        try:
            await self._send_acp("session/set_mode", {
                "sessionId": self.session_id,
                "modeId": mode_id,
            })
            self.agent_mode = mode_id
        except Exception as e:
            self.log(f"session/set_mode({mode_id}) failed: {e}")

    def usage_from_tool_update(self, upd: dict) -> Optional[dict]:
        """Optional usage payload embedded in tool_call_update."""
        return None

    def usage_from_prompt_result(self, result: dict) -> Optional[dict]:
        """Optional usage from session/prompt result (e.g. Grok _meta tokens)."""
        meta = (result or {}).get("_meta") or {}
        if not meta:
            return None
        # Normalize common token fields if present.
        keys = ("inputTokens", "outputTokens", "cachedReadTokens",
                "reasoningTokens", "totalTokens")
        if not any(k in meta for k in keys):
            return None
        def _tok(key: str) -> int:
            v = meta.get(key)
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0
        return {
            "input_tokens": _tok("inputTokens"),
            "output_tokens": _tok("outputTokens"),
            "cache_read_input_tokens": _tok("cachedReadTokens"),
            "reasoning_tokens": _tok("reasoningTokens"),
            "total_tokens": _tok("totalTokens"),
            "model": meta.get("modelId"),
        }

    def build_session_meta(self, *, system_prompt: str = "",
                           resume_failed: bool = False) -> Dict[str, Any]:
        meta: Dict[str, Any] = {}
        if system_prompt:
            meta["systemPromptOverride"] = system_prompt
        if resume_failed:
            meta["rules"] = (
                "This Sublime session was reopened without a loadable agent "
                "transcript. The user can still see prior UI history; do not "
                "assume you remember earlier turns unless restated."
            )
        return meta

    def _reset_conversation_local(self) -> None:
        """Drop turn-local maps that belong to the conversation being cleared."""
        self._bg_tool_ids.clear()
        self._terminal_bg.clear()
        self._bg_notified_tasks.clear()
        self._bg_notified_tools.clear()
        self._tool_ids_emitted.clear()
        self._tool_results_sent.clear()
        self._tool_id_alias.clear()
        self._tool_inputs_by_id.clear()
        self._tool_names_by_id.clear()
        self._pending_execute_ids.clear()
        self._last_execute_id = None
        self._last_bg_tool_id = None
        self._resumed = False
        self._resume_fallback = False
        self._loading_session = False
        self._in_plan_mode = False
        try:
            self._cancel_child_sessions("clear")
        except Exception:
            for slot in list(getattr(self, "_child_sessions", {}).values()):
                ev = slot.get("event")
                slot["done"] = True
                if slot.get("exit") is None:
                    slot["exit"] = {"exitCode": None, "signal": "SIGTERM"}
                if ev is not None and not ev.is_set():
                    ev.set()
        if hasattr(self, "_child_sessions"):
            self._child_sessions.clear()
        if hasattr(self, "_released_terminals"):
            self._released_terminals.clear()
        self._leftover_end_pending = False
        keys = list(self._client_schedule_tasks.keys())
        for key in keys:
            self._cancel_client_schedule(key)

    async def handle_clear(self, req_id: Optional[int],
                            params: dict) -> None:
        """Harness /clear: session/new on the live agent, keep the process."""
        if self.session_id is None:
            send_error(req_id, -32000, "session not initialized")
            return
        if (
            self._query_req_id is not None
            or (self._prompt_fut is not None and not self._prompt_fut.done())
            or self._cancel_in_flight
        ):
            await self._cancel_agent_turn(
                reason="clear", wait_s=2.0, settle_s=0.3,
                force_local=True, orphan_ok=True)
        self._reset_conversation_local()
        old = self.session_id
        mcp_servers = self._collect_mcp_servers()
        await self._create_session(
            mcp_servers,
            system_prompt=getattr(self, "_system_prompt", "") or "",
            additional_dirs=getattr(self, "_additional_dirs", None),
        )
        await self.apply_mode()
        if self.model:
            await self.apply_model()
        self.file_log(
            f"clear: {old} → {self.session_id} model={self.model}")
        send_result(req_id, {
            "ok": True,
            "session_id": self.session_id,
            "sessionId": self.session_id,
            "model": self.model,
        })

    async def handle_initialize(self, req_id: Optional[int],
                                 params: dict) -> None:
        raw_model = params.get("model")
        self._host_model = bool(raw_model)
        if raw_model:
            self.model = self.normalize_model(raw_model)
        # Capture effort before first agent spawn (Grok CLI flag is spawn-time).
        self.effort = self.normalize_effort(params.get("effort"))
        self._agent_exited = False
        if self.effort:
            self.file_log(f"initialize: effort={self.effort!r}")
        self.cwd = params.get("cwd") or self.cwd
        if self.cwd and os.path.isdir(self.cwd):
            try:
                os.chdir(self.cwd)
            except OSError:
                pass
        self._agent_id = params.get("agent_id")
        # Optional vision MCP tool — default from class (Grok=on); host may
        # override via mcp_enable_read_image (settings auto/true/false).
        if "mcp_enable_read_image" in params:
            self._mcp_enable_read_image = bool(params.get("mcp_enable_read_image"))
        else:
            self._mcp_enable_read_image = bool(
                getattr(self, "MCP_ENABLE_READ_IMAGE", False))
        # Plugin permission rules — same payload Claude bridge receives.
        self.permission_mode = params.get("permission_mode") or "default"
        raw_allowed = params.get("allowed_tools") or []
        self.allowed_tools = [
            str(t) for t in raw_allowed if isinstance(t, str) and t.strip()
        ]
        self._reload_auto_allow_patterns()
        self.agent_mode = self.permission_mode_to_agent_mode(
            self.permission_mode)
        self.file_log(
            f"permissions: mode={self.permission_mode!r} "
            f"allowed_tools={self.allowed_tools} "
            f"auto_patterns={len(self._auto_allow_patterns)}")
        resume_id = params.get("resume")
        fork_session = bool(params.get("fork_session", False))
        system_prompt = params.get("system_prompt") or ""
        additional_dirs = params.get("additional_dirs") or []
        self._additional_dirs = list(additional_dirs)
        self._system_prompt = system_prompt

        try:
            # kimi-code 0.36: AskUser Q1+ only via elicitation/create.
            # request_permission handleQuestion drops every question after q0.
            client_caps: Dict[str, Any] = {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True,
            }
            if getattr(self, "BACKEND_NAME", "") == "kimi":
                client_caps["elicitation"] = {"form": {}}
            init_request = {
                "protocolVersion": 1,
                "clientCapabilities": client_caps,
                "clientInfo": {
                    "name": self.CLIENT_NAME,
                    "version": self.CLIENT_VERSION,
                },
            }
            init_result = await self._send_acp("initialize", init_request) or {}
            negotiated = init_result.get("protocolVersion", 1)
            if negotiated != 1:
                self.log(f"agent negotiated protocolVersion={negotiated} "
                         f"(client requested 1); proceeding")
            self.negotiated_protocol_version = negotiated
            self.agent_capabilities = init_result.get("agentCapabilities", {}) or {}
            self._auth_methods = init_result.get("authMethods") or []
            self._init_meta = init_result.get("_meta") or {}

            await self.after_agent_initialize(init_result)

            mcp_servers = self._collect_mcp_servers()
            self.file_log(
                f"_collect_mcp_servers → {len(mcp_servers)} server(s): "
                f"{json.dumps(mcp_servers)[:600]}")

            can_load = bool(self.agent_capabilities.get("loadSession"))
            loaded = False
            if resume_id and not fork_session and can_load:
                loaded = await self._try_load_session(resume_id, mcp_servers)

            if not loaded:
                await self._create_session(
                    mcp_servers,
                    system_prompt=system_prompt,
                    additional_dirs=additional_dirs,
                    resume_failed=bool(resume_id and not fork_session),
                )
                if resume_id and not fork_session:
                    self._resume_fallback = True
                if fork_session and resume_id:
                    self.log(f"fork from {resume_id}: ACP has no fork; "
                             f"opened new session {self.session_id}")

            await self.apply_mode()
            if self.model:
                await self.apply_model()

            send_result(req_id, {
                "status": "initialized",
                "ok": True,
                "backend": self.BACKEND_NAME,
                "session_id": self.session_id,
                "sessionId": self.session_id,
                "agent": init_result.get("agentInfo", {}),
                "agent_capabilities": self.agent_capabilities,
                "protocol_version": negotiated,
                "mcp_servers": [s.get("name") for s in mcp_servers],
                "agents": [],
                "streaming": True,
                "resumed": self._resumed,
                "resume_fallback": self._resume_fallback,
                "edit_mode": self.agent_mode,
                "modes": self._available_modes,
                "models": self._available_models,
                "effort": self.effort or None,
                "model": self.model,
            })
            if self._resume_fallback:
                send_notification("message", {
                    "type": "system",
                    "subtype": "init",
                    "data": {
                        "message": (
                            "Could not load prior ACP session; started fresh. "
                            "UI history is intact but the agent has no prior turns."
                        ),
                    },
                })
        except Exception as e:
            send_error(req_id, -32000,
                       f"{self.BACKEND_NAME} initialize failed: {e}")

    async def _try_load_session(self, resume_id: str,
                                 mcp_servers: list) -> bool:
        load_params: Dict[str, Any] = {
            "sessionId": resume_id,
            "cwd": self.cwd,
        }
        load_params["mcpServers"] = list(mcp_servers or [])
        self._loading_session = True
        try:
            try:
                result = await self._send_acp("session/load", load_params) or {}
            except Exception as e:
                if mcp_servers and self._is_mcp_runtime_identity_error(e):
                    self.file_log(
                        f"session/load MCP rejected ({e}); retry mcpServers=[]")
                    load_params = dict(load_params)
                    load_params["mcpServers"] = []
                    result = await self._send_acp("session/load", load_params) or {}
                else:
                    raise
            self.session_id = (
                result.get("sessionId")
                or result.get("session_id")
                or resume_id
            )
            self._ingest_session_result(result)
            self._resumed = True
            self.log(f"session/load ok: {self.session_id}")
            return True
        except Exception as e:
            self.log(f"session/load failed for {resume_id!r}: {e}")
            return False
        finally:
            # Trailing session/update after the load RPC is still replay.
            # Clearing immediately lets those adopt as a live ⚙ and steal ◎.
            def _end_load():
                self._loading_session = False
            try:
                asyncio.get_running_loop().call_later(0.4, _end_load)
            except Exception:
                self._loading_session = False

    async def _create_session(self, mcp_servers: list, *,
                               system_prompt: str = "",
                               additional_dirs: Optional[list] = None,
                               resume_failed: bool = False) -> None:
        new_params: Dict[str, Any] = {"cwd": self.cwd}
        # Kimi 0.37: mcpServers is required (missing → Invalid params).
        new_params["mcpServers"] = list(mcp_servers or [])
        if additional_dirs:
            new_params["additionalDirectories"] = list(additional_dirs)
        meta = self.build_session_meta(
            system_prompt=system_prompt, resume_failed=resume_failed)
        if meta:
            new_params["_meta"] = meta

        try:
            new_result = await self._send_acp("session/new", new_params) or {}
        except Exception as e:
            # Kimi 0.37.2: type:stdio MCP is stripped then rejected
            # ("does not declare a runtime identity"). Empty mcpServers works.
            if mcp_servers and self._is_mcp_runtime_identity_error(e):
                self.file_log(
                    f"session/new MCP rejected ({e}); retry mcpServers=[]")
                new_params = dict(new_params)
                new_params["mcpServers"] = []
                new_result = await self._send_acp("session/new", new_params) or {}
            else:
                raise
        self.session_id = (
            new_result.get("sessionId") or new_result.get("session_id")
        )
        if not self.session_id:
            raise RuntimeError(
                f"session/new returned no sessionId: {new_result!r}")
        self._ingest_session_result(new_result)
        self._resumed = False

    def _ingest_session_result(self, result: dict) -> None:
        modes = result.get("modes") or {}
        if modes.get("availableModes"):
            self._available_modes = modes["availableModes"]
            self.file_log(
                f"session modes advertised: "
                f"{self._advertised_mode_ids()}")
        if modes.get("currentModeId"):
            # Prefer agent-reported mode, then re-resolve host permission intent
            cur = str(modes["currentModeId"])
            want = self.permission_mode_to_agent_mode(self.permission_mode)
            self.agent_mode = want or cur
            # Clamp to advertised list
            self.agent_mode = self._resolve_set_mode_id()
        models = result.get("models") or {}
        if models.get("availableModels"):
            self._available_models = models["availableModels"]
        if models.get("currentModelId") and not self._host_model:
            self.model = models["currentModelId"]

    def _collect_mcp_servers(self) -> list:
        """MCP servers for ACP session/new (Grok, Kimi, …).

        Always injects submarine (editor tools). Also injects **irr** (codebase
        search) when the `irr` binary is available — same stdio shape Claude
        uses from ~/.claude.json.

        Grok/Kimi McpServer untagged enum requires:
          {name, type:"stdio", command, args?, env: [{name,value}, ...] | []}
        A dict env {} is rejected with Invalid params.
        """
        servers: List[dict] = []
        # _BRIDGE_DIR is bridge/ (this file lives in bridge/acp/).
        bridge_dir = _BRIDGE_DIR
        plugin_dir = os.path.dirname(bridge_dir)
        mcp_server_path = os.path.join(plugin_dir, "mcp", "server.py")
        if os.path.exists(mcp_server_path):
            args = [mcp_server_path]
            if self._agent_id is not None:
                args.append(f"--agent-id={self._agent_id}")
            if self._mcp_enable_read_image:
                args.append("--enable-read-image")
            servers.append({
                "name": "submarine",
                "type": "stdio",
                "command": sys.executable,
                "args": args,
                "env": [],
            })
        else:
            self.file_log(f"MCP server missing: {mcp_server_path}")

        irr = self._collect_irr_mcp_server()
        if irr:
            servers.append(irr)
            self.file_log(
                f"MCP irr: {irr.get('command')} {' '.join(irr.get('args') or [])}")
        return servers

    def _collect_irr_mcp_server(self) -> Optional[dict]:
        """stdio MCP for irr (semantic/code search). None if unavailable.

        Binary: PATH `irr`, else ~/.nimble/bin/irr.
        Index db (--db), first hit wins:
          1) env SUBMARINE_IRR_DB / IRR_MCP_DB
          2) Claude ~/.claude.json mcpServers.irr --db
          3) cwd/.irr or parent project .irr
          4) ~/.irr-pil/db/pil-core (common multi-project index)
        Disable with env SUBMARINE_IRR_MCP=0.
        """
        if os.environ.get("SUBMARINE_IRR_MCP", "1").strip() in (
                "0", "false", "off", "no"):
            return None
        irr_bin = (
            shutil.which("irr")
            or os.path.expanduser("~/.nimble/bin/irr")
        )
        if not irr_bin or not os.path.isfile(irr_bin):
            if not shutil.which("irr"):
                self.file_log("MCP irr: binary not found — skip")
                return None
            irr_bin = shutil.which("irr")

        db = (
            (os.environ.get("SUBMARINE_IRR_DB") or "").strip()
            or (os.environ.get("IRR_MCP_DB") or "").strip()
            or self._irr_db_from_claude_json()
            or self._irr_db_near_cwd()
            or self._irr_db_default()
        )
        if not db:
            self.file_log("MCP irr: no index db found — skip")
            return None
        if not os.path.isdir(db):
            self.file_log(f"MCP irr: db not a directory {db!r} — skip")
            return None

        return {
            "name": "irr",
            "type": "stdio",
            "command": irr_bin,
            "args": ["mcp", "--db", db],
            "env": [],
        }

    @staticmethod
    def _irr_db_from_claude_json() -> str:
        """Parse --db from Claude Code user mcpServers.irr if present."""
        path = os.path.expanduser("~/.claude.json")
        if not os.path.isfile(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            entry = (data.get("mcpServers") or {}).get("irr") or {}
            args = entry.get("args") or []
            if not isinstance(args, list):
                return ""
            for i, a in enumerate(args):
                if a == "--db" and i + 1 < len(args):
                    return str(args[i + 1]).strip()
                if isinstance(a, str) and a.startswith("--db="):
                    return a.split("=", 1)[1].strip()
        except Exception:
            pass
        return ""

    def _irr_db_near_cwd(self) -> str:
        """Walk cwd→parents for a .irr index directory."""
        start = (self.cwd or os.getcwd() or "").strip() or os.getcwd()
        try:
            cur = os.path.abspath(start)
        except Exception:
            return ""
        for _ in range(8):
            cand = os.path.join(cur, ".irr")
            if os.path.isdir(cand):
                return cand
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        return ""

    @staticmethod
    def _irr_db_default() -> str:
        cand = os.path.expanduser("~/.irr-pil/db/pil-core")
        return cand if os.path.isdir(cand) else ""

    async def handle_set_model(self, req_id: Optional[int],
                                params: dict) -> None:
        self.model = self.normalize_model(params.get("model"))
        if "effort" in params:
            self.effort = self.normalize_effort(params.get("effort"))
        try:
            await self.apply_model()
            send_result(req_id, {
                "ok": True,
                "model": self.model,
                "effort": self.effort or None,
            })
        except Exception as e:
            send_error(req_id, -32000, f"set_model failed: {e}")

    async def handle_set_permission_mode(self, req_id: Optional[int],
                                          params: dict) -> None:
        mode = params.get("mode") or "default"
        self.permission_mode = mode
        self.agent_mode = self.permission_mode_to_agent_mode(mode)
        # Refresh patterns in case user managed auto-allows while idle.
        self._reload_auto_allow_patterns()
        try:
            await self.apply_mode()
            send_result(req_id, {
                "ok": True,
                "mode": mode,
                "edit_mode": self.agent_mode,
            })
        except Exception as e:
            send_error(req_id, -32000, f"set_permission_mode failed: {e}")

    async def handle_shutdown(self, req_id: Optional[int],
                               params: dict) -> None:
        self.running = False
        held = getattr(self, "_http_mcp_httpd", None) or []
        for httpd in held:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                child = getattr(httpd, "child", None)
                if child is not None:
                    child.close()
            except Exception:
                pass
        self._http_mcp_httpd = []
        for tid in list(self._terminals):
            try:
                await self._terminal_close(tid)
            except Exception:
                pass
        for tid, proc in list(getattr(self, "_detached_procs", {}).items()):
            self._kill_terminal_proc(proc)
        if hasattr(self, "_detached_procs"):
            self._detached_procs.clear()
        try:
            self._cancel_child_sessions("shutdown")
        except Exception:
            pass
        if self.proc is not None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
        send_result(req_id, {"ok": True})
