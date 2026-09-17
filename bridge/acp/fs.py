"""ACP fs/read_text_file and fs/write_text_file.

Invariants: absolute paths only; images become a short path note
(no multi-MB base64); missing Kimi plan files return empty content
(§9.33). Grok bundled-path remap lives on GrokBridge.
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional


class FsMixin:
    _IMAGE_EXTS = (
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
        ".tif", ".tiff", ".heic", ".heif", ".ico",
    )

    # Auto-captured screenshots (screencapture, Playwright, etc.) often land
    # as real files; agent then read_file → fs/read_text_file. ACP has no
    # binary fs method, so we re-encode pixels as a data URL in the text
    # response. Cap ≈ xAI vision / common host limits.
    _IMAGE_READ_MAX_BYTES = 5 * 1024 * 1024

    @staticmethod
    def _image_mime_from_bytes(head: bytes, path: str = "") -> Optional[str]:
        """Detect image MIME from magic bytes, else common extensions."""
        if len(head) >= 8 and head[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if len(head) >= 3 and head[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if len(head) >= 6 and head[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return "image/webp"
        if len(head) >= 2 and head[:2] == b"BM":
            return "image/bmp"
        if len(head) >= 4 and head[:4] in (b"II*\x00", b"MM\x00*"):
            return "image/tiff"
        low = (path or "").lower()
        for ext, mime in (
            (".png", "image/png"), (".jpg", "image/jpeg"),
            (".jpeg", "image/jpeg"), (".gif", "image/gif"),
            (".webp", "image/webp"), (".bmp", "image/bmp"),
            (".tif", "image/tiff"), (".tiff", "image/tiff"),
            (".ico", "image/x-icon"),
            (".heic", "image/heic"), (".heif", "image/heif"),
        ):
            if low.endswith(ext):
                return mime
        return None

    async def _acp_fs_read(self, params: dict) -> dict:
        """fs/read_text_file — UTF-8 text; images as short path metadata.

        Grok's read_file still marks PNGs failed ("Cannot read binary file")
        and tool_output_error if we dump multi-MB base64 into the text FS
        result. Real vision is read_image (MCP) or media tools with a path.
        """
        path = params.get("path") or ""
        if not path or not os.path.isabs(path):
            raise ValueError(
                f"fs/read_text_file requires an absolute path; got {path!r}")
        line = params.get("line")
        limit = params.get("limit")
        max_chars = self.fs_read_max_chars if self.fs_read_max_chars > 0 else (
            2 * 1024 * 1024)

        # Kimi EnterPlanMode: plan file is read before write — empty is OK.
        if not os.path.exists(path) and self._is_kimi_plan_file(path):
            self.file_log(
                f"fs/read_text_file: missing kimi plan file {path!r} → empty")
            return {"content": ""}

        if os.path.isdir(path):
            return {"content": await asyncio.to_thread(
                self._fs_read_directory_as_text, path)}

        low = path.lower()
        by_ext = any(low.endswith(ext) for ext in self._IMAGE_EXTS)

        def _read() -> str:
            with open(path, "rb") as bf:
                head = bf.read(512)
            mime = self._image_mime_from_bytes(head, path)
            if by_ext or mime:
                return self._fs_read_image_as_text(path, mime_hint=mime)
            # NUL ⇒ not text (archives, wasm, …) — don't UTF-8-mangle
            if b"\x00" in head:
                size = os.path.getsize(path)
                raise ValueError(
                    f"fs/read_text_file: binary file {path!r} ({size} bytes); "
                    f"not UTF-8 text")
            with open(path, "r", encoding="utf-8", errors="strict") as f:
                if line is None and limit is None:
                    chunk = f.read(max_chars + 1)
                    if len(chunk) > max_chars:
                        raise ValueError(
                            f"fs/read_text_file: {path!r} exceeds "
                            f"{max_chars} chars; re-read with line/limit "
                            f"to page")
                    return chunk
                lines = f.readlines()
            start = max(0, (line or 1) - 1)
            end = start + limit if limit else len(lines)
            content = "".join(lines[start:end])
            if len(content) > max_chars:
                raise ValueError(
                    f"fs/read_text_file: page is {len(content)} chars "
                    f"(max {max_chars}); reduce limit")
            return content

        try:
            content = await asyncio.to_thread(_read)
        except IsADirectoryError:
            return {"content": await asyncio.to_thread(
                self._fs_read_directory_as_text, path)}
        return {"content": content}

    def _fs_read_directory_as_text(self, path: str, limit: int = 80) -> str:
        """read_file on a directory → listing, not Errno 21."""
        names = []
        try:
            entries = sorted(os.listdir(path))
        except OSError as e:
            raise ValueError(
                f"fs/read_text_file: cannot list directory {path!r}: {e}")
        extra = len(entries) - limit
        for name in entries[:limit]:
            p = os.path.join(path, name)
            names.append(name + "/" if os.path.isdir(p) else name)
        body = "\n".join(names)
        if extra > 0:
            body += f"\n… ({extra} more)"
        self.file_log(
            f"fs/read_text_file: directory {path!r} → {len(names)} names")
        return (
            f"Directory: {path}\n{body}\n"
            "(path is a directory; read a file inside)"
        )

    def _fs_read_image_as_text(
            self, path: str, mime_hint: Optional[str] = None) -> str:
        """Short image metadata for text FS (no multi-MB base64).

        Grok rejects base64 dumps as tool_output_error / binary. Point the
        agent at read_image for pixels; keep path for image_edit etc.
        """
        import struct
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(64)
        mime = mime_hint or self._image_mime_from_bytes(head, path) or "image/png"
        w = h = 0
        if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
            w, h = struct.unpack(">II", head[16:24])
        dim = f"{w}x{h}" if w and h else "unknown"
        if self._mcp_enable_read_image:
            vision = (
                f"Do not use read_file for pixels. Call use_tool with "
                f"tool_name=\"submarine__read_image\" and "
                f"tool_input={{\"path\": {path!r}}} "
                f"(search_tool query=\"read_image\" first if unknown). "
                f"Or pass the path to image_edit / image_gen."
            )
        else:
            vision = (
                "read_image MCP is not enabled for this session "
                "(path is usable by media tools that accept file paths)."
            )
        note = (
            f"Image file ({mime}), {size} bytes, dimensions≈{dim}.\n"
            f"Path: {path}\n"
            f"{vision}"
        )
        self.file_log(
            f"fs/read_text_file: image {path!r} → path note "
            f"(mime={mime}, size={size}, no base64)")
        return note

    async def _acp_fs_write(self, params: dict) -> dict:
        if self._cancel_in_flight or getattr(self, "_drop_grok_leftover", False):
            raise ValueError("fs/write_text_file rejected: turn cancelled")
        path = params.get("path") or ""
        if not path or not os.path.isabs(path):
            raise ValueError(
                f"fs/write_text_file requires an absolute path; got {path!r}")
        content = params.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        max_chars = self.fs_write_max_chars if self.fs_write_max_chars > 0 else (
            2 * 1024 * 1024)
        if len(content) > max_chars:
            raise ValueError(
                f"fs/write_text_file: content is {len(content)} chars "
                f"(max {max_chars})")
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        def _write() -> None:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

        await asyncio.to_thread(_write)
        self._maybe_note_artifact_write(path)
        return {}

    def _artifact_root(self) -> str:
        env = os.environ.get("SUBMARINE_ARTIFACTS_DIR")
        if env:
            return os.path.abspath(os.path.expanduser(env))
        return os.path.join(os.path.expanduser("~"), ".submarine", "artifacts")

    def _is_under_artifact_root(self, path: str) -> bool:
        if not path:
            return False
        root = os.path.realpath(self._artifact_root())
        try:
            ap = os.path.realpath(os.path.expanduser(path))
            common = os.path.commonpath([ap, root])
        except (OSError, ValueError):
            return False
        return common == root

    def _maybe_note_artifact_write(self, path: str) -> None:
        """Convention path: writes under ~/.submarine/artifacts journal on the host."""
        if not self._is_under_artifact_root(path):
            return
        name = os.path.basename(path or "")
        if name.endswith(".journal.jsonl") or name == "index.json":
            return
        try:
            from rpc_helpers import send_notification
        except ImportError:
            return
        payload = {
            "path": os.path.abspath(os.path.expanduser(path)),
            "agent_id": getattr(self, "_agent_id", None),
        }
        try:
            send_notification("artifact_write", payload)
        except Exception:
            pass
