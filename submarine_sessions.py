#!/usr/bin/env python3
"""Human entry: `python3 submarine_sessions.py <command>`."""
from __future__ import annotations

import os
import sys

# realpath: this file is normally reached through a symlink on PATH.
ROOT = os.path.dirname(os.path.realpath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from features.sessions_cli import main

if __name__ == "__main__":
    sys.exit(main())
