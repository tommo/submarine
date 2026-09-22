"""Session lifecycle commands: start/restart/query/interrupt/queue/sleep/wake."""
from __future__ import annotations

import os
import shutil

import sublime
import sublime_plugin

from backend import specs as backend_specs
from core.records import (
    load_bookmark_records,
    load_bookmarks,
    load_saved_sessions,
    toggle_bookmark,
)
from core.registry import unregister_view
from main import (
    SETTINGS_FILE,
    create_session,
    get_active_session,
    get_session_for_view,
)
from plat.constants import DEFAULT_SESSION_NAME
from plat.settings import load_profiles
from ui import keys


def restart_session_new(window, session=None):
    """Harness /clear in the current view. Live bridge: session/new.
    Dead bridge: respawn a new Session on the same sheet.
    """
    old_session = session or get_active_session(window)
    if (
        old_session
        and old_session.client
        and getattr(old_session.client, "is_alive", lambda: True)()
        and old_session.initialized
        and not getattr(old_session, "quick_mode", False)
    ):
        old_session.clear_conversation()
        return
    if old_session and getattr(old_session, "quick_mode", False):
        from features import quick as quick_agent
        quick_agent.stop_quick_session(window)
        quick_agent.ensure_quick_session(window, force_new=True)
        sublime.status_message("Quick Agent restarted")
        return
    old_view = None
    backend = None
    profile = None
    model = None
    if old_session:
        old_view = old_session.output.view if old_session.output else None
        backend = old_session.backend
        profile = old_session.profile
        model = getattr(old_session, "model", None)
        if not model and old_view:
            try:
                model = keys.read_setting(old_view.settings(), keys.MODEL)
            except Exception:
                model = None
        old_session.stop()
        if old_view and old_view.is_valid():
            unregister_view(old_view.id())

    if backend is None:
        backend = sublime.load_settings(SETTINGS_FILE).get("default_backend", "claude")
    new_session = create_session(
        window, profile=profile, backend=backend,
        attach_view=old_view if old_view and old_view.is_valid() else None,
        show=not (old_view and old_view.is_valid()),
        start=True,
        focus=True,
    )
    if model:
        new_session.model = model
        if old_view and old_view.is_valid():
            keys.write_setting(old_view.settings(), keys.MODEL, model)
            try:
                keys.erase_setting(old_view.settings(), keys.SESSION_ID)
            except Exception:
                pass
            new_session.output.clear(keep_supportive=False)
    if new_session.output.view:
        if backend != "claude":
            spec = backend_specs.get(backend)
            keys.write_setting(new_session.output.view.settings(), keys.BACKEND, backend)
            new_session.output.set_name(spec.label)
            if spec.theme:
                new_session.output.view.settings().set("color_scheme", spec.theme)
        else:
            new_session.output.set_name("Submarine")
    new_session.output.show()
    mid = getattr(new_session, "model", None) or "?"
    try:
        label = backend_specs.get(new_session.backend).label or new_session.backend
    except Exception:
        label = new_session.backend
    print("[Submarine] RESTART NEW %s/%s" % (label, mid))
    sublime.status_message("RESTART NEW — %s/%s" % (label, mid))


def _project_profiles_path(window):
    folders = window.folders() if window else None
    if not folders:
        return None
    return os.path.join(folders[0], ".claude", "profiles.json")


def _default_start_detail(backend):
    spec = backend_specs.get(backend)
    label = spec.label or backend
    models = sublime.load_settings(SETTINGS_FILE).get("default_models", {}) or {}
    model = models.get(backend) or spec.fallback_model
    if backend == "claude":
        return "Start fresh with default settings"
    extra = " · %s" % model if model else ""
    return "Default provider: %s%s" % (label, extra)


class SubmarineStartCommand(sublime_plugin.WindowCommand):
    """Start a new session. Shows profile picker if profiles are configured."""

    def run(self, profile=None, backend=None):
        if backend is None:
            backend = sublime.load_settings(SETTINGS_FILE).get(
                "default_backend", "claude")
        project_path = _project_profiles_path(self.window)
        profiles = load_profiles(project_path)
        if profile:
            create_session(
                self.window, profile=profiles.get(profile, {}), backend=backend)
            return
        options = [
            ("default", None, "🆕 New Session", _default_start_detail(backend)),
        ]
        for name, config in profiles.items():
            desc = config.get("description", "%s model" % config.get("model", "default"))
            options.append(("profile", name, "📋 %s" % name, desc))
        if len(options) == 1:
            create_session(self.window, backend=backend)
            return
        items = [[opt[2], opt[3]] for opt in options]

        def on_select(idx):
            if idx < 0:
                return
            opt_type, opt_name, _, _ = options[idx]
            if opt_type == "default":
                create_session(self.window, backend=backend)
            elif opt_type == "profile":
                create_session(
                    self.window, profile=profiles.get(opt_name, {}),
                    backend=backend)

        self.window.show_quick_panel(items, on_select)


class CodexStartCommand(sublime_plugin.WindowCommand):
    def run(self):
        if not shutil.which("codex"):
            sublime.error_message(
                "Codex CLI not found. Install from: https://github.com/openai/codex")
            return
        create_session(self.window, backend="codex")


