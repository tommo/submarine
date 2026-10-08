"""Themes: palettes over the shipped templates, chosen per window.

Before: three fixed Ayu Mirage schemes (sheet, Codex, Quick) and one for the
session list; the Codex copy had drifted (rules missing), and no window
could look different from another.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import tempfile
import unittest
from unittest import mock

from tests.stubs import install_sublime

install_sublime()

from ui import keys, themes  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _template_colors():
    out = set()
    plist = plistlib.load(open(os.path.join(_ROOT, "SubmarineOutput.hidden-tmTheme"), "rb"))
    for e in plist["settings"]:
        for v in (e.get("settings") or {}).values():
            if isinstance(v, str) and v.startswith("#"):
                out.add(v.lower()[:7])
    lst = json.load(open(os.path.join(_ROOT, "SessionList.hidden-color-scheme")))
    for v in list(lst["globals"].values()) + [
            c for r in lst["rules"] for c in r.values()]:
        if isinstance(v, str) and v.startswith("#"):
            out.add(v.lower()[:7])
    return out


def _colors_of(scheme):
    vals = list(scheme["globals"].values()) + [
        v for r in scheme["rules"] for v in r.values()]
    return {v.lower()[:7] for v in vals if isinstance(v, str) and v.startswith("#")}


def _lum(c):
    def ch(x):
        x /= 255.0
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    r, g, b = themes._rgb(c)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class _Settings(object):
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, k, d=None):
        return self.data.get(k, d)

    def set(self, k, v):
        self.data[k] = v

    def erase(self, k):
        self.data.pop(k, None)

    def add_on_change(self, *a):
        pass


class _Window(object):
    def __init__(self, wid, views=(), theme=None):
        self._id = wid
        self._settings = _Settings({themes.WINDOW_KEY: theme} if theme else {})
        self._views = list(views)
        for v in self._views:
            v._window = self

    def id(self):
        return self._id

    def settings(self):
        return self._settings

    def views(self):
        return self._views


class _View(object):
    _next = [100]

    def __init__(self, **st):
        _View._next[0] += 1
        self._id = _View._next[0]
        self._settings = _Settings(st)
        self._window = None

    def id(self):
        return self._id

    def settings(self):
        return self._settings

    def window(self):
        return self._window

    def is_valid(self):
        return True


class TemplateTest(unittest.TestCase):
    def test_every_template_color_has_a_role(self):
        missing = _template_colors() - set(themes.ROLES)
        self.assertEqual(missing, set(), "give these template colors a role in ROLES")

    def test_every_palette_names_every_role(self):
        for name, pal in themes.PALETTES.items():
            for key in themes.ROLE_KEYS:
                self.assertIn(key, pal, "%s lacks %s" % (name, key))

    def test_default_theme_is_the_template_unchanged(self):
        built = themes.build_scheme(themes.DEFAULT_THEME, themes.OUTPUT)
        tpl = themes._output_template()
        self.assertEqual(built["globals"], tpl["globals"])
        self.assertEqual(built["rules"], tpl["rules"])

    def test_other_themes_leave_no_template_color_behind(self):
        tpl = _template_colors()
        for name, pal in themes.PALETTES.items():
            if name == themes.DEFAULT_THEME:
                continue
            own = {pal[k].lower() for k in themes.ROLE_KEYS}
            for kind in themes.KINDS:
                left = (_colors_of(themes.build_scheme(name, kind)) & tpl) - own
                self.assertEqual(left, set(), "%s/%s kept %s" % (name, kind, left))

    def test_text_is_readable_on_every_theme(self):
        for name in themes.PALETTES:
            for kind in themes.KINDS:
                g = themes.build_scheme(name, kind)["globals"]
                self.assertGreaterEqual(_contrast(g["foreground"], g["background"]), 4.5,
                                        "%s/%s body text" % (name, kind))

    def test_no_rule_is_fainter_than_the_template_made_it(self):
        tpl = {r["scope"]: r.get("foreground") for r in themes._output_template()["rules"]}
        for name in themes.PALETTES:
            for kind in (themes.OUTPUT, themes.LIST):
                s = themes.build_scheme(name, kind)
                bg = s["globals"]["background"]
                src = tpl if kind == themes.OUTPUT else {
                    r["scope"]: r.get("foreground") for r in themes._list_template()["rules"]}
                for r in s["rules"]:
                    if not r.get("foreground") or r.get("background") or not src.get(r["scope"]):
                        continue
                    want = min(_contrast(src[r["scope"]], "#1f2430"), 7.0)
                    self.assertGreaterEqual(_contrast(r["foreground"], bg) + 0.05, want,
                                            "%s/%s %s" % (name, kind, r["scope"]))

    def test_codex_and_quick_are_tinted_not_the_plain_sheet(self):
        for name in ("one-dark", "github-light"):
            plain = themes.build_scheme(name, themes.OUTPUT)["globals"]["background"]
            codex = themes.build_scheme(name, themes.CODEX)["globals"]["background"]
            quick = themes.build_scheme(name, themes.QUICK)["globals"]["background"]
            self.assertEqual(len({plain, codex, quick}), 3)
            r, g, b = themes._rgb(codex)
            pr, pg, pb = themes._rgb(plain)
            self.assertGreater(g - pg, r - pr)        # leans green

    def test_codex_keeps_every_rule_of_the_sheet(self):
        # The shipped Codex copy had lost rules (retry hint, answered question…).
        sheet = [r["scope"] for r in themes.build_scheme("ayu-mirage", themes.OUTPUT)["rules"]]
        codex = [r["scope"] for r in themes.build_scheme("ayu-mirage", themes.CODEX)["rules"]]
        self.assertEqual(sheet, codex)
        self.assertEqual(themes.build_scheme("ayu-mirage", themes.CODEX)["globals"]["background"],
                         "#1a2a1f")

    def test_alpha_is_kept_when_swapping(self):
        cmap = {"#1f2430": "#000000"}
        self.assertEqual(themes._swap("#1F243080", cmap), "#00000080")


class ChoiceTest(unittest.TestCase):
    def setUp(self):
        self.settings = _Settings()
        p = mock.patch.object(themes, "_settings", lambda: self.settings)
        p.start()
        self.addCleanup(p.stop)

    def test_window_override_beats_default(self):
        self.settings.set("theme", "nord")
        self.assertEqual(themes.theme_for_window(_Window(1)), "nord")
        self.assertEqual(themes.theme_for_window(_Window(2, theme="dracula")), "dracula")

    def test_unknown_names_fall_back(self):
        self.settings.set("theme", "nope")
        self.assertEqual(themes.default_theme(), themes.DEFAULT_THEME)
        self.assertEqual(themes.theme_for_window(_Window(1, theme="gone")), themes.DEFAULT_THEME)

    def test_custom_theme_inherits_and_overrides(self):
        self.settings.set("custom_themes", {"midnight": {
            "inherits": "tokyo-night", "label": "Midnight", "bg": "#101018", "light": False}})
        pals = themes.palettes()
        self.assertEqual(pals["midnight"]["bg"], "#101018")
        self.assertEqual(pals["midnight"]["blue"], themes.PALETTES["tokyo-night"]["blue"])
        built = themes.build_scheme("midnight", themes.OUTPUT)
        self.assertEqual(built["globals"]["background"], "#101018")
        self.assertIn("midnight", [n for n, _ in themes.picker_items("midnight")])

    def test_picker_lists_dark_before_light(self):
        items = [n for n, _ in themes.picker_items("ayu-mirage")]
        lights = [i for i, n in enumerate(items) if themes.PALETTES[n].get("light")]
        darks = [i for i, n in enumerate(items) if not themes.PALETTES[n].get("light")]
        self.assertLess(max(darks), min(lights))


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.settings = _Settings()
        for name, value in (("_settings", lambda: self.settings),
                            ("_themes_dir", lambda: self.tmp),
                            ("sublime", None)):
            p = mock.patch.object(themes, name, value)
            p.start()
            self.addCleanup(p.stop)
        themes._ensured.clear()
        themes._written.clear()
        themes._watched.clear()

    def _sheet(self, backend="claude", **extra):
        st = {keys.OUTPUT: True, keys.BACKEND: backend}
        st.update(extra)
        return _View(**st)

    def test_each_window_gets_its_own_theme(self):
        a, b = self._sheet(), self._sheet()
        wa, wb = _Window(1, [a], theme="nord"), _Window(2, [b], theme="github-light")
        themes.apply_to_window(wa)
        themes.apply_to_window(wb)
        self.assertEqual(a.settings().get("color_scheme"),
                         themes.scheme_resource("nord", themes.OUTPUT))
        self.assertEqual(b.settings().get("color_scheme"),
                         themes.scheme_resource("github-light", themes.OUTPUT))
        written = json.load(open(os.path.join(self.tmp, "nord-output.hidden-color-scheme")))
        self.assertEqual(written["globals"]["background"], "#2e3440")

    def test_kinds(self):
        self.assertEqual(themes.kind_for_view(self._sheet()), themes.OUTPUT)
        self.assertEqual(themes.kind_for_view(self._sheet("codex")), themes.CODEX)
        self.assertEqual(themes.kind_for_view(self._sheet(**{keys.QUICK: True})), themes.QUICK)
        self.assertEqual(themes.kind_for_view(_View(**{keys.SESSION_LIST: True})), themes.LIST)
        self.assertIsNone(themes.kind_for_view(_View()))      # a file: not ours

    def test_set_window_theme_and_clear(self):
        v = self._sheet()
        w = _Window(1, [v])
        self.settings.set("theme", "dracula")
        themes.set_window_theme(w, "nord")
        self.assertEqual(w.settings().get(themes.WINDOW_KEY), "nord")
        self.assertIn("nord-output", v.settings().get("color_scheme"))
        themes.set_window_theme(w, None)
        self.assertIsNone(w.settings().get(themes.WINDOW_KEY))
        self.assertIn("dracula-output", v.settings().get("color_scheme"))

    def test_preview_does_not_save(self):
        v = self._sheet()
        w = _Window(1, [v])
        themes.apply_to_window(w, theme="gruvbox-dark")
        self.assertIn("gruvbox-dark", v.settings().get("color_scheme"))
        self.assertIsNone(w.settings().get(themes.WINDOW_KEY))

    def test_default_quick_keeps_the_hand_made_scheme(self):
        v = self._sheet(**{keys.QUICK: True})
        themes.apply_to_window(_Window(1, [v]))
        self.assertEqual(v.settings().get("color_scheme"), themes._QUICK_ORIGINAL)

    def test_file_is_rewritten_only_when_it_changes(self):
        path, fresh = themes.ensure_scheme("nord", themes.OUTPUT)
        self.assertTrue(fresh)
        f = os.path.join(self.tmp, "nord-output.hidden-color-scheme")
        mtime = os.path.getmtime(f)
        themes._written.clear()
        os.utime(f, (mtime - 100, mtime - 100))
        _, fresh = themes.ensure_scheme("nord", themes.OUTPUT)
        self.assertFalse(fresh)
        self.assertEqual(os.path.getmtime(f), mtime - 100)

    def test_watched_panel_counts_as_the_windows(self):
        panel = self._sheet()
        w = _Window(1, [])
        panel._window = w
        themes.watch_view(panel)
        themes.apply_to_window(w, theme="nord")
        self.assertIn("nord", panel.settings().get("color_scheme"))


if __name__ == "__main__":
    unittest.main()
