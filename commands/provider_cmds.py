"""Custom providers wizard, model/effort/default pickers."""
from __future__ import annotations

import os

import sublime
import sublime_plugin

from backend import providers as provider_mod
from backend import specs as backend_specs
from core.session import resolve_model_id
from main import SETTINGS_FILE, create_session, get_active_session
from ui import keys


_PROVIDER_FIELDS = [
    ("name",         "Provider name (unique key)",                 "deepseek",   True,  False),
    ("label",        "Display label (blank = use name)",           "DeepSeek",   False, False),
    ("abbrev",       "Tab abbreviation (blank = first 2 chars)",   "DS",         False, False),
    ("base_url",     "Anthropic-compatible base URL",              "https://api.deepseek.com/anthropic", True, False),
    ("auth_token",   "Auth token (blank → read from auth_env_var)", "sk-...",    False, True),
    ("auth_env_var", "Env var holding the auth token",             "DEEPSEEK_API_KEY", False, False),
    ("opus_model",   "Opus alias → model id",                      "deepseek-v4-pro[1m]", False, False),
    ("sonnet_model", "Sonnet alias → model id",                    "deepseek-v4-pro",     False, False),
    ("haiku_model",  "Haiku alias → model id",                     "deepseek-v4-flash",   False, False),
    ("effort",       "Reasoning effort override (low/medium/high/max, blank = global)", "high", False, False),
]


def _load_custom_providers():
    s = sublime.load_settings(SETTINGS_FILE)
    providers = s.get("custom_providers", {}) or {}
    return providers if isinstance(providers, dict) else {}


def _save_custom_providers(providers):
    s = sublime.load_settings(SETTINGS_FILE)
    s.set("custom_providers", providers)
    sublime.save_settings(SETTINGS_FILE)


def _mask_secret(v):
    if not v:
        return ""
    if len(v) <= 6:
        return "<set>"
    return v[:3] + "…" + v[-3:]


def _provider_summary(name, cfg):
    base = (cfg.get("base_url") or "").strip()
    token = (cfg.get("auth_token") or "").strip()
    auth_env_var = (cfg.get("auth_env_var") or "").strip()
    if token:
        cred = _mask_secret(token)
    elif auth_env_var:
        cred = "$%s" % auth_env_var
    else:
        cred = "<no auth>"
    effort = (cfg.get("effort") or "").strip()
    effort_tag = "  •  effort:%s" % effort if effort else ""
    return "%s  •  %s%s" % (base, cred, effort_tag)


def _is_claude_bridge_spec(spec):
    script = os.path.basename(getattr(spec, "bridge_script", "") or "")
    return script in ("claude_main.py", "main.py")


