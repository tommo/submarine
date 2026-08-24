"""Exit-plan handling, plan file discovery/diff, disk ensure.

Invariants: ExitPlanMode always shows plan UI (§9.19); Grok
_x.ai/exit_plan_mode response must be {outcome:approved} — 
{approved:true} is interpreted as revise (§9.26).
"""
from __future__ import annotations

import json
import os
import sys
from typing import Optional

_BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_DIR)

from rpc_helpers import process_cwd, send_notification, send_result  # noqa: E402


class PlanModeMixin:
    async def handle_plan_response(self, req_id: Optional[int],
                                    params: dict) -> None:
        """Resolve shared plan future, then map mode for the ACP agent."""
        from base import resolve_plan_response
        payload = resolve_plan_response(self, params)
        approved = (
            payload.get("approved") if isinstance(payload, dict) else payload
        )
        if self.session_id:
            try:
                if approved is True:
                    self.agent_mode = (
                        self.permission_mode_to_agent_mode("acceptEdits")
                        or self.agent_mode
                        or "auto"
                    )
                    self._in_plan_mode = False
                else:
                    self.agent_mode = (
                        self.permission_mode_to_agent_mode("plan") or "plan"
                    )
                    self._in_plan_mode = True
                await self.apply_mode()
            except Exception as e:
                self.log(f"plan_response mode switch failed: {e}")
        send_result(req_id, {"ok": True, "approved": approved})

    @staticmethod
    def _resolve_grok_plan_path(session_id: str, cwd: str) -> str:
        """Canonical Grok plan path: ~/.grok/sessions/<enc_cwd>/<sid>/plan.md.

        cwd is URL-encoded with literal %2F segments.
        """
        if not session_id or not cwd:
            return ""
        enc_cwd = cwd.replace("/", "%2F")
        return os.path.join(
            os.path.expanduser("~/.grok/sessions"),
            enc_cwd, session_id, "plan.md",
        )

    def _find_kimi_plan_file(self, session_id: str = "") -> str:
        """Newest plan under ~/.kimi-code/sessions/.../plans/*.md."""
        import glob
        root = os.path.expanduser("~/.kimi-code/sessions")
        if not os.path.isdir(root):
            return ""
        sid = (session_id or self.session_id or "").strip()
        if sid:
            cands = glob.glob(os.path.join(
                root, "*", sid, "agents", "*", "plans", "*.md"))
        else:
            cands = glob.glob(os.path.join(
                root, "*", "session_*", "agents", "*", "plans", "*.md"))
        cands = [p for p in cands if os.path.isfile(p)]
        return max(cands, key=os.path.getmtime) if cands else ""

    @staticmethod
    def _plan_unified_diff(before: str, after: str, *, max_chars: int = 12000) -> str:
        """Unified diff of plan before approval UI vs after user edits."""
        import difflib
        if (before or "") == (after or ""):
            return ""
        lines = list(difflib.unified_diff(
            (before or "").splitlines(),
            (after or "").splitlines(),
            fromfile="plan (proposed)",
            tofile="plan (current)",
            lineterm="",
        ))
        if not lines:
            return ""
        diff = "\n".join(lines)
        if len(diff) > max_chars:
            return diff[:max_chars] + "\n… (diff truncated)"
        return diff

    @staticmethod
    def _format_plan_user_feedback(
        *,
        approved: bool,
        user_notes: str = "",
        plan_before: str = "",
        plan_after: str = "",
        plan_path: str = "",
    ) -> str:
        """Embed plan path / diff / body in ExtResponse feedback.

        Used for both approve and request_changes so the agent sees user
        edits immediately (Grok surfaces feedback as review comments /
        "The user said: …"). Diff is always included when non-empty.
        """
        parts = []
        notes = (user_notes or "").strip()
        if notes:
            parts.append(notes)
        elif not approved:
            parts.append("Plan rejected — revise or stop")

        after = plan_after or ""
        before = plan_before or ""
        if plan_path:
            parts.append(f"Plan file: {plan_path}")

        diff = PlanModeMixin._plan_unified_diff(before, after)
        if diff:
            parts.append(
                "## Plan diff (proposed → current)\n```diff\n"
                + diff
                + "\n```"
            )
        elif approved and (before or after):
            parts.append("## Plan diff\n(no changes from proposed plan)")

        # Always include current body on reject; on approve include when
        # there was a diff or notes so the agent has the final plan text.
        include_body = (not approved) or bool(diff) or bool(notes)
        if include_body:
            if after.strip():
                body = (
                    after if len(after) <= 16000
                    else after[:16000] + "\n… (plan truncated)"
                )
                parts.append("## Current plan\n" + body)
            elif before.strip() and not approved:
                body = (
                    before if len(before) <= 16000
                    else before[:16000] + "\n… (plan truncated)"
                )
                parts.append("## Proposed plan (unchanged on disk)\n" + body)
        return "\n\n".join(parts)

    @staticmethod
    def _exit_plan_ext_response(approved, feedback: str = ""):
        """Grok ExitPlanModeExtResponse — outcome-tagged (like AskUser).

        Confirmed via ACP probe against grok agent stdio:
          {"outcome": "approved"}  → PlanReady / "User has approved your plan…"
          {"approved": true}       → WRONG; maps to revise text

        Outcomes (snake_case, internally tagged on `outcome`):
          approved | request_changes | abandoned
        Optional `feedback` only for request_changes (revision notes) or
        approve-with-comments.
        """
        fb = (feedback or "").strip()
        if approved is True:
            resp = {"outcome": "approved"}
            if fb:
                resp["feedback"] = fb
            return resp
        if approved is False:
            # Explicit reject → request changes (stay in plan mode).
            return {
                "outcome": "request_changes",
                "feedback": fb or "Plan rejected — revise or stop",
            }
        # Cancel / dismiss without approve → abandon plan mode.
        return {"outcome": "abandoned"}

    def _ensure_plan_on_disk(self, plan_content: str, plan_path: str) -> str:
        """Write plan_content to disk if not already present; return path or fallback."""
        if not plan_content:
            return plan_path or ""
        if plan_path:
            try:
                os.makedirs(os.path.dirname(plan_path), exist_ok=True)
                if not os.path.isfile(plan_path):
                    with open(plan_path, "w", encoding="utf-8") as f:
                        f.write(plan_content)
                return plan_path
            except Exception as e:
                self.file_log(f"exit_plan_mode: plan file write failed: {e}")
        fallback = os.path.join(self.cwd or process_cwd(), ".grok-plan.md")
        try:
            with open(fallback, "w", encoding="utf-8") as f:
                f.write(plan_content)
            return fallback
        except Exception as e:
            self.file_log(f"exit_plan_mode: fallback plan write failed: {e}")
            return ""

    async def _handle_acp_exit_plan_permission(
            self, tool_input: dict, tool_call: Optional[dict] = None) -> bool:
        """session/request_permission for ExitPlanMode → plan approval UI.

        Kimi (and any ACP agent without a dedicated exit_plan_mode method) hits
        this instead of the generic Y/N permission. Returns True if approved.
        """
        tool_call = tool_call or {}
        plan_content = (
            (tool_input or {}).get("plan")
            or (tool_input or {}).get("planContent")
            or (tool_input or {}).get("content")
            or (tool_input or {}).get("markdown")
            or ""
        )
        if not isinstance(plan_content, str):
            plan_content = str(plan_content or "")
        # Prefer full plan from rawInput when present
        raw = tool_call.get("rawInput") or {}
        if isinstance(raw, dict) and not plan_content:
            for k in ("plan", "planContent", "content", "markdown", "body"):
                if raw.get(k):
                    plan_content = str(raw.get(k) or "")
                    break

        plan_path = (
            (tool_input or {}).get("planFilePath")
            or (tool_input or {}).get("plan_file")
            or (tool_input or {}).get("file_path")
            or ""
        )
        # Kimi: path is often only on display / locations, not rawInput
        if not plan_path:
            display = tool_call.get("display") or {}
            if isinstance(display, dict):
                plan_path = display.get("path") or ""
        if not plan_path:
            for loc in (tool_call.get("locations") or []):
                if isinstance(loc, dict) and loc.get("path"):
                    plan_path = str(loc["path"])
                    break
        # Prefer real on-disk plan: Kimi fancy names, then Grok plan.md
        if not plan_path or not os.path.isfile(plan_path):
            kimi = self._find_kimi_plan_file(self.session_id or "")
            if kimi:
                plan_path = kimi
            elif not plan_path:
                plan_path = self._resolve_grok_plan_path(
                    self.session_id or "", self.cwd or "")
        if plan_content:
            plan_path = self._ensure_plan_on_disk(plan_content, plan_path)
        elif plan_path and os.path.isfile(plan_path):
            try:
                with open(plan_path, "r", encoding="utf-8", errors="replace") as f:
                    plan_content = f.read()
            except Exception:
                pass

        approval_input = {
            "plan": plan_content,
            "planFilePath": plan_path or "",
            "allowedPrompts": (tool_input or {}).get("allowedPrompts") or [],
        }
        self._in_plan_mode = True
        send_notification("plan_mode_enter", {})  # ensure host knows we're in plan
        result = await self.request_plan_approval(approval_input, timeout=3600)
        approved = bool(isinstance(result, dict) and result.get("approved") is True)
        if approved:
            self._in_plan_mode = False
            try:
                self.agent_mode = (
                    self.permission_mode_to_agent_mode("acceptEdits")
                    or self.agent_mode or "auto"
                )
                await self.apply_mode()
            except Exception as e:
                self.file_log(f"plan approve mode switch failed: {e}")
        else:
            self._in_plan_mode = True
            try:
                self.agent_mode = (
                    self.permission_mode_to_agent_mode("plan") or "plan"
                )
                await self.apply_mode()
            except Exception as e:
                self.file_log(f"plan reject mode switch failed: {e}")
        self.file_log(
            f"acp ExitPlanMode permission approved={approved!r} "
            f"plan_chars={len(plan_content)} path={plan_path!r}")
        return approved

    async def _acp_exit_plan_mode(self, params: dict) -> dict:
        """Grok `_x.ai/exit_plan_mode` → same plan UI as Claude ExitPlanMode."""
        plan_content = params.get("planContent") or params.get("plan") or ""
        tool_call_id = params.get("toolCallId") or ""
        self.file_log(
            f"exit_plan_mode: toolCallId={tool_call_id!r} "
            f"plan_chars={len(plan_content)}")

        if tool_call_id and tool_call_id not in self._tool_ids_emitted:
            # session/update often already opened this id — don't second-paint
            self._tool_ids_emitted.add(tool_call_id)
            self._tool_names_by_id[tool_call_id] = "ExitPlanMode"
            send_notification("message", {
                "type": "tool_use",
                "id": tool_call_id,
                "name": "ExitPlanMode",
                "input": {"plan": plan_content[:2000]},
            })

        plan_path = self._resolve_grok_plan_path(
            self.session_id or "", self.cwd or "")
        plan_path = self._ensure_plan_on_disk(plan_content, plan_path)

        tool_input = {
            "plan": plan_content,
            "planFilePath": plan_path,
            "allowedPrompts": params.get("allowedPrompts") or [],
        }
        self._in_plan_mode = True
        result = await self.request_plan_approval(tool_input, timeout=3600)

        if not result:
            approved = None
            plan_text = ""
            if plan_path and os.path.isfile(plan_path):
                try:
                    with open(plan_path, "r", encoding="utf-8", errors="replace") as f:
                        plan_text = f.read()
                except Exception:
                    plan_text = plan_content
            else:
                plan_text = plan_content
        else:
            approved = result.get("approved")
            plan_text = result.get("plan") or plan_content
            if result.get("planFilePath"):
                plan_path = result["planFilePath"]

        # Prefer on-disk plan at response time (user may have saved edits).
        if plan_path and os.path.isfile(plan_path):
            try:
                with open(plan_path, "r", encoding="utf-8", errors="replace") as f:
                    disk = f.read()
                if disk:
                    plan_text = disk
            except Exception:
                pass

        # Optional freeform notes from plugin.
        user_feedback = ""
        if isinstance(result, dict):
            user_feedback = (
                result.get("feedback")
                or result.get("comments")
                or result.get("message")
                or ""
            )
            if not isinstance(user_feedback, str):
                user_feedback = str(user_feedback or "")

        # Approve + reject: embed plan path / diff / body in feedback so the
        # agent sees user edits without re-reading plan.md.
        if approved is True or approved is False:
            user_feedback = self._format_plan_user_feedback(
                approved=bool(approved),
                user_notes=user_feedback,
                plan_before=plan_content,
                plan_after=plan_text,
                plan_path=plan_path or "",
            )

        resp = self._exit_plan_ext_response(approved, user_feedback)
        ok = approved is True
        if ok:
            summary = (
                (user_feedback[:800] + "…") if len(user_feedback) > 800
                else (user_feedback or "Plan approved — implement")
            )
        elif approved is False:
            # Plugin transcript: keep short; full plan+diff is in ExtResponse feedback.
            summary = (user_feedback[:800] + "…") if len(user_feedback) > 800 else (
                user_feedback or "Plan rejected — revise or stop"
            )
        else:
            summary = "Continue planning"
        if tool_call_id and tool_call_id not in self._tool_results_sent:
            self._tool_results_sent.add(tool_call_id)
            send_notification("message", {
                "type": "tool_result",
                "tool_use_id": tool_call_id,
                "content": summary,
                "is_error": not ok,
            })
        self.file_log(
            f"exit_plan_mode result approved={approved!r} "
            f"plan_chars={len(plan_text)} resp={json.dumps(resp)[:200]}")
        return resp

    @staticmethod
    def _is_kimi_plan_file(path: str) -> bool:
        """True for Kimi Code plan-mode scratch files under session workdir.

        EnterPlanMode picks a plan id then fs/read_text_file the plan path
        before it exists. ENOENT there must be empty content, not a hard
        error — otherwise EnterPlanMode fails immediately (seen in
        submarine_acp_bridge log: plans/mantis-moon-knight-swamp-thing.md).
        """
        if not path:
            return False
        norm = path.replace("\\", "/")
        if "/.kimi-code/sessions/" not in norm:
            return False
        # .../agents/<name>/plans/<id>.md  (main or subagent)
        return "/plans/" in norm and norm.rstrip("/").endswith(".md")
