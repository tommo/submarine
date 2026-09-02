#!/usr/bin/env python3
"""Stdio MCP server (Python 3.10+). Spawned per session by bridges.

JSON-RPC 2024-11-05 over newline-terminated stdin/stdout. Routes tools/call
to the in-plugin Unix socket; ``read_image`` is served locally (no socket).
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
from typing import Any

# Standalone: tools.py + plugin root (sidecar_skill).
_HERE = os.path.dirname(os.path.abspath(__file__))
_PLUGIN = os.path.dirname(_HERE)
for _p in (_HERE, _PLUGIN):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from tools import (  # noqa: E402
    CALLER_INJECT_TOOLS,
    is_gated_tool,
    is_local_tool,
    list_tool_descriptors,
    parse_tool_call,
    route,
)

SOCKET_PATH = os.path.join(tempfile.gettempdir(), "submarine_mcp.sock")

_IMAGE_EXTS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".tif", ".tiff", ".heic", ".heif", ".ico",
)
_READ_IMAGE_MAX_BYTES = 4 * 1024 * 1024
_READ_IMAGE_MAX_EDGE = 1600

CALLER_AGENT_ID: str | None = None
ENABLE_READ_IMAGE = False
_env_aid = (os.environ.get("SUBMARINE_AGENT_ID") or "").strip()
if _env_aid:
    CALLER_AGENT_ID = _env_aid
for _arg in sys.argv[1:]:
    if _arg.startswith("--agent-id="):
        val = _arg.split("=", 1)[1].strip()
        if val:
            CALLER_AGENT_ID = val
    elif _arg in ("--enable-read-image", "--enable-read-image=1",
                  "--enable-read-image=true"):
        ENABLE_READ_IMAGE = True
    elif _arg in ("--enable-read-image=0", "--enable-read-image=false",
                  "--disable-read-image"):
        ENABLE_READ_IMAGE = False


def send_to_sublime(
    code: str = "",
    tool: str | None = None,
    agent_id: str | None = None,
) -> dict:
    """One newline-terminated JSON request per connection."""
    if not hasattr(socket, "AF_UNIX"):
        return {"error": "Unix sockets are not available on this platform."}
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(SOCKET_PATH)
        msg: dict[str, Any] = {"code": code, "tool": tool}
        if agent_id is not None:
            msg["agent_id"] = agent_id
        sock.sendall((json.dumps(msg) + "\n").encode())
        response_bytes = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response_bytes += chunk
            if b"\n" in chunk:
                break
        sock.close()
        return json.loads(response_bytes.decode())
    except FileNotFoundError:
        return {"error": "Sublime Text not connected. Make sure the plugin is running."}
    except Exception as e:
        return {"error": str(e)}


def make_response(id: Any, result: Any = None, error: Any = None) -> dict:
    resp: dict[str, Any] = {"jsonrpc": "2.0", "id": id}
    if error:
        resp["error"] = {"code": -32000, "message": str(error)}
    else:
        resp["result"] = result
    return resp


def _image_mime(path: str, head: bytes = b"") -> str:
    if len(head) >= 8 and head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(head) >= 3 and head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(head) >= 6 and head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    low = path.lower()
    for ext, mime in (
        (".png", "image/png"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"),
        (".gif", "image/gif"), (".webp", "image/webp"), (".bmp", "image/bmp"),
        (".tif", "image/tiff"), (".tiff", "image/tiff"),
        (".heic", "image/heic"), (".heif", "image/heif"), (".ico", "image/x-icon"),
    ):
        if low.endswith(ext):
            return mime
    return "image/png"


def _png_size(raw: bytes) -> tuple[int, int]:
    if len(raw) >= 24 and raw[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", raw[16:24])
    return 0, 0


def _shrink_image(path: str, max_edge: int, max_bytes: int) -> tuple[bytes, str, str]:
    """Return (bytes, mime, note). Prefer sips/PIL shrink when large."""
    with open(path, "rb") as f:
        raw = f.read()
    mime = _image_mime(path, raw[:64])
    w, h = _png_size(raw)
    note_bits = [f"path={path}", f"bytes={len(raw)}"]
    if w and h:
        note_bits.append(f"dim={w}x{h}")

    need_resize = (w and h and max(w, h) > max_edge) or len(raw) > max_bytes
    if not need_resize:
        return raw, mime, ", ".join(note_bits)

    try:
        with tempfile.TemporaryDirectory(prefix="sm-read-image-") as td:
            out = os.path.join(td, "out.jpg")
            cmd = ["sips", "-Z", str(max_edge), "-s", "format", "jpeg",
                   path, "--out", out]
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            if r.returncode == 0 and os.path.isfile(out):
                shrunk = open(out, "rb").read()
                if shrunk and len(shrunk) <= max_bytes:
                    note_bits.append(f"shrunk_via=sips edge≤{max_edge}")
                    note_bits.append(f"out_bytes={len(shrunk)}")
                    return shrunk, "image/jpeg", ", ".join(note_bits)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    try:
        from PIL import Image  # type: ignore
        import io
        im = Image.open(path)
        im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
        im.thumbnail((max_edge, max_edge))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85, optimize=True)
        shrunk = buf.getvalue()
        if shrunk and len(shrunk) <= max_bytes:
            note_bits.append(f"shrunk_via=pillow edge≤{max_edge}")
            note_bits.append(f"out_bytes={len(shrunk)}")
            return shrunk, "image/jpeg", ", ".join(note_bits)
    except Exception:
        pass

    if len(raw) > max_bytes:
        raise ValueError(
            f"image {path!r} is {len(raw)} bytes after failed shrink "
            f"(max {max_bytes})")
    return raw, mime, ", ".join(note_bits)


def handle_read_image(args: dict) -> dict:
    """Read an image file and return MCP image content for vision."""
    path = (args.get("path") or args.get("file_path") or args.get("target_file")
            or "").strip()
    if not path:
        return {
            "content": [{"type": "text", "text": "Error: path is required"}],
            "isError": True,
        }
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    if not os.path.isfile(path):
        return {
            "content": [{"type": "text",
                         "text": f"Error: file not found: {path}"}],
            "isError": True,
        }

    low = path.lower()
    with open(path, "rb") as f:
        head = f.read(16)
    by_ext = any(low.endswith(e) for e in _IMAGE_EXTS)
    by_magic = (
        head[:8] == b"\x89PNG\r\n\x1a\n"
        or head[:3] == b"\xff\xd8\xff"
        or head[:6] in (b"GIF87a", b"GIF89a")
        or (len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP")
        or head[:2] == b"BM"
    )
    if not by_ext and not by_magic:
        return {
            "content": [{"type": "text",
                         "text": f"Error: not an image file: {path}"}],
            "isError": True,
        }

    try:
        max_edge = int(args.get("max_edge") or _READ_IMAGE_MAX_EDGE)
        max_bytes = int(args.get("max_bytes") or _READ_IMAGE_MAX_BYTES)
        max_edge = max(256, min(max_edge, 4096))
        max_bytes = max(64 * 1024, min(max_bytes, 8 * 1024 * 1024))
        raw, mime, note = _shrink_image(path, max_edge, max_bytes)
    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"Error: {e}"}],
            "isError": True,
        }

    b64 = base64.b64encode(raw).decode("ascii")
    return {
        "content": [
            {"type": "text", "text": f"Image loaded ({note}, mime={mime})"},
            {"type": "image", "mimeType": mime, "data": b64},
        ]
    }


def _inject_caller(tool_name: str, args: dict) -> None:
    if CALLER_AGENT_ID is None:
        return
    if tool_name == "signal_complete" and args.get("session_id") is None:
        args["session_id"] = CALLER_AGENT_ID
    elif tool_name in CALLER_INJECT_TOOLS and tool_name != "signal_complete":
        if "_caller_agent_id" not in args:
            args["_caller_agent_id"] = CALLER_AGENT_ID


def handle_request(request: dict) -> dict | None:
    req_id = request.get("id")
    method = request.get("method", "")
    params = request.get("params") or {}

    if method == "initialize":
        sidecar_rule = ""
        try:
            from sidecar_skill import RULE as sidecar_rule
        except Exception:
            sidecar_rule = (
                'Unqualified "sidecar" means SUBLIME SIDECAR: MCP spawn_session, '
                "not grok/kimi/codex CLI."
            )
        payload = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "submarine", "version": "0.1.0"},
        }
        if sidecar_rule:
            payload["instructions"] = sidecar_rule
        return make_response(req_id, payload)

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return make_response(req_id, {})

    if method == "tools/list":
        return make_response(req_id, {
            "tools": list_tool_descriptors(enable_read_image=ENABLE_READ_IMAGE),
        })

    if method == "tools/call":
        try:
            tool_name, args = parse_tool_call(method, params)
            _inject_caller(tool_name, args)

            if is_local_tool(tool_name) and tool_name == "read_image":
                if is_gated_tool(tool_name) and not ENABLE_READ_IMAGE:
                    return make_response(req_id, {
                        "content": [{
                            "type": "text",
                            "text": (
                                "Error: read_image is disabled for this session. "
                                "Enable with settings mcp_enable_read_image=true "
                                "(default auto: on for Grok ACP only)."
                            ),
                        }],
                        "isError": True,
                    })
                return make_response(req_id, handle_read_image(args))

            if tool_name == "sublime_eval":
                result = send_to_sublime(code=args.get("code") or "",
                                         agent_id=CALLER_AGENT_ID)
            elif tool_name == "sublime_tool":
                result = send_to_sublime(tool=args.get("name") or "",
                                         agent_id=CALLER_AGENT_ID)
            else:
                code = route(tool_name, args)
                result = send_to_sublime(code=code, agent_id=CALLER_AGENT_ID)
        except ValueError as e:
            return make_response(req_id, error=str(e))

        if result.get("error"):
            return make_response(req_id, {
                "content": [{"type": "text", "text": f"Error: {result['error']}"}],
                "isError": True,
            })
        output = result.get("result")
        if output is None:
            text = "(no return value)"
        elif isinstance(output, str):
            text = output
        else:
            text = json.dumps(output, indent=2)
        return make_response(req_id, {
            "content": [{"type": "text", "text": text}],
        })

    return None


def main() -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = handle_request(request)
            if response:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError as e:
            sys.stderr.write(f"JSON parse error: {e}\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"Error: {e}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    main()
