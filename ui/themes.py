"""Session-sheet themes: palettes, per-window choice, generated color schemes.

The shipped Ayu Mirage schemes (SubmarineOutput.hidden-tmTheme,
SessionList.hidden-color-scheme) are the templates: every color they use has
a role (`ROLES`), and a palette names the colors of those roles. A theme is
the template with each color swapped for its role's color in the palette —
so a rule added to the template is themed everywhere at once, and the default
theme is the template itself, unchanged.

Generated schemes go to Packages/User/Submarine/themes/ as
`.hidden-color-scheme` (not offered in Sublime's own scheme picker).

Choice, most specific first: the window's `submarine_theme` setting (saved
with the window), then `theme` in Submarine.sublime-settings, then
DEFAULT_THEME. Custom palettes: `custom_themes` in the same settings, each
inheriting a built-in one and overriding any role.
"""
from __future__ import annotations

import json
import os
import plistlib
from typing import Dict, List, Optional, Tuple

try:
    import sublime  # type: ignore
except ImportError:  # tests
    sublime = None  # type: ignore

DEFAULT_THEME = "ayu-mirage"
WINDOW_KEY = "submarine_theme"
SETTINGS_KEY = "theme"
CUSTOM_KEY = "custom_themes"

PACKAGE = "Packages/Submarine"
_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUTPUT_TEMPLATE = "SubmarineOutput.hidden-tmTheme"
_LIST_TEMPLATE = "SessionList.hidden-color-scheme"
_QUICK_ORIGINAL = PACKAGE + "/SubmarineOutput-quick.hidden-tmTheme"

# Kinds of scheme: the session sheet, its Codex and Quick Agent tints, and
# the session list.
OUTPUT, CODEX, QUICK, LIST = "output", "codex", "quick", "list"
KINDS = (OUTPUT, CODEX, QUICK, LIST)

# Template color → role. A role is a palette key, or ("mix", a, b, t): a
# blended with b, t being b's share (0..1). Mixing toward "bg" mutes a color
# on dark and light palettes alike.
ROLES = {
    "#1f2430": "bg",
    "#cbccc6": "fg",
    "#33415e": "sel",
    "#191e2a": "alt",
    "#5c6773": "comment",
    "#ffcc66": "yellow",
    "#ffad66": "orange",
    "#ffa759": "orange",
    "#f28779": "red",
    "#f07178": "red",
    "#ff6666": "red",
    "#87d96c": "green",
    "#bae67e": "green",
    "#95e6cb": "aqua",
    "#5ccfe6": "cyan",
    "#73d0ff": "blue",
    "#d4bfff": "purple",
    "#f29e74": ("mix", "orange", "red", 0.5),
    "#ffd580": ("mix", "yellow", "fg", 0.25),
    "#f0a0a4": ("mix", "red", "fg", 0.4),
    "#707a8c": ("mix", "fg", "bg", 0.45),
    "#7a8a9a": ("mix", "fg", "bg", 0.4),
    "#8a9199": ("mix", "fg", "bg", 0.35),
    "#8a93a3": ("mix", "fg", "bg", 0.3),
    "#9aa3af": ("mix", "fg", "bg", 0.25),
    "#a9b1bd": ("mix", "fg", "bg", 0.18),
    "#b0b8c6": ("mix", "fg", "bg", 0.15),
    "#4a5568": ("mix", "fg", "bg", 0.65),
    "#262d3a": ("mix", "fg", "bg", 0.9),
    "#2a3348": ("mix", "fg", "bg", 0.86),
    "#5a9484": ("mix", "aqua", "bg", 0.4),
    "#a06a74": ("mix", "red", "bg", 0.4),
    "#b08a4a": ("mix", "yellow", "bg", 0.4),
    "#678ac7": ("mix", "blue", "bg", 0.3),
    "#4f9fae": ("mix", "cyan", "bg", 0.3),
    "#4a92a0": ("mix", "cyan", "bg", 0.35),
}

ROLE_KEYS = ("bg", "fg", "alt", "sel", "comment", "red", "orange", "yellow",
             "green", "aqua", "cyan", "blue", "purple")

