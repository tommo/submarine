#!/usr/bin/env python3
"""opencode ACP backend: pure helpers, bridge adapters, static wiring."""
import ast
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from backend import opencode as oc  # noqa: E402
from backend import specs  # noqa: E402
from opencode_main import OpencodeBridge  # noqa: E402


class TestOpencodeHelpers(unittest.TestCase):
    def test_bin_env_override_wins(self):
        with mock.patch.dict(os.environ, {"OPENCODE_BIN": "/opt/oc"}):
            self.assertEqual(oc.resolve_opencode_bin(), "/opt/oc")

    def test_unavailable_when_missing(self):
        with mock.patch.dict(os.environ, {"OPENCODE_BIN": ""}), \
                mock.patch("backend.opencode.shutil.which", return_value=None), \
                mock.patch("backend.opencode.os.path.isfile", return_value=False):
            self.assertEqual(oc.resolve_opencode_bin(), "")
            self.assertFalse(oc.opencode_available())

    def test_agent_argv_is_acp_with_cwd(self):
        with mock.patch.object(oc, "resolve_opencode_bin", return_value="/bin/oc"):
            self.assertEqual(oc.agent_argv("/proj"), ["/bin/oc", "acp", "--cwd", "/proj"])

    def test_normalize_model_keeps_only_provider_ids(self):
        self.assertEqual(oc.normalize_model("opus"), "")
        self.assertEqual(oc.normalize_model(""), "")
        self.assertEqual(oc.normalize_model(" lemonade/x.gguf "), "lemonade/x.gguf")

    def test_parse_models_output_skips_noise(self):
        out = "\x1b[1mbanner\x1b[0m\nopencode/big-pickle\nnot a model\n" \
              "lemonade/Ornith.gguf\nopencode/big-pickle\n"
        self.assertEqual(oc.parse_models_output(out), [
            ("opencode/big-pickle", "opencode/big-pickle"),
            ("lemonade/Ornith.gguf", "lemonade/Ornith.gguf"),
        ])

    def test_picker_live_first_then_cli_catalog(self):
        cli = [("lemonade/a", "lemonade/a"), ("opencode/b", "opencode/b")]
        live = [{"modelId": "opencode/b", "name": "B live"}, ("x/y", "XY")]
        with mock.patch.object(oc, "_models_cache", cli), \
                mock.patch.object(oc, "warm_catalog"):
            got = oc.opencode_picker_models(extra=live)
        self.assertEqual(got, [("opencode/b", "B live"), ("x/y", "XY"),
                               ("lemonade/a", "lemonade/a")])

    def test_picker_falls_back_before_fetch(self):
        with mock.patch.object(oc, "_models_cache", None), \
                mock.patch.object(oc, "warm_catalog"):
            self.assertEqual(oc.opencode_picker_models(),
                             list(oc.OPENCODE_FALLBACK_MODELS))

    def test_spec_registered_with_real_opencode_fallback(self):
        spec = specs.BACKENDS["opencode"]
        self.assertEqual(spec.bridge_script, "opencode_main.py")
        # An empty fallback falls through to the global Claude default_model.
        self.assertIn("/", spec.fallback_model)
        self.assertTrue(os.path.isfile(os.path.join(_BRIDGE, spec.bridge_script)))


