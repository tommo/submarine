"""Removal guard — deleted features must not reappear in shipping source.

Scans plugin source (not ``_research/``, not ``tests/``). Comments-only
mentions count as failures (removal-map §12 + CONVENTIONS.md).
"""
from __future__ import annotations

import ast
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# removal-map §12 + p4-verify work item 3
FORBIDDEN_TOKENS = (
    "order_table",
    "OrderTable",
    "notalone",
    "persona",
    "checkpoint",
    "claude_terminal",
    "cc_pty",
    "cc_launch",
    "cc_transcript",
    "chatroom",
    "grok_cc",
    "grok_proxy",
    "copilot",
    "dsr_main",
    "list_personas",
    "terminal_run",
)

# Word-ish match so `persona` does not fire on `personal`.
# Underscore/digit after the token still matches (`persona_id`).
_TOKEN_RES = {
    tok: re.compile(r"(?<![A-Za-z])" + re.escape(tok) + r"(?![A-Za-z])")
    for tok in FORBIDDEN_TOKENS
}

_SKIP_DIR_NAMES = {
    "_research",
    "tests",
    "sandbox",
    "__pycache__",
    ".git",
    ".pytest_cache",
}

_SCAN_SUFFIXES = (
    ".py",
    ".json",
    ".sublime-commands",
    ".sublime-keymap",
    ".sublime-mousemap",
    ".sublime-menu",
    ".sublime-settings",
    ".sublime-syntax",
    ".hidden-tmTheme",
    ".hidden-color-scheme",
    ".yml",
    ".yaml",
    ".toml",
)
# NOTE: ".md" is deliberately NOT scanned. Documentation is where removals
# SHOULD be named (README "Removed" section, docs/architecture.md); a raw
# token ban there only forces euphemisms. Code and Sublime resources are
# where a token means a real leftover.

# Sublime command identifiers (not view-settings, not .claude/ paths).
_CMD_STRING = re.compile(
    r"""(?:["']command["']\s*:\s*["']|run_command\(\s*["'])(claude_[a-z0-9_]+)"""
)
_CLAUDE_CMD_CLASS = re.compile(
    r"\bclass\s+(ClaudeCode\w*|ClaudeOutput\w*|Claude\w*Command)\b"
)

# Allowed leftovers that look like claude_* but are not plugin commands.
_ALLOWED_CLAUDE_CMD = frozenset()


def _shipping_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        for name in filenames:
            if name.startswith("."):
                continue
            if not name.endswith(_SCAN_SUFFIXES):
                continue
            yield os.path.join(dirpath, name)


def _rel(path):
    return os.path.relpath(path, ROOT)


class TestRemovedFeatureTokens(unittest.TestCase):
    def test_forbidden_tokens_absent_from_shipping_source(self):
        hits = []
        for path in _shipping_files():
            try:
                text = open(path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                for tok, rx in _TOKEN_RES.items():
                    if rx.search(line):
                        hits.append("%s:%d %r → %s" % (_rel(path), i, tok, line.strip()))
        self.assertEqual(hits, [], "removed-feature tokens still in shipping source:\n" + "\n".join(hits))

    def test_terminal_package_is_the_renamed_vendored_copy(self):
        """terminal/ is ported — the removal-map verdict flipped.

        The emulator ships as the vendored Terminus-derived package with the
        naming map applied (SubmarineTerminal.*), and its commands surface from
        a ROOT shim so Sublime discovers them. claude-term-mode (the TUI glue),
        cc_pty/cc_launch/cc_transcript and the MCP terminal_* tools stay out.
        """
        pkg = os.path.join(ROOT, "terminal")
        self.assertTrue(os.path.isdir(pkg), "terminal/ missing")
        for name in ("LICENSE_TERMINUS", "terminal.py", "ptty.py",
                     "render.py", "commands.py", "event.py",
                     "theme_generator.py"):
            self.assertTrue(os.path.isfile(os.path.join(pkg, name)), name)
        self.assertTrue(os.path.isfile(
            os.path.join(ROOT, "SubmarineTerminal.sublime-settings")))
        shim_path = os.path.join(ROOT, "submarine_terminal_plugin.py")
        self.assertTrue(os.path.isfile(shim_path))
        shim = open(shim_path, encoding="utf-8").read()
        self.assertIn("SubmarineTerminalOpenCommand", shim)
        for banned in ("claude_terminal", "ClaudeTerminal", "claude_terminal_mode"):
            self.assertNotIn(banned, shim)

        classes = []
        for f in sorted(os.listdir(pkg)):
            if not f.endswith(".py"):
                continue
            src = open(os.path.join(pkg, f), encoding="utf-8").read()
            classes += re.findall(r"class (\w+Command)\b", src)
        self.assertTrue(classes)
        for cls in classes:
            self.assertTrue(
                cls.startswith(("SubmarineTerminal", "Noop")),
                "un-renamed terminal command: %s" % cls)

        keymap = open(os.path.join(ROOT, "Default.sublime-keymap"),
                      encoding="utf-8").read()
        # Keymap carries the renamed terminal commands; the ROOT keymap is the
        # only one ST loads, so the bindings live there, not in terminal/.
        self.assertIn("submarine_terminal_keypress", keymap)
        self.assertIn("submarine_terminal_close", keymap)
        self.assertNotIn("claude_terminal", keymap)
        self.assertFalse(os.path.isfile(
            os.path.join(pkg, "Default.sublime-keymap")))

    def test_no_legacy_command_names(self):
        hits = []
        for path in _shipping_files():
            try:
                text = open(path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            rel = _rel(path)
            if path.endswith(".py"):
                for m in _CLAUDE_CMD_CLASS.finditer(text):
                    hits.append("%s class %s" % (rel, m.group(1)))
            for i, line in enumerate(text.splitlines(), 1):
                for m in _CMD_STRING.finditer(line):
                    name = m.group(1)
                    if name in _ALLOWED_CLAUDE_CMD:
                        continue
                    hits.append("%s:%d command %s" % (rel, i, name))
        self.assertEqual(hits, [], "legacy command names remain:\n" + "\n".join(hits))
