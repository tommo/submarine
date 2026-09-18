"""ST plugin host packaging constraints."""
from __future__ import annotations

import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _command_names_imported(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    names = set()
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.name.endswith("Command"):
                names.add(alias.name)
    return names


def _command_classes_defined(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    names = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name.endswith("Command"):
            names.add(node.name)
    return names


class TestPluginHost(unittest.TestCase):
    def test_python_version_is_3_8(self):
        path = os.path.join(ROOT, ".python-version")
        self.assertTrue(
            os.path.isfile(path),
            "missing .python-version — Sublime defaults to Python 3.3 and "
            "the plugin will fail to load (all commands grayed out)",
        )
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "3.8")

    def test_devtools_cli_does_not_import_sublime(self):
        init = os.path.join(ROOT, "features", "devtools", "__init__.py")
        cli = os.path.join(ROOT, "features", "devtools", "cli.py")
        with open(init, encoding="utf-8") as f:
            init_src = f.read()
        self.assertNotIn("from .server import", init_src)
        with open(cli, encoding="utf-8") as f:
            self.assertNotIn("import sublime", f.read())
        import sys
        saved = {
            k: sys.modules.get(k)
            for k in list(sys.modules)
            if k == "features.devtools" or k.startswith("features.devtools.")
            or k in ("sublime", "sublime_plugin")
        }
        try:
            for key in list(saved):
                sys.modules.pop(key, None)
            import features.devtools.cli as cli_mod  # noqa: F401
            self.assertNotIn("features.devtools.server", sys.modules)
        finally:
            for key, mod in saved.items():
                if mod is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = mod

    def test_commands_package_exports_every_command_class(self):
        """A class in commands/*.py that never reaches commands/__init__.py
        is invisible to ST — the palette entry and keymap chord both no-op."""
        defined = set()
        cmd_dir = os.path.join(ROOT, "commands")
        for name in os.listdir(cmd_dir):
            if not name.endswith(".py") or name.startswith("_"):
                continue
            defined |= _command_classes_defined(os.path.join(cmd_dir, name))
        exported = _command_names_imported(os.path.join(cmd_dir, "__init__.py"))
        missing = sorted(defined - exported)
        self.assertEqual(
            missing, [],
            "commands/__init__.py missing command re-exports: %s" % missing,
        )

    def test_root_reexports_all_command_classes(self):
        # ST only discovers Command subclasses on ROOT plugin modules.
        # sublime-claude keeps session_list.py at package root so SetText is
        # auto-loaded; here it lives in ui/ and must be re-exported.
        exported = _command_names_imported(os.path.join(ROOT, "commands", "__init__.py"))
        root = _command_names_imported(os.path.join(ROOT, "submarine.py"))
        missing = sorted(exported - root)
        self.assertEqual(
            missing, [],
            "submarine.py missing command re-exports (ST will not register them): %s"
            % missing,
        )