class TestOpencodeBridge(unittest.TestCase):
    def setUp(self):
        self.b = OpencodeBridge()
        self.b.file_log = lambda *a, **k: None

    def test_config_options_become_modes_and_models(self):
        self.b._ingest_session_result({"configOptions": [
            {"id": "model", "currentValue": "opencode/big-pickle",
             "options": [{"value": "opencode/big-pickle", "name": "Big Pickle"},
                         {"value": "lemonade/x", "name": "X"}]},
            {"id": "mode", "currentValue": "build",
             "options": [{"value": "build", "name": "Build"},
                         {"value": "plan", "name": "Plan"}]},
        ]})
        self.assertEqual([m["modelId"] for m in self.b._available_models],
                         ["opencode/big-pickle", "lemonade/x"])
        self.assertEqual([m["id"] for m in self.b._available_modes],
                         ["build", "plan"])
        # Raw options are still kept for effort / set_config_option.
        self.assertEqual(len(self.b._config_options), 2)

    def test_usage_read_from_top_level(self):
        u = self.b.usage_from_prompt_result({"usage": {
            "inputTokens": 10, "outputTokens": 3, "cachedReadTokens": 2,
            "totalTokens": 15}})
        self.assertEqual(u["input_tokens"], 10)
        self.assertEqual(u["output_tokens"], 3)
        self.assertEqual(u["cache_read_input_tokens"], 2)
        self.assertEqual(u["total_tokens"], 15)

    def test_mcp_names_mapped_to_claude_convention(self):
        f = OpencodeBridge._canonical_mcp_name
        self.assertEqual(f("submarine_read_view"), "mcp__submarine__read_view")
        self.assertEqual(f("submarine_sublime_eval"), "mcp__submarine__sublime_eval")
        self.assertEqual(f("irr_search"), "mcp__irr__search")
        self.assertEqual(f("bash"), "")
        self.assertEqual(f("submarine__x"), "")

    def test_opening_tool_name_survives_prose_titles(self):
        opening = {"toolCallId": "t1", "title": "bash", "kind": "execute"}
        self.assertEqual(self.b._normalize_tool_name(opening), "Bash")
        self.b._ensure_call("t1").name = "Bash"
        # Follow-ups carry the command line as title — "ls" must not → Glob.
        for title in ("ls", "echo hi"):
            upd = {"toolCallId": "t1", "title": title}
            self.assertEqual(self.b._normalize_tool_name(upd), "Bash")

    def test_non_opencode_model_ids_dropped(self):
        self.assertEqual(self.b.normalize_model("opus"), "")
        self.assertEqual(self.b.normalize_model("opencode/big-pickle"),
                         "opencode/big-pickle")

    def test_argv_spawns_acp(self):
        self.b.cwd = "/proj"
        argv = self.b.agent_argv()
        self.assertEqual(argv[1:], ["acp", "--cwd", "/proj"])


class TestStaticWiring(unittest.TestCase):
    """Every command class the plugin imports must exist.

    Importing a class that is not defined yet fails the whole plugin load
    (Submarine does not start at all).
    """

    @staticmethod
    def _parse(path):
        with open(path, encoding="utf-8") as f:
            return ast.parse(f.read())

    @classmethod
    def _module_names(cls, path):
        """Top-level names a module binds: classes, functions, imports."""
        names = set()
        for n in cls._parse(path).body:
            if isinstance(n, (ast.ClassDef, ast.FunctionDef)):
                names.add(n.name)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                names.update(a.asname or a.name for a in n.names)
        return names

    @classmethod
    def _commands_reexports(cls):
        """{name: source file} for every `from X import name` in commands/."""
        out = {}
        init = os.path.join(_ROOT, "commands", "__init__.py")
        for n in cls._parse(init).body:
            if not isinstance(n, ast.ImportFrom) or not n.module:
                continue
            if n.level:  # from .session_cmds import …
                src = os.path.join(_ROOT, "commands", *n.module.split(".")) + ".py"
            else:        # from ui.session_list import …
                src = os.path.join(_ROOT, *n.module.split(".")) + ".py"
            for a in n.names:
                out[a.asname or a.name] = src
        return out

    def test_submarine_py_imports_resolve(self):
        imported = set()
        for n in ast.walk(self._parse(os.path.join(_ROOT, "submarine.py"))):
            if isinstance(n, ast.ImportFrom) and n.module == "commands":
                imported.update(a.name for a in n.names)
        self.assertEqual(imported - set(self._commands_reexports()), set())

    def test_commands_init_reexports_resolve(self):
        missing = sorted(
            "%s (from %s)" % (name, os.path.relpath(src, _ROOT))
            for name, src in self._commands_reexports().items()
            if not os.path.isfile(src) or name not in self._module_names(src))
        self.assertEqual(missing, [])

    def test_opencode_start_is_registered(self):
        self.assertIn("OpencodeStartCommand", self._commands_reexports())
        self.assertIn("OpencodeStartCommand", self._module_names(
            os.path.join(_ROOT, "commands", "session_cmds.py")))
        for fn in ("Default.sublime-commands", "Main.sublime-menu"):
            with open(os.path.join(_ROOT, fn), encoding="utf-8") as f:
                self.assertIn('"opencode_start"', f.read(), fn)


if __name__ == "__main__":
    unittest.main()