class DeepSeekStartCommand(sublime_plugin.WindowCommand):
    def run(self):
        if not backend_specs.is_available("deepseek"):
            sublime.error_message(
                "DeepSeek provider not configured. Open "
                "'Submarine: Manage Anthropic Providers' and set the "
                "deepseek entry's base_url + auth_token (or auth_env_var).")
            return
        create_session(self.window, backend="deepseek")


class StepFunStartCommand(sublime_plugin.WindowCommand):
    def run(self):
        if not backend_specs.is_available("stepfun"):
            sublime.error_message(
                "StepFun provider not configured. Set STEPFUN_API_KEY "
                "(or Manage Anthropic Providers → stepfun auth).")
            return
        create_session(self.window, backend="stepfun")


class PiStartCommand(sublime_plugin.WindowCommand):
    def run(self):
        bun_pi = os.path.expanduser("~/.bun/install/global/node_modules/.bin/pi")
        if not os.path.isfile(bun_pi) and not shutil.which("pi"):
            sublime.error_message(
                "Pi CLI not found.\n\n"
                "Install: npm install -g @earendil-works/pi-coding-agent\n\n"
                "Then authenticate with: pi (and follow /login)")
            return
        create_session(self.window, backend="pi")


class GrokStartCommand(sublime_plugin.WindowCommand):
    def run(self):
        if not os.environ.get("GROK_BIN") and not shutil.which("grok"):
            sublime.error_message(
                "grok CLI not found.\n\n"
                "Install Grok Build and put `grok` in PATH, or set GROK_BIN.\n"
                "Then run `grok login` once.")
            return
        create_session(self.window, backend="grok")


class KimiStartCommand(sublime_plugin.WindowCommand):
    def run(self):
        try:
            from backend import kimi as kimi_backend
            ok = kimi_backend.kimi_available()
        except Exception:
            ok = bool(
                os.environ.get("KIMI_BIN")
                or shutil.which("kimi")
                or os.path.isfile(os.path.expanduser("~/.kimi-code/bin/kimi"))
            )
        if not ok:
            sublime.error_message(
                "Kimi Code CLI not found.\n\n"
                "Install Kimi Code so `kimi` is on PATH, or set KIMI_BIN.\n"
                "Default install: ~/.kimi-code/bin/kimi\n"
                "Then run `kimi login` once.\n\n"
                "Note: this is native ACP (`kimi acp`), not a custom_providers "
                "Moonshot Anthropic base_url entry.")
            return
        create_session(self.window, backend="kimi")


class SubmarineQueryCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window) or create_session(self.window)
        s.output.show()
        if hasattr(s, "_enter_input_with_draft"):
            s._enter_input_with_draft()
        else:
            s.output.enter_input_mode()


class SubmarineRestartCommand(sublime_plugin.WindowCommand):
    def run(self):
        restart_session_new(self.window)


class SubmarineRestartNewCommand(sublime_plugin.WindowCommand):
    def run(self):
        restart_session_new(self.window)


class SubmarineCopyAgentIdCommand(sublime_plugin.WindowCommand):
    """The id agents address this session by (send_to_session agent_id=…)."""

    def run(self):
        view = self.window.active_view()
        s = (get_session_for_view(view) if view else None) or get_active_session(self.window)
        aid = getattr(s, "agent_id", None) if s else None
        if not aid:
            sublime.status_message("Submarine: no agent id for this view")
            return
        sublime.set_clipboard(aid)
        sublime.status_message(
            "Submarine: agent id copied — %s (send_to_session agent_id=…)" % aid)

    def is_enabled(self):
        view = self.window.active_view()
        s = (get_session_for_view(view) if view else None) or get_active_session(self.window)
        return bool(getattr(s, "agent_id", None)) if s else False


class SubmarineCopySessionIdCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.active_view()
        s = (get_session_for_view(view) if view else None) or get_active_session(self.window)
        sid = getattr(s, "session_id", None) if s else None
        if not sid:
            sublime.status_message("Submarine: no session id for this view")
            return
        sublime.set_clipboard(sid)
        sublime.status_message("Submarine: session id copied — %s" % sid)

    def is_enabled(self):
        view = self.window.active_view()
        s = (get_session_for_view(view) if view else None) or get_active_session(self.window)
        return bool(getattr(s, "session_id", None)) if s else False


class SubmarineQueuePromptCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if not s:
            sublime.status_message("No active session")
            return
        if hasattr(s, "show_queue_input"):
            s.show_queue_input()
            return

        def on_done(text):
            text = (text or "").strip()
            if text:
                s.queue_prompt(text)

        self.window.show_input_panel("Queue prompt:", "", on_done, None, None)


class SubmarineInterruptCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if not s or not s.output:
            return
        # Non-empty composer always clears first — never interrupt while typing.
        # Exception: AskUser Other reuses input mode; Esc/Ctrl+C must still
        # cancel the question turn, not just wipe the Other line.
        modal = False
        try:
            modal = bool(s.output.has_turn_modal_ui())
        except Exception:
            modal = bool(getattr(s.output, "pending_question", None))
        if (not modal) and s.output.is_input_mode() and s.output.get_input_text().strip():
            view = s.output.view
            if not view or not view.is_valid():
                return
            start = s.output._input_start
            view.run_command("submarine_replace", {
                "start": start,
                "end": view.size(),
                "text": "",
            })
            view.sel().clear()
            view.sel().add(sublime.Region(start, start))
            s.draft_prompt = ""
            return
        s.interrupt()


class SubmarineCloseSessionCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        session = get_session_for_view(view)
        win = view.window() if view else None

        def _handoff(s):
            try:
                from ui.host import HostView, is_single_mode
                if (is_single_mode() and win is not None and s is not None
                        and not getattr(s, "torn_off", False)):
                    if HostView.for_window(win).handoff_host_on_dismiss(win, s):
                        try:
                            s.stop()
                        except Exception:
                            pass
                        sublime.status_message(
                            "Submarine: switched to the remaining session")
                        return True
            except Exception:
                pass
            return False

        if not session or not (getattr(session, "initialized", False)
                               or getattr(session, "is_sleeping", False)):
            if _handoff(session):
                return
            view.close()
            return

        def _ask(s0=session):
            s = get_session_for_view(view) or s0
            if not sublime.ok_cancel_dialog("Close this Submarine session?", "Close"):
                return
            if _handoff(s):
                try:
                    from core.registry import default_registry
                    aid = getattr(s, "agent_id", None)
                    if aid:
                        default_registry.by_agent.pop(aid, None)
                except Exception:
                    pass
                return
            try:
                s.stop()
            except Exception:
                pass
            try:
                unregister_view(view.id())
            except Exception:
                pass
            try:
                view.close()
            except Exception:
                pass

        sublime.set_timeout(_ask, 0)


class SubmarineRenameCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if not s:
            return
        current = s.name or ""
        self.window.show_input_panel(
            "Session name:", current, lambda name: self._done(name), None, None)

    def _done(self, name):
        if name.strip():
            s = get_active_session(self.window)
            if s:
                s._set_name(name.strip())


class SubmarineToggleCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if s and s.output.view and s.output.view.is_valid():
            group, _ = self.window.get_view_index(s.output.view)
            if group >= 0:
                self.window.focus_view(s.output.view)
                self.window.run_command("close_file")
            else:
                s.output.show()
        elif s:
            s.output.show()


class SubmarineStopCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if s and s.output and s.output.view:
            view_id = s.output.view.id()
            s.stop()
            unregister_view(view_id)
            # The sheet stays open, so `on_close` will not report this stop.
            try:
                from ui.session_list import schedule_session_list_refresh
                schedule_session_list_refresh()
            except Exception:
                pass


class SubmarineTearOffSessionCommand(sublime_plugin.WindowCommand):
    """Promote the bound host session to its own sheet (single mode)."""

    def run(self):
        from ui.host import tear_off_session
        tear_off_session(self.window)

    def is_enabled(self):
        from ui.host import can_tear_off
        return can_tear_off(self.window)

    def is_visible(self):
        return self.is_enabled()


class SubmarineDockSessionCommand(sublime_plugin.WindowCommand):
    """Return a torn-off sheet to the host view (single mode)."""

    def run(self):
        from ui.host import dock_session
        dock_session(self.window)

    def is_enabled(self):
        from ui.host import can_dock
        return can_dock(self.window)

    def is_visible(self):
        return self.is_enabled()


class SubmarineHideSessionCommand(sublime_plugin.WindowCommand):
    def run(self):
        from core.registry import detach_session
        s = get_active_session(self.window)
        if not s or not s.output or not s.output.view:
            return
        view = s.output.view
        if not view.is_valid():
            return
        try:
            from ui.host import HostView, is_single_mode
            if is_single_mode() and not getattr(s, "torn_off", False):
                if HostView.for_window(self.window).handoff_host_on_dismiss(
                        self.window, s):
                    sublime.status_message(
                        "Submarine: switched to the remaining session")
                    return
        except Exception:
            pass
        keys.write_setting(view.settings(), keys.SOFT_CLOSE, True)
        detach_session(s)
        try:
            view.close()
        except Exception:
            pass
        sublime.status_message("Submarine: session still running (open from Sessions)")

    def is_enabled(self):
        s = get_active_session(self.window)
        return bool(s and s.output and s.output.view and s.output.view.is_valid())


