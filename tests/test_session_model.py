"""resolve_init_model chain; new session ignores leftover view stamp."""
from __future__ import annotations

import unittest

from core.registry import resolve_init_model


class TestResolveInitModel(unittest.TestCase):
    def setUp(self):
        self.resolve = resolve_init_model

    def test_new_session_uses_default(self):
        self.assertEqual(
            self.resolve(default_model="grok-4.6"),
            "grok-4.6",
        )

    def test_profile_beats_default(self):
        self.assertEqual(
            self.resolve(
                profile_model="deepseek-v4-pro",
                default_model="grok-4.6",
            ),
            "deepseek-v4-pro",
        )

    def test_live_session_beats_default(self):
        self.assertEqual(
            self.resolve(
                session_model="deepseek-v4-flash",
                default_model="grok-4.6",
            ),
            "deepseek-v4-flash",
        )

    def test_new_session_ignores_view_stamp(self):
        self.assertEqual(
            self.resolve(
                view_model="deepseek-v4-pro",
                default_model="grok-4.6",
            ),
            "grok-4.6",
        )

    def test_resume_view_stamp_beats_default(self):
        self.assertEqual(
            self.resolve(
                view_model="deepseek-v4-pro",
                default_model="grok-4.6",
                resume=True,
            ),
            "deepseek-v4-pro",
        )

    def test_saved_beats_default(self):
        self.assertEqual(
            self.resolve(
                saved_model="deepseek-v4-pro",
                default_model="grok-4.6",
            ),
            "deepseek-v4-pro",
        )

    def test_resume_uses_default_if_nothing_saved(self):
        self.assertEqual(
            self.resolve(default_model="grok-4.6", resume=True),
            "grok-4.6",
        )

    def test_resume_keeps_saved_deepseek(self):
        self.assertEqual(
            self.resolve(
                saved_model="deepseek-v4-pro",
                default_model="grok-4.6",
                resume=True,
            ),
            "deepseek-v4-pro",
        )

    def test_profile_beats_live_and_saved(self):
        self.assertEqual(
            self.resolve(
                profile_model="deepseek-v4-flash",
                session_model="grok-4.6",
                saved_model="grok-4.6",
                default_model="grok-4.6",
            ),
            "deepseek-v4-flash",
        )

    def test_blank_strings_are_skipped(self):
        self.assertEqual(
            self.resolve(
                session_model="  ",
                view_model="",
                saved_model="deepseek-v4-pro",
                default_model="grok-4.6",
            ),
            "deepseek-v4-pro",
        )


if __name__ == "__main__":
    unittest.main()
