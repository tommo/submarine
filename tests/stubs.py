"""Shared sublime / sublime_plugin fakes for feature tests that import UI."""
from __future__ import annotations

import sys
import types


_settings_store = {}


class FakeSettings:
    def get(self, k, d=None):
        return _settings_store.get(k, d)

    def set(self, k, v):
        _settings_store[k] = v

    def erase(self, k):
        _settings_store.pop(k, None)

    def has(self, k):
        return k in _settings_store

    def add_on_change(self, key, fn):
        pass

    def clear_on_change(self, key):
        pass


def install():
    """Alias used by UI tests."""
    return install_sublime()


class FakeView:
    def __init__(self, vid=1):
        self._id = vid
        self._settings = FakeSettings()
        self._name = ""
        self._scratch = False
        self._read_only = False
        self._size = 0

    def id(self):
        return self._id

    def is_valid(self):
        return True

    def settings(self):
        return self._settings

    def set_name(self, name):
        self._name = name

    def set_scratch(self, v):
        self._scratch = v

    def set_read_only(self, v):
        self._read_only = v

    def size(self):
        return self._size

    def sel(self):
        return []

    def assign_syntax(self, path):
        pass


class FakeWindow:
    def __init__(self):
        self._id = 1
        self._settings = FakeSettings()
        self._views = []
        self._folders = []
        self._active = None

    def id(self):
        return self._id

    def settings(self):
        return self._settings

    def folders(self):
        return list(self._folders)

    def views(self):
        return list(self._views)

    def active_view(self):
        return self._active

    def focus_view(self, v):
        self._active = v

    def new_file(self):
        v = FakeView(len(self._views) + 1)
        self._views.append(v)
        self._active = v
        return v

    def open_file(self, path, flags=0):
        return self.new_file()


def install_sublime():
    """Install sublime + sublime_plugin stubs. Idempotent."""
    if "sublime" in sys.modules and getattr(sys.modules["sublime"], "_submarine_stub", False):
        return sys.modules["sublime"], sys.modules["sublime_plugin"]

    sublime = types.ModuleType("sublime")
    sublime._submarine_stub = True
    class _Region(object):
        def __init__(self, a=0, b=None):
            self.a = a
            self.b = a if b is None else b

        def begin(self):
            return min(self.a, self.b)

        def end(self):
            return max(self.a, self.b)

        def size(self):
            return abs(self.b - self.a)

        def empty(self):
            return self.a == self.b

    sublime.Region = _Region
    sublime.Phantom = lambda *a, **k: None
    sublime.PhantomSet = lambda *a, **k: None
    sublime.LAYOUT_BLOCK = 1
    sublime.LAYOUT_BELOW = 2
    sublime.load_settings = lambda n: FakeSettings()
    sublime.save_settings = lambda n: None
    sublime.status_message = lambda m: None
    sublime.error_message = lambda m: None
    # Modal answers: tests set `_submarine_dialog` (True/False) and read back the
    # questions asked from `_submarine_dialogs`.
    sublime._submarine_dialog = None
    sublime._submarine_dialogs = []

    def _ok_cancel(message, ok="OK", cancel=""):
        sublime._submarine_dialogs.append(message)
        return sublime._submarine_dialog

    sublime.ok_cancel_dialog = _ok_cancel
    sublime.DIALOG_CANCEL = 0
    sublime.DIALOG_YES = 1
    sublime.DIALOG_NO = 2
    # Three-way answer: tests set `_submarine_ync` to DIALOG_YES/NO/CANCEL.
    sublime._submarine_ync = 0

    def _yes_no_cancel(message, yes="Yes", no="No"):
        sublime._submarine_dialogs.append(message)
        return sublime._submarine_ync

    sublime.yes_no_cancel_dialog = _yes_no_cancel
    sublime.active_window = lambda: None
    sublime.windows = lambda: []
    sublime.set_timeout = lambda f, t=0: None
    # Prefer None for the new map so tests that assign `_claude_sessions`
    # (legacy) are still seen by ui.session_api.sessions_map().
    sublime._submarine_sessions = None
    sublime._claude_sessions = {}
    sublime._submarine_devtools = None
    sublime.HIDDEN = 1
    sublime.DRAW_NO_OUTLINE = 2
    sublime.LAYOUT_INLINE = 8
    sublime.OP_EQUAL = 0
    sublime.OP_NOT_EQUAL = 1
    sublime.HIDE_ON_MOUSE_MOVE_AWAY = 1
    sublime.ENCODED_POSITION = 1
    sublime.platform = lambda: "osx"
    sublime.get_clipboard = lambda: ""
    sys.modules["sublime"] = sublime

    sp = types.ModuleType("sublime_plugin")
    sp.WindowCommand = object
    sp.TextCommand = object
    sp.EventListener = object
    sp.ViewEventListener = object
    sp.ApplicationCommand = object
    sys.modules["sublime_plugin"] = sp
    return sublime, sp