class SubmarineManageProvidersCommand(sublime_plugin.WindowCommand):
    def run(self):
        self._show_main()

    def _show_main(self):
        providers = _load_custom_providers()
        items = []
        actions = []
        ordered = sorted(
            providers.items(),
            key=lambda kv: not bool((kv[1] or {}).get("pinned", False)))
        for name, cfg in ordered:
            cfg = cfg or {}
            label = cfg.get("label") or name
            pin = "📌 " if cfg.get("pinned") else "    "
            note = "pinned → shows in quick panel" if cfg.get("pinned") else "not pinned"
            items.append([
                "✎  %s%s" % (pin, label),
                "%s  •  %s" % (_provider_summary(name, cfg), note),
            ])
            actions.append(("edit", name))
        items.append(["+  Add Provider…", "Define a new Anthropic-compatible endpoint"])
        actions.append(("add", None))
        items.append(["{ }  Edit raw JSON…", "Open custom_providers in the settings file"])
        actions.append(("raw", None))
        if providers:
            for name, cfg in ordered:
                cfg = cfg or {}
                label = cfg.get("label") or name
                pin_label = "Unpin" if cfg.get("pinned") else "Pin"
                items.append([
                    "%s  %s: %s" % (
                        "📌" if cfg.get("pinned") else "📍", pin_label, label),
                    "Toggle whether it shows in the quick panels",
                ])
                actions.append(("pin", name))
                items.append(["📋 Duplicate: %s" % label, "Copy this provider's config"])
                actions.append(("dup", name))
                items.append([
                    "🎯 Generate model config: %s" % label,
                    "Fetch live models and set opus/sonnet/haiku aliases",
                ])
                actions.append(("genmodels", name))
                items.append(["🔍 Test config: %s" % label, "Validate base_url + auth presence"])
                actions.append(("test", name))
                items.append(["🗑 Delete: %s" % label, "Remove this provider"])
                actions.append(("delete", name))

        def on_select(idx):
            if idx < 0:
                return
            action, data = actions[idx]
            if action == "edit":
                self._run_wizard(existing=data)
            elif action == "add":
                self._run_wizard(existing=None)
            elif action == "raw":
                self.window.run_command("edit_settings", {
                    "base_file": "${packages}/Submarine/Submarine.sublime-settings",
                })
            elif action == "dup":
                self._duplicate(data)
            elif action == "test":
                self._test(data)
            elif action == "genmodels":
                self.window.run_command(
                    "submarine_generate_provider_models", {"provider": data})
            elif action == "delete":
                self._delete(data)
            elif action == "pin":
                self._toggle_pin(data)

        self.window.show_quick_panel(
            items, on_select, placeholder="Manage Anthropic providers")

    def _toggle_pin(self, name):
        providers = _load_custom_providers()
        cfg = providers.get(name) or {}
        cfg["pinned"] = not bool(cfg.get("pinned", False))
        providers[name] = cfg
        _save_custom_providers(providers)
        state = "pinned → shows in quick panel" if cfg["pinned"] else "unpinned"
        sublime.status_message("'%s' %s" % (name, state))
        self._show_main()

    def _run_wizard(self, existing=None):
        cfg = dict(_load_custom_providers().get(existing, {}) or {}) if existing else {}
        self._fields = list(_PROVIDER_FIELDS)
        self._editing = existing
        self._values = {}
        for key, label, example, required, secret in self._fields:
            if key == "name":
                self._values[key] = existing or ""
            elif existing and key in cfg:
                self._values[key] = str(cfg.get(key, ""))
            elif secret:
                self._values[key] = ""
            else:
                self._values[key] = example
        self._step = 0
        self._return_to_review = False
        self._prompt_field()

    def _prompt_field(self, return_to_review=False):
        self._return_to_review = return_to_review
        if self._step >= len(self._fields):
            self._review()
            return
        key, label, example, required, secret = self._fields[self._step]
        current = self._values.get(key, "")
        title = label
        if example and not current:
            title += "   e.g. %s" % example
        if required:
            title += "  [required]"
        if secret:
            title += "  (stored in settings — prefer auth_env_var)"

        def reopen(attempt):
            self.window.show_input_panel(title, attempt, on_done, None, on_cancel)

        def on_done(value):
            value = (value or "").strip()
            if required and not value:
                sublime.status_message("%s is required" % label)
                reopen(value)
                return
            if key == "base_url" and value:
                low = value.lower()
                if not (low.startswith("http://") or low.startswith("https://")):
                    sublime.status_message("base_url must start with http:// or https://")
                    reopen(value)
                    return
            if key == "name":
                providers = _load_custom_providers()
                if value != self._editing and value in providers:
                    sublime.status_message("A provider named '%s' already exists" % value)
                    reopen(value)
                    return
            if key == "effort" and value and value not in ("low", "medium", "high", "max"):
                sublime.status_message("effort must be one of: low, medium, high, max (or blank)")
                reopen(value)
                return
            self._values[key] = value
            if self._return_to_review:
                self._review()
                return
            self._step += 1
            self._prompt_field()

        def on_cancel():
            if self._return_to_review:
                self._review()
            else:
                sublime.status_message("Provider wizard cancelled")

        reopen(current)

    def _assembled_cfg(self):
        cfg = {}
        for key, label, example, required, secret in self._fields:
            if key == "name":
                continue
            v = (self._values.get(key) or "").strip()
            if v:
                cfg[key] = v
        if self._editing:
            old = _load_custom_providers().get(self._editing, {}) or {}
            for preserve in ("extra_env", "auth_via_api_key", "subagent_model", "pinned"):
                if preserve in old and preserve not in cfg:
                    cfg[preserve] = old[preserve]
        return cfg

    def _auth_display(self, cfg, name):
        if (cfg.get("auth_token") or "").strip():
            return "token " + _mask_secret(cfg["auth_token"])
        env = (cfg.get("auth_env_var") or "").strip()
        if env:
            resolved = provider_mod.resolve_auth_token(cfg, name)
            return "$%s %s" % (env, "✓ set" if resolved else "⚠ UNSET")
        return "⚠ no auth"

    def _review(self):
        name = self._values.get("name") or self._editing
        if not name:
            sublime.status_message("Provider not saved: no name")
            return
        cfg = self._assembled_cfg()
        label = cfg.get("label") or name
        auth = self._auth_display(cfg, name)
        model_parts = []
        for key, human in (("opus_model", "opus"), ("sonnet_model", "sonnet"), ("haiku_model", "haiku")):
            v = cfg.get(key)
            if v:
                model_parts.append("%s: %s" % (human, v))
        models = "  ".join(model_parts) if model_parts else "(no aliases — picker falls back to opus/sonnet/haiku)"
        items = [
            ["✓  Save '%s'" % label,
             "%s  •  %s  •  %s" % (cfg.get("base_url", "<no base_url>"), auth, models)],
            ["✎  Edit a field…", "Re-prompt any field, then return here"],
            ["✗  Cancel", "Discard — nothing saved"],
        ]
        actions = [("save", None), ("edit", None), ("cancel", None)]

        def on_select(idx):
            if idx < 0:
                return
            action, _ = actions[idx]
            if action == "save":
                self._commit()
            elif action == "edit":
                self._pick_field_to_edit()

        self.window.show_quick_panel(
            items, on_select, placeholder="Review provider '%s'" % name)

    def _pick_field_to_edit(self):
        items = []
        for key, label, example, required, secret in self._fields:
            v = self._values.get(key, "")
            shown = _mask_secret(v) if secret else (v or "(blank)")
            items.append(["%s" % label, shown])

        def on_select(idx):
            if idx < 0:
                self._review()
                return
            self._step = idx
            self._prompt_field(return_to_review=True)

        self.window.show_quick_panel(items, on_select, placeholder="Edit which field?")

    def _commit(self):
        name = self._values.get("name") or self._editing
        if not name:
            sublime.status_message("Provider not saved: no name")
            return
        cfg = self._assembled_cfg()
        providers = _load_custom_providers()
        if self._editing and self._editing != name:
            providers.pop(self._editing, None)
        providers[name] = cfg
        _save_custom_providers(providers)
        label = cfg.get("label", name)
        sublime.status_message("Saved provider '%s'" % label)
        self._show_main()

    def _duplicate(self, name):
        providers = _load_custom_providers()
        cfg = dict(providers.get(name, {}) or {})
        i = 2
        new_name = "%s_copy" % name
        while new_name in providers:
            new_name = "%s_copy%s" % (name, i)
            i += 1
        providers[new_name] = cfg
        _save_custom_providers(providers)
        sublime.status_message("Duplicated '%s' → '%s'" % (name, new_name))
        self._show_main()

    def _delete(self, name):
        providers = _load_custom_providers()
        if name not in providers:
            return

        def on_confirm(idx):
            if idx == 0:
                providers.pop(name, None)
                _save_custom_providers(providers)
                sublime.status_message("Deleted provider '%s'" % name)
            self._show_main()

        self.window.show_quick_panel(
            ["Yes, delete", "Cancel"],
            on_confirm,
            placeholder="Delete provider '%s'?" % name,
        )

    def _test(self, name):
        providers = _load_custom_providers()
        cfg = providers.get(name, {}) or {}
        problems = []
        warnings = []
        base = (cfg.get("base_url") or "").strip()
        auth_env_var = (cfg.get("auth_env_var") or "").strip()
        if not base:
            problems.append("missing base_url")
        elif not (base.lower().startswith("http://") or base.lower().startswith("https://")):
            problems.append("base_url is not a valid URL")
        eff_token = provider_mod.resolve_auth_token(cfg, name)
        if not eff_token:
            problems.append(
                "no auth_token and auth_env_var '%s' is unset" % (auth_env_var or "<blank>"))
        if eff_token:
            low = eff_token.lower()
            if ("your-" in low or "your_" in low or "yourkey" in low
                    or "sk-your" in low or "replace" in low or "xxxx" in low
                    or eff_token in ("your-api-key", "your-api_key")):
                warnings.append(
                    "resolved token looks like a PLACEHOLDER ('%s…') — likely from "
                    "~/.ccm_config clobbering the real key. Check `echo $%s` and "
                    "~/.ccm_config." % (eff_token[:20], auth_env_var or "<var>"))
        available = backend_specs.is_available(name)
        try:
            spec = backend_specs.get(name)
            overwrite, defaults = spec.dynamic_env({
                "custom_providers": providers,
            }) if spec.dynamic_env else ({}, {})

            def _m(k, v):
                if any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                    return _mask_secret(v) if v else "<empty>"
                return v
            env_preview = {k: _m(k, v) for k, v in dict(defaults, **overwrite).items()}
        except Exception as e:
            env_preview = {"<error>": str(e)}
        status = "OK" if (not problems and available and not warnings) else (
            "PROBLEM" if problems or not available else "WARNING")
        lines = ["Provider '%s': %s" % (name, status)]
        if problems:
            lines.append("  Issues: %s" % "; ".join(problems))
        if warnings:
            for w in warnings:
                lines.append("  ⚠ %s" % w)
        if not available:
            lines.append("  backends.is_available → False")
        lines.append("  Resolved env (masked):")
        for k in sorted(env_preview):
            lines.append("    %s = %s" % (k, env_preview[k]))
        sublime.message_dialog("\n".join(lines))


