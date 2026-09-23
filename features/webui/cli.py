"""Command line for the web UI.

  python3 -m features.webui [--host 0.0.0.0] [--port 8787] [--socket PATH] [--token S]
  python3 submarine_web.py …          # same thing through the repo shim

The default bind is every interface: a browser on another machine on the LAN can
drive the sessions this Sublime is running. Loopback is open so this machine
is never locked out. Everyone else needs a device cookie (granted from Sublime)
or `--token` / `SUBMARINE_WEB_TOKEN`. `--require-auth-on-loopback` applies that
gate to this machine too.
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
                   help="shared secret accepted on /api routes; defaults to "
                        "$%s. A granted device cookie is also accepted."
                        % TOKEN_ENV)
    p.add_argument("--require-auth-on-loopback", action="store_true",
                   help="this machine also needs a device cookie or --token "
                        "(default: loopback is open)")
    p.add_argument("--quiet", action="store_true", help="no startup banner")
    p.add_argument("--verbose", action="store_true",
                   help="log every request (default: only failures)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return run(host=args.host, port=args.port, socket_path=args.socket,
               token=args.token, quiet=args.quiet, verbose=args.verbose,
               auth_loopback=args.require_auth_on_loopback)


if __name__ == "__main__":
    sys.exit(main())
