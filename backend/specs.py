"""BackendSpec registry: built-ins + custom Anthropic-compatible providers.

Built-ins: claude, codex, kimi, grok, pi.

`all_backends(settings=None)` merges `custom_providers` on every call. Pass an
injected settings dict in tests (`{"custom_providers": {...}}`); when omitted,
settings are loaded lazily via sublime (and silently skipped without it).

Name-collision remap: a custom provider named like a built-in is stored as
`{name}_api` with label suffix `" (API)"`. Built-ins always win.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Tuple

from . import antigravity as antigravity_backend
from . import grok as grok_backend
from . import kimi as kimi_backend
from . import providers as provider_mod

try:
    from plat.constants import BACKEND_ABBREV, PLUGIN_NAME
except ImportError:
    BACKEND_ABBREV = {}
    PLUGIN_NAME = "Submarine"


def _pi_available():
    # type: () -> bool
    bun_pi = os.path.expanduser("~/.bun/install/global/node_modules/.bin/pi")
    if os.path.isfile(bun_pi):
        return True
    return bool(shutil.which("pi"))


def _codex_available():
    # type: () -> bool
    return bool(shutil.which("codex"))


def _grok_available():
    # type: () -> bool
    return grok_backend.grok_available()


def _antigravity_available():
    # type: () -> bool
    return antigravity_backend.antigravity_available()


def _kimi_available():
    # type: () -> bool
    try:
        return bool(kimi_backend.kimi_available())
    except Exception:
        return bool(
            os.environ.get("KIMI_BIN")
            or shutil.which("kimi")
            or os.path.isfile(os.path.expanduser("~/.kimi-code/bin/kimi"))
        )


@dataclass
class BackendSpec:
    name: str
    label: str
    abbrev: str
    bridge_script: str
    fallback_model: str
    default_models: List[Tuple[str, str]]
    theme: str = ""
    static_env: Dict[str, str] = field(default_factory=dict)
    dynamic_env: Optional[Callable[[dict], Tuple[Dict[str, str], Dict[str, str]]]] = None
    """Build env from runtime settings dict. Returns (overwrite, defaults).
    Called once per session start. Overwrite entries always replace; defaults use setdefault."""
    available: Optional[Callable[[], bool]] = None
    """Returns True if this backend can be used (CLI installed, API key set, etc)."""
    pinned: bool = True
    """If True the provider is eligible for the quick panels. Built-ins default
    True; custom providers default False (opt-in)."""
    effort: Optional[str] = None
    """Per-provider reasoning effort override. None → global `effort` setting."""


BACKENDS = {
    "claude": BackendSpec(
        name="claude",
        label="Claude",
        abbrev="",
        bridge_script="claude_main.py",
        fallback_model="opus",
        default_models=[
            ("claude-fable-5-1", "Fable 5.1"),
            # The CLI's alias (claude 2.1.280+ maps it to claude-opus-5-5).
            ("opus", "Opus 5.5"),
            # The CLI's alias: whichever Sonnet the installed claude maps it to.
            ("sonnet", "Sonnet (latest)"),
            # The CLI's alias (claude 2.1.294 maps it to claude-haiku-5-5).
            ("haiku", "Haiku 5.5"),
            ("claude-opus-5-5", "Opus 5.5 (pinned)"),
            ("claude-sonnet-5-5", "Sonnet 5.5 (pinned, claude 2.1.284+)"),
            ("claude-haiku-5-5", "Haiku 5.5 (pinned)"),
            ("claude-opus-5", "Opus 5 (pinned)"),
            ("claude-sonnet-5", "Sonnet 5 (pinned)"),
            ("claude-fable-5", "Fable 5"),
            ("claude-opus-4-8", "Opus 4.8"),
            ("claude-sonnet-4-6", "Sonnet 4.6"),
            ("claude-opus-4-7", "Opus 4.7"),
            ("claude-opus-4-6", "Opus 4.6"),
            ("claude-sonnet-4-5", "Sonnet 4.5"),
            ("claude-haiku-4-5", "Haiku 4.5"),
        ],
    ),
    "codex": BackendSpec(
        name="codex",
        label="Codex",
        abbrev="CX",
        bridge_script="codex_main.py",
        fallback_model="gpt-6-astra",
        # No fixed scheme: ui.themes gives Codex sheets the window theme's
        # green tint.
        default_models=[
            ("gpt-6-astra", "GPT-6 Astra"),
            # Codex's own default since 0.159 ("latest workhorse").
            ("gpt-6.1-sol", "GPT-6.1 Sol"),
            ("gpt-6-sol", "GPT-6 Sol"),
            ("gpt-6-luna", "GPT-6 Luna"),
            ("gpt-5.6-sol", "GPT-5.6 Sol"),
            ("gpt-5.6-terra", "GPT-5.6 Terra"),
            ("gpt-5.6-luna", "GPT-5.6 Luna"),
            ("gpt-5.5", "GPT-5.5"),
        ],
        available=_codex_available,
    ),
    "kimi": BackendSpec(
        name="kimi",
        label="Kimi Code",
        abbrev="KM",
        bridge_script="kimi_main.py",
        fallback_model="kimi-code/k3",
        default_models=list(kimi_backend.KIMI_MODELS),
        available=_kimi_available,
        pinned=True,
    ),
    # Google's official ACP server; signs in with the Google account.
    "antigravity": BackendSpec(
        name="antigravity",
        label="Antigravity",
        abbrev="AG",
        bridge_script="antigravity_main.py",
        fallback_model="gemini-3.8-flash-high",
        default_models=list(antigravity_backend.ANTIGRAVITY_MODELS),
        available=_antigravity_available,
        pinned=True,
    ),
    "grok": BackendSpec(
        name="grok",
        label="Grok",
        abbrev="GR",
        bridge_script="grok_main.py",
        fallback_model="grok-4.7",
        default_models=list(grok_backend.GROK_MODELS),
        available=_grok_available,
        pinned=True,
    ),
    "pi": BackendSpec(
        name="pi",
        label="Pi",
        abbrev="Pi",
        bridge_script="pi_main.py",
        fallback_model="claude-sonnet-5",
        default_models=[
            ("claude-fable-5-1", "Claude Fable 5.1"),
            ("claude-opus-5", "Claude Opus 5"),
            ("claude-sonnet-5", "Claude Sonnet 5"),
            ("claude-haiku-4-5", "Claude Haiku 4.5"),
            ("claude-fable-5", "Claude Fable 5"),
            ("claude-opus-4-8", "Claude Opus 4.8"),
            ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
            ("gpt-5.5", "GPT-5.5"),
        ],
        available=_pi_available,
    ),
}  # type: Dict[str, BackendSpec]


# Collision notices are hot-path (all_backends is called often). Log once per key.
_collision_notices = set()


def all_backends(settings: Optional[dict] = None) -> Dict[str, BackendSpec]:
    """All usable backends: built-ins ∪ custom_providers.

    Built-ins always win on name collision (e.g. native ACP ``kimi`` must not
    be replaced by settings.custom_providers.kimi). Colliding customs are
    re-homed as ``{name}_api``. Fresh-merged on every call.

    `settings` is an optional injected dict for tests (must contain
    `custom_providers` if customs should appear). When omitted, settings are
    loaded lazily from Sublime.
    """
    merged = dict(BACKENDS)  # type: Dict[str, BackendSpec]
    try:
        if "grok" in merged:
            merged["grok"] = replace(
                merged["grok"],
                default_models=list(grok_backend.grok_picker_models()),
            )
    except Exception as e:
        print("[%s] grok model catalog refresh failed: %s" % (PLUGIN_NAME, e))
    try:
        custom = provider_mod.load_custom_providers(settings)
    except Exception as e:
        print("[%s] failed to load custom providers: %s" % (PLUGIN_NAME, e))
        custom = {}
    for name, spec in custom.items():
        if name in BACKENDS:
            alt = "%s_api" % name
            if alt in merged or alt in BACKENDS:
                notice = "skip:%s:%s" % (name, alt)
                if notice not in _collision_notices:
                    _collision_notices.add(notice)
                    print(
                        "[%s] custom_providers.%s skipped: built-in '%s' "
                        "exists and '%s' is taken — rename the custom key" % (
                            PLUGIN_NAME, name, name, alt)
                    )
                continue
            try:
                merged[alt] = replace(
                    spec,
                    name=alt,
                    label=(spec.label or name) + " (API)",
                )
            except Exception:
                merged[alt] = spec
            notice = "remap:%s:%s" % (name, alt)
            if notice not in _collision_notices:
                _collision_notices.add(notice)
                print(
                    "[%s] custom_providers.%s → backend '%s' "
                    "(built-in '%s' is %s)" % (
                        PLUGIN_NAME, name, alt, name, BACKENDS[name].bridge_script)
                )
            continue
        merged[name] = spec
    return merged


def get(name: str, settings: Optional[dict] = None) -> BackendSpec:
    """Look up backend spec; falls back to claude if unknown."""
    return all_backends(settings).get(name, BACKENDS["claude"])


def abbrev_for(name: str, settings: Optional[dict] = None) -> str:
    """Tab / list abbrev: registry → BACKEND_ABBREV → first two letters."""
    b = (name or "claude").strip() or "claude"
    spec = get(b, settings)
    tok = (spec.abbrev or "").strip()
    if tok:
        return tok
    return BACKEND_ABBREV.get(b) or b[:2].upper()


def default_model_for(name: str, settings: Optional[dict] = None,
                      spec: Optional[BackendSpec] = None) -> str:
    """The model a new session on `name` starts with when none is asked for:
    the user's `default_models[name]`, else the backend's fallback, else the
    global `default_model`."""
    settings = settings or {}
    if spec is None:
        try:
            spec = get(name, settings)
        except Exception:
            spec = None
    return str(
        (settings.get("default_models") or {}).get(name)
        or (getattr(spec, "fallback_model", None) if spec else None)
        or settings.get("default_model")
        or "")


def is_available(name: str, settings: Optional[dict] = None) -> bool:
    """True if the backend can currently be used (defaults to True if no checker)."""
    spec = all_backends(settings).get(name)
    if spec is None:
        return False
    return spec.available is None or spec.available()


def default_models_dict(settings: Optional[dict] = None) -> Dict[str, List[List[str]]]:
    """Backwards-compat shape for legacy DEFAULT_MODELS consumers (list-of-lists)."""
    return {
        name: [list(m) for m in spec.default_models]
        for name, spec in all_backends(settings).items()
    }