class SubmarineStartCustomProviderCommand(sublime_plugin.WindowCommand):
    def run(self):
        providers = _load_custom_providers()
        pinned = {n: c for n, c in providers.items() if (c or {}).get("pinned")}
        if not pinned:
            sublime.error_message(
                "No providers are pinned to the quick panel.\n\n"
                "Run 'Submarine: Manage Anthropic Providers' → Pin a provider.")
            return
        items = []
        names = []
        for name, cfg in pinned.items():
            cfg = cfg or {}
            label = cfg.get("label", name)
            avail = backend_specs.is_available(name)
            mark = "●" if avail else "○"
            items.append(["%s  %s" % (mark, label), _provider_summary(name, cfg)])
            names.append(name)

        def on_select(idx):
            if idx < 0:
                return
            name = names[idx]
            if not backend_specs.is_available(name):
                sublime.error_message(
                    "Provider '%s' is not usable (missing base_url or auth).\n"
                    "Run 'Submarine: Manage Anthropic Providers' → Test config." % name)
                return
            create_session(self.window, backend=name)

        self.window.show_quick_panel(items, on_select, placeholder="Start session on provider…")


class SubmarineGenerateProviderModelsCommand(sublime_plugin.WindowCommand):
    ALIASES = [("opus_model", "Opus"), ("sonnet_model", "Sonnet"), ("haiku_model", "Haiku")]

    def run(self, provider=None):
        providers = _load_custom_providers()
        if not providers:
            sublime.error_message(
                "No custom providers configured.\n\n"
                "Run 'Submarine: Manage Anthropic Providers' to add one first.")
            return
        if provider and provider in providers:
            self._fetch_then_pick(provider)
            return
        items = []
        names = []
        for name, cfg in providers.items():
            cfg = cfg or {}
            label = cfg.get("label", name)
            cur = " / ".join((cfg.get(a) or "—") for a, _ in self.ALIASES)
            items.append([label, "current: %s" % cur])
            names.append(name)

        def on_select(idx):
            if idx < 0:
                return
            self._fetch_then_pick(names[idx])

        self.window.show_quick_panel(
            items, on_select, placeholder="Pick provider to configure models…")

    def _resolve_auth(self, cfg, name=None):
        token = provider_mod.resolve_auth_token(cfg, name)
        if not token:
            return None, None
        if cfg.get("auth_via_api_key", False):
            return "x-api-key", token
        return "Authorization", "Bearer %s" % token

    def _fetch_then_pick(self, name):
        providers = _load_custom_providers()
        cfg = providers.get(name, {}) or {}
        base_url = (cfg.get("base_url") or "").strip()
        if not base_url:
            sublime.error_message("Provider '%s' has no base_url." % name)
            return
        sublime.status_message("Fetching models for '%s'…" % name)
        import threading

        def work():
            models = self._fetch_models(name, cfg, base_url)
            sublime.set_timeout(lambda: self._pick_aliases(name, models), 0)

        threading.Thread(target=work, daemon=True).start()

    def _fetch_models(self, name, cfg, base_url):
        import json as _json
        import urllib.request

        if "openrouter.ai" in base_url.lower():
            try:
                req = urllib.request.Request("https://openrouter.ai/api/v1/models")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = _json.loads(resp.read().decode())
                out = []
                for m in data.get("data", []):
                    mid = m.get("id", "")
                    if mid:
                        out.append([mid, mid])
                if out:
                    out.sort(key=lambda x: x[0])
                    return out
            except Exception as e:
                print("[Submarine] fetch openrouter models error: %s" % e)

        auth_h, auth_v = self._resolve_auth(cfg, name)
        url = base_url.rstrip("/") + "/v1/models"
        try:
            headers = {"anthropic-version": "2023-06-01"}
            if auth_h:
                headers[auth_h] = auth_v
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read().decode())
            out = []
            for m in data.get("data", []):
                mid = m.get("id", "")
                if not mid:
                    continue
                mname = m.get("display_name") or m.get("name") or mid
                out.append([mid, mname])
            if out:
                out.sort(key=lambda x: x[0])
                return out
        except Exception as e:
            print("[Submarine] fetch %s models error: %s" % (name, e))
        return []

    def _pick_aliases(self, name, models):
        providers = _load_custom_providers()
        cfg = providers.get(name, {}) or {}
        self._new_cfg = dict(cfg)
        self._models = models
        self._alias_idx = 0
        self._pick_one_alias(name)

    def _pick_one_alias(self, name):
        if self._alias_idx >= len(self.ALIASES):
            self._commit(name)
            return
        key, label = self.ALIASES[self._alias_idx]
        current = (self._new_cfg.get(key) or "").strip()
        items = []
        actions = []
        if current:
            items.append(["● %s  (current)" % current, "keep current %s" % label])
            actions.append(("model", current))
        for mid, mname in self._models:
            mark = "● " if mid == current else "  "
            items.append(["%s%s" % (mark, mname), mid])
            actions.append(("model", mid))
        items.append(["✎  Type a model id manually…", "enter an arbitrary model id"])
        actions.append(("manual", None))

        def on_select(idx):
            if idx < 0:
                sublime.status_message("Model config cancelled (nothing saved)")
                return
            action, data = actions[idx]
            if action == "model":
                self._new_cfg[key] = data
                self._alias_idx += 1
                self._pick_one_alias(name)
            elif action == "manual":
                def on_done(value):
                    value = (value or "").strip()
                    if not value:
                        sublime.status_message("Empty model id; %s not changed" % label)
                        self._pick_one_alias(name)
                        return
                    self._new_cfg[key] = value
                    self._alias_idx += 1
                    self._pick_one_alias(name)
                self.window.show_input_panel(
                    "%s alias → model id" % label, current, on_done, None, None)

        if self._models:
            placeholder = "%s alias (%s models fetched)" % (label, len(self._models))
        else:
            placeholder = "%s alias (no models fetched — type manually)" % label
        self.window.show_quick_panel(items, on_select, placeholder=placeholder)

    def _commit(self, name):
        providers = _load_custom_providers()
        providers[name] = self._new_cfg
        _save_custom_providers(providers)
        summary = " / ".join("%s=%s" % (l, self._new_cfg.get(k, "—")) for k, l in self.ALIASES)
        sublime.status_message("Saved %s models: %s" % (name, summary))


class SubmarineChangeProviderCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if not s:
            sublime.error_message("No active session to change.")
            return
        items, names = [], []
        try:
            from features import quota as quota_client
            quota_client.warm_cache()
        except Exception:
            quota_client = None  # type: ignore
        for name, spec in backend_specs.all_backends().items():
            if not _is_claude_bridge_spec(spec):
                continue
            label = spec.label or name
            avail = backend_specs.is_available(name)
            cur = "   (current)" if name == s.backend else ""
            avail_tag = "" if avail else "   (unavailable)"
            detail = "backend: %s" % name + ("" if avail else " — missing base_url/auth")
            if quota_client is not None and avail:
                try:
                    u = quota_client.usage_detail_for_backend(name, fallback="")
                    if u:
                        detail = "%s · %s" % (detail, u)
                except Exception:
                    pass
            items.append(["with %s…%s%s" % (label, cur, avail_tag), detail])
            names.append(name)
        if not items:
            sublime.error_message("No Claude-bridge providers configured.")
            return

        def on_select(idx):
            if idx < 0:
                return
            ok, detail = s.change_backend(names[idx])
            sublime.status_message(
                "Submarine: %s" % (
                    ("switched to %s — restarting with the session history"
                     % detail) if ok else detail))

        self.window.show_quick_panel(
            items, on_select, placeholder="Change provider for current session…")

    def is_enabled(self):
        return get_active_session(self.window) is not None