class SubmarineRevealSessionCommand(sublime_plugin.WindowCommand):
    def run(self):
        session = get_active_session(self.window)
        window = self.window
        if session is None:
            found = self._session_elsewhere()
            if found is not None:
                session, window = found
                try:
                    window.bring_to_front()
                except Exception:
                    pass
        if session is not None:
            # _land below settles the caret now; a second, deferred pass runs
            # after attach's set_timeout(0) hooks so they cannot undo it.
            if sublime is not None:
                try:
                    from ui.session_list import reveal_tail_soon
                    reveal_tail_soon(session)
                except Exception:
                    pass
            already = False
            try:
                v = session.output.view if session.output else None
                av = window.active_view() if window else None
                already = (
                    v is not None and av is not None and v.id() == av.id())
            except Exception:
                already = False
            if self._reveal(session, window):
                if not already:
                    self._land(session)
                self._trace("revealed", session, window)
                return
        if self._focus_sheet():
            self._trace("focused the window's sheet", None, window)
            return
        self._trace("no session to reveal", None, window, level="warn")
        sublime.status_message("Submarine: no session to reveal")

    def _trace(self, what, session, window, level="info"):
        """One line into the devtools ring/file — a dead chord is otherwise
        indistinguishable from a dead binding, and this is where that gets read."""
        try:
            from features.devtools import server as devtools
            devtools.log("reveal session: %s" % what, level=level,
                         agent=getattr(session, "agent_id", "") if session else "",
                         window=window.id() if window else None)
        except Exception:
            pass

    def _reveal(self, session, window):
        """Attach the session to `window`'s sheet. False when neither path works."""
        if window is None:
            return False
        try:
            from ui.host import HostView, is_single_mode
            if is_single_mode() and not getattr(session, "torn_off", False):
                if HostView.for_window(window).attach(
                        window, session, focus=True):
                    return True
        except Exception:
            pass
        try:
            from ui.session_list import reveal_live_session
            return bool(reveal_live_session(
                window, session, focus=True, force_sheet=True))
        except Exception:
            return False

    def _session_elsewhere(self):
        """(session, window) of the most recently active session in another window."""
        try:
            windows = dict((w.id(), w) for w in sublime.windows())
        except Exception:
            return None
        try:
            from core.registry import default_registry
            mine = self.window.id() if self.window else None
            best = None
            for session in default_registry.iter_sessions():
                if getattr(session, "quick_mode", False):
                    continue
                try:
                    wid = session.window.id()
                except Exception:
                    continue
                if wid == mine or wid not in windows:
                    continue
                stamp = 0.0
                for attr in ("last_access", "last_activity", "last_interaction"):
                    try:
                        stamp = max(stamp, float(getattr(session, attr, 0) or 0))
                    except Exception:
                        pass
                if best is None or stamp >= best[0]:
                    best = (stamp, session, windows[wid])
            if best is None:
                return None
            return best[1], best[2]
        except Exception:
            return None

    def _land(self, session):
        """Caret in the composer if it's open, else the tail of the sheet."""
        out = getattr(session, "output", None)
        if out is None:
            return
        if not getattr(session, "working", False):
            try:
                enter = getattr(session, "_enter_input_with_draft", None)
                if callable(enter):
                    enter()
            except Exception:
                pass
        try:
            out.set_caret_owner("draft")
        except Exception:
            pass
        try:
            if out.is_input_mode():
                out.focus_composer(
                    force_show=True, steal_focus=True, park_at_end=True)
                return
        except Exception:
            pass
        try:
            from ui.session_list import reveal_session_bottom
            reveal_session_bottom(session)
        except Exception:
            view = getattr(out, "view", None)
            if view is None:
                return
            try:
                end = view.size()
                view.show(sublime.Region(end, end), False)
            except Exception:
                pass

    def _focus_sheet(self):
        """No session here: at least bring a Submarine sheet forward, at the tail."""
        try:
            for view in self.window.views():
                if keys.is_output_view(view):
                    self.window.focus_view(view)
                    try:
                        end = view.size()
                        view.show(sublime.Region(end, end), False)
                    except Exception:
                        pass
                    return True
        except Exception:
            pass
        return False

    def is_enabled(self):
        return self.window is not None


class SubmarineSleepSessionCommand(sublime_plugin.WindowCommand):
    def run(self):
        session = get_active_session(self.window)
        if session and not session.is_sleeping:
            session.sleep()

    def is_enabled(self):
        session = get_active_session(self.window)
        return session is not None and not session.is_sleeping


class SubmarineWakeSessionCommand(sublime_plugin.WindowCommand):
    def run(self):
        session = get_active_session(self.window)
        if session and session.is_sleeping:
            session.wake()

    def is_enabled(self):
        session = get_active_session(self.window)
        return session is not None and session.is_sleeping


class SubmarineToggleAutoSleepCommand(sublime_plugin.WindowCommand):
    def run(self):
        session = get_active_session(self.window)
        if not session:
            return
        session.sleep_disabled = not session.sleep_disabled
        state = "disabled" if session.sleep_disabled else "enabled"
        sublime.status_message("Submarine: auto-sleep %s for this session" % state)
        session.output.set_name(session.name or DEFAULT_SESSION_NAME)

    def is_enabled(self):
        return get_active_session(self.window) is not None

    def is_checked(self):
        session = get_active_session(self.window)
        return bool(session and session.sleep_disabled)