# Built-in palettes. "light": True marks a light background (the picker
# groups by it). ayu-mirage is the template's own colors.
PALETTES = {
    "ayu-mirage": dict(label="Ayu Mirage", bg="#1f2430", fg="#cbccc6", alt="#191e2a",
                       sel="#33415e", comment="#5c6773", red="#f28779", orange="#ffad66",
                       yellow="#ffcc66", green="#bae67e", aqua="#95e6cb", cyan="#5ccfe6",
                       blue="#73d0ff", purple="#d4bfff"),
    "ayu-dark": dict(label="Ayu Dark", bg="#0d1017", fg="#bfbdb6", alt="#0b0e14",
                     sel="#273747", comment="#626a73", red="#f07178", orange="#ff8f40",
                     yellow="#e6b450", green="#aad94c", aqua="#95e6cb", cyan="#39bae6",
                     blue="#59c2ff", purple="#d2a6ff"),
    "one-dark": dict(label="One Dark", bg="#282c34", fg="#abb2bf", alt="#21252b",
                     sel="#3e4451", comment="#5c6370", red="#e06c75", orange="#d19a66",
                     yellow="#e5c07b", green="#98c379", aqua="#56b6c2", cyan="#56b6c2",
                     blue="#61afef", purple="#c678dd"),
    "dracula": dict(label="Dracula", bg="#282a36", fg="#f8f8f2", alt="#21222c",
                    sel="#44475a", comment="#6272a4", red="#ff5555", orange="#ffb86c",
                    yellow="#f1fa8c", green="#50fa7b", aqua="#8be9fd", cyan="#8be9fd",
                    blue="#8be9fd", purple="#bd93f9"),
    "nord": dict(label="Nord", bg="#2e3440", fg="#d8dee9", alt="#272c36",
                 sel="#434c5e", comment="#616e88", red="#bf616a", orange="#d08770",
                 yellow="#ebcb8b", green="#a3be8c", aqua="#8fbcbb", cyan="#88c0d0",
                 blue="#81a1c1", purple="#b48ead"),
    "gruvbox-dark": dict(label="Gruvbox Dark", bg="#282828", fg="#ebdbb2", alt="#1d2021",
                         sel="#504945", comment="#928374", red="#fb4934", orange="#fe8019",
                         yellow="#fabd2f", green="#b8bb26", aqua="#8ec07c", cyan="#8ec07c",
                         blue="#83a598", purple="#d3869b"),
    "tokyo-night": dict(label="Tokyo Night", bg="#1a1b26", fg="#c0caf5", alt="#16161e",
                        sel="#283457", comment="#565f89", red="#f7768e", orange="#ff9e64",
                        yellow="#e0af68", green="#9ece6a", aqua="#73daca", cyan="#7dcfff",
                        blue="#7aa2f7", purple="#bb9af7"),
    "catppuccin-mocha": dict(label="Catppuccin Mocha", bg="#1e1e2e", fg="#cdd6f4",
                             alt="#181825", sel="#45475a", comment="#6c7086", red="#f38ba8",
                             orange="#fab387", yellow="#f9e2af", green="#a6e3a1",
                             aqua="#94e2d5", cyan="#89dceb", blue="#89b4fa", purple="#cba6f7"),
    "solarized-dark": dict(label="Solarized Dark", bg="#002b36", fg="#93a1a1", alt="#073642",
                           sel="#0d4a5a", comment="#586e75", red="#dc322f", orange="#cb4b16",
                           yellow="#b58900", green="#859900", aqua="#2aa198", cyan="#2aa198",
                           blue="#268bd2", purple="#6c71c4"),
    "github-dark": dict(label="GitHub Dark", bg="#0d1117", fg="#c9d1d9", alt="#161b22",
                        sel="#264f78", comment="#8b949e", red="#ff7b72", orange="#ffa657",
                        yellow="#e3b341", green="#7ee787", aqua="#56d4dd", cyan="#79c0ff",
                        blue="#58a6ff", purple="#d2a8ff"),
    "ayu-light": dict(label="Ayu Light", light=True, bg="#fcfcfc", fg="#5c6166",
                      alt="#f3f4f5", sel="#d1e4f4", comment="#9da2a6", red="#e65050",
                      orange="#ed9366", yellow="#c78b00", green="#6f9500", aqua="#3fa985",
                      cyan="#3797b8", blue="#2a87d4", purple="#9566c4"),
    "solarized-light": dict(label="Solarized Light", light=True, bg="#fdf6e3", fg="#586e75",
                            alt="#eee8d5", sel="#e3dcc6", comment="#93a1a1", red="#dc322f",
                            orange="#cb4b16", yellow="#b58900", green="#859900",
                            aqua="#2aa198", cyan="#2aa198", blue="#268bd2", purple="#6c71c4"),
    "github-light": dict(label="GitHub Light", light=True, bg="#ffffff", fg="#24292f",
                         alt="#f6f8fa", sel="#cfe3ff", comment="#6e7781", red="#cf222e",
                         orange="#bc4c00", yellow="#9a6700", green="#116329", aqua="#1b7c83",
                         cyan="#0550ae", blue="#0969da", purple="#8250df"),
    "catppuccin-latte": dict(label="Catppuccin Latte", light=True, bg="#eff1f5",
                             fg="#4c4f69", alt="#e6e9ef", sel="#ccd0da", comment="#8c8fa1",
                             red="#d20f39", orange="#fe640b", yellow="#df8e1d",
                             green="#40a02b", aqua="#179299", cyan="#04a5e5", blue="#1e66f5",
                             purple="#8839ef"),
}