def install():
    """Alias used by UI tests. Same as install_sublime()."""
    return install_sublime()[0]


class _Sel(list):
    def clear(self):
        del self[:]

    def add(self, region):
        self.append(region)


class FakeView:
    def __init__(self, view_id=1, name=""):
        self._id = view_id
        self._name = name
        self._settings = FakeSettings()
        self._content = ""
        self._regions = {}
        self._valid = True
        self._sel = _Sel()

    def id(self):
        return self._id

    def is_valid(self):
        return self._valid

    def is_scratch(self):
        return True

    def file_name(self):
        return None

    def name(self):
        return self._name

    def set_name(self, name):
        self._name = name

    def size(self):
        return len(self._content)

    def substr(self, region):
        if hasattr(region, "begin"):
            return self._content[region.begin():region.end()]
        return self._content

    def settings(self):
        return self._settings

    def window(self):
        return None

    def sel(self):
        return self._sel

    def get_regions(self, key):
        return self._regions.get(key, [])

    def add_regions(self, key, regions, *args, **kwargs):
        self._regions[key] = list(regions)

    def erase_regions(self, key):
        self._regions.pop(key, None)

    def set_read_only(self, value):
        pass

    def is_read_only(self):
        return False

    def run_command(self, name, args=None):
        args = args or {}
        if name in ("submarine_replace", "claude_replace"):
            start, end = args.get("start", 0), args.get("end", 0)
            text = args.get("text", "")
            self._content = self._content[:start] + text + self._content[end:]
        elif name in ("submarine_insert", "claude_insert"):
            pos = args.get("pos", len(self._content))
            text = args.get("text", "")
            self._content = self._content[:pos] + text + self._content[pos:]
        elif name == "append":
            self._content += args.get("characters", "")
        elif name in ("submarine_clear_all", "claude_clear_all"):
            self._content = ""

    def viewport_extent(self):
        return (700.0, 400.0)

    def em_width(self):
        return 7.0

    def line_height(self):
        return 16.0

    def layout_extent(self):
        return (700.0, 400.0)

    def viewport_position(self):
        return (0.0, 0.0)

    def set_viewport_position(self, pos, animate=False):
        pass

    def visible_region(self):
        return type("R", (), {
            "begin": lambda self=None: 0,
            "end": lambda self=None, n=None: len(self._content) if False else len(self._content),
        })()

    def text_to_layout(self, pt):
        return (0.0, float(pt))

    def assign_syntax(self, path):
        pass

    def set_scratch(self, v):
        pass


class FakeWindow:
    def __init__(self):
        self._settings = FakeSettings()
        self._views = []
        self._folders = []

    def settings(self):
        return self._settings

    def folders(self):
        return list(self._folders)

    def views(self):
        return list(self._views)

    def active_view(self):
        return self._views[0] if self._views else None

    def focus_view(self, view):
        pass

    def new_file(self):
        v = FakeView(view_id=len(self._views) + 1)
        self._views.append(v)
        return v


def reset_settings():
    _settings_store.clear()
