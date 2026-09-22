"""Output data models and constants."""
from __future__ import annotations

import re as _re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from plat.constants import BACKEND_ABBREV

# Status-icon chars that prefix a tab title (see OutputSheet.update_title).
_TITLE_ICON_RE = _re.compile(r'^(?:[◉◇•○◐◓◑◒❓⏸↻⚠✘❌!❗⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏*]\s*)+')


def _title_abbrev_tokens():
    """The set of backend abbrev tokens update_title can emit as `ABBR> `.

    Resolved the same way (registry abbrev → static map → name[:2]) so
    strip_title_decoration stays in sync with decoration and covers custom
    providers, not just the built-ins.
    """
    toks = set()
    try:
        from backend.specs import all_backends, abbrev_for
        for name in all_backends():
            tok = abbrev_for(name)
            if tok:
                toks.add(tok)
    except Exception:
        pass
    for tok in BACKEND_ABBREV.values():
        if tok:
            toks.add(tok)
    return toks


def strip_title_decoration(title):
    """Inverse of OutputSheet.update_title: peel status icons and any number
    of stacked `ABBR> ` prefixes (+ legacy "[x] " / "Claude: " / "Submarine: "
    forms) so re-decorating across reconnects cannot accumulate prefixes.
    Returns the clean base name (empty string if nothing left). Does NOT
    touch a trailing truncation ellipsis — callers that need it handle it.
    """
    if not title:
        return ""
    name = title
    if name.startswith("[") and "] " in name:
        name = name[name.index("] ") + 2:]
    if name.startswith("Claude: "):
        name = name[8:]
    if name.startswith("Submarine: "):
        name = name[11:]
    toks = sorted(_title_abbrev_tokens(), key=len, reverse=True)
    abbr_re = (
        _re.compile(r'^(?:%s)>\s*' % "|".join(_re.escape(t) for t in toks))
        if toks else None
    )
    while True:
        new = _TITLE_ICON_RE.sub('', name)
        if abbr_re:
            new = abbr_re.sub('', new)
        if new == name:
            break
        name = new
    return name.strip()


PENDING = "pending"
DONE = "done"
ERROR = "error"
BACKGROUND = "background"

PERM_ALLOW = "allow"
PERM_DENY = "deny"
PERM_ALLOW_ALL = "allow_all"
PERM_ALLOW_SESSION = "allow_session"

PLAN_APPROVE = "approve"
PLAN_REJECT = "reject"
PLAN_VIEW = "view"

HISTORY_CAP = 20


@dataclass
class PlanApproval:
    """A pending plan approval request."""
    id: int
    plan_file: str
    allowed_prompts: List[dict]
    callback: Callable[[str], None]
    region: Optional[tuple] = None
    button_regions: Dict[str, tuple] = field(default_factory=dict)

    def descriptor(self) -> dict:
        return {
            "kind": "plan",
            "payload": {
                "id": self.id,
                "plan_file": self.plan_file,
                "allowed_prompts": list(self.allowed_prompts or []),
            },
        }


@dataclass
class PermissionRequest:
    """A pending permission request."""
    id: int
    tool: str
    tool_input: dict
    callback: Callable[[str], None]
    region: Optional[tuple] = None
    button_regions: Dict[str, tuple] = field(default_factory=dict)
    created_at: float = 0.0

    def descriptor(self) -> dict:
        return {
            "kind": "permission",
            "payload": {
                "id": self.id,
                "tool": self.tool,
                "tool_input": dict(self.tool_input or {}),
            },
        }


@dataclass
class QuestionRequest:
    """A pending inline question request."""
    qid: int
    questions: List[dict]
    current_idx: int = 0
    answers: Dict[str, Any] = field(default_factory=dict)
    callback: Callable = None
    region: Optional[tuple] = None
    button_regions: Dict[str, tuple] = field(default_factory=dict)
    selected: set = field(default_factory=set)

    def descriptor(self) -> dict:
        return {
            "kind": "question",
            "payload": {
                "qid": self.qid,
                "questions": list(self.questions or []),
                "current_idx": int(self.current_idx or 0),
                "answers": dict(self.answers or {}),
                "selected": sorted(self.selected or []),
            },
        }


