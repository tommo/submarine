"""Command line for the web UI.

  python3 -m features.webui [--host 0.0.0.0] [--port 8787] [--socket PATH] [--token S]
  python3 submarine_web.py …          # same thing through the repo shim

The default bind is every interface: a browser on another machine on the LAN can
drive the sessions this Sublime is running. `--token` (or
`SUBMARINE_WEB_TOKEN`) gates every `/api` route for that case; without it the
port is open to whoever can reach it.
"""
from __future__ import annotations

import argparse
import os
import sys

from features.webui.client import DEFAULT_HOST, DEFAULT_PORT
from features.webui.server import run

TOKEN_ENV = "SUBMARINE_WEB_TOKEN"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="submarine-webui",
        description="Browser UI for the Submarine session-control socket "
                    "(same sessions the CLI lists).")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help="bind address (default %(default)s = every interface)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help="TCP port (default %(default)s)")
    p.add_argument("--socket", default="",
                   help="plugin socket path override (default $TMPDIR/"
                        "submarine_mcp.sock)")
    p.add_argument("--token", default=os.environ.get(TOKEN_ENV, ""),
                   help="shared secret required on /api routes; defaults to "
                        "$%s. Without it the port is open to anyone who can "
                        "reach it." % TOKEN_ENV)
    p.add_argument("--quiet", action="store_true", help="no startup banner")
    p.add_argument("--verbose", action="store_true",
                   help="log every request (default: only failures)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return run(host=args.host, port=args.port, socket_path=args.socket,
               token=args.token, quiet=args.quiet, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
