"""BackendSpec registry: built-ins + custom Anthropic-compatible providers.

Built-ins: claude, codex, kimi, grok, opencode, pi.

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

from . import grok as grok_backend
from . import kimi as kimi_backend
from . import opencode as opencode_backend
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


def _opencode_available():
    # type: () -> bool
    return opencode_backend.opencode_available()


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
            ("sonnet", "Sonnet 5"),
            ("haiku", "Haiku 4.5"),
            ("claude-opus-5-5", "Opus 5.5 (pinned)"),
            ("claude-opus-5", "Opus 5 (pinned)"),
            ("claude-sonnet-5", "Sonnet 5 (pinned)"),
            ("claude-fable-5", "Fable 5"),
            ("claude-opus-4-8", "Opus 4.8"),
            ("claude-sonnet-4-6", "Sonnet 4.6"),
            ("claude-opus-4-7", "Opus 4.7"),
            ("claude-opus-4-6", "Opus 4.6"),
            ("claude-sonnet-4-5", "Sonnet 4.5"),
        ],
    ),
    "codex": BackendSpec(
        name="codex",
        label="Codex",
        abbrev="CX",
        bridge_script="codex_main.py",
        fallback_model="gpt-6-astra",
        theme="Packages/Submarine/SubmarineOutput-codex.hidden-tmTheme",
        default_models=[
            ("gpt-6-astra", "GPT-6 Astra"),
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
    # Native opencode via ACP (`opencode acp`). Model ids are provider/model;
    # catalog refreshed in all_backends() from `opencode models`.
    "opencode": BackendSpec(
        name="opencode",
        label="opencode",
        abbrev="OC",
        bridge_script="opencode_main.py",
        fallback_model=opencode_backend.DEFAULT_MODEL,
        default_models=list(opencode_backend.OPENCODE_FALLBACK_MODELS),
        available=_opencode_available,
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
        if "opencode" in merged and _opencode_available():
            merged["opencode"] = replace(
                merged["opencode"],
                default_models=list(opencode_backend.opencode_picker_models()),
            )
    except Exception as e:
        print("[%s] opencode model catalog refresh failed: %s" % (PLUGIN_NAME, e))
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