@dataclass
class ToolCall:
    """A single tool call."""
    name: str
    tool_input: dict
    status: str = PENDING
    result: Optional[str] = None
    id: Optional[str] = None


@dataclass
class ArtifactCard:
    """Compact transcript row for an artifact write/edit.

    Card update rule: every write/edit appends a new card (timeline).
    User (external) saves are journaled only and do not emit a card.
    """
    path: str
    name: str
    bytes: int = 0
    summary: str = ""
    title: Optional[str] = None

    def line(self) -> str:
        try:
            from core.artifacts import format_card_line
            return format_card_line(self.name, self.bytes, self.summary)
        except Exception:
            size = "%s B" % int(self.bytes or 0)
            summ = (self.summary or "").replace("\n", " ").strip()
            if summ:
                return "📄 %s — %s — %s        [open] [path]\n" % (
                    self.name, size, summ)
            return "📄 %s — %s        [open] [path]\n" % (self.name, size)


@dataclass
class TodoItem:
    """A todo item from TodoWrite or Task* tools."""
    content: str
    status: str
    id: str = ""


_TODO_CLOSED = frozenset({
    "completed", "cancelled", "canceled", "deleted",
})


def _todo_status_norm(status: str) -> str:
    """Normalize Claude / Grok / Kimi todo status strings to a common set."""
    s = (status or "pending").strip().lower().replace("-", "_").replace(" ", "_")
    if s in ("done", "complete", "finished"):
        return "completed"
    if s in ("inprogress",):
        return "in_progress"
    if s in ("todo", "open", "not_started"):
        return "pending"
    return s or "pending"


def _todo_is_open(todo: "TodoItem") -> bool:
    if not (todo.content or "").strip():
        return False
    return _todo_status_norm(todo.status) not in _TODO_CLOSED


def _todo_is_active(todo: "TodoItem") -> bool:
    return _todo_status_norm(todo.status) == "in_progress"


def _todo_is_completed(todo: "TodoItem") -> bool:
    return _todo_status_norm(todo.status) == "completed"


def _open_todos(todos: list) -> list:
    return [t for t in (todos or []) if _todo_is_open(t)]


@dataclass
class GoalState:
    """Host-owned goal strip snapshot (from GoalTracker, not tool invention)."""
    status: str = "active"
    message: str = ""
    blocked_reason: str = ""
    objective: str = ""
    phase: str = "idle"
    pause_message: str = ""
    token_budget: Optional[int] = None
    tokens_used: Optional[int] = None
    verify_runs: int = 0
    verify_max: int = 0
    gaps: List[str] = field(default_factory=list)
    verifying: bool = False
    planning: bool = False
    goal_id: str = ""


def _goal_is_open(goal: Optional["GoalState"]) -> bool:
    if not goal:
        return False
    return goal.status in (
        "active", "user_paused", "blocked", "budget_limited", "infra_paused",
    )


def _ui_phase_label(status="", phase="", gaps=None):
    st = (status or "").strip()
    ph = (phase or "").strip()
    if ph == "verifying":
        return "verifying"
    if ph == "planning":
        return "planning"
    if ph == "executing":
        return "executing"
    if st == "blocked":
        return "blocked"
    if st == "budget_limited":
        return "budget"
    if st in ("user_paused", "infra_paused"):
        return "paused"
    if st == "complete":
        return "complete"
    if st == "active":
        return "active"
    return st or "idle"


def _ui_phase_body(
        status="", phase="", message="", objective="",
        pause_message="", blocked_reason="", gaps=None):
    ph = (phase or "").strip()
    msg = (message or "").strip()
    obj = (objective or "").strip()
    if (pause_message or "").strip():
        return pause_message.strip()
    if (blocked_reason or "").strip():
        return blocked_reason.strip()
    gs = [g.strip() for g in (gaps or []) if (g or "").strip()]
    if gs and ph != "verifying" and status in (
            "active", "user_paused", "blocked", "budget_limited", "infra_paused"):
        if len(gs) == 1:
            return gs[0]
        return gs[0] + " (+%d more)" % (len(gs) - 1)
    if msg and msg != obj:
        low = msg.lower()
        if low not in (
                "plan accepted — executing", "plan accepted", "executing"):
            return msg
    if ph == "verifying":
        return msg or ""
    return ""