class SubmarineSelectEffortCommand(sublime_plugin.WindowCommand):
    """Effort for THIS session — live where the backend allows it."""

    def run(self):
        s = get_active_session(self.window)
        if not s:
            sublime.status_message("No active session")
            return
        levels = s.effort_levels()
        current = str(getattr(s, "effort", "") or "")
        items = [("● %s" % lv) if lv == current else ("   %s" % lv) for lv in levels]
        selected = levels.index(current) if current in levels else -1

        def report(ok, detail):
            sublime.set_timeout(
                lambda: sublime.status_message("Submarine: %s" % detail), 0)

        def on_select(idx):
            if idx < 0:
                return
            ok, detail = s.set_effort(levels[idx], on_done=report)
            sublime.status_message("Submarine: %s" % detail)

        self.window.show_quick_panel(
            items, on_select, selected_index=selected,
            placeholder="Effort for this session")

    def is_enabled(self):
        return get_active_session(self.window) is not None


class SubmarineSetDefaultEffortCommand(sublime_plugin.WindowCommand):
    """The `effort` setting — what new sessions start with."""

    LEVELS = ["low", "medium", "high", "xhigh", "max"]

    def run(self):
        settings = sublime.load_settings(SETTINGS_FILE)
        current = str(settings.get("effort") or "high")
        items = [("● %s" % lv) if lv == current else ("   %s" % lv) for lv in self.LEVELS]
        selected = self.LEVELS.index(current) if current in self.LEVELS else -1

        def on_select(idx):
            if idx < 0:
                return
            settings.set("effort", self.LEVELS[idx])
            sublime.save_settings(SETTINGS_FILE)
            sublime.status_message(
                "Submarine: default effort %s — new sessions start with it"
                % self.LEVELS[idx])

        self.window.show_quick_panel(
            items, on_select, selected_index=selected,
            placeholder="Default effort for new sessions")


