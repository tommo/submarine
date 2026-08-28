"""Dependency-inversion ports. Sublime-free; UI and timers are injected.

Tokens from Scheduler.call_later cannot be cancelled (same as
sublime.set_timeout). Callers must use generation / epoch guards.
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional, Protocol


class OutputPort(Protocol):
    """Conversation renderer. Session never touches a view."""

    def prompt(
        self,
        text: str,
        context_names: Optional[List[str]] = None,
        context_refs: Optional[list] = None,
    ) -> None:
        ...

    def tool(
        self,
        name: str,
        tool_input: Any,
        tool_id: Optional[str] = None,
        background: bool = False,
    ) -> None:
        ...

    def tool_done(
        self,
        name: str,
        result: Any = None,
        tool_id: Optional[str] = None,
    ) -> None:
        ...

    def tool_error(
        self,
        name: str,
        result: Any = None,
        tool_id: Optional[str] = None,
    ) -> None:
        ...

    def text(self, content: str) -> None:
        ...

    def meta(
        self,
        duration: float,
        cost: float = 0.0,
        usage: Optional[dict] = None,
    ) -> None:
        ...

    def interrupted(self, show_banner: bool = True) -> None:
        ...

    def clear_asking_state(self) -> None:
        ...

    def apply_plan_todos(self, entries: list) -> None:
        ...

    def clear(self, keep_supportive: bool = False) -> None:
        ...

    def permission_request(
        self,
        pid: Any,
        tool: str,
        tool_input: Any,
        callback: Callable[[str], None],
    ) -> None:
        ...

    def question_request(
        self,
        qid: Any,
        questions: list,
        callback: Callable[[Any], None],
    ) -> None:
        ...

    def plan_approval_request(
        self,
        plan_id: Any,
        plan_file: str,
        allowed_prompts: list,
        callback: Callable[[str], None],
    ) -> None:
        ...

    def advance_spinner(self, frames: Any = None) -> None:
        ...

    def set_retry_hint(self, text: str) -> None:
        ...

    def enter_input_mode(self) -> None:
        ...

    def reset_active_states(self, soft: bool = False) -> None:
        ...

    def set_name(self, name: str) -> None:
        ...

    def show(self, focus: bool = False) -> None:
        ...

    def is_input_mode(self) -> bool:
        ...

    def find_tool_by_id(self, tool_id: Optional[str]) -> Any:
        ...

    def refresh_background_hints(self) -> None:
        ...

    def remove_tool(self, tool: Any) -> None:
        ...

    def active_background_tools(self) -> list:
        ...

    def clear_all_permissions(self) -> None:
        ...

    def set_pending_context(self, context_items: list) -> None:
        ...

    def artifact_card(
        self,
        path: str,
        name: str,
        bytes: int = 0,
        summary: str = "",
        title: Optional[str] = None,
    ) -> None:
        ...


class ChromePort(Protocol):
    """Phantom / status / tab chrome. Implementations live in ui/."""

    def sleep_banner(self, show: bool, text: str = "") -> None:
        ...

    def connecting_banner(self, show: bool) -> None:
        ...

    def queue_chips(self, prompts: List[str]) -> None:
        ...

    def wakeup_banner(self, fire_at: Optional[float]) -> None:
        ...

    def set_status(self, text: str) -> None:
        ...

    def refresh_tab_title(self) -> None:
        ...

    def clear_phantoms(self, keys: Optional[List[str]] = None) -> None:
        ...

    def set_unread(self, on: bool) -> None:
        ...


class Scheduler(Protocol):
    """Timer port. Tokens cannot be cancelled — use generation/epoch guards."""

    def call_later(self, ms: int, fn: Callable[[], None]) -> Any:
        ...


class PersistPort(Protocol):
    """View stamps. ST adapter wraps view.settings; tests use a dict."""

    def stamp(self, key: str, value: Any) -> None:
        ...

    def read(self, key: str) -> Any:
        ...

    def clear(self, key: str) -> None:
        ...
