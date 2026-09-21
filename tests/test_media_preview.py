"""Inline image preview under media tool lines (sublime-claude port)."""
from __future__ import annotations

import os
import tempfile
import unittest

from ui.renderer import TurnRenderer


# 1×1 red PNG
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf"
    b"\xc0\x00\x00\x00\x03\x00\x01\x00\x05\xfe\xd4\xef\x00\x00\x00\x00IEND"
    b"\xaeB`\x82"
)


class _Owner(object):
    view = None


class TestMediaPreview(unittest.TestCase):
    def setUp(self):
        self.r = TurnRenderer(_Owner())
        fd, self.path = tempfile.mkstemp(suffix=".png")
        os.write(fd, _PNG)
        os.close(fd)

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def test_png_header_size(self):
        self.assertEqual(self.r._image_dimensions_from_bytes(_PNG), (1, 1))

    def test_phantom_html_embeds_data_uri(self):
        html = self.r._media_phantom_html(self.path, "images/1.png")
        self.assertIn("<img src=\"data:image/", html)
        self.assertIn("width=", html)
        self.assertIn("height=", html)
        self.assertIn("enlarge", html)
        self.assertNotIn("🖼", html)


if __name__ == "__main__":
    unittest.main()