class SubmarineResumeCommand(sublime_plugin.WindowCommand):
    def run(self):
        cwd = self.window.folders()[0] if self.window.folders() else ""
        starred = load_bookmarks(cwd or None)
        saved = load_saved_sessions()
        sessions = [s for s in saved if s.get("project", "") == cwd]
        have = {s.get("session_id") for s in sessions}
        # Starred ids pruned from the disk cap or saved under a different
        # project path still belong on this resume list.
        saved_by = {s.get("session_id"): s for s in saved if s.get("session_id")}
        try:
            records = load_bookmark_records(cwd or None) or {}
        except Exception:
            records = {}
        for sid in starred:
            if not sid or sid in have:
                continue
            s = saved_by.get(sid) or records.get(sid) or {
                "session_id": sid, "name": sid}
            if not isinstance(s, dict):
                s = {"session_id": sid, "name": sid}
            s = dict(s)
            s.setdefault("session_id", sid)
            s.setdefault("name", sid)
            sessions.append(s)
            have.add(sid)
        if not sessions:
            sublime.status_message("No saved sessions to resume")
            return
        sessions = sorted(sessions, key=lambda s: s.get("session_id") not in starred)
        items = []
        for s in sessions:
            sid = s.get("session_id", "")
            name = s.get("name") or "(unnamed)"
            backend = s.get("backend", "claude")
            star = "★ " if sid in starred else ""
            prefix = "[%s] " % backend if backend != "claude" else ""
            project = s.get("project", "")
            if project:
                project = "  " + project.split("/")[-1]
            cost = s.get("total_cost", 0)
            cost_str = "  $%.4f" % cost if cost else ""
            items.append(["%s%s%s" % (star, prefix, name), "%s%s" % (project, cost_str)])

        def on_select(idx):
            if idx < 0:
                return
            session_id = sessions[idx].get("session_id")
            name = sessions[idx].get("name")
            backend = sessions[idx].get("backend", "claude")
            s = create_session(self.window, resume_id=session_id, backend=backend)
            if name:
                s.name = name
                s.output.show()
                s.output.set_name(name)

        self.window.show_quick_panel(items, on_select)