# Codex and Quick Agent sheets keep their own tint whatever the theme: the
# background leans green (Codex) or warm (Quick) so the kind of sheet shows.
_TINTS = {CODEX: "#3fb950", QUICK: "#ff9e3b"}
_TINT_SHARE = {False: 0.09, True: 0.07}       # dark, light
# The template's own tints, kept exactly for the default theme.
_TEMPLATE_TINT_BG = {CODEX: "#1a2a1f"}


# ── colors ──────────────────────────────────────────────────────────────

def _rgb(c: str) -> Tuple[int, int, int]:
    c = c.strip().lstrip("#")
    if len(c) in (3, 4):
        c = "".join(ch * 2 for ch in c[:3])
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in rgb)


def mix(a: str, b: str, t: float) -> str:
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(x + (y - x) * t for x, y in zip(ra, rb)))


def _resolve(role, palette: dict) -> str:
    if isinstance(role, tuple):
        _, a, b, t = role
        return mix(_resolve(a, palette), _resolve(b, palette), t)
    return palette[role]


def _lum(c: str) -> float:
    def ch(x):
        x /= 255.0
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(c)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


_TEMPLATE_BG = "#1f2430"
_CONTRAST_CAP = 7.0


def readable(color: str, bg: str, template_color: str) -> str:
    """`color`, darkened (light bg) or lightened (dark bg) until it stands
    out from `bg` at least as much as `template_color` does in the template:
    faint things stay faint, nothing fades further than designed."""
    want = min(contrast(template_color, _TEMPLATE_BG), _CONTRAST_CAP)
    if contrast(color, bg) >= want:
        return color
    toward = "#000000" if _lum(bg) > 0.4 else "#ffffff"
    lo, hi = 0.0, 1.0
    for _ in range(14):
        mid = (lo + hi) / 2
        if contrast(mix(color, toward, mid), bg) >= want:
            hi = mid
        else:
            lo = mid
    return mix(color, toward, hi)


def color_map(palette: dict) -> Dict[str, str]:
    """Template color → this palette's color."""
    return {src: _resolve(role, palette) for src, role in ROLES.items()}


def _swap(value, cmap: Dict[str, str]):
    if isinstance(value, str) and value.startswith("#"):
        key = value.lower()
        if len(key) == 9:                       # #rrggbbaa: keep the alpha
            return cmap.get(key[:7], key[:7]) + key[7:]
        return cmap.get(key, value)
    return value


