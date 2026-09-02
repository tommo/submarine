"""Host-owned turn state. One busy bit, every busy kind has a closer.

working/busy is true only when a known closer exists:
  live          — query() sent session/prompt; closer is the prompt RPC
  compacting    — /compact RPC returned (Kimi); closer is compact-done text
                  or the 180s timeout
  rewinding     — undo restart; closer is start() finishing
  interrupting  — Esc in flight; closer is cancel ACK / settle

Inbound leftovers (text, thinking, synth Bash, task_notification) never
enter a busy kind. That was ◎ ⚙ task completed: working=True, no RPC, no end.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .ports import Scheduler

Kind = Literal["idle", "live", "compacting", "interrupting", "rewinding"]
Inbound = Literal["drop", "paint", "paint_bg"]
Notify = Literal["hold", "surface", "query"]

# Agents that inject/auto-continue on bg complete AND emit session/update
# without a new host query(). Host query() would double the turn.
# Kimi: check_recovery.py — no session/update without a live prompt.
# Grok: after wait_for_exit the agent keeps sending tool_call.
# Own that with resume_stream; closer is `_x.ai/session/prompt_complete`
# / turn_completed for synthetic prompt ids.
_SELF_WAKE_BACKENDS = frozenset({"grok"})

COMPACT_TIMEOUT_MS = 180000
INTERRUPT_SETTLE_MS = (450, 900, 1600)


@dataclass
class TurnState:
    kind: Kind = "idle"
    gen: int = 0
    awaiting_rpc: bool = False

    @property
    def busy(self) -> bool:
        # interrupting stays busy until cancel ACK — otherwise Esc + bg
        # task looks idle while the agent is still streaming.
        return self.kind in ("live", "compacting", "rewinding", "interrupting")

    @property
    def working(self) -> bool:
        return self.busy

    def begin_query(self) -> int:
        self.kind = "live"
        self.gen += 1
        self.awaiting_rpc = True
        return self.gen

    def begin_rewind(self) -> int:
        self.kind = "rewinding"
        self.gen += 1
        self.awaiting_rpc = False
        return self.gen

    def enter_compacting(self) -> None:
        if self.kind == "live":
            self.kind = "compacting"
            self.awaiting_rpc = False

    def finish_compact(self) -> bool:
        if self.kind != "compacting":
            return False
        self.kind = "idle"
        self.awaiting_rpc = False
        return True

    def end_live(self, gen: Optional[int] = None) -> bool:
        if gen is not None and gen != self.gen:
            return False
        if self.kind == "compacting":
            return False
        if self.kind in ("live", "rewinding"):
            self.kind = "idle"
            self.awaiting_rpc = False
            return True
        return False

    def begin_interrupt(self) -> bool:
        if self.kind == "idle":
            return False
        self.kind = "interrupting"
        self.awaiting_rpc = False
        return True

    def settle_interrupt(self) -> None:
        if self.kind == "interrupting":
            self.kind = "idle"
            self.awaiting_rpc = False

    def resume_stream(self) -> None:
        """Agent kept outputting after cancel ACK. Own busy until a closer."""
        if self.kind in ("live", "compacting", "rewinding"):
            return
        self.kind = "live"
        self.awaiting_rpc = False

    def inbound_action(self, event: str) -> Inbound:
        """What leftover/stream events may do. Never begins a turn."""
        if event == "thinking":
            return "drop"
        if self.kind == "interrupting":
            return "paint"
        if self.busy:
            return "paint"
        if event in ("synth_bash", "tool_use_bg"):
            return "paint_bg"
        return "paint"

    def notify_action(self, backend: str) -> Notify:
        if self.busy or self.kind == "interrupting":
            return "hold"
        if (backend or "") in _SELF_WAKE_BACKENDS:
            return "surface"
        return "query"

    def should_queue_prompt(self) -> bool:
        return self.busy or self.kind == "interrupting"


class TurnController:
    """Owns TurnState as the session's only busy flag.

    Scheduler tokens cannot be cancelled; compact timeout and interrupt
    settle use generation guards (old sublime.set_timeout pattern).
    """

    def __init__(
        self,
        scheduler: Optional["Scheduler"] = None,
        on_compact_timeout: Optional[Callable[[], None]] = None,
        on_interrupt_settle: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.state = TurnState()
        self.scheduler = scheduler
        self.on_compact_timeout = on_compact_timeout
        self.on_interrupt_settle = on_interrupt_settle
        self._interrupt_gen = 0
        self._compact_armed_gen = 0

    @property
    def kind(self) -> Kind:
        return self.state.kind

    @property
    def gen(self) -> int:
        return self.state.gen

    @property
    def awaiting_rpc(self) -> bool:
        return self.state.awaiting_rpc

    @awaiting_rpc.setter
    def awaiting_rpc(self, value: bool) -> None:
        self.state.awaiting_rpc = bool(value)

    @property
    def busy(self) -> bool:
        return self.state.busy

    @property
    def working(self) -> bool:
        return self.state.busy

    def begin_query(self) -> int:
        return self.state.begin_query()

    def begin_rewind(self) -> int:
        return self.state.begin_rewind()

    def end_live(self, gen: Optional[int] = None) -> bool:
        return self.state.end_live(gen)

    def enter_compacting(self, timeout: bool = True) -> None:
        """Kimi: RPC end_turn does not close /compact. Arm 180s closer."""
        self.state.enter_compacting()
        if not timeout or self.scheduler is None:
            return
        if self.state.kind != "compacting":
            return
        armed = self.state.gen
        self._compact_armed_gen = armed

        def _timeout(g=armed):
            if self.state.kind != "compacting":
                return
            if self.state.gen != g:
                return
            self.finish_compact()
            cb = self.on_compact_timeout
            if cb is not None:
                cb()

        self.scheduler.call_later(COMPACT_TIMEOUT_MS, _timeout)

    def finish_compact(self) -> bool:
        return self.state.finish_compact()

    def begin_interrupt(self) -> bool:
        if not self.state.begin_interrupt():
            return False
        self._interrupt_gen += 1
        gen = self._interrupt_gen
        if self.scheduler is None:
            return True
        for ms in INTERRUPT_SETTLE_MS:
            def _settle(g=gen):
                if self._interrupt_gen != g:
                    return
                cb = self.on_interrupt_settle
                if cb is not None:
                    cb(g)

            self.scheduler.call_later(ms, _settle)
        return True

    def settle_interrupt(self) -> None:
        self.state.settle_interrupt()

    def resume_stream(self) -> None:
        self.state.resume_stream()

    def inbound_action(self, event: str) -> Inbound:
        return self.state.inbound_action(event)

    def notify_action(self, backend: str) -> Notify:
        return self.state.notify_action(backend)

    def should_queue_prompt(self) -> bool:
        return self.state.should_queue_prompt()

    def matches_gen(self, gen: Optional[int]) -> bool:
        """False when a stale _on_done must not kill the new turn."""
        if gen is None:
            return True
        return gen == self.state.gen


def looks_like_compact_done(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return (
        "compaction completed" in low
        or "context compaction completed" in low
        or "compacted." in low
        or low.strip() == "compacted"
        or ("messages compacted" in low and "tokens after" in low)
    )


def looks_like_compact_start(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    # kimi acp auto-compact: "Compacting conversation context"
    # (not "compaction started" — that phrase is TUI/slash only).
    return (
        "compacting conversation context" in low
        or "compaction started" in low
        or "context compaction started" in low
        or "compacting context" in low
    )


def is_compact_prompt(prompt: str) -> bool:
    raw = (prompt or "").strip()
    return raw in ("/compact", "compact") or raw.startswith("/compact ")
