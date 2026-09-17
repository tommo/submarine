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
    """Whether sublime MCP read_image should be advertised for this Grok model.

    Native Grok: yes. DeepSeek V4 / V4.1 BYOK (pro, flash, vision-exp, and
    wire aliases like deepseek-v4.1-flash-expires-on-*) : yes — ACP
    read_file still rejects binary, so they need sublime__read_image.
    Older DeepSeek without v4: no (tool call can hard-fail the turn).
    """
    if not (model_id or "").strip():
        return True  # unknown → keep Grok default vision
    wire = normalize_grok_model(model_id, default="")
    low = (wire or model_id).strip().lower()
    if "vision" in low:
        return True
    if not _is_deepseek_model(wire or model_id):
        return True
    # v4, v4.1, v41, deepseek-v4-flash, expires-on aliases
    if "v4" in low:
        return True
    return False


# ─── Context-window overflow (shared window + packed prompt) ──────────────────
# DeepSeek V4 hosted API: input + reserved max_tokens share this envelope.
# Grok still sends the full max_completion_tokens (default 384k) on every
# request, so usable prompt is context − reservation. Auto-compact at 85% of
# a 1_000_000 context_window fires at 850k — after the 664,576 cliff.
DEEPSEEK_V4_CONTEXT_TOKENS = 1_048_576
DEEPSEEK_V4_MAX_OUTPUT_TOKENS = 384_000
_SHARED_WINDOW_COMMENT = (
    "# submarine: usable input (API context minus reserved "
    "max_completion; Grok auto-compacts at 85% of this and still sends "
    "the full reservation)"
)
_OVERFLOW_RE = re.compile(
    r"maximum context length is (?P<max>\d+)\s*tokens?"
    r".*?requested (?P<requested>\d+)\s*tokens?"
    r".*?\((?P<messages>\d+)\s+in the messages,\s*"
    r"(?P<completion>\d+)\s+in the completion\)",
    re.I | re.S,
)
_INPUT_TOO_LARGE_RE = re.compile(
    r"(?:input_too_large.{0,80})?prompt is too long.{0,80}?"
    r"context window\s*\((?P<prompt>\d+)\s*tokens?\s*>\s*"
    r"(?P<max>\d+)\s*tokens?\)",
    re.I | re.S,
)


def usable_prompt_tokens(context: int, max_completion: int) -> int:
    """Largest prompt that still fits with a reserved completion budget."""
    try:
        ctx = int(context)
        out = int(max_completion)
    except (TypeError, ValueError):
        return 0
    return max(0, ctx - out)


def parse_shared_window_overflow(text: str):
    """Parse DeepSeek/OpenAI shared-window 400: messages + completion > cap.

    Returns None if `text` is not that error. Extra promptUsage fields in the
    same blob (session-cumulative inputTokens / cachedReadTokens) are ignored
    — they are not the in-window size.
    """
    if not text:
        return None
    m = _OVERFLOW_RE.search(str(text))
    if not m:
        return None
    max_ctx = int(m.group("max"))
    requested = int(m.group("requested"))
    messages = int(m.group("messages"))
    completion = int(m.group("completion"))
    return {
        "max_context": max_ctx,
        "requested": requested,
        "messages": messages,
        "completion": completion,
        "over_by": requested - max_ctx,
        "usable_input": usable_prompt_tokens(max_ctx, completion),
    }


def format_shared_window_overflow(parsed: dict) -> str:
    """One-line host error: math, not the 11M cumulative cache ledger."""
    messages = int(parsed.get("messages") or 0)
    completion = int(parsed.get("completion") or 0)
    max_ctx = int(parsed.get("max_context") or 0)
    requested = int(parsed.get("requested") or (messages + completion))
    over = int(parsed.get("over_by") or (requested - max_ctx))
    usable = int(parsed.get("usable_input") or usable_prompt_tokens(
        max_ctx, completion))
    return (
        f"DeepSeek shares a {max_ctx}-token window between input and "
        f"reserved output. This request: {messages} messages + "
        f"{completion} completion = {requested} ({over} over the cap). "
        f"Grok sent the full max_completion_tokens instead of clamping. "
        f"Usable input with that reservation is {usable}. Compact or "
        f"start a new session. promptUsage.inputTokens is session-cumulative "
        f"(cached reads), not the in-window size."
    )


def parse_input_too_large(text: str):
    """Parse Grok Build 400: packed prompt > model window.

    Occupancy (`promptUsage.inputTokens`) is often far below this packed
    size, so auto-compact at 85% of context_tokens never fires.
    """
    if not text:
        return None
    m = _INPUT_TOO_LARGE_RE.search(str(text))
    if not m:
        return None
    prompt = int(m.group("prompt"))
    max_ctx = int(m.group("max"))
    return {
        "max_context": max_ctx,
        "prompt": prompt,
        "over_by": prompt - max_ctx,
        "kind": "input_too_large",
    }