# ── palettes and the choice ─────────────────────────────────────────────

def _settings():
    if sublime is None:
        return None
    try:
        return sublime.load_settings("Submarine.sublime-settings")
    except Exception:
        return None


def palettes() -> Dict[str, dict]:
    """Built-ins plus `custom_themes` (each inheriting a built-in)."""
    out = dict(PALETTES)
    st = _settings()
    custom = (st.get(CUSTOM_KEY) if st is not None else None) or {}
    if isinstance(custom, dict):
        for name, spec in custom.items():
            if not isinstance(spec, dict):
                continue
            base = out.get(spec.get("inherits") or "", out[DEFAULT_THEME])
            merged = dict(base)
            merged["label"] = spec.get("label") or name
            for k, v in spec.items():
                if k in ROLE_KEYS and isinstance(v, str) and v.startswith("#"):
                    merged[k] = v
            if "light" in spec:
                merged["light"] = bool(spec["light"])
            out[str(name)] = merged
    return out


def default_theme() -> str:
    st = _settings()
    name = (st.get(SETTINGS_KEY) if st is not None else None) or DEFAULT_THEME
    return name if name in palettes() else DEFAULT_THEME


def window_theme_override(window) -> str:
    try:
        name = window.settings().get(WINDOW_KEY) if window else None
    except Exception:
        name = None
    return name if name and name in palettes() else ""


def theme_for_window(window) -> str:
    return window_theme_override(window) or default_theme()


# ── generation ──────────────────────────────────────────────────────────

_TEMPLATES = {}  # type: Dict[str, bytes]


def _read_template(name: str) -> bytes:
    if name not in _TEMPLATES:
        _TEMPLATES[name] = _load_template(name)
    return _TEMPLATES[name]


def _load_template(name: str) -> bytes:
    path = os.path.join(_PKG_DIR, name)
    if os.path.isfile(path):
        with open(path, "rb") as f:
            return f.read()
    if sublime is not None:                    # packed .sublime-package
        return sublime.load_binary_resource("%s/%s" % (PACKAGE, name))
    raise FileNotFoundError(path)


_TM_GLOBALS = {"background": "background", "foreground": "foreground", "caret": "caret",
               "selection": "selection", "lineHighlight": "line_highlight",
               "gutter": "gutter", "gutterForeground": "gutter_foreground",
               "selectionForeground": "selection_foreground",
               "invisibles": "invisibles", "findHighlight": "find_highlight"}


def _output_template() -> dict:
    """The tmTheme template as a .sublime-color-scheme dict."""
    plist = plistlib.loads(_read_template(_OUTPUT_TEMPLATE))
    entries = plist.get("settings") or []
    glob = {}
    rules = []
    for e in entries:
        st = e.get("settings") or {}
        if "scope" not in e:
            for k, v in st.items():
                glob[_TM_GLOBALS.get(k, k)] = v
            continue
        rule = {"name": e.get("name", ""), "scope": e["scope"]}
        for k, v in st.items():
            if k == "fontStyle":
                rule["font_style"] = v
            elif k in ("foreground", "background"):
                rule[k] = v
        rules.append(rule)
    return {"globals": glob, "rules": rules}


def _list_template() -> dict:
    return json.loads(_read_template(_LIST_TEMPLATE).decode("utf-8"))


