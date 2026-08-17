"""Session lifecycle commands: start/restart/query/interrupt/queue/sleep/wake."""
from __future__ import annotations

import os
import shutil

import sublime
import sublime_plugin

from backend import specs as backend_specs
from core.records import load_bookmarks, load_saved_sessions, toggle_bookmark
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
        if s.output.is_input_mode() and s.output.get_input_text().strip():
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
        if not session or not (session.initialized or session.is_sleeping):
            view.close()
            return

        def _ask():
            s = get_session_for_view(view)
            if not s or not (s.initialized or s.is_sleeping):
                view.close()
                return
            if sublime.ok_cancel_dialog("Close this Submarine session?", "Close"):
                s.stop()
                try:
                    unregister_view(view.id())
                except Exception:
                    pass
                view.close()

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


class SubmarineHideSessionCommand(sublime_plugin.WindowCommand):
    def run(self):
        from core.registry import detach_session
        s = get_active_session(self.window)
        if not s or not s.output or not s.output.view:
            return
        view = s.output.view
        if not view.is_valid():
            return
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
        sessions = [s for s in load_saved_sessions() if s.get("project", "") == cwd]
        if not sessions:
            sublime.status_message("No saved sessions to resume")
            return
        starred = load_bookmarks(cwd or None)
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
    """Switch between active sessions / start a new one (Cmd+\\)."""

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
                vid = session.output.view.id() if session.output and session.output.view else session.view_id
            except Exception:
                vid = session.view_id
            sessions_in_window.append((vid, session))

        active_view_id = keys.read_setting(self.window.settings(), keys.ACTIVE_VIEW)
        items = []
        actions = []
        active_session = None
        for view_id, s in sessions_in_window:
            if view_id == active_view_id:
                active_session = s
                break

        current_view = self.window.active_view()
        in_output_view = current_view and keys.is_output_view(current_view)
        current_file = current_view.file_name() if current_view else None
        backend_prefix = "[%s] " % backend if backend != "claude" else ""

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

        other = [(v, s) for v, s in sessions_in_window
                 if v != active_view_id and s is not active_session]

        def _session_list_key(pair):
            _v, s = pair
            if s.is_sleeping:
                liveness = 2
            elif s.working:
                liveness = 0
            else:
                liveness = 1
            starred_rank = 0 if s.session_id in starred else 1
            return (liveness, starred_rank)

        other.sort(key=_session_list_key)
        for view_id, s in other:
            name = s.name or "(unnamed)"
            is_starred = s.session_id in starred
            if s.is_sleeping:
                marker = "⏸ " + ("★ " if is_starred else "")
                status = "sleeping"
            elif s.working:
                marker = "\u2022 " + ("★ " if is_starred else "")
                status = "working..."
            else:
                marker = "★ " if is_starred else "  "
                status = "ready"
            cost = "$%.4f" % s.total_cost if s.total_cost > 0 else ""
            detail = "%s  %s  %sq" % (status, cost, s.query_count) if cost else "%s  %sq" % (status, s.query_count)
            items.append(["%s%s" % (marker, name), detail])
            actions.append(("focus", s))

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
            items.append(["RESTART NEW", "Fresh session in this view — same provider/model"])
            actions.append(("restart_new", active_session))
            items.append(["🔄 Restart Session…", "Restart with a profile"])
            actions.append(("restart", active_session))

        profiles = load_profiles(_project_profiles_path(self.window))
        for name, config in profiles.items():
            desc = config.get("description", "%s model" % config.get("model", "default"))
            items.append(["😶 %s%s" % (backend_prefix, name), desc])
            actions.append(("profile", config))

        _mlabel = " [%s]" % model if model else ""
        items.append([
            "🆕 %sNew Session%s" % (backend_prefix, _mlabel),
            "Start fresh with %s" % model if model else "Start fresh with default model",
        ])
        actions.append(("new", None))

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
            if action == "toggle_star" and data and data.session_id:
                now_starred = toggle_bookmark(data.session_id, project_path)
                msg = ("★ Starred: %s" % (data.name or data.session_id)
                       if now_starred else "☆ Unstarred: %s" % (data.name or data.session_id))
                sublime.status_message(msg)
                return
            if action == "undo_message" and data:
                self._undo_picker(data)
            elif action == "restart_new" and data:
                restart_session_new(self.window, data)
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
            elif action == "profile":
                create_session(self.window, profile=data, backend=backend)
            elif action == "fork" and data and data.session_id:
                from core.session import fork_session_title
                forked = create_session(
                    self.window, resume_id=data.session_id, fork=True,
                    backend=data.backend)
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
            self.window, resume_id=s.session_id, fork=True, backend=s.backend)
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
            sources.append(("active", session.session_id, name, session.backend))
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
            sources.append(("saved", session_id, name, s.get("backend", "claude")))
        if not items:
            sublime.status_message("No sessions to fork from")
            return

        def on_select(idx):
            if idx < 0:
                return
            _kind, session_id, name, src_backend = sources[idx]
            forked = create_session(
                self.window, resume_id=session_id, fork=True, backend=src_backend)
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