def format_input_too_large(parsed: dict) -> str:
    prompt = int(parsed.get("prompt") or 0)
    max_ctx = int(parsed.get("max_context") or 0)
    over = int(parsed.get("over_by") or (prompt - max_ctx))
    trip = int(max_ctx * 0.85)
    return (
        f"Grok packed prompt {prompt} tokens > {max_ctx} window "
        f"({over} over). Auto-compact trips at 85% of occupancy "
        f"(~{trip}), not the API packed size — occupancy often "
        f"undercounts tools/skills so it never fires. Compact or "
        f"start a new session. promptUsage.inputTokens is billed "
        f"input (mostly cache), not the packed prompt."
    )


def rewrite_grok_query_error(text: str) -> str:
    """Keep non-overflow errors intact; rewrite window 400s."""
    raw = text if isinstance(text, str) else str(text)
    parsed = parse_shared_window_overflow(raw)
    if parsed:
        return format_shared_window_overflow(parsed)
    too_big = parse_input_too_large(raw)
    if too_big:
        return format_input_too_large(too_big)
    return raw


def is_context_overflow_error(text: str) -> bool:
    """True if Grok/DeepSeek rejected the turn for context length."""
    raw = text if isinstance(text, str) else str(text)
    return bool(
        parse_shared_window_overflow(raw) or parse_input_too_large(raw)
    )


def _toml_int_assign(body: str, key: str, default=None):
    pat = re.compile(
        r"^" + re.escape(key) + r"\s*=\s*([0-9_]+)\s*$", re.M)
    m = pat.search(body or "")
    if not m:
        return default
    try:
        return int(m.group(1).replace("_", ""))
    except (TypeError, ValueError):
        return default


def _is_deepseek_v4_section(mid: str, body: str) -> bool:
    blob = f"{mid}\n{body or ''}".lower()
    if not _is_deepseek_model(mid) and "deepseek" not in blob:
        return False
    if "v4" in blob:
        return True
    # picker aliases without v4 in the section key still point at V4
    if any(s in blob for s in ("flash", "pro", "vision")):
        return "chat" not in (mid or "").lower() and "reasoner" not in blob
    return False


def _set_toml_int(body: str, key: str, value: int, comment: str = ""):
    """Replace or insert `key = value`. Returns (body, changed)."""
    want = f"{key} = {int(value)}"
    pat = re.compile(
        r"^" + re.escape(key) + r"\s*=\s*[0-9_]+\s*$", re.M)
    m = pat.search(body or "")
    block = ((comment + "\n") if comment else "") + want
    if m:
        before = body[: m.start()]
        if comment and before.rstrip().endswith(comment.strip()):
            if m.group(0) == want:
                return body, False
            return before + want + body[m.end():], True
        replacement = block
        if replacement == m.group(0):
            return body, False
        return before + replacement + body[m.end():], True
    insert = ("\n" if body and not body.endswith("\n") else "") + block + "\n"
    return (body or "") + insert, True


def patch_deepseek_shared_window_toml(text: str) -> str:
    """Shrink DeepSeek V4 context_window to API context − max_completion.

    Grok auto-compacts at 85% of context_window and still sends the full
    max_completion_tokens. A 1_000_000 window + 384_000 reservation overflows
    at 664_576 in-window tokens — before that 85% trip.
    """
    if not text:
        return text
    section_re = re.compile(
        r'^\[model\.(?:"([^"]+)"|([A-Za-z0-9_.\-]+))\]\s*$', re.M)
    parts = section_re.split(text)
    if len(parts) < 4:
        return text
    out = [parts[0]]
    i = 1
    changed = False
    while i + 2 < len(parts):
        g1, g2, body = parts[i], parts[i + 1], parts[i + 2]
        mid = (g1 or g2 or "").strip()
        if g1:
            header = f'[model."{g1}"]'
        else:
            header = f"[model.{g2}]"
        nxt = ""
        cut = re.search(r"^\[", body, re.M)
        if cut:
            nxt = body[cut.start():]
            body = body[: cut.start()]
        if _is_deepseek_v4_section(mid, body):
            max_out = _toml_int_assign(
                body, "max_completion_tokens", DEEPSEEK_V4_MAX_OUTPUT_TOKENS)
            ctx = _toml_int_assign(
                body, "context_window", DEEPSEEK_V4_CONTEXT_TOKENS)
            safe = usable_prompt_tokens(DEEPSEEK_V4_CONTEXT_TOKENS, max_out)
            if ctx is None or ctx > safe:
                body, did = _set_toml_int(
                    body, "context_window", safe, comment=_SHARED_WINDOW_COMMENT)
                changed = changed or did
        out.append(header)
        if body and not body.startswith("\n"):
            out.append("\n")
        out.append(body)
        out.append(nxt)
        i += 3
    new = "".join(out)
    return new if changed else text


def apply_deepseek_shared_window_config(path=None) -> bool:
    """Patch grok config.toml on disk. True if the file changed."""
    if not path:
        path = next((p for p in _grok_config_paths() if os.path.isfile(p)), None)
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    new = patch_deepseek_shared_window_toml(text)
    if new == text:
        return False
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
    except OSError:
        return False
    return True


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