def build_scheme(name: str, kind: str, pals: Optional[Dict[str, dict]] = None) -> dict:
    pals = pals or palettes()
    pal = pals.get(name) or pals[DEFAULT_THEME]
    tpl = _list_template() if kind == LIST else _output_template()
    identity = name == DEFAULT_THEME and pal is PALETTES.get(DEFAULT_THEME)
    cmap = {} if identity else color_map(pal)
    glob = {k: _swap(v, cmap) for k, v in tpl["globals"].items()}
    if kind in _TINTS:
        if identity and kind in _TEMPLATE_TINT_BG:
            bg = _TEMPLATE_TINT_BG[kind]
        else:
            bg = mix(pal["bg"], _TINTS[kind], _TINT_SHARE[bool(pal.get("light"))])
        glob["background"] = bg
        glob["gutter"] = bg
    rules = []
    for r in tpl["rules"]:
        rule = {k: _swap(v, cmap) for k, v in r.items()}
        fg, src = rule.get("foreground"), r.get("foreground")
        if cmap and fg and src and len(fg) == 7:
            rule["foreground"] = readable(fg, rule.get("background") or glob["background"],
                                          src.lower())
        rules.append(rule)
    gfg = glob.get("gutter_foreground")
    if cmap and gfg and len(gfg) == 7:
        glob["gutter_foreground"] = readable(
            gfg, glob["background"], tpl["globals"]["gutter_foreground"].lower())
    label = pal.get("label") or name
    suffix = {CODEX: " (Codex)", QUICK: " (Quick)", LIST: " (Session List)"}.get(kind, "")
    return {"name": "Submarine %s%s" % (label, suffix), "globals": glob, "rules": rules}


def _themes_dir() -> str:
    return os.path.join(sublime.packages_path(), "User", "Submarine", "themes")


def scheme_resource(name: str, kind: str) -> str:
    return "Packages/User/Submarine/themes/%s-%s.hidden-color-scheme" % (name, kind)


_written = {}  # type: Dict[str, str]   # file → content this process wrote
# (theme, kind) → resource path, once generated. The watcher runs on every
# settings change of a sheet, so a hit must not rebuild anything. Cleared
# when Submarine's settings change (a custom palette edited).
_ensured = {}  # type: Dict[Tuple[str, str], str]
_settings_hooked = [False]


def _hook_settings() -> None:
    if _settings_hooked[0]:
        return
    st = _settings()
    if st is None:
        return
    _settings_hooked[0] = True

    def _changed():
        _ensured.clear()
        _written.clear()
        if sublime is not None:
            sublime.set_timeout(apply_everywhere, 0)
    try:
        st.add_on_change("submarine_themes", _changed)
    except Exception:
        pass


def resolve(name: str, kind: str) -> Tuple[str, bool]:
    """ensure_scheme behind the cache."""
    _hook_settings()
    hit = _ensured.get((name, kind))
    if hit:
        return hit, False
    path, fresh = ensure_scheme(name, kind)
    _ensured[(name, kind)] = path
    return path, fresh


def ensure_scheme(name: str, kind: str) -> Tuple[str, bool]:
    """(resource path, freshly created). Writes only when content changed."""
    if name == DEFAULT_THEME and kind == QUICK and name not in _custom_names():
        return _QUICK_ORIGINAL, False           # the hand-made warm original
    path = os.path.join(_themes_dir(), "%s-%s.hidden-color-scheme" % (name, kind))
    text = json.dumps(build_scheme(name, kind), indent=2)
    if _written.get(path) == text:
        return scheme_resource(name, kind), False
    fresh = not os.path.isfile(path)
    old = None
    if not fresh:
        try:
            with open(path, encoding="utf-8") as f:
                old = f.read()
        except OSError:
            old = None
    if old != text:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    _written[path] = text
    return scheme_resource(name, kind), fresh


def _custom_names():
    st = _settings()
    custom = (st.get(CUSTOM_KEY) if st is not None else None) or {}
    return set(custom) if isinstance(custom, dict) else set()


# ── applying to views ───────────────────────────────────────────────────

def kind_for_view(view) -> Optional[str]:
    """Which scheme a Submarine view takes; None for other views."""
    from . import keys
    try:
        st = view.settings()
    except Exception:
        return None
    if st.get("submarine_session_list"):
        return LIST
    if not (keys.read_setting(st, keys.OUTPUT) or keys.read_setting(st, keys.QUICK)):
        return None
    if keys.read_setting(st, keys.QUICK):
        return QUICK
    backend = keys.read_setting(st, keys.BACKEND) or "claude"
    return CODEX if backend == "codex" else OUTPUT


def scheme_for(window, kind: str) -> str:
    """The resource path for this window's theme. Falls back to the shipped
    schemes when generating fails (no Packages dir, read-only disk)."""
    try:
        path, _fresh = resolve(theme_for_window(window), kind)
        return path
    except Exception as e:
        print("[Submarine] theme: %s" % e)
        return _shipped(kind)


