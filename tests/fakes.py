"""In-process ports for core/ tests. No sublime.

Fake extensions used by the bg/subagent lifecycle suite
(tests/test_bg_lifecycle.py):

* FakeOutput.tool records expose ``tool_input`` (alias of ``input``) so
  formatters and composer ⚙-hint tests read the same attribute as
  production ToolCall.
* FakeOutput.tool upserts an open pending/background row by id the
  same way ui.renderer.tool does (including gear-to-pending demote).
* FakeOutput.removed lists every tool passed to remove_tool (abort /
  failed-notify / force-sleep assertions).
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional


class FakeScheduler:
    def __init__(self):
        self.pending = []  # type: list
        self.token = 0

    def call_later(self, ms, fn):
        # type: (int, Callable[[], None]) -> int
        self.token += 1
        self.pending.append((ms, fn, self.token))
        return self.token

    def fire_due(self, max_ms=None):
        # type: (Optional[int]) -> int
        keep = []
        fired = 0
        for ms, fn, tok in self.pending:
            if max_ms is None or ms <= max_ms:
                fn()
                fired += 1
            else:
                keep.append((ms, fn, tok))
        self.pending = keep
        return fired

    def fire_all(self):
        # type: () -> int
        return self.fire_due(None)


class DictPersist:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def stamp(self, key, value):
        self.data[key] = value

    def read(self, key):
        return self.data.get(key)

    def clear(self, key):
        self.data.pop(key, None)


class FakeOutput:
    def __init__(self):
        self.prompts = []  # type: list
        self.tools = []  # type: list
        self.done = []  # type: list
        self.errors = []  # type: list
        self.texts = []  # type: list
        self.metas = []  # type: list
        self.interrupts = []  # type: list
        self.todos = []  # type: list
        self.cleared = []  # type: list
        self.permissions = []  # type: list
        self.questions = []  # type: list
        self.plans = []  # type: list
        self.spinners = 0
        self.retry_hints = []  # type: list
        self.input_enters = 0
        self.resets = []  # type: list
        self.names = []  # type: list
        self.shown = []  # type: list
        self.hint_refreshes = 0
        self._input = False
        self._tools_by_id = {}  # type: dict
        self.removed = []  # type: list  # tools passed to remove_tool
        self.view = None
        self.pending_context = []  # type: list

    def prompt(self, text, context_names=None, context_refs=None):
        self.prompts.append((text, context_names, context_refs))

    def tool(self, name, tool_input, tool_id=None, background=False):
        # Upsert matches ui.renderer.tool: same id keeps the open row and
        # may demote ⚙ → pending when a later paint is not background.
        # `tool_input` mirrors production ToolCall so formatters / composer
        # ⚙-hint tests can read the same attribute the UI uses.
        from core.background import is_shell_background_tool
        if background and not is_shell_background_tool(name):
            background = False
        existing = self._tools_by_id.get(tool_id) if tool_id else None
        if existing is not None and existing.status in ("background", "pending"):
            existing.name = name
            existing.input = tool_input
            existing.tool_input = tool_input
            if background and is_shell_background_tool(name):
                existing.status = "background"
            elif existing.status == "background" and (
                    not background or not is_shell_background_tool(name)):
                existing.status = "pending"
            return existing
        rec = type("Tool", (), {
            "name": name, "input": tool_input, "tool_input": tool_input,
            "id": tool_id,
            "status": "background" if background else "pending",
        })()
        self.tools.append(rec)
        if tool_id:
            self._tools_by_id[tool_id] = rec

    def tool_done(self, name, result=None, tool_id=None):
        self.done.append((name, result, tool_id))
        if tool_id and tool_id in self._tools_by_id:
            self._tools_by_id[tool_id].status = "done"

    def tool_error(self, name, result=None, tool_id=None):
        self.errors.append((name, result, tool_id))
        if tool_id and tool_id in self._tools_by_id:
            self._tools_by_id[tool_id].status = "error"

    def text(self, content):
        self.texts.append(content)

    def meta(self, duration, cost=0.0, usage=None):
        self.metas.append((duration, cost, usage))

    def interrupted(self, show_banner=True):
        self.interrupts.append(show_banner)

    def apply_plan_todos(self, entries):
        self.todos.append(entries)

    def clear(self, keep_supportive=False):
        self.cleared.append(keep_supportive)

    def permission_request(self, pid, tool, tool_input, callback):
        self.permissions.append((pid, tool, tool_input, callback))

    def question_request(self, qid, questions, callback):
        self.questions.append((qid, questions, callback))

    def plan_approval_request(self, plan_id, plan_file, allowed_prompts, callback):
        self.plans.append((plan_id, plan_file, allowed_prompts, callback))

    def advance_spinner(self, frames=None):
        self.spinners += 1

    def set_retry_hint(self, text):
        self.retry_hints.append(text)

    def enter_input_mode(self):
        self.input_enters += 1
        self._input = True

    def reset_active_states(self, soft=False):
        self.resets.append(soft)

    def set_name(self, name):
        self.names.append(name)

    def show(self, focus=False):
        self.shown.append(focus)

    def is_input_mode(self):
        return self._input

    def find_tool_by_id(self, tool_id):
        return self._tools_by_id.get(tool_id)

    def refresh_background_hints(self):
        self.hint_refreshes += 1

    def remove_tool(self, tool):
        # EXTENDED: record removals so lifecycle tests can assert ⚙ rows
        # were dropped (force-sleep / abort / failed notify) without
        # inspecting private maps only.
        self.removed.append(tool)
        if getattr(tool, "id", None) in self._tools_by_id:
            self._tools_by_id.pop(tool.id, None)

    def active_background_tools(self):
        return [t for t in self._tools_by_id.values() if t.status == "background"]

    def clear_all_permissions(self):
        pass

    def set_pending_context(self, context_items):
        self.pending_context = list(context_items or [])


class FakeChrome:
    def __init__(self):
        self.sleep = []  # type: list
        self.connecting = []  # type: list
        self.queues = []  # type: list
        self.wakeups = []  # type: list
        self.status = []  # type: list
        self.titles = 0
        self.clears = []  # type: list
        self.unread = []  # type: list

    def sleep_banner(self, show, text=""):
        self.sleep.append((show, text))

    def connecting_banner(self, show):
        self.connecting.append(show)

    def queue_chips(self, prompts):
        self.queues.append(list(prompts))

    def wakeup_banner(self, fire_at):
        self.wakeups.append(fire_at)

    def set_status(self, text):
        self.status.append(text)

    def refresh_tab_title(self):
        self.titles += 1

    def clear_phantoms(self, keys=None):
        self.clears.append(keys)

    def set_unread(self, on):
        self.unread.append(on)


class FakeClient:
    def __init__(self, on_notification=None):
        self.on_notification = on_notification
        self.alive = True
        self.started = None  # type: Optional[tuple]
        self.sent = []  # type: list
        self.stopped = False

    def start(self, cmd, env=None):
        self.started = (list(cmd), dict(env or {}))

    def is_alive(self):
        return self.alive

    def send(self, method, params, callback=None):
        self.sent.append((method, params, callback))
        return True

    def stop(self):
        self.stopped = True
        self.alive = False


def make_session(**kwargs):
    """Session wired to fakes. Does not spawn a bridge."""
    from core.registry import SessionRegistry
    from core.session import Session

    output = kwargs.pop("output", None) or FakeOutput()
    chrome = kwargs.pop("chrome", None) or FakeChrome()
    scheduler = kwargs.pop("scheduler", None) or FakeScheduler()
    persist = kwargs.pop("persist", None) or DictPersist()
    registry = kwargs.pop("registry", None) or SessionRegistry()
    client = kwargs.pop("client", None)
    initialized = kwargs.pop("initialized", False)
    rpc_factory = kwargs.pop("rpc_factory", None)
    if rpc_factory is None and client is not None:
        rpc_factory = lambda on_n, _c=client: _c
    elif rpc_factory is None:
        rpc_factory = FakeClient
    s = Session(
        output, chrome, scheduler, persist,
        registry=registry, rpc_factory=rpc_factory, **kwargs)
    # Never let a test write the real plugin-dir store.
    import os as _os
    import tempfile as _tf
    s.store.path = _os.path.join(
        _tf.mkdtemp(prefix="submarine-test-"), ".sessions.json")
    if client is not None:
        s.client = client
        client.on_notification = s._on_notification
    if initialized:
        s.initialized = True
        if s.session_id is None:
            s.session_id = "sess-test"
    return s
