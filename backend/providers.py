"""Custom Anthropic-compatible providers. Sublime-free at import.

Settings `custom_providers.<name>` schema (all keys optional except base_url
for a usable provider):

    base_url            required for availability
    auth_token          inline (discouraged)
    auth_env_var        preferred (e.g. GLM_API_KEY)
    auth_via_api_key    if true, set ANTHROPIC_API_KEY and clear AUTH_TOKEN
    opus_model / sonnet_model / haiku_model / subagent_model
    label, abbrev, pinned (default False), effort, extra_env

Auth resolution (`resolve_auth_token`): inline token if it doesn't look like a
template → `os.environ[auth_env_var]` → legacy `settings.deepseek_api_key`
when name == "deepseek".

`dynamic_env` MUST:
- overwrite ANTHROPIC_BASE_URL + exactly one of AUTH_TOKEN / API_KEY
- forcibly clear the sibling auth var (leaked shell keys must not win)
- map aliases via ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL
- NEVER set ANTHROPIC_MODEL to a real id (clear it)
- also clear ANTHROPIC_SMALL_FAST_MODEL
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .specs import BackendSpec

try:
    from plat.constants import PLUGIN_NAME, SETTINGS_FILE
except ImportError:
    PLUGIN_NAME = "Submarine"
    SETTINGS_FILE = "Submarine.sublime-settings"


def valid_auth_token(token: Any) -> bool:
    """True for non-empty tokens that do not look like config templates."""
    if not token:
        return False
    low = str(token).strip().lower()
    if not low:
        return False
    if low == "from_env_var" or low.startswith("sk-your-"):
        return False
    if "your" in low and "api" in low and "key" in low:
        return False
    return True


def resolve_auth_token(
    cfg: Optional[dict],
    name: Optional[str] = None,
    settings: Optional[dict] = None,
) -> str:
    """Resolve the effective auth token for a custom-provider cfg.

    Order: inline auth_token → named auth_env_var → (deepseek legacy setting).
    Returns '' when no usable token is found.
    """
    cfg = cfg or {}
    token = (cfg.get("auth_token") or "").strip()
    if not valid_auth_token(token):
        token = ""
    auth_env_var = (cfg.get("auth_env_var") or "").strip()
    if not token and auth_env_var:
        env_token = os.environ.get(auth_env_var, "")
        if valid_auth_token(env_token):
            token = env_token
    if not token and name == "deepseek":
        # Legacy: old configs stored the key under top-level deepseek_api_key
        # instead of custom_providers.deepseek.auth_env_var.
        legacy = ""
        if isinstance(settings, dict):
            legacy = (settings.get("deepseek_api_key") or "").strip()
        else:
            try:
                import sublime
                legacy = (
                    sublime.load_settings(SETTINGS_FILE).get("deepseek_api_key", "")
                    or ""
                ).strip()
            except Exception:
                legacy = ""
        if valid_auth_token(legacy):
            token = legacy
    return token


def dynamic_env(settings: dict, name: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Build (overwrite, defaults) env for a custom Anthropic-compatible provider.

    overwrite: endpoint + one auth var (sibling cleared) + alias mappings.
    defaults: nonessential-traffic flags + user extra_env.
    """
    providers = (settings or {}).get("custom_providers", {}) or {}
    cfg = providers.get(name, {}) or {}
    base_url = (cfg.get("base_url") or "").strip()
    auth_env_var = (cfg.get("auth_env_var") or "").strip()
    auth_token = resolve_auth_token(cfg, name, settings)
    auth_via_key = bool(cfg.get("auth_via_api_key", False))

    overwrite = {}  # type: Dict[str, str]
    if base_url:
        overwrite["ANTHROPIC_BASE_URL"] = base_url
    if auth_via_key:
        if auth_token:
            overwrite["ANTHROPIC_API_KEY"] = auth_token
        overwrite["ANTHROPIC_AUTH_TOKEN"] = ""  # sibling must not win
    else:
        if auth_token:
            overwrite["ANTHROPIC_AUTH_TOKEN"] = auth_token
        overwrite["ANTHROPIC_API_KEY"] = ""  # sibling must not win

    if not base_url or not auth_token:
        src = (
            "settings.custom_providers.%s.auth_token" % name
            if not auth_env_var
            else "env $%s" % auth_env_var
        )
        print("[%s] WARNING: custom provider '%s' has no base_url or auth "
              "(%s). Requests will likely fail with 401." % (
                  PLUGIN_NAME, name, src))

    opus = (cfg.get("opus_model") or "").strip()
    sonnet = (cfg.get("sonnet_model") or "").strip()
    haiku = (cfg.get("haiku_model") or "").strip()
    subagent = (cfg.get("subagent_model") or "").strip()

    # Alias mappings must point at this provider, but do not force
    # ANTHROPIC_MODEL. The bridge passes --model opus/sonnet/haiku; Claude Code
    # then resolves that alias through ANTHROPIC_DEFAULT_*_MODEL. Forcing
    # ANTHROPIC_MODEL here changes that path and can pin every request to the
    # provider's heavy model. Still clear leaked single-model vars so a parent
    # shell's prior ccm selection cannot override the alias mapping.
    if opus:
        overwrite["ANTHROPIC_DEFAULT_OPUS_MODEL"] = opus
    if sonnet:
        overwrite["ANTHROPIC_DEFAULT_SONNET_MODEL"] = sonnet
    if haiku:
        overwrite["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = haiku
    overwrite["ANTHROPIC_MODEL"] = ""
    overwrite["ANTHROPIC_SMALL_FAST_MODEL"] = ""
    if subagent:
        overwrite["CLAUDE_CODE_SUBAGENT_MODEL"] = subagent
    else:
        overwrite["CLAUDE_CODE_SUBAGENT_MODEL"] = ""

    defaults = {
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
    }  # type: Dict[str, str]
    extra = cfg.get("extra_env") or {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            defaults[str(k)] = str(v)
    return overwrite, defaults


def provider_spec(name: str, cfg: dict) -> "BackendSpec":
    """Build a BackendSpec for a user-defined Anthropic-compatible provider."""
    from .specs import BackendSpec

    label = (cfg.get("label") or name).strip() or name
    abbrev = (cfg.get("abbrev") or name[:2].upper()).strip() or name[:2].upper()
    opus = (cfg.get("opus_model") or "").strip()
    sonnet = (cfg.get("sonnet_model") or "").strip()
    haiku = (cfg.get("haiku_model") or "").strip()
    default_models = []  # type: list
    if opus:
        default_models.append(("opus", "Opus → %s" % opus))
    if sonnet:
        default_models.append(("sonnet", "Sonnet → %s" % sonnet))
    if haiku:
        default_models.append(("haiku", "Haiku → %s" % haiku))
    if not default_models:
        default_models = [("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku")]

    def _available():
        if not (cfg.get("base_url") or "").strip():
            return False
        return bool(resolve_auth_token(cfg, name))

    # Closure captures `name`; the builder reads the live settings dict at call
    # time so edits to custom_providers take effect on the next session start.
    def _dyn(settings):
        return dynamic_env(settings, name)

    return BackendSpec(
        name=name,
        label=label,
        abbrev=abbrev,
        bridge_script="claude_main.py",
        fallback_model="opus",
        default_models=default_models,
        dynamic_env=_dyn,
        available=_available,
        pinned=bool(cfg.get("pinned", False)),
        effort=((cfg.get("effort") or "").strip() or None),
    )


def _settings_from_sublime():
    # type: () -> Optional[dict]
    try:
        import sublime
        s = sublime.load_settings(SETTINGS_FILE)
        return {
            "custom_providers": s.get("custom_providers", {}) or {},
            "deepseek_api_key": s.get("deepseek_api_key", "") or "",
        }
    except Exception:
        return None


def load_custom_providers(settings: Optional[dict] = None) -> Dict[str, "BackendSpec"]:
    """Read custom_providers from `settings` (or lazy Sublime load) → {name: BackendSpec}.

    Pass an injected settings dict in tests so this never needs sublime.
    """
    if settings is None:
        settings = _settings_from_sublime()
        if settings is None:
            return {}
    providers = (settings or {}).get("custom_providers", {}) or {}
    out = {}  # type: Dict[str, Any]
    if not isinstance(providers, dict):
        return out
    for name, cfg in providers.items():
        if not isinstance(cfg, dict):
            continue
        try:
            out[str(name)] = provider_spec(str(name), cfg)
        except Exception as e:
            print("[%s] skipping malformed custom provider '%s': %s" % (
                PLUGIN_NAME, name, e))
    return out
