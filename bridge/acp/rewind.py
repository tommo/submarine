"""Rewind points / execute and Grok conversation truncate.

Client-side fallback walks the Grok session dir and truncates the
conversation file to the chosen prompt index.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Optional

_BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_DIR)

from rpc_helpers import send_error, send_result  # noqa: E402


class RewindMixin:
    # ── Message-round rewind (Grok x.ai/rewind/* + disk truncate) ─────

    def _grok_session_dir(self) -> Optional[str]:
        """Locate ~/.grok/sessions/<encoded_cwd>/<session_id>/."""
        sid = self.session_id
        if not sid:
            return None
        root = os.path.expanduser("~/.grok/sessions")
        if not os.path.isdir(root):
            return None
        # Preferred: cwd-encoded path used by Grok Build.
        if self.cwd:
            enc = self.cwd.replace("/", "%2F")
            cand = os.path.join(root, enc, sid)
            if os.path.isdir(cand):
                return cand
        # Fallback: scan for session id directory.
        try:
            for name in os.listdir(root):
                cand = os.path.join(root, name, sid)
                if os.path.isdir(cand):
                    return cand
        except OSError:
            pass
        return None

    async def handle_rewind_points(self, req_id: Optional[int],
                                    params: dict) -> None:
        """List rewind points for the current Grok ACP session."""
        if not self.session_id:
            send_error(req_id, -32000, "session not initialized")
            return
        try:
            result = await self._send_acp(
                "_x.ai/rewind/points",
                {"sessionId": self.session_id},
            ) or {}
            points = result.get("rewind_points") or result.get("points") or []
            if not isinstance(points, list):
                points = []
            # Normalize for plugin UI.
            out = []
            for p in points:
                if not isinstance(p, dict):
                    continue
                idx = p.get("prompt_index")
                if idx is None:
                    idx = p.get("promptIndex")
                try:
                    idx = int(idx)
                except (TypeError, ValueError):
                    continue
                preview = (
                    p.get("prompt_preview")
                    or p.get("promptPreview")
                    or p.get("prompt_text")
                    or ""
                )
                out.append({
                    "prompt_index": idx,
                    "prompt_preview": str(preview),
                    "created_at": p.get("created_at") or p.get("createdAt") or "",
                    "has_file_changes": bool(
                        p.get("has_file_changes")
                        or p.get("hasFileChanges")
                        or (p.get("num_file_snapshots") or 0)
                    ),
                    "num_file_snapshots": int(
                        p.get("num_file_snapshots")
                        or p.get("numFileSnapshots")
                        or 0
                    ),
                })
            out.sort(key=lambda x: x["prompt_index"])
            send_result(req_id, {
                "ok": True,
                "session_id": self.session_id,
                "points": out,
                "leaf_response_id": (
                    result.get("leaf_response_id")
                    or result.get("leafResponseId")
                ),
            })
        except Exception as e:
            send_error(req_id, -32000, f"rewind_points failed: {e}")

    async def handle_rewind_execute(self, req_id: Optional[int],
                                     params: dict) -> None:
        """Conversation-only rewind to a prompt_index.

        Never restores project file snapshots. Truncates Grok *session*
        transcript files (chat_history / rewind_points / updates) so the next
        session/load forgets later turns. ACP ``_x.ai/rewind/execute`` is
        best-effort (often success:false with no error) — session truncate is
        the reliable path.
        """
        if not self.session_id:
            send_error(req_id, -32000, "session not initialized")
            return
        try:
            target = params.get("prompt_index")
            if target is None:
                target = params.get("target_prompt_index")
            target = int(target)
        except (TypeError, ValueError):
            send_error(req_id, -32602, "prompt_index required (int)")
            return
        # Product scope: conversation only — ignore mode/restore_files from host.
        mode = "conversation_only"

        disk = await asyncio.to_thread(self._client_rewind_conversation, target)
        acp_result: dict = {}
        try:
            acp_params = {
                "sessionId": self.session_id,
                "target_prompt_index": target,
                "mode": mode,
            }
            leaf = params.get("expected_leaf_response_id") or params.get(
                "leaf_response_id")
            if leaf:
                acp_params["expected_leaf_response_id"] = leaf
            # Grok ACP execute often hangs or returns success:false; never
            # block the bridge forever — session truncate is the real work.
            acp_result = await asyncio.wait_for(
                self._send_acp("_x.ai/rewind/execute", acp_params),
                timeout=12.0,
            ) or {}
            self.file_log(
                f"rewind/execute target={target} mode={mode} "
                f"acp={str(acp_result)[:300]}")
        except asyncio.TimeoutError:
            self.file_log(
                f"rewind/execute ACP TIMEOUT (session still cut) target={target}")
            acp_result = {"success": False, "error": "acp timeout"}
        except Exception as e:
            self.file_log(f"rewind/execute ACP error (session still cut): {e}")
            acp_result = {"success": False, "error": str(e)}

        draft = (disk or {}).get("draft_prompt") or ""
        if not draft and isinstance(acp_result, dict):
            draft = acp_result.get("prompt_text") or ""
        send_result(req_id, {
            "ok": True,
            "prompt_index": target,
            "draft_prompt": draft,
            "mode": mode,
            "disk": disk or {},
            "acp": acp_result,
            "session_id": self.session_id,
        })

    @staticmethod
    def _chat_row_text(o: dict) -> str:
        content = o.get("content") or ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(b.get("text") or "")
            return "".join(parts)
        return ""

    @staticmethod
    def _user_query_body(text: str) -> str:
        if "<user_query>" not in text:
            return ""
        try:
            body = text.split("<user_query>", 1)[1]
            return body.split("</user_query>", 1)[0].strip()
        except IndexError:
            return text.strip()

    def _client_rewind_conversation(self, target_prompt_index: int) -> dict:
        """Truncate Grok *session* history to before target_prompt_index.

        Conversation only — never writes project files / file_snapshots.
        Cut key is real ``prompt_index`` (not Nth user_query). Updates are
        stream-cut at the first line with ``_meta.promptIndex >= target``.
        """
        sdir = self._grok_session_dir()
        if not sdir:
            return {"error": "session dir not found", "draft_prompt": ""}

        chat_path = os.path.join(sdir, "chat_history.jsonl")
        rp_path = os.path.join(sdir, "rewind_points.jsonl")
        up_path = os.path.join(sdir, "updates.jsonl")
        draft = ""
        cut_at = None
        kept_rp = 0
        dropped_rp = 0
        kept_up = 0
        dropped_up = 0

        # rewind_points: keep prompt_index < target; draft from matching row
        if os.path.isfile(rp_path):
            kept_lines = []
            with open(rp_path, "r", encoding="utf-8") as f:
                for line in f:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        o = json.loads(raw)
                    except json.JSONDecodeError:
                        kept_lines.append(raw)
                        continue
                    try:
                        idx = int(o.get("prompt_index"))
                    except (TypeError, ValueError):
                        kept_lines.append(raw)
                        continue
                    if idx < target_prompt_index:
                        kept_lines.append(raw)
                        kept_rp += 1
                    else:
                        dropped_rp += 1
                        if idx == target_prompt_index:
                            draft = (
                                o.get("prompt_preview")
                                or o.get("prompt_text")
                                or draft
                            )
            with open(rp_path, "w", encoding="utf-8") as f:
                for line in kept_lines:
                    f.write(line + "\n")

        # chat_history: cut at first row whose prompt_index >= target
        if os.path.isfile(chat_path):
            lines = open(chat_path, "r", encoding="utf-8").read().splitlines()
            for i, line in enumerate(lines):
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    pi = o.get("prompt_index")
                    if pi is None:
                        pi = o.get("promptIndex")
                    pi = int(pi) if pi is not None else None
                except (TypeError, ValueError):
                    pi = None
                if pi is None or pi < target_prompt_index:
                    continue
                cut_at = i
                text = self._chat_row_text(o)
                if not draft:
                    draft = self._user_query_body(text) or draft
                break
            # Fallback: no prompt_index fields (rare / heavily compacted) —
            # treat target as Nth real <user_query> only if it fits.
            if cut_at is None:
                user_turns = 0
                for i, line in enumerate(lines):
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if o.get("type") != "user" or o.get("synthetic_reason"):
                        continue
                    text = self._chat_row_text(o)
                    if "<user_query>" not in text:
                        continue
                    if user_turns == target_prompt_index:
                        cut_at = i
                        if not draft:
                            draft = self._user_query_body(text)
                        break
                    user_turns += 1
            if cut_at is not None:
                with open(chat_path, "w", encoding="utf-8") as f:
                    for line in lines[:cut_at]:
                        f.write(line + "\n")

        # updates.jsonl: stream-cut at first _meta.promptIndex >= target
        # (later tool_call rows lack promptIndex and must still drop).
        if os.path.isfile(up_path):
            kept_u: list = []
            cutting = False
            with open(up_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    raw = line.rstrip("\n")
                    if cutting:
                        dropped_up += 1
                        continue
                    pi = self._updates_line_prompt_index(raw)
                    if pi is not None and pi >= target_prompt_index:
                        cutting = True
                        dropped_up += 1
                        continue
                    kept_u.append(raw)
                    kept_up += 1
            with open(up_path, "w", encoding="utf-8") as f:
                for line in kept_u:
                    f.write(line + "\n")

        self.file_log(
            f"client rewind conversation target={target_prompt_index} "
            f"cut_at={cut_at} draft_len={len(draft or '')} "
            f"rp_keep={kept_rp} rp_drop={dropped_rp} "
            f"up_keep={kept_up} up_drop={dropped_up} dir={sdir}")
        return {
            "session_dir": sdir,
            "draft_prompt": draft or "",
            "cut_at": cut_at,
            "files_restored": [],  # conversation only — never project files
            "kept_rp": kept_rp,
            "dropped_rp": dropped_rp,
            "kept_updates": kept_up,
            "dropped_updates": dropped_up,
        }

    @staticmethod
    def _updates_line_prompt_index(line: str):
        """Return _meta.promptIndex from an updates.jsonl line, or None."""
        if "promptIndex" not in line and "prompt_index" not in line:
            return None
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            return None
        u = (o.get("params") or {}).get("update") or {}
        meta = u.get("_meta") or {}
        pi = meta.get("promptIndex")
        if pi is None:
            pi = meta.get("prompt_index")
        if pi is None:
            pi = u.get("promptIndex")
        try:
            return int(pi) if pi is not None else None
        except (TypeError, ValueError):
            return None
