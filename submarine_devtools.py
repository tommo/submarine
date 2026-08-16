#!/usr/bin/env python3
"""Human entry: `python3 submarine_devtools.py <action>`."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from features.devtools.cli import main

if __name__ == "__main__":
    sys.exit(main())