def _shipped(kind: str) -> str:
    from . import keys
    return {LIST: keys.SESSION_LIST_SCHEME, QUICK: keys.THEME_QUICK,
            CODEX: keys.THEME_CODEX}.get(kind, keys.THEME_DEFAULT)


def _view_window(view):
    try:
        w = view.window()
    except Exception:
        w = None
    if w is None and sublime is not None:
        try:
            w = sublime.active_window()
        except Exception:
            w = None
    return w


def apply_to_view(view, window=None, kind: Optional[str] = None,
                  theme: Optional[str] = None) -> None:
    """Set the view's color_scheme to its window's theme (no-op when equal).

    A scheme file created just now may not be indexed yet; setting it at once
    can raise Sublime's "unable to load" error, so that one waits a moment.
    """
    kind = kind or kind_for_view(view)
    if kind is None:
        return
    window = window or _view_window(view)
    try:
        path, fresh = resolve(theme or theme_for_window(window), kind)
    except Exception as e:
        print("[Submarine] theme: %s" % e)
        path, fresh = _shipped(kind), False

    def _set(p=path):
        try:
            st = view.settings()
            if st.get("color_scheme") != p:
                st.set("color_scheme", p)
        except Exception:
            pass
    if fresh and sublime is not None:
        sublime.set_timeout(_set, 400)
    else:
        _set()


_watched = {}  # type: Dict[int, object]   # view id → view (panels included)


def watch_view(view) -> None:
    """Keep a Submarine view on its window's theme: any color_scheme some
    older code path sets (a shipped scheme, a backend's) is put back, and a
    backend change (Codex tint) is followed."""
    try:
        st = view.settings()
    except Exception:
        return
    tag = "submarine_theme_watch"
    _watched[view.id()] = view
    if st.get(tag):
        return
    st.set(tag, True)
    state = {"busy": False}

    def _changed():
        if state["busy"]:
            return
        state["busy"] = True
        try:
            apply_to_view(view)
        finally:
            state["busy"] = False
    try:
        st.add_on_change(tag, _changed)
    except Exception:
        pass


def submarine_views(window) -> List:
    """This window's Submarine views: its tabs, plus watched output panels
    (outside window.views())."""
    out, seen = [], set()
    try:
        views = list(window.views())
    except Exception:
        views = []
    for vid, v in list(_watched.items()):
        try:
            if not v.is_valid():
                _watched.pop(vid, None)
                continue
            w = v.window()
        except Exception:
            continue
        if w is not None and w.id() == window.id():
            views.append(v)
    for v in views:
        if v.id() in seen:
            continue
        seen.add(v.id())
        if kind_for_view(v) is not None:
            out.append(v)
    return out


def apply_to_window(window, theme: Optional[str] = None) -> None:
    """`theme` previews one without saving it (the picker's highlight)."""
    for v in submarine_views(window):
        apply_to_view(v, window, theme=theme)


def apply_everywhere() -> None:
    if sublime is None:
        return
    for w in sublime.windows():
        apply_to_window(w)


def set_window_theme(window, name: Optional[str]) -> None:
    """`name` None/"" clears the override: the window follows the default."""
    st = window.settings()
    if name:
        st.set(WINDOW_KEY, name)
    else:
        st.erase(WINDOW_KEY)
    apply_to_window(window)


def set_default_theme(name: str) -> None:
    st = _settings()
    if st is None:
        return
    st.set(SETTINGS_KEY, name)
    sublime.save_settings("Submarine.sublime-settings")
    apply_everywhere()


def picker_items(current: str) -> List[Tuple[str, str]]:
    """(name, label) for the picker: dark palettes, then light, built-ins
    before custom ones."""
    pals = palettes()
    def order(n):
        p = pals[n]
        return (bool(p.get("light")), n not in PALETTES, p.get("label") or n)
    return [(n, pals[n].get("label") or n) for n in sorted(pals, key=order)]
