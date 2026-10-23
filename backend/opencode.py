"""opencode ACP helpers — pure Python (no Sublime).

Native path: `opencode acp` (Agent Client Protocol over stdio).

Model ids are `provider/model` ("opencode/big-pickle", "anthropic/claude-…").
Custom providers from opencode.json (e.g. a local Lemonade server) only
surface through `opencode models`, so the picker catalog is fetched from the
CLI off-thread and merged with the live per-session ACP catalog.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from typing import Any, Iterable, List, Optional, Tuple

# Default install location when not on PATH (opencode install script).
_DEFAULT_HOME_BIN = os.path.expanduser("~/.local/bin/opencode")

# Shown only until `opencode models` lands (warmed at plugin load). Kept to
# the CLI's own default: the free Zen lineup rotates, and a static list goes
# stale into dead ids.
OPENCODE_FALLBACK_MODELS = [  # type: List[Tuple[str, str]]
    ("opencode/big-pickle", "Zen / Big Pickle"),
]

# Must stay a real opencode id: an empty fallback makes the session fall
# through to the global `default_model` (a Claude id like "opus").
DEFAULT_MODEL = "opencode/big-pickle"

# Populated off-thread by _fetch_models(); None = not fetched yet.
_models_cache = None  # type: Optional[List[Tuple[str, str]]]
_fetch_started = False
_fetch_lock = threading.Lock()


def resolve_opencode_bin() -> str:
    """opencode CLI path: OPENCODE_BIN → PATH → ~/.local/bin. "" if absent."""
    cand = (os.environ.get("OPENCODE_BIN") or "").strip()
    if cand:
        return cand
    found = shutil.which("opencode")
    if found:
        return found
    return _DEFAULT_HOME_BIN if os.path.isfile(_DEFAULT_HOME_BIN) else ""


def opencode_available() -> bool:
    return bool(resolve_opencode_bin())


def agent_argv(cwd: Optional[str] = None) -> List[str]:
    return [resolve_opencode_bin() or "opencode", "acp",
            "--cwd", cwd or os.getcwd()]


def normalize_model(model_id: Optional[str]) -> str:
    """Keep only opencode ids (`provider/model`); anything else → "".

    Backend switching or the global default_model can hand us a Claude id
    like "opus"; sending that to session/set_model errors the session.
    "" leaves opencode on its own configured default.
    """
    mid = (model_id or "").strip()
    return mid if "/" in mid else ""


def parse_models_output(out: str) -> List[Tuple[str, str]]:
    """`opencode models` stdout → [(id, id)]. Skips banner / ANSI noise."""
    models = []  # type: List[Tuple[str, str]]
    seen = set()
    for line in (out or "").splitlines():
        mid = line.strip()
        if not mid or "/" not in mid or " " in mid or "\x1b" in mid:
            continue
        if mid in seen:
            continue
        seen.add(mid)
        models.append((mid, mid))
    return models


def _fetch_models() -> None:
    """Run `opencode models` once and cache the result (worker thread)."""
    global _models_cache
    binpath = resolve_opencode_bin()
    if not binpath:
        return
    try:
        out = subprocess.run(
            [binpath, "models"], capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception as e:
        print("[Submarine] opencode models failed: %s" % e)
        return
    models = parse_models_output(out)
    if models:
        _models_cache = models


def warm_catalog() -> None:
    """Start the `opencode models` fetch off-thread (idempotent).

    Called from plugin_loaded so the first picker after load already has the
    custom providers instead of the static zen-only fallback.
    """
    global _fetch_started
    with _fetch_lock:
        if _fetch_started or not opencode_available():
            return
        _fetch_started = True
    threading.Thread(target=_fetch_models, daemon=True).start()


def opencode_picker_models(extra: Optional[Iterable[Any]] = None) -> List[Tuple[str, str]]:
    """Picker catalog: live ACP models → `opencode models` → static fallback.

    extra: live session catalog (ACP availableModels dicts or (id, label)
    pairs). Earlier sources win on duplicate ids. all_backends() is a hot
    path, so this never blocks — it kicks off the CLI fetch if needed.
    """
    warm_catalog()
    merged = []  # type: List[Tuple[str, str]]
    seen = set()

    def add(mid, label=None):
        mid = str(mid or "").strip()
        if not mid or mid in seen:
            return
        seen.add(mid)
        merged.append((mid, str(label or mid).strip() or mid))

    for item in extra or []:
        if isinstance(item, dict):
            mid = item.get("modelId") or item.get("model_id") or item.get("id")
            add(mid, item.get("name") or item.get("label") or mid)
        elif isinstance(item, (list, tuple)) and item:
            add(item[0], item[1] if len(item) > 1 else item[0])
        elif isinstance(item, str):
            add(item)
    for mid, label in (_models_cache or OPENCODE_FALLBACK_MODELS):
        add(mid, label)
    return merged