class SubmarineSwitchCommand(sublime_plugin.WindowCommand):
    """The active session's actions / start a new one (Cmd+\\)."""

    def run(self, backend=None, model=None):
        if backend is None:
            backend = sublime.load_settings(SETTINGS_FILE).get(
                "default_backend", "claude")
        available_backends = [
            name for name, spec in backend_specs.all_backends().items()
            if spec.available is None or spec.available()
        ]
        project_path = self.window.folders()[0] if self.window.folders() else None
        starred = load_bookmarks(project_path)
        sessions_in_window = []
        from core.registry import default_registry
        for session in default_registry.sessions_for_window(self.window):
            vid = None
            try:
                if session.output and session.output.view:
                    vid = session.output.view.id()
                if vid is None:
                    vid = default_registry.bound_view_id(session)
            except Exception:
                vid = default_registry.bound_view_id(session)
            sessions_in_window.append((vid, session))

        active_agent = keys.read_setting(self.window.settings(), keys.ACTIVE_AGENT)
        active_view_id = keys.read_setting(self.window.settings(), keys.ACTIVE_VIEW)
        items = []
        actions = []
        active_session = None
        if active_agent:
            for view_id, s in sessions_in_window:
                if getattr(s, "agent_id", None) == active_agent:
                    active_session = s
                    break
        if active_session is None:
            for view_id, s in sessions_in_window:
                if view_id == active_view_id:
                    active_session = s
                    break

        current_view = self.window.active_view()
        in_output_view = current_view and keys.is_output_view(current_view)
        current_file = current_view.file_name() if current_view else None
        backend_prefix = "[%s] " % backend if backend != "claude" else ""

        # New Session stays on top (commits accumulated backend/transport/model).
        _mlabel = " [%s]" % model if model else ""
        items.append([
            "🆕 %sNew Session%s" % (backend_prefix, _mlabel),
            "Start fresh with %s" % model if model else "Start fresh with default model",
        ])
        actions.append(("new", None))

        if not in_output_view and current_file:
            filename = os.path.basename(current_file)
            items.append([
                "📎 %sNew with ctx:%s" % (backend_prefix, filename),
                "Create session with this file as context",
            ])
            actions.append(("new_with_file", current_file))

        if active_session and not in_output_view:
            name = active_session.name or "(unnamed)"
            star = "★ " if active_session.session_id in starred else ""
            if active_session.is_sleeping:
                status, prefix = "sleeping", "⏸ "
            elif active_session.working:
                status, prefix = "working...", "Active: "
            else:
                status, prefix = "ready", "Active: "
            cost = "$%.4f" % active_session.total_cost if active_session.total_cost > 0 else ""
            detail = "%s  %s  %sq" % (status, cost, active_session.query_count) if cost else "%s  %sq" % (status, active_session.query_count)
            items.append(["%s%s%s" % (prefix, star, name), detail])
            actions.append(("focus", active_session))

        # Other sessions live in the Sessions list (Cmd+Shift+\); this panel
        # is the active session and what to start next.

        if in_output_view and active_session:
            if active_session.session_id:
                is_starred = active_session.session_id in starred
                star_label = "★ Unstar Session" if is_starred else "☆ Star Session"
                star_detail = "Remove from pinned sessions" if is_starred else "Pin to top of session list"
                items.append([star_label, star_detail])
                actions.append(("toggle_star", active_session))
            if not active_session.working and active_session.session_id:
                items.append(["↩ Undo Message", "Rewind session to previous turn"])
                actions.append(("undo_message", active_session))
            if not active_session.is_sleeping:
                items.append(["○ Sleep Session", "Put session to sleep, free resources"])
                actions.append(("sleep", active_session))
            items.append(["🔄 Restart Session…", "Restart with a profile"])
            actions.append(("restart", active_session))

        profiles = load_profiles(_project_profiles_path(self.window))

        backend_models = _models_for_backend(backend, active_session)
        for m in backend_models:
            if isinstance(m, str):
                model_id, model_name = m, m
            elif isinstance(m, (list, tuple)) and len(m) >= 2:
                model_id, model_name = m[0], m[1]
            else:
                continue
            sel = "● " if model == model_id else ""
            items.append([
                "/model %s%s%s" % (sel, backend_prefix, model_name),
                "Select %s for the next session" % model_id,
            ])
            actions.append(("set_model", model_id))

        if in_output_view and active_session:
            from core.session import _is_claude_bridge
            try:
                same_family = _is_claude_bridge(
                    backend_specs.get(active_session.backend,
                                      active_session.settings))
            except Exception:
                same_family = False
            if same_family:
                items.append([
                    "⇄ Change Provider…",
                    "Move THIS session to another Claude-bridge provider "
                    "(official ↔ (CC) …), keeping its history",
                ])
                actions.append(("change_provider", active_session))
            items.append([
                "◈ Select Effort… (now %s)" % (active_session.effort or "default"),
                "Change THIS session's effort — live on Claude (low–xhigh)",
            ])
            actions.append(("select_effort", active_session))
            items.append(["🍴 Fork Session", "Create new session with copy of history"])
            actions.append(("fork", active_session))

        try:
            from features import quota as quota_client
            quota_client.warm_cache()
        except Exception:
            quota_client = None  # type: ignore
        ordered = []
        for name in ("claude", "codex", "pi", "grok", "kimi"):
            if name in available_backends and name not in ordered:
                ordered.append(name)
        for name in available_backends:
            if name not in ordered:
                ordered.append(name)
        for other in ordered:
            spec = backend_specs.get(other)
            label = spec.label
            if other == backend:
                label = "%s (current)" % label
            detail = "Show %s options" % other
            if quota_client is not None:
                try:
                    detail = quota_client.usage_detail_for_backend(other, fallback=detail)
                except Exception:
                    pass
            items.append(["with %s…" % label, detail])
            actions.append(("switch_backend", other))

        def on_select(idx):
            if idx < 0:
                return
            action, data = actions[idx]
            if action == "switch_backend":
                sublime.set_timeout(lambda: self.run(backend=data), 0)
                return
            if action == "set_model":
                sublime.set_timeout(lambda: self.run(backend=backend, model=data), 0)
                return
            if action == "select_effort":
                sublime.set_timeout(
                    lambda: self.window.run_command("submarine_select_effort"), 0)
                return
            if action == "change_provider":
                sublime.set_timeout(
                    lambda: self.window.run_command("submarine_change_provider"), 0)
                return
            if action == "toggle_star" and data and data.session_id:
                now_starred = toggle_bookmark(
                    data.session_id, project_path,
                    record={
                        "name": getattr(data, "name", None),
                        "backend": getattr(data, "backend", None),
                        "project": project_path,
                        "model": getattr(data, "model", None),
                        "query_count": getattr(data, "query_count", None),
                    })
                msg = ("★ Starred: %s" % (data.name or data.session_id)
                       if now_starred else "☆ Unstarred: %s" % (data.name or data.session_id))
                sublime.status_message(msg)
                return
            if action == "undo_message" and data:
                self._undo_picker(data)
            elif action == "restart" and data:
                self._show_restart_picker(data, profiles)
            elif action == "new_with_file" and data:
                s = create_session(
                    self.window,
                    profile=({"model": model} if model else None),
                    backend=backend)
                try:
                    with open(data, "r", encoding="utf-8") as f:
                        content = f.read()
                    if hasattr(s, "add_context_file"):
                        s.add_context_file(data, content)
                    elif getattr(s, "context", None) is not None:
                        s.context.add_file(data, content)
                except Exception as e:
                    print("[Submarine] Error adding file context: %s" % e)
            elif action == "new":
                create_session(
                    self.window,
                    profile=({"model": model} if model else None),
                    backend=backend)
            elif action == "fork" and data and data.session_id:
                from core.session import fork_session_title
                forked = create_session(
                    self.window, resume_id=data.session_id, fork=True,
                    backend=data.backend, model=getattr(data, "model", None))
                if forked:
                    forked_name = fork_session_title(
                        getattr(data, "name", None) or "session")
                    forked.name = forked_name
                    try:
                        forked.output.set_name(forked_name)
                    except Exception:
                        pass
            elif action == "sleep" and data:
                data.sleep()
            elif action == "focus" and data:
                try:
                    from ui.host import HostView, is_single_mode
                    if is_single_mode() and not getattr(data, "torn_off", False):
                        hv = HostView.for_window(self.window)
                        if hv.bound_session(self.window) is data:
                            host = hv.host_view(self.window)
                            if host is not None:
                                self.window.focus_view(host)
                            if getattr(data, "is_sleeping", False):
                                data.wake()
                            return
                        hv.attach(self.window, data)
                        if getattr(data, "is_sleeping", False):
                            data.wake()
                        return
                except Exception:
                    pass
                data.output.show()

        _ph = []
        if model:
            _ph.append("model: %s" % model)
        self.window.show_quick_panel(
            items, on_select, placeholder=(" · ".join(_ph) if _ph else None))

    def _undo_picker(self, session):
        if getattr(session, "backend", "") == "grok" and hasattr(session, "show_grok_undo_panel"):
            session.show_grok_undo_panel(self.window)
            return
        turns = session.get_turns_for_undo()
        if not turns:
            sublime.status_message("No undoable turns")
            return
        labels = [t[0] for t in turns]

        def _on_undo(uidx, _turns=turns, _s=session):
            if uidx < 0:
                return
            _, rewind_id, draft_prompt = _turns[uidx]
            if hasattr(_s, "_apply_undo"):
                _s._apply_undo(rewind_id, draft_prompt)
            elif hasattr(_s, "rewind") and hasattr(_s.rewind, "apply"):
                _s.rewind.apply(rewind_id, draft_prompt)
            else:
                _s.undo_message()

        self.window.show_quick_panel(labels, _on_undo, placeholder="Rewind to…")

    def _show_restart_picker(self, session, profiles):
        items = [["🆕 Fresh Start", "Restart with default settings"]]
        actions = [("default", None)]
        for name, config in profiles.items():
            desc = config.get("description", "%s model" % config.get("model", "default"))
            items.append(["📋 %s" % name, desc])
            actions.append(("profile", config))

        def on_select(idx):
            if idx < 0:
                return
            action, data = actions[idx]
            if action != "profile":
                restart_session_new(self.window, session)
                return
            old_view = session.output.view if session.output else None
            session.stop()
            if old_view and old_view.is_valid():
                unregister_view(old_view.id())
            create_session(
                self.window, profile=data, backend=session.backend,
                attach_view=old_view if old_view and old_view.is_valid() else None,
                show=not (old_view and old_view.is_valid()),
                start=True,
            )

        self.window.show_quick_panel(items, on_select)


