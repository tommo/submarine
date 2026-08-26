"""Grok ACP catalog + BYOK config.toml picker. Sublime-free.

Native path: `grok agent stdio`. No proxy, no OAuth.
"""
from __future__ import annotations

import os
import re
import shutil
from typing import Any, Iterable, List, Optional, Tuple

# Curated picker only (current defaults). Older grok-4.x stay in ALIASES
# for set_model if already saved, but are not listed.
GROK_MODELS = [  # type: List[Tuple[str, str]]
    ("grok-4.6", "Grok 4.6"),
    ("grok-composer-2.5-fast", "Composer 2.5"),
    ("deepseek-v4-pro", "DeepSeek V4 Pro"),
    ("deepseek-v4-flash", "DeepSeek V4 Flash"),
    ("deepseek-v4-flash-vision-exp", "DeepSeek V4 Flash Vision"),
]

# Never show these in the picker (legacy / noise from ACP availableModels).
_GROK_PICKER_HIDDEN = frozenset({
    "grok-4", "grok-4.0", "grok-4.1", "grok-4.2", "grok-4.3", "grok-4.5",
    "grok-4-fast", "grok-4-fast-non-reasoning", "grok-4-fast-reasoning",
    "grok-3", "grok-3-mini", "grok-2", "grok-2-mini",
    "grok-beta", "grok-vision-beta",
})

# Short aliases → wire modelId for spawn / set_model
GROK_MODEL_ALIASES = {
    "grok-4.6": "grok-4.6",
    "grok-4.5": "grok-4.6",  # legacy saved sessions → current default
    "grok-4-fast": "grok-composer-2.5-fast",  # legacy → current fast
    "grok-composer-2.5-fast": "grok-composer-2.5-fast",
    "composer": "grok-composer-2.5-fast",
    "composer-2.5": "grok-composer-2.5-fast",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "deepseek-v4-flash": "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-v4-flash-vision-exp",
    "deepseek-pro": "deepseek-v4-pro",
    "deepseek-flash": "deepseek-v4-flash",
    "deepseek-flash-vision": "deepseek-v4-flash-vision-exp",
    "ds-pro": "deepseek-v4-pro",
    "ds-flash": "deepseek-v4-flash",
    "ds-flash-vision": "deepseek-v4-flash-vision-exp",
    "ds-vision": "deepseek-v4-flash-vision-exp",
    "ds": "deepseek-v4-pro",
}


def _is_deepseek_model(model_id):
    # type: (str) -> bool
    m = (model_id or "").strip().lower()
    if not m:
        return False
    return (
        m.startswith("deepseek")
        or "deepseek" in m
        or m.startswith("ds-")
        or m in ("ds", "ds-pro", "ds-flash", "deepseek-pro", "deepseek-flash")
    )


def model_supports_reasoning_effort(model_id: str) -> bool:
    """DeepSeek BYOK via Grok is chat_completions — no reasoningEffort."""
    if not (model_id or "").strip():
        return True
    return not _is_deepseek_model(model_id)


def model_supports_vision(model_id: str) -> bool:
    """Most DeepSeek BYOK has no image/vision — read_image would error the turn.

    `deepseek-v4-flash-vision-exp` is the exception (multimodal).
    """
    if not (model_id or "").strip():
        return True  # unknown → keep Grok default vision
    wire = normalize_grok_model(model_id, default="")
    low = (wire or model_id).strip().lower()
    if "vision" in low:
        return True
    return not _is_deepseek_model(wire or model_id)


def normalize_grok_model(model_id: Optional[str], default: str = "grok-4.6") -> str:
    if not model_id:
        return default
    key = model_id.strip()
    return GROK_MODEL_ALIASES.get(key, GROK_MODEL_ALIASES.get(key.lower(), key))


def _grok_config_paths():
    # type: () -> List[str]
    home = os.environ.get("GROK_HOME") or os.path.expanduser("~/.grok")
    return [
        os.path.join(home, "config.toml"),
        os.path.expanduser("~/.grok/config.toml"),
    ]


def load_grok_config_models() -> List[Tuple[str, str]]:
    """Parse [model.<id>] blocks from ~/.grok/config.toml → [(id, label)].

    Lightweight TOML subset — only what we need for the picker. Grok Build
    already uses these for BYOK DeepSeek etc.
    """
    out = []  # type: List[Tuple[str, str]]
    seen = set()
    path = next((p for p in _grok_config_paths() if os.path.isfile(p)), None)
    if not path:
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return out

    section_re = re.compile(
        r'^\[model\.(?:"([^"]+)"|([A-Za-z0-9_.\-]+))\]\s*$', re.M)
    name_re = re.compile(r'^name\s*=\s*"([^"]*)"', re.M)
    model_re = re.compile(r'^model\s*=\s*"([^"]*)"', re.M)

    parts = section_re.split(text)
    # split yields: pre, g1, g2, body, g1, g2, body, ...
    i = 1
    while i + 2 < len(parts):
        mid = (parts[i] or parts[i + 1] or "").strip()
        body = parts[i + 2] if i + 2 < len(parts) else ""
        i += 3
        if not mid or mid in seen:
            continue
        cut = re.search(r'^\[', body, re.M)
        if cut:
            body = body[: cut.start()]
        display = None
        m = name_re.search(body)
        if m:
            display = m.group(1).strip()
        wire = mid
        m2 = model_re.search(body)
        if m2 and m2.group(1).strip():
            wire = m2.group(1).strip()
        label = display or wire
        picker_id = mid
        seen.add(mid)
        seen.add(wire)
        out.append((picker_id, label))
    return out


def _picker_hide(mid):
    # type: (str) -> bool
    """True if model should not appear in the UI list."""
    m = (mid or "").strip()
    if not m:
        return True
    if m in _GROK_PICKER_HIDDEN:
        return True
    curated = {x[0] for x in GROK_MODELS}
    low = m.lower()
    if low.startswith("grok-") and m not in curated:
        if any(low.startswith(p) for p in (
            "grok-2", "grok-3", "grok-4", "grok-beta", "grok-vision",
        )) and not low.startswith("grok-4.6"):
            return True
    return False


def grok_picker_models(extra: Optional[Iterable[Any]] = None) -> List[Tuple[str, str]]:
    """Short picker: curated + ~/.grok/config.toml BYOK + filtered ACP extras.

    extra: ACP availableModels — only non-hidden, non-legacy ids are kept.
    """
    merged = []  # type: List[Tuple[str, str]]
    seen = set()
    curated = {x[0] for x in GROK_MODELS}

    def add(mid, label=None, force=False):
        mid = (mid or "").strip()
        if not mid or mid in seen:
            return
        if not force and _picker_hide(mid) and mid not in curated:
            return
        seen.add(mid)
        merged.append((mid, (label or mid).strip() or mid))

    for mid, label in GROK_MODELS:
        add(mid, label, force=True)
    for mid, label in load_grok_config_models():
        if not _picker_hide(mid):
            add(mid, label, force=True)
    if extra:
        for item in extra:
            if isinstance(item, dict):
                mid = item.get("modelId") or item.get("model_id") or item.get("id")
                label = item.get("name") or item.get("label") or mid
                add(mid, label)
            elif isinstance(item, (list, tuple)) and item:
                add(item[0], item[1] if len(item) > 1 else item[0])
            elif isinstance(item, str):
                add(item, item)
    return merged


def grok_available() -> bool:
    """Native Grok Build ACP: `grok` CLI on PATH (or GROK_BIN)."""
    return bool(os.environ.get("GROK_BIN") or shutil.which("grok"))