def _apply_session_model(session, real_model):
    session.model = real_model
    try:
        if session.output and session.output.view:
            keys.write_setting(session.output.view.settings(), keys.MODEL, real_model)
        session._save_session()
    except Exception:
        pass


class SubmarineSelectModelCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if not s:
            sublime.error_message("No active Submarine session")
            return
        if s.working:
            sublime.error_message("Session is busy — wait for the current request to finish")
            return
        backend = s.backend
        models = self._get_models(backend)
        if not models:
            sublime.error_message(
                "No models for %s.\nRun 'Submarine: Refresh Models' first." % backend)
            return
        items = []
        model_ids = []
        for m in models:
            if isinstance(m, str):
                mid, mname = m, m
            elif isinstance(m, (list, tuple)) and len(m) >= 2:
                mid, mname = m[0], m[1]
            else:
                continue
            items.append([mname, mid])
            model_ids.append(mid)

        def on_select(idx):
            if idx < 0:
                return
            mid = model_ids[idx]
            real_model, ctx = resolve_model_id(mid)
            if not ctx:
                try:
                    cfg = (_load_custom_providers() or {}).get(s.backend) or {}
                    ctx = provider_mod.context_tokens_for_provider(cfg, mid)
                    if not ctx:
                        ctx = provider_mod.context_tokens_for_provider(
                            cfg, real_model)
                except Exception:
                    ctx = None
            if ctx:
                if sublime.ok_cancel_dialog(
                    "Context limit (%sK) requires session restart.\n\nRestart session with %s?"
                    % (ctx // 1000, mid),
                    "Restart",
                ):
                    _apply_session_model(s, real_model)
                    s.restart()
                return
            if s.backend == "grok":
                try:
                    from backend import grok as grok_backend
                    old = getattr(s, "model", None) or (
                        keys.read_setting(s.output.view.settings(), keys.MODEL)
                        if s.output and s.output.view else "")
                    old_v = grok_backend.model_supports_vision(old or "")
                    new_v = grok_backend.model_supports_vision(real_model)
                    if old_v != new_v:
                        if sublime.ok_cancel_dialog(
                            "Model '%s' changes vision support (%s).\n"
                            "Restart session so MCP read_image matches "
                            "(avoids broken image tool calls)?"
                            % (mid, "on" if new_v else "off"),
                            "Restart",
                        ):
                            _apply_session_model(s, real_model)
                            s.restart()
                        return
                except Exception as e:
                    print("[Submarine] vision check on set_model: %s" % e)
            if s.client:
                s.client.send("set_model", {"model": real_model})
            _apply_session_model(s, real_model)
            sublime.status_message("Model: %s" % mid)

        self.window.show_quick_panel(items, on_select)

    def _get_models(self, backend):
        settings = sublime.load_settings(SETTINGS_FILE)
        all_models = dict(settings.get("models", {}) or {})
        cached_file = os.path.expanduser("~/.claude/sublime_cached_models.json")
        if os.path.exists(cached_file):
            try:
                import json as _json
                with open(cached_file) as f:
                    cached = _json.load(f)
                for b, models in cached.items():
                    if b not in all_models:
                        all_models[b] = models
            except Exception:
                pass
        if backend not in all_models:
            try:
                all_models[backend] = [list(m) for m in backend_specs.get(backend).default_models]
            except Exception:
                all_models[backend] = backend_specs.default_models_dict().get(backend, [])
        models = list(all_models.get(backend, []) or [])
        if backend == "grok":
            try:
                from backend import grok as grok_backend
                extra = []
                s = get_active_session(self.window)
                if s is not None and getattr(s, "available_models", None):
                    extra.extend(s.available_models)
                models = [list(m) for m in grok_backend.grok_picker_models(extra=extra)]
            except Exception as e:
                print("[Submarine] grok model merge: %s" % e)
        elif backend == "opencode":
            # Live ACP catalog + `opencode models` outrank the on-disk cache,
            # which can predate the CLI fetch and pin the zen-only fallback.
            try:
                from backend import opencode as opencode_backend
                extra = []
                s = get_active_session(self.window)
                if s is not None and getattr(s, "available_models", None):
                    extra.extend(s.available_models)
                models = [list(m) for m in opencode_backend.opencode_picker_models(extra=extra)]
            except Exception as e:
                print("[Submarine] opencode model merge: %s" % e)
        return models


class SubmarineSetDefaultModelCommand(sublime_plugin.WindowCommand):
    def run(self):
        seen = set()
        backends_list = []
        for name in ("claude", "codex", "pi", "grok", "kimi", "opencode"):
            if backend_specs.is_available(name) or name == "claude":
                backends_list.append(name)
                seen.add(name)
        for name, spec in backend_specs.all_backends().items():
            if name in seen:
                continue
            if not spec.pinned:
                continue
            if spec.available is None or spec.available():
                backends_list.append(name)
                seen.add(name)
        items = [[b.title(), "Set default model for %s" % b] for b in backends_list]

        def on_backend(idx):
            if idx < 0:
                return
            backend = backends_list[idx]
            models = SubmarineSelectModelCommand._get_models(None, backend)
            if not models:
                sublime.status_message(
                    "No models for %s. Run Submarine: Refresh Models first." % backend)
                return
            model_items = []
            model_ids = []
            for m in models:
                if isinstance(m, str):
                    mid, mname = m, m
                elif isinstance(m, (list, tuple)) and len(m) >= 2:
                    mid, mname = m[0], m[1]
                else:
                    continue
                model_items.append([mname, mid])
                model_ids.append(mid)

            def on_model(midx):
                if midx < 0:
                    return
                mid = model_ids[midx]
                settings = sublime.load_settings(SETTINGS_FILE)
                defaults = dict(settings.get("default_models", {}) or {})
                defaults[backend] = mid
                settings.set("default_models", defaults)
                if backend == "claude":
                    settings.set("default_model", mid)
                sublime.save_settings(SETTINGS_FILE)
                sublime.status_message("Default %s model: %s" % (backend, mid))

            self.window.show_quick_panel(model_items, on_model)

        self.window.show_quick_panel(items, on_backend)


class SubmarineSetDefaultProviderCommand(sublime_plugin.WindowCommand):
    def run(self):
        settings = sublime.load_settings(SETTINGS_FILE)
        current_backend = settings.get("default_backend", "claude")
        default_models = settings.get("default_models", {}) or {}
        seen = set()
        backends_list = []
        for name in ("claude", "codex", "pi", "grok", "kimi", "opencode"):
            if name == "claude" or backend_specs.is_available(name):
                backends_list.append(name)
                seen.add(name)
        for name, spec in backend_specs.all_backends().items():
            if name in seen:
                continue
            if not spec.pinned:
                continue
            if spec.available is None or spec.available():
                backends_list.append(name)
                seen.add(name)
        items = []
        for name in backends_list:
            spec = backend_specs.get(name)
            label = spec.label or name
            is_current = (name == current_backend)
            effective = default_models.get(name) or spec.fallback_model or "—"
            mark = "● " if is_current else "  "
            detail = ("current default · model: %s" % effective if is_current
                      else "model: %s" % effective)
            items.append(["%s%s" % (mark, label), detail])

        def on_select(idx):
            if idx < 0:
                return
            self._save(backends_list[idx])

        placeholder = "Set default provider"
        cur_label = backend_specs.get(current_backend).label or current_backend
        cur_model = (default_models.get(current_backend)
                     or backend_specs.get(current_backend).fallback_model)
        placeholder += "  →  %s / %s" % (cur_label, cur_model or "—")
        self.window.show_quick_panel(items, on_select, placeholder=placeholder)

    def _save(self, backend):
        settings = sublime.load_settings(SETTINGS_FILE)
        settings.set("default_backend", backend)
        sublime.save_settings(SETTINGS_FILE)
        spec = backend_specs.get(backend)
        label = spec.label or backend
        sublime.status_message(
            "Default provider: %s (model: %s)" % (label, spec.fallback_model or "—"))


class SubmarineRefreshModelsCommand(sublime_plugin.WindowCommand):
    def run(self):
        import threading

        def fetch():
            import json as _json
            cached = {}
            try:
                import urllib.request
                api_key = os.environ.get("ANTHROPIC_API_KEY", "")
                if api_key:
                    req = urllib.request.Request(
                        "https://api.anthropic.com/v1/models",
                        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = _json.loads(resp.read().decode())
                    result = []
                    for m in data.get("data", []):
                        mid = m.get("id", "")
                        name = m.get("display_name", mid)
                        result.append([mid, name])
                    if result:
                        cached["claude"] = result
            except Exception as e:
                print("[Submarine] refresh models claude error: %s" % e)

            for backend_name, fallback_models in backend_specs.default_models_dict().items():
                if backend_name not in cached:
                    cached[backend_name] = fallback_models

            cache_path = os.path.expanduser("~/.claude/sublime_cached_models.json")
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w") as f:
                _json.dump(cached, f, indent=2)
            count = sum(len(v) for v in cached.values())
            sublime.set_timeout(
                lambda: sublime.status_message("Cached %s models" % count), 0)

        sublime.status_message("Fetching models...")
        threading.Thread(target=fetch, daemon=True).start()
