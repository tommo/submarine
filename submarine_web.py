#!/usr/bin/env python3
"""Human entry: `python3 submarine_web.py [--port 8787]`.

Same pattern as `submarine_sessions.py`: a shim so the UI can be started from
anywhere, next to a symlink on PATH if wanted.
"""
from __future__ import annotations

import os
import sys

# realpath: this file is normally reached through a symlink on PATH.
ROOT = os.path.dirname(os.path.realpath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from features.webui.cli import main

if __name__ == "__main__":
    sys.exit(main())