def _models_for_backend(backend, active_session=None):
    settings = sublime.load_settings(SETTINGS_FILE)
    all_models = dict(settings.get("models", {}) or {})
    cached_models_file = os.path.expanduser("~/.claude/sublime_cached_models.json")
    if os.path.exists(cached_models_file):
        try:
            import json as _json
            with open(cached_models_file) as f:
                cached = _json.load(f)
            for b, models in cached.items():
                if b not in all_models:
                    all_models[b] = models
        except Exception:
            pass
    backend_models = all_models.get(backend, [])
    if not backend_models:
        try:
            backend_models = backend_specs.get(backend).default_models
        except Exception:
            backend_models = []
    if backend == "grok":
        try:
            from backend import grok as grok_backend
            extra = []
            if active_session is not None and getattr(active_session, "available_models", None):
                extra.extend(active_session.available_models)
            backend_models = list(grok_backend.grok_picker_models(extra=extra))
        except Exception as e:
            print("[Submarine] grok session model list: %s" % e)
    return backend_models


class SubmarineForkCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if not s or not s.session_id:
            sublime.status_message("No active session to fork")
            return
        forked = create_session(
            self.window, resume_id=s.session_id, fork=True,
            backend=s.backend, model=getattr(s, "model", None))
        from core.session import fork_session_title
        forked_name = fork_session_title(s.name or "session")
        forked.name = forked_name
        forked.output.set_name(forked_name)
        sublime.status_message("Forked session: %s" % forked_name)


class SubmarineForkFromCommand(sublime_plugin.WindowCommand):
    def run(self):
        from core.registry import default_registry
        items = []
        sources = []
        for session in default_registry.sessions_for_window(self.window):
            if not session.session_id:
                continue
            name = session.name or "(unnamed)"
            cost = "$%.4f" % session.total_cost if session.total_cost > 0 else ""
            items.append(["● %s" % name, "active  %s  %sq" % (cost, session.query_count)])
            sources.append(("active", session.session_id, name, session.backend, getattr(session, "model", None)))
        for s in load_saved_sessions():
            session_id = s.get("session_id")
            name = s.get("name") or "(unnamed)"
            if any(src[1] == session_id for src in sources):
                continue
            project = s.get("project", "")
            if project:
                project = project.split("/")[-1]
            cost = s.get("total_cost", 0)
            cost_str = "$%.4f" % cost if cost else ""
            items.append([name, "saved  %s  %s" % (project, cost_str)])
            sources.append(("saved", session_id, name, s.get("backend", "claude"), s.get("model")))
        if not items:
            sublime.status_message("No sessions to fork from")
            return

        def on_select(idx):
            if idx < 0:
                return
            _kind, session_id, name, src_backend, src_model = sources[idx]
            forked = create_session(
                self.window, resume_id=session_id, fork=True,
                backend=src_backend, model=src_model)
            from core.session import fork_session_title
            forked_name = fork_session_title(name)
            forked.name = forked_name
            forked.output.set_name(forked_name)
            sublime.status_message("Forked session: %s" % forked_name)

        self.window.show_quick_panel(items, on_select)


class SubmarineSessionListCommand(sublime_plugin.WindowCommand):
    def run(self):
        from ui.session_list import show_session_list
        show_session_list(self.window)


