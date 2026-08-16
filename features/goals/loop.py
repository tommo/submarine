"""Goal drive loop — attaches to a core Session via on_turn_end + query.

Planner → accept → implementer → verify → complete. Host owns complete
(structured verdict only). Host-state reject must not re-enter the planner.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from . import plan as goal_plan
from . import prompts as goal_prompts
from .skeptic import (
    MODE_TASK,
    is_goal_skeptic,
    parent_view_id_for_skeptic,
    prepare_task_verify_abort,
    preserve_working_after_verify_finish,
)
from .tracker import GoalTracker, parse_goal_slash


def attach_goal_harness(session: Any) -> Any:
    """Bind a GoalTracker + drive-loop methods onto a core Session."""
    if getattr(session, "_goal_harness_attached", False):
        return session
    session._goal_harness_attached = True

    if getattr(session, "goal_tracker", None) is None:
        session.goal_tracker = GoalTracker()
    session._goal_planning_turn = False
    session._goal_verify_awaiting = False
    session._goal_verify_mode = None
    session._goal_skeptic_view_id = None
    session._goal_skip_continue_once = False
    session._goal_last_update_sig = None
    session._goal_verify_turn = False

    _restore_goal(session)

    session.handle_goal_command = lambda args, s=session: handle_goal_command(s, args)
    session.apply_goal_update = lambda **kw: apply_goal_update(session, **kw)
    session.apply_goal_verdict = lambda **kw: apply_goal_verdict(session, **kw)
    session.sync_goal_ui = lambda s=session: sync_goal_ui(s)
    session._goal_fire_verifier = lambda claim, s=session: _goal_fire_verifier(s, claim)
    session._goal_finish_verify_cycle = lambda s=session: _goal_finish_verify_cycle(s)
    session._goal_abort_task_verify = (
        lambda gap, message="", s=session: _goal_abort_task_verify(s, gap, message)
    )
    session._goal_on_turn_success = lambda s=session: _goal_on_turn_success(s)
    session.cancel_scheduled_loop = getattr(
        session, "cancel_scheduled_loop", None
    ) or (lambda s=session: _cancel_scheduled_loop(s))

    session.on_turn_end.append(lambda s, completion: _on_turn_end(s, completion))
    session.on_saved.append(lambda s: _persist_goal(s))

    _wrap_interrupt(session)
    _wrap_clear(session)
    return session


def _restore_goal(session: Any) -> None:
    raw = getattr(session, "_saved_goal_json", None)
    if not raw:
        sid = session.session_id or session.resume_id
        store = getattr(session, "store", None)
        if sid and store is not None and hasattr(store, "find"):
            try:
                entry = store.find(sid) or {}
                raw = entry.get("goal")
            except Exception:
                raw = None
    if raw and isinstance(raw, dict):
        try:
            session.goal_tracker = GoalTracker.from_json(raw)
        except Exception:
            pass


def _persist_goal(session: Any) -> None:
    gt = getattr(session, "goal_tracker", None)
    if gt is None or not getattr(session, "session_id", None):
        return
    store = getattr(session, "store", None)
    if store is None or not hasattr(store, "find"):
        return
    try:
        entry = store.find(session.session_id)
        if not entry:
            return
        if gt.goal_id:
            entry["goal"] = gt.to_json()
        else:
            entry.pop("goal", None)
        store.upsert(entry)
    except Exception:
        pass


def _wrap_interrupt(session: Any) -> None:
    orig = session.interrupt

    def interrupt(break_channel=True, s=session, _orig=orig):
        try:
            gt = getattr(s, "goal_tracker", None)
            if gt is not None and gt.is_active():
                gt.pause("user", "Interrupted by user")
                s._goal_skip_continue_once = True
                s._goal_verify_turn = False
                s._goal_planning_turn = False
                sync_goal_ui(s)
        except Exception:
            pass
        return _orig(break_channel=break_channel)

    session.interrupt = interrupt


def _wrap_clear(session: Any) -> None:
    orig = session.clear_conversation

    def clear_conversation(s=session, _orig=orig):
        gt = getattr(s, "goal_tracker", None)
        if gt is not None:
            try:
                gt.clear()
            except Exception:
                s.goal_tracker = GoalTracker()
        s._goal_verify_turn = False
        s._goal_verify_awaiting = False
        s._goal_planning_turn = False
        s._goal_skip_continue_once = False
        try:
            sync_goal_ui(s)
        except Exception:
            pass
        return _orig()

    session.clear_conversation = clear_conversation


def _context_tokens_k(session: Any) -> Optional[int]:
    fn = getattr(session, "_context_tokens_k", None)
    if callable(fn):
        try:
            return fn()
        except Exception:
            pass
    usage = getattr(session, "context_usage", None) or {}
    if not isinstance(usage, dict):
        return None
    for key in ("input_tokens", "inputTokens", "total_tokens", "totalTokens"):
        if usage.get(key) is not None:
            try:
                return int(usage[key]) // 1000
            except (TypeError, ValueError):
                continue
    return None


def _goal_project_root(session: Any) -> Optional[str]:
    try:
        window = getattr(session, "window", None)
        folders = window.folders() if window else None
        if folders:
            return folders[0]
    except Exception:
        pass
    cwd = getattr(session, "cwd", None) or ""
    if cwd:
        return cwd
    fn = getattr(session, "_default_cwd", None)
    if callable(fn):
        try:
            return fn()
        except Exception:
            pass
    return None


def _ensure_idle(session: Any, reason: str = "") -> None:
    fn = getattr(session, "_ensure_idle_input", None) or getattr(
        session, "_enter_input_if_idle", None
    )
    if callable(fn):
        try:
            fn()
        except Exception:
            pass


def _sessions_map() -> dict:
    try:
        import sublime  # type: ignore
        for attr in ("_submarine_sessions", "_claude_sessions"):
            reg = getattr(sublime, attr, None)
            if isinstance(reg, dict):
                return reg
    except Exception:
        pass
    return {}


def _cancel_scheduled_loop(session: Any) -> None:
    try:
        from features.scheduler import cancel_loop
        cancel_loop(session)
        return
    except Exception:
        pass
    try:
        session.is_looping = False
        session.next_wake_at = None
        session._send("cancel_loop", {})
        session.chrome.wakeup_banner(None)
    except Exception:
        pass


def sync_goal_ui(session: Any) -> None:
    gt = getattr(session, "goal_tracker", None)
    output = getattr(session, "output", None)
    if output is None:
        return
    try:
        from ui.models import GoalState, _goal_is_open
    except ImportError:
        GoalState = None  # type: ignore
        _goal_is_open = None  # type: ignore
    current = getattr(output, "current", None)
    if not gt or not gt.is_open():
        if current is not None:
            current.goal = None
        _refresh_output(output)
        return
    if GoalState is None:
        _refresh_output(output)
        return
    snap = gt.to_ui_dict()
    gs = GoalState(
        status=snap["status"],
        message=snap.get("message") or "",
        blocked_reason=snap.get("blocked_reason") or "",
        objective=snap.get("objective") or "",
        phase=snap.get("phase") or "idle",
        pause_message=snap.get("pause_message") or "",
        token_budget=snap.get("token_budget"),
        tokens_used=snap.get("tokens_used"),
        verify_runs=int(snap.get("verify_runs") or 0),
        verify_max=int(snap.get("verify_max") or 0),
        gaps=list(snap.get("gaps") or []),
        verifying=bool(snap.get("verifying")),
        planning=bool(snap.get("planning")),
        goal_id=snap.get("goal_id") or "",
    )
    if current is not None:
        current.goal = gs if _goal_is_open(gs) else None
    _refresh_output(output)


def _refresh_output(output: Any) -> None:
    try:
        if getattr(output, "current", None) is None:
            return
        if output.is_input_mode() and hasattr(output, "refresh_preserving_input"):
            output.refresh_preserving_input()
        elif hasattr(output, "_render_current"):
            output._render_current()
    except Exception:
        pass


def apply_goal_update(
    session: Any,
    message: str = "",
    completed: bool = False,
    blocked_reason: str = "",
) -> dict:
    gt = getattr(session, "goal_tracker", None)
    if gt is None:
        session.goal_tracker = GoalTracker()
        gt = session.goal_tracker
    sig = (
        (message or "").strip(),
        bool(completed),
        (blocked_reason or "").strip(),
        int(getattr(session, "query_count", 0) or 0),
    )
    if getattr(session, "_goal_last_update_sig", None) == sig:
        sync_goal_ui(session)
        return {
            "ok": True,
            "deduped": True,
            "status": gt.status,
            "summary": "already applied",
        }
    session._goal_last_update_sig = sig
    if is_goal_skeptic(session):
        return {
            "ok": False,
            "error": "Goal skeptic cannot call update_goal; use goal_verdict.",
            "rejected": True,
        }
    if getattr(session, "_goal_verify_turn", False) and completed:
        return {
            "ok": False,
            "error": "Verifier turn cannot call update_goal(completed=true).",
            "rejected": True,
        }
    result = gt.apply_update(
        message=message or "",
        completed=bool(completed),
        blocked_reason=blocked_reason or "",
        mid_turn=True,
    )
    try:
        _goal_refresh_tokens(session)
    except Exception:
        pass
    sync_goal_ui(session)
    return result


def apply_goal_verdict(
    session: Any,
    achieved: bool = False,
    evidence=None,
    gaps=None,
    message: str = "",
) -> dict:
    if is_goal_skeptic(session):
        pvid = parent_view_id_for_skeptic(session)
        parent = _sessions_map().get(pvid) if pvid is not None else None
        if parent is None:
            return {
                "ok": False,
                "error": "Skeptic parent session not found for goal_verdict.",
                "rejected": True,
            }
        return apply_goal_verdict(
            parent,
            achieved=achieved,
            evidence=evidence,
            gaps=gaps,
            message=message,
        )
    gt = getattr(session, "goal_tracker", None)
    if gt is None:
        return {"ok": False, "error": "No goal tracker.", "rejected": True}
    if getattr(session, "quick_mode", False):
        return {
            "ok": False,
            "error": "goal_verdict is not for Quick Agent.",
            "rejected": True,
        }
    if not hasattr(gt, "record_tool_verdict"):
        return {
            "ok": False,
            "error": (
                "GoalTracker.record_tool_verdict missing — soft-reload left a "
                "stale tracker class. Reload the Submarine package and re-open."
            ),
            "rejected": True,
        }

    def _as_list(v):
        if v is None:
            return []
        if isinstance(v, str):
            return [ln.strip().lstrip("-* ") for ln in v.splitlines() if ln.strip()]
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if str(x).strip()]
        return [str(v).strip()] if str(v).strip() else []

    return gt.record_tool_verdict(
        achieved=bool(achieved),
        evidence=_as_list(evidence),
        gaps=_as_list(gaps),
        message=message or "",
    )


def handle_goal_command(session: Any, args: str) -> None:
    if getattr(session, "quick_mode", False):
        try:
            session.output.text("\n*Goal mode is not available in Quick Agent.*\n")
        except Exception:
            pass
        _ensure_idle(session, "goal/quick")
        return

    action, payload = parse_goal_slash(args)
    gt = session.goal_tracker

    if action == "status":
        session.output.text("\n" + gt.status_summary() + "\n")
        sync_goal_ui(session)
        _ensure_idle(session, "goal/status")
        return

    if action == "pause":
        if gt.is_active():
            gt.pause("user", "Paused via /goal pause")
            session.output.text("\n*Goal paused.*\n")
        else:
            session.output.text("\n*" + gt.status_summary() + "*\n")
        sync_goal_ui(session)
        _ensure_idle(session, "goal/pause")
        return

    if action == "clear":
        old_path = (gt.plan_path or "").strip()
        gt.clear()
        session._goal_verify_turn = False
        session._goal_verify_awaiting = False
        session.output.text("\n*Goal cleared.*\n")
        if old_path:
            session.output.text("  (plan was: %s)\n" % old_path)
        sync_goal_ui(session)
        _ensure_idle(session, "goal/clear")
        return

    if action == "resume":
        if not gt.is_open():
            session.output.text("\n*No goal to resume. /goal <objective>*\n")
            _ensure_idle(session, "goal/resume-empty")
            return
        if gt.status in ("complete", "cleared"):
            session.output.text("\n*Goal already finished. Start a new /goal.*\n")
            _ensure_idle(session, "goal/resume-done")
            return
        gt.resume()
        if gt.phase == "planning" or not gt.has_plan():
            session._goal_planning_turn = True
            if not (gt.plan_path or "").strip():
                gt.plan_path = goal_plan.default_plan_path(
                    _goal_project_root(session), gt.goal_id)
            sync_goal_ui(session)
            session.query(
                goal_prompts.planner_kickoff(gt, plan_path=gt.plan_path or ""),
                display_prompt="/goal resume (plan)",
            )
            return
        sync_goal_ui(session)
        session.query(
            goal_prompts.resume_recap(gt),
            display_prompt="/goal resume",
        )
        return

    objective, budget = payload
    baseline = 0
    try:
        k = _context_tokens_k(session)
        if k is not None:
            baseline = k * 1000
    except Exception:
        pass
    gt.create(objective, token_budget=budget, tokens_baseline=baseline)
    session._goal_verify_turn = False
    session._goal_skip_continue_once = False
    session._goal_planning_turn = True
    try:
        gt.plan_path = goal_plan.default_plan_path(
            _goal_project_root(session), gt.goal_id)
    except Exception:
        pass
    sync_goal_ui(session)
    disp = "/goal %s" % objective
    if budget:
        disp += " --budget %s" % budget
    session.output.text(
        "\n*Goal planning* — write a concrete plan to "
        "`%s` (host rejects templates).\n"
        % (gt.plan_path or ".claude/goals/…/plan.md")
    )
    session.query(
        goal_prompts.planner_kickoff(gt, plan_path=gt.plan_path or ""),
        display_prompt=disp,
    )


def _goal_refresh_tokens(session: Any) -> None:
    gt = getattr(session, "goal_tracker", None)
    if not gt or not gt.is_open():
        return
    k = _context_tokens_k(session)
    if k is not None:
        gt.set_tokens_used(k * 1000)
    gt.enforce_budget()


def _goal_try_accept_plan_from_disk(session: Any) -> dict:
    gt = session.goal_tracker
    if not gt or not gt.goal_id:
        return {"ok": False, "error": "no goal"}
    path = (gt.plan_path or "").strip() or goal_plan.default_plan_path(
        _goal_project_root(session), gt.goal_id)
    gt.plan_path = path
    body = ""
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                body = f.read()
        except Exception as e:
            return {"ok": False, "error": "read plan failed: %s" % e, "path": path}
    if not body.strip():
        return {
            "ok": False,
            "error": "Plan file missing or empty: %s" % path,
            "path": path,
            "issues": ["write plan.md with required sections"],
        }
    issues = goal_plan.plan_quality_issues(body, gt.objective)
    if issues:
        return {"ok": False, "error": "quality", "issues": issues, "path": path}
    ack = gt.accept_plan(body, plan_path=path)
    if not ack.get("ok"):
        return {
            "ok": False,
            "error": ack.get("error") or "accept_plan failed",
            "issues": ack.get("issues") or [ack.get("error") or "accept failed"],
            "path": path,
            "host_state": bool(ack.get("host_state")),
        }
    return {"ok": True, "path": path}


def _goal_abort_task_verify(session: Any, gap: str, message: str = "") -> bool:
    gt = getattr(session, "goal_tracker", None)
    if not prepare_task_verify_abort(gt, gap, message=message):
        return False
    session._goal_verify_awaiting = False
    started = bool(_goal_finish_verify_cycle(session))
    try:
        sync_goal_ui(session)
    except Exception:
        pass
    return preserve_working_after_verify_finish(started)


def _on_turn_end(session: Any, completion: str) -> None:
    if getattr(session, "quick_mode", False):
        return
    if completion == "interrupted":
        # Esc already paused in interrupt wrap; skip auto-continue/re-plan.
        return
    if completion == "error":
        if (
            getattr(session, "_goal_verify_awaiting", False)
            and (getattr(session, "_goal_verify_mode", None) or "task") == MODE_TASK
        ):
            _goal_abort_task_verify(
                session, "Host: verify turn error", message="verify error")
        return
    if completion != "success":
        return
    _goal_on_turn_success(session)


def _goal_on_turn_success(session: Any) -> bool:
    if is_goal_skeptic(session):
        return False

    gt = getattr(session, "goal_tracker", None)
    if not gt or getattr(session, "quick_mode", False):
        return False

    if getattr(session, "_goal_verify_awaiting", False):
        session._goal_verify_awaiting = False
        return _goal_finish_verify_cycle(session)

    if gt.phase == "verifying" and gt.is_active():
        return _goal_finish_verify_cycle(session)

    if getattr(session, "_goal_skip_continue_once", False):
        session._goal_skip_continue_once = False
        session._goal_planning_turn = False
        return False

    if gt.phase == "planning" or getattr(session, "_goal_planning_turn", False):
        session._goal_planning_turn = False
        if gt.is_open() and not gt.is_active() and not gt.has_plan():
            if gt.status in ("user_paused", "infra_paused"):
                gt.resume()
                sync_goal_ui(session)
        result = _goal_try_accept_plan_from_disk(session)
        sync_goal_ui(session)
        if result.get("ok"):
            session.output.text(
                "\n*Plan accepted* — `%s`\n*Executing…*\n" % result.get("path")
            )
            session.query(
                goal_prompts.implementer_kickoff(gt),
                display_prompt="↻ goal execute",
            )
            return True
        issues = result.get("issues") or [result.get("error") or "plan invalid"]
        err = str(result.get("error") or "")
        host_state = bool(result.get("host_state")) or any(
            "not active" in str(i).lower()
            or "host state" in str(i).lower()
            or "no open goal" in str(i).lower()
            for i in issues
        ) or "not active" in err.lower() or "no open goal" in err.lower()
        detail = "; ".join(str(i) for i in issues[:6])
        path = result.get("path") or gt.plan_path or ""
        if host_state:
            session.output.text(
                "\n*Plan accept blocked* (host state, not schema) — %s\n"
                "  path: `%s`\n"
                "  Use `/goal resume` (or start `/goal …` again), then continue.\n"
                % (detail, path)
            )
            sync_goal_ui(session)
            return False
        session.output.text(
            "\n*Plan rejected* (schema/quality gate) — %s\n"
            "  path: `%s`\n"
            "  Host re-sends full plan.md schema to the planner.\n"
            % (detail, path)
        )
        session._goal_planning_turn = True
        session.query(
            goal_prompts.planner_revise(
                gt, issues, plan_path=gt.plan_path or ""),
            display_prompt="↻ goal re-plan",
        )
        return True

    if getattr(session, "_goal_verify_turn", False):
        session._goal_verify_turn = False
        return _goal_finish_verify_cycle(session)

    try:
        _goal_refresh_tokens(session)
    except Exception:
        pass
    if gt.enforce_budget():
        sync_goal_ui(session)
        session.output.text("\n*Goal paused: token budget reached.*\n")
        return False

    if not gt.is_active():
        sync_goal_ui(session)
        return False

    if gt.has_pending_complete():
        claim = gt.begin_verify()
        sync_goal_ui(session)
        if claim is None:
            session.output.text("\n*Goal paused: verification cap.*\n")
            sync_goal_ui(session)
            return False
        return _goal_fire_verifier(session, claim)

    if gt.should_continue():
        return _goal_fire_continuation(session)
    return False


def _goal_fire_verifier(session: Any, claim: str) -> bool:
    gt = session.goal_tracker
    session._goal_verify_mode = MODE_TASK
    session._goal_verify_turn = False
    session._goal_skeptic_view_id = None
    prompt = goal_prompts.executor_verify_prompt(
        gt.objective,
        claim,
        gaps_prior=gt.gaps or None,
        plan_body=getattr(gt, "plan_body", "") or "",
    )
    session._goal_verify_awaiting = True
    n = gt.verify_runs
    cap = gt.verify_max
    session.output.text(
        "\n*Goal · verifying* (%s/%s) — stay the **executor** on this "
        "sheet; spawn one Task/Agent **reviewer** (not a new session). "
        "Complete unlocks only via `goal_verdict`.\n" % (n, cap)
    )
    sync_goal_ui(session)
    session.query(prompt, display_prompt="↻ goal verify")
    return True


def _goal_apply_verifier_result(session: Any) -> None:
    gt = session.goal_tracker
    if gt.status == "complete":
        try:
            gt._push("verify_applied", "already_complete")
        except Exception:
            pass
        return

    source = "none"
    achieved = False
    gaps = []
    detail = gt.message
    tool_v = None
    if hasattr(gt, "take_tool_verdict"):
        tool_v = gt.take_tool_verdict()
        if tool_v:
            source = "tool"
    if not tool_v and hasattr(gt, "try_load_verdict_file"):
        try:
            file_rec = gt.try_load_verdict_file()
            if file_rec and file_rec.get("ok"):
                tool_v = (
                    gt.take_tool_verdict()
                    if hasattr(gt, "take_tool_verdict") else None
                )
                if not tool_v and file_rec.get("recorded") is not False:
                    tool_v = {
                        "achieved": file_rec.get("achieved"),
                        "evidence": [],
                        "gaps": file_rec.get("gaps") or [],
                        "message": file_rec.get("message") or "",
                    }
                if tool_v:
                    source = "verdict_file"
        except Exception:
            pass
    if tool_v:
        achieved = bool(tool_v.get("achieved"))
        gaps = list(tool_v.get("gaps") or [])
        detail = (tool_v.get("message") or detail or "").strip() or detail
    else:
        source = "no_tool"
        achieved = False
        gaps = list(gt.gaps or [])
        if not gaps:
            gaps = [
                "Host: no goal_verdict / VERDICT.json — complete stays locked "
                "(MCP goal_verdict or evidence/VERDICT.json next to plan.md)",
            ]

    gt.apply_verdict(achieved, gaps=gaps, detail=detail or gt.message)
    try:
        gt._push(
            "verify_applied",
            ("achieved" if achieved else "not_achieved") + " via %s" % source,
        )
    except Exception:
        pass


def _goal_finish_verify_cycle(session: Any) -> bool:
    gt = session.goal_tracker
    session._goal_verify_awaiting = False
    session._goal_skeptic_view_id = None
    session._goal_verify_turn = False
    _goal_apply_verifier_result(session)
    session._goal_verify_mode = None
    sync_goal_ui(session)
    if gt.status == "complete":
        try:
            if getattr(session, "is_looping", False):
                _cancel_scheduled_loop(session)
        except Exception:
            pass
        session.output.text(
            "\n*Goal · complete* — verified via structured verdict.\n")
        sync_goal_ui(session)
        try:
            session.output.set_name(session.display_name)
        except Exception:
            pass
        return False
    if gt.phase == "verifying":
        gt.phase = "executing"
    gaps = list(gt.gaps or [])
    if gaps:
        shown = "; ".join(gaps[:3])
        more = " (+%s more)" % (len(gaps) - 3) if len(gaps) > 3 else ""
        session.output.text(
            "\n*Goal · not verified* — back to **executing**. Gaps: %s%s\n"
            % (shown, more)
        )
    else:
        session.output.text(
            "\n*Goal · not verified* — back to **executing** "
            "(no `goal_verdict` or empty evidence).\n"
        )
    sync_goal_ui(session)
    if not gt.should_continue():
        return False
    return _goal_fire_continuation(session)


def _goal_fire_continuation(session: Any) -> bool:
    gt = session.goal_tracker
    if gt.phase == "planning" or not gt.has_plan():
        return False
    if not gt.should_continue():
        if gt.is_active() and gt.continue_count >= gt.continue_max:
            gt.pause(
                "user",
                "Continue cap (%s) without verified complete." % gt.continue_max,
            )
            sync_goal_ui(session)
            session.output.text("\n*Goal paused: continue cap.*\n")
        return False
    gt.note_continue()
    if not gt.is_active():
        sync_goal_ui(session)
        return False
    sync_goal_ui(session)
    session.query(
        goal_prompts.continuation_directive(gt),
        display_prompt="↻ goal",
    )
    return True