def _clip_goal(text, n):
    t = (text or "").replace("\n", " ").strip()
    if len(t) <= n:
        return t
    if n <= 1:
        return "…"
    low = t.lower()
    if low.startswith(("don't ", "do not ", "never ", "without ")):
        return "…" + t[-(n - 1):]
    return t[: max(0, n - 1)] + "…"


def format_goal_strip_line(
        status="", phase="", message="", objective="",
        pause_message="", blocked_reason="", gaps=None,
        token_budget=None, tokens_used=None,
        verify_runs=0, verify_max=0, compact=False):
    """One compact sticky work-strip line. Uses ◆ (not ◎)."""
    try:
        from features.goals.tracker import format_goal_strip_line as _fn
        return _fn(
            status=status, phase=phase, message=message,
            objective=objective, pause_message=pause_message,
            blocked_reason=blocked_reason, gaps=gaps,
            token_budget=token_budget, tokens_used=tokens_used,
            verify_runs=verify_runs, verify_max=verify_max,
            compact=compact,
        )
    except Exception:
        pass
    label = _ui_phase_label(status=status, phase=phase, gaps=gaps)
    obj = _clip_goal(objective, 36 if compact else 44)
    body = _ui_phase_body(
        status=status, phase=phase, message=message,
        objective=objective, pause_message=pause_message,
        blocked_reason=blocked_reason, gaps=gaps,
    )
    body = _clip_goal(body, 56 if compact else 72)
    if body and obj and body.lower() == obj.lower():
        body = ""
    line = "  ◆ goal · %s" % label
    if obj:
        line += " · %s" % obj
    if body:
        line += "  — %s" % body
    extra = []
    if token_budget:
        extra.append("~%s/%s" % (tokens_used or 0, token_budget))
    if verify_max:
        extra.append("v%s/%s" % (verify_runs or 0, verify_max))
    if extra:
        line += "  (%s)" % " · ".join(extra)
    return line


def goal_strip_label(goal: Optional["GoalState"]) -> str:
    if not goal:
        return ""
    try:
        from features.goals.tracker import ui_phase_label
        fn = ui_phase_label
    except Exception:
        fn = _ui_phase_label
    phase = goal.phase or ""
    if goal.verifying:
        phase = "verifying"
    elif goal.planning and phase != "verifying":
        phase = "planning"
    return fn(
        status=goal.status or "",
        phase=phase,
        gaps=list(goal.gaps or []),
    )


def goal_strip_body(goal: Optional["GoalState"]) -> str:
    if not goal:
        return ""
    try:
        from features.goals.tracker import ui_phase_body
        fn = ui_phase_body
    except Exception:
        fn = _ui_phase_body
    phase = goal.phase or ""
    if goal.verifying:
        phase = "verifying"
    return fn(
        status=goal.status or "",
        phase=phase,
        message=goal.message or "",
        objective=goal.objective or "",
        pause_message=goal.pause_message or "",
        blocked_reason=goal.blocked_reason or "",
        gaps=list(goal.gaps or []),
    )


@dataclass
class Conversation:
    """A single prompt + tools + response + meta."""
    prompt: str = ""
    events: List = field(default_factory=list)
    todos: List[TodoItem] = field(default_factory=list)
    todos_all_done: bool = False
    goal: Optional[GoalState] = None
    working: bool = True
    duration: float = 0.0
    has_meta: bool = False
    usage: dict = None
    region: Optional[tuple] = (0, 0)  # buffer span; None while detached
    context_names: List[str] = field(default_factory=list)
    context_refs: List[dict] = field(default_factory=list)
    # Who ran this turn: (provider label, model, effort), taken from the
    # session when the turn ends. The @done line renders from this, never
    # from the view's stamps — in single-view mode the host view carries
    # whichever session is bound now, so old turns (and turns of a session
    # that finished while another was on screen) showed a foreign provider.
    identity: Optional[tuple] = None

    @property
    def tools(self) -> List[ToolCall]:
        return [e for e in self.events if isinstance(e, ToolCall)]

    @property
    def text_chunks(self) -> List[str]:
        return [e for e in self.events if isinstance(e, str)]
