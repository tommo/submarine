#!/usr/bin/env python3
"""Claude CLI settings cascade + profiles-only loader."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from plat import jsonio, settings


class TestSafeJson(unittest.TestCase):
    def test_load_missing_returns_default(self):
        self.assertEqual(jsonio.safe_json_load("/no/such/file.json"), {})
        self.assertEqual(jsonio.safe_json_load("/no/such/file.json", default=[]), [])

    def test_dump_and_load_roundtrip(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            self.assertTrue(jsonio.safe_json_dump({"a": 1}, path))
            self.assertEqual(jsonio.safe_json_load(path), {"a": 1})
        finally:
            os.remove(path)


class TestLoadProjectSettings(unittest.TestCase):
    def test_cascade_and_permissions_mapping(self):
        cwd = tempfile.mkdtemp()
        claude = os.path.join(cwd, ".claude")
        os.makedirs(claude)
        with open(os.path.join(claude, "settings.json"), "w") as f:
            json.dump({
                "env": {"A": "project"},
                "permissions": {"allow": ["mcp__sublime__*"]},
            }, f)
        with open(os.path.join(claude, "settings.local.json"), "w") as f:
            json.dump({"env": {"B": "local"}, "autoAllowedMcpTools": ["Read"]}, f)

        # Isolate from the developer's real ~/.claude.json
        original = settings.USER_SETTINGS_FILE
        empty = os.path.join(cwd, "empty-user.json")
        jsonio.safe_json_dump({}, empty)
        try:
            settings.USER_SETTINGS_FILE = empty
            result = settings.load_project_settings(cwd)
        finally:
            settings.USER_SETTINGS_FILE = original

        self.assertEqual(result["env"]["A"], "project")
        self.assertEqual(result["env"]["B"], "local")
        self.assertIn("Read", result["autoAllowedMcpTools"])
        self.assertIn("mcp__sublime__*", result["autoAllowedMcpTools"])


class TestLoadProfiles(unittest.TestCase):
    def test_ignores_checkpoints_and_merges_project(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        jsonio.safe_json_dump({
            "profiles": {"work": {"model": "opus"}},
            "checkpoints": {"should": "be ignored"},
        }, path)
        original_dir = settings.USER_PROFILES_DIR
        empty_home = tempfile.mkdtemp()
        try:
            settings.USER_PROFILES_DIR = empty_home
            profiles = settings.load_profiles(path)
        finally:
            settings.USER_PROFILES_DIR = original_dir
            os.remove(path)
        self.assertEqual(profiles, {"work": {"model": "opus"}})
        self.assertNotIn("should", profiles)
        self.assertNotIn("checkpoints", profiles)

    def test_strips_persona_keys_from_profile(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        jsonio.safe_json_dump({
            "profiles": {
                "work": {
                    "model": "opus",
                    "persona_id": 9,
                    "persona_session_id": "ps",
                    "persona_url": "http://gone.example",
                    "system_prompt": "hi",
                },
            },
        }, path)
        original_dir = settings.USER_PROFILES_DIR
        empty_home = tempfile.mkdtemp()
        try:
            settings.USER_PROFILES_DIR = empty_home
            profiles = settings.load_profiles(path)
        finally:
            settings.USER_PROFILES_DIR = original_dir
            os.remove(path)
        self.assertEqual(profiles["work"]["model"], "opus")
        self.assertEqual(profiles["work"]["system_prompt"], "hi")
        self.assertNotIn("persona_id", profiles["work"])
        self.assertNotIn("persona_session_id", profiles["work"])
        self.assertNotIn("persona_url", profiles["work"])


if __name__ == "__main__":
    unittest.main()