def cycle_sessions_for_window(window):
    """The sessions Ctrl+] / Ctrl+[ step through: every current session of
    this window, sleeping ones included — a sleeping sheet is still a place
    to go back to (revealing it does not wake it). Quick agents stay out.

    The order is the Sessions list's CURRENT order (status bands, starred
    pins, children under their parent), so the chord walks the list as it
    reads; a session the list does not know yet goes last."""
    from core.registry import default_registry
    out = []
    for s in default_registry.iter_sessions():
        if getattr(s, "quick_mode", False):
            continue
        if window is not None and not _session_in_window(s, window):
            continue
        out.append(s)
    order = {}
    try:
        from ui.session_list import live_agent_ids_in_list_order
        for i, aid in enumerate(live_agent_ids_in_list_order(window)):
            order.setdefault(aid, i)
    except Exception:
        order = {}
    tail = len(order)
    out.sort(key=lambda sess: (
        order.get(str(getattr(sess, "agent_id", "") or ""), tail),
        -float(getattr(sess, "last_access", 0) or 0),
        str(getattr(sess, "agent_id", "") or ""),
    ))
    return out


# Older name: the cycle used to skip sleeping sessions.
awake_sessions_for_window = cycle_sessions_for_window


def _session_in_window(session, window):
    if window is None:
        return True
    view = None
    try:
        view = session.output.view if session.output else None
    except Exception:
        view = None
    if view is not None:
        try:
            vw = view.window()
            if vw is window:
                return True
            if vw is not None and vw.id() == window.id():
                return True
        except Exception:
            pass
    try:
        win = session.window
        if win is window:
            return True
        if win is not None:
            return win.id() == window.id()
    except Exception:
        return False
    return False


class SubmarineCycleSessionCommand(sublime_plugin.WindowCommand):
    """Ctrl+] / Ctrl+[ — next / previous session in this window (sleeping
    too). Works from a session sheet or from the Sessions list; either way
    the target's sheet takes focus."""

    def run(self, direction=1):
        try:
            step = int(direction)
        except (TypeError, ValueError):
            step = 1
        if step == 0:
            step = 1
        sessions = cycle_sessions_for_window(self.window)
        if not sessions:
            sublime.status_message("Submarine: no session in this window")
            return
        if len(sessions) == 1:
            target = sessions[0]
        else:
            current = get_active_session(self.window)
            av = self.window.active_view() if self.window else None
            if av is not None:
                hit = get_session_for_view(av)
                if hit is not None:
                    current = hit
            idx = -1
            if current is not None:
                for i, s in enumerate(sessions):
                    if s is current:
                        idx = i
                        break
                    if (
                        getattr(s, "agent_id", None)
                        and getattr(s, "agent_id", None)
                        == getattr(current, "agent_id", None)
                    ):
                        idx = i
                        break
            if idx < 0:
                idx = 0 if step > 0 else len(sessions) - 1
            else:
                idx = (idx + step) % len(sessions)
            target = sessions[idx]
        try:
            reveal = SubmarineRevealSessionCommand(self.window)
        except TypeError:
            reveal = SubmarineRevealSessionCommand.__new__(
                SubmarineRevealSessionCommand)
            reveal.window = self.window
        if reveal._reveal(target, self.window):
            # Invoked from the Sessions list too: the chord means "go there",
            # so focus moves into the session view, not back to the list.
            try:
                view = target.output.view if target.output else None
                if view is not None and view.is_valid():
                    self.window.focus_view(view)
            except Exception:
                pass
            # The list follows: its caret and scroll move to the new row, so
            # what it points at is what the window shows.
            try:
                from ui.session_list import sync_list_to_session
                sync_list_to_session(self.window, target)
            except Exception:
                pass
            reveal._land(target)
            # attach's deferred paint hooks run after _land; the focus and
            # tail hand-off has to be the last thing to touch the view.
            try:
                from ui.session_list import focus_sheet_soon
                focus_sheet_soon(self.window, target)
            except Exception:
                pass
            name = (
                getattr(target, "display_name", None)
                or getattr(target, "name", None)
                or "session"
            )
            sublime.status_message("Submarine: %s" % name)
        else:
            sublime.status_message("Submarine: could not switch session")


class SubmarineToggleListCommand(sublime_plugin.WindowCommand):
    """⌘⇧\\ — session list ↔ session view; reveal the target if it is hidden."""

    def run(self):
        av = self.window.active_view() if self.window else None
        on_list = False
        if av is not None:
            try:
                on_list = bool(keys.read_setting(av.settings(), keys.SESSION_LIST))
            except Exception:
                on_list = False
        if on_list:
            self.window.run_command("submarine_reveal_session")
            return
        from ui.session_list import show_session_list
        show_session_list(self.window)


class SubmarineSessionListRefreshCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        from ui.session_list import SETTING, refresh_session_list
        if not self.view.settings().get(SETTING):
            return
        win = self.view.window()
        if win:
            refresh_session_list(win)

    def is_enabled(self):
        from ui.session_list import SETTING
        return bool(self.view.settings().get(SETTING))


class SubmarineSessionListOpenCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        import json
        from ui.session_list import (
            SETTING, ROWS_KEY, row_at_line, open_row, refresh_session_list,
        )
        if not self.view.settings().get(SETTING):
            return
        raw = self.view.settings().get(ROWS_KEY) or "[]"
        try:
            index = json.loads(raw)
        except Exception:
            index = []
        sel = self.view.sel()
        if not sel:
            return
        line = self.view.rowcol(sel[0].begin())[0] + 1
        row = row_at_line(index, line)
        win = self.view.window()
        if not win or not row:
            return
        if open_row(win, row):
            refresh_session_list(win)

    def is_enabled(self):
        from ui.session_list import SETTING
        return bool(self.view.settings().get(SETTING))
