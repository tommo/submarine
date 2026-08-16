"""Host goal skeptic helpers — Task-mode only.

Main session stays the goal flow executor (single sheet).
Plan / worker / reviewer = Task (or spawn_subagent) *under* that session.

Legacy MODE_SESSION sheet spawn is deleted.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

GOAL_ROLE_SKEPTIC = "skeptic"
# Default POC path
MODE_TASK = "task"


def resolve_skeptic_mode(settings: Any = None) -> str:
    """Always Task-mode. Legacy ``session`` sheets are deleted."""
    return MODE_TASK


def is_goal_skeptic(session_like: Any) -> bool:
    role = getattr(session_like, "goal_role", None)
    if role is None:
        ctx = getattr(session_like, "initial_context", None) or {}
        if isinstance(ctx, dict):
            role = ctx.get("goal_role")
    return (role or "").strip().lower() == GOAL_ROLE_SKEPTIC


def parent_view_id_for_skeptic(session_like: Any) -> Optional[int]:
    pvid = getattr(session_like, "parent_view_id", None)
    if pvid is None:
        ctx = getattr(session_like, "initial_context", None) or {}
        if isinstance(ctx, dict):
            pvid = ctx.get("goal_parent_view_id") or ctx.get("parent_view_id")
    if pvid is None:
        return None
    try:
        return int(pvid)
    except (TypeError, ValueError):
        return None


def resolve_verdict_session(
    caller: Any,
    sessions: Dict[Any, Any],
) -> Any:
    """Map MCP caller → session that owns the goal (parent if sheet-skeptic)."""
    if caller is None:
        return None
    if is_goal_skeptic(caller):
        pvid = parent_view_id_for_skeptic(caller)
        if pvid is not None and pvid in sessions:
            return sessions[pvid]
        return None
    return caller


def skeptic_display_name(verify_run: int) -> str:
    return f"goal-skeptic-{int(verify_run or 1)}"


def prepare_task_verify_abort(gt: Any, gap: str, message: str = "") -> bool:
    """Fail-closed prep for Task-mode verify on error/interrupt.

    If ``gt.phase == verifying`` and no tool verdict yet, records
    not-achieved. Returns True when the host should run finish cycle.
    """
    if gt is None:
        return False
    if getattr(gt, "phase", None) != "verifying":
        return False
    if not getattr(gt, "pending_tool_verdict", None):
        gt.record_tool_verdict(
            achieved=False,
            evidence=[],
            gaps=[(gap or "Host: verify aborted")[:200]],
            message=(message or gap or "aborted")[:200],
        )
    return True


def preserve_working_after_verify_finish(finish_started_next: bool) -> bool:
    """True → completion handler must return early (do not force working=False).

    When ``_goal_finish_verify_cycle`` returns True it already started the
    next turn (continuation query). The non-success gate must not clobber
    that busy state.
    """
    return bool(finish_started_next)
