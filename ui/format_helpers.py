"""Tool-result format helpers used by formatters via the output view."""
from __future__ import annotations

import json
import os
import re
from typing import Optional
from urllib.parse import urlparse


class FormatHelpers:
    """Mixin: compact result summaries for tool rows."""

    @staticmethod
    def _strip_ansi(text: str) -> str:
        if not text or "\x1b" not in text:
            return text or ""
        return re.sub(
            r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))",
            "",
            text,
        )

    def _format_x_search_result(self, result: str) -> str:
        if not result or not result.strip():
            return " → 0"
        text = result.strip()
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return " → %d posts" % len(data)
            if isinstance(data, dict):
                posts = data.get("posts") or data.get("results") or data.get("data")
                if isinstance(posts, list):
                    return " → %d posts" % len(posts)
                if "username" in data or "name" in data:
                    return " → @%s" % (data.get("username") or data.get("name"))
        except Exception:
            pass
        lines = [l for l in text.splitlines() if l.strip()]
        urls = re.findall(r"https?://(?:x|twitter)\.com/\S+", text)
        if urls:
            return " → %d links" % len(set(urls))
        if len(lines) <= 1 and len(text) < 80:
            return " → %s" % text[:60]
        return " → %d lines" % len(lines)

    def _parse_websearch_hits(self, result: str) -> list:
        if not result or not str(result).strip():
            return []
        text = str(result).strip()
        hits = []

        def _add(title, url):
            title = (title or "").strip()
            url = (url or "").strip()
            if not title and not url:
                return
            if not title:
                try:
                    title = urlparse(url).netloc or url
                except Exception:
                    title = url
            key = url or title
            if any((h.get("url") or h.get("title")) == key for h in hits):
                return
            hits.append({"title": title, "url": url})

        idx = text.find("Links:")
        if idx >= 0:
            rest = text[idx + len("Links:"):].lstrip()
            if rest.startswith("["):
                try:
                    arr, _ = json.JSONDecoder().raw_decode(rest)
                    if isinstance(arr, list):
                        for item in arr:
                            if isinstance(item, dict):
                                _add(item.get("title") or item.get("name"),
                                     item.get("url") or item.get("link"))
                            elif isinstance(item, str) and item.startswith("http"):
                                _add("", item)
                except Exception:
                    pass
        if not hits:
            try:
                data = json.loads(text)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            _add(item.get("title") or item.get("name"),
                                 item.get("url") or item.get("link"))
                elif isinstance(data, dict):
                    arr = (data.get("results") or data.get("organic")
                           or data.get("items") or data.get("data") or [])
                    if isinstance(arr, list):
                        for item in arr:
                            if not isinstance(item, dict):
                                continue
                            content = item.get("content")
                            if isinstance(content, list):
                                for c in content:
                                    if isinstance(c, dict):
                                        _add(c.get("title"),
                                             c.get("url") or c.get("link"))
                            else:
                                _add(item.get("title") or item.get("name"),
                                     item.get("url") or item.get("link"))
            except Exception:
                pass
        if not hits:
            for title, url in re.findall(
                    r"\[([^\]]+)\]\((https?://[^)\s]+)\)", text):
                _add(title, url)
        if not hits:
            for url in re.findall(r"https?://[^\s\]\"'<>]+", text):
                _add("", url.rstrip(".,);"))
        return hits

    def _format_websearch_result(self, result: str) -> str:
        hits = self._parse_websearch_hits(result)
        if not hits:
            if not result or not str(result).strip():
                return " → 0 results"
            for line in str(result).splitlines():
                s = line.strip()
                if not s or s.startswith("Web search results") or s.startswith("Links:"):
                    continue
                if len(s) > 90:
                    s = s[:89] + "…"
                return " → %s" % s
            return " → done"
        max_show = 6
        max_title = 72
        lines = [" → %d result%s" % (len(hits), "" if len(hits) == 1 else "s")]
        for h in hits[:max_show]:
            title = (h.get("title") or "").replace("\n", " ").strip()
            if len(title) > max_title:
                title = title[: max_title - 1] + "…"
            host = ""
            url = h.get("url") or ""
            if url:
                try:
                    host = urlparse(url).netloc
                    if host.startswith("www."):
                        host = host[4:]
                except Exception:
                    host = ""
            if host and title and host.lower() not in title.lower():
                lines.append("    │ %s  · %s" % (title, host))
            elif title:
                lines.append("    │ %s" % title)
            elif host:
                lines.append("    │ %s" % host)
        omitted = len(hits) - max_show
        if omitted > 0:
            lines.append("    │ … +%d more" % omitted)
        return "\n".join(lines) if len(lines) > 1 else lines[0]

    def _format_bash_result(self, result: str) -> str:
        if not result or not result.strip():
            return ""
        result = self._strip_ansi(result)
        lines = result.strip().split("\n")
        max_head, max_tail, max_width = 3, 5, 80

        def truncate(line):
            return line[:max_width] + "…" if len(line) > max_width else line

        output_lines = []
        if len(lines) <= max_head + max_tail:
            for line in lines:
                output_lines.append("    │ %s" % truncate(line))
        else:
            for line in lines[:max_head]:
                output_lines.append("    │ %s" % truncate(line))
            omitted = len(lines) - max_head - max_tail
            output_lines.append("    │ ... (%d more lines)" % omitted)
            for line in lines[-max_tail:]:
                output_lines.append("    │ %s" % truncate(line))
        return "\n" + "\n".join(output_lines)

    def _format_glob_result(self, result: str) -> str:
        if not result or not result.strip():
            return " → 0 files"
        lines = [l for l in result.strip().split("\n") if l.strip()]
        return " → %d files" % len(lines)

    def _format_grep_result(self, result: str) -> str:
        if not result or not result.strip():
            return " → 0 matches"
        lines = [l for l in result.strip().split("\n") if l.strip()]
        files = set()
        for line in lines:
            if ":" in line:
                files.add(line.split(":")[0])
        if files:
            return " → %d matches in %d files" % (len(lines), len(files))
        return " → %d matches" % len(lines)

    def _format_read_result(self, result: str) -> str:
        if not result or not result.strip():
            return " → 0 lines"
        text = result.strip()
        lines = [l for l in text.split("\n") if l.strip()]
        if lines and self._looks_like_dir_listing(lines):
            return " → %d entries" % len(lines)
        return " → %d lines" % len(text.splitlines())

    @staticmethod
    def _looks_like_dir_listing(lines: list) -> bool:
        if not lines or len(lines) > 500:
            return False
        numbered = 0
        for ln in lines[:20]:
            if re.match(r"^\s*\d+[|:\t]", ln):
                numbered += 1
        if numbered >= max(2, min(5, len(lines) // 2)):
            return False
        short = 0
        for ln in lines[:30]:
            s = ln.strip()
            if len(s) > 80:
                return False
            if re.match(r"^[\w.\-@+]+/?$", s) or re.match(
                    r"^[d\-][rwx\-]{9}\s", s):
                short += 1
            elif " " not in s and len(s) < 64:
                short += 1
        return short >= max(2, int(len(lines[:30]) * 0.6))

    def _format_mcp_result(self, result: str) -> str:
        try:
            if result is None:
                return ""
            if not isinstance(result, str):
                result = str(result)
            raw = result.strip()
            if not raw:
                return ""
            text = ""
            if "Output:" in raw and (
                    raw.startswith("Wall time") or "\nOutput:\n" in raw):
                rest = raw.split("Output:", 1)[1].strip()
                try:
                    data = json.loads(rest)
                    if isinstance(data, list):
                        parts = []
                        for b in data:
                            if isinstance(b, dict) and b.get("text") is not None:
                                parts.append(str(b.get("text") or ""))
                        text = "\n".join(parts)
                    elif isinstance(data, dict):
                        if data.get("text") is not None:
                            text = str(data.get("text") or "")
                        elif "content" in data:
                            return self._format_mcp_result(json.dumps(data["content"]))
                except Exception:
                    text = rest
            if not text and raw[:1] in "{[":
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        if "Ok" in data:
                            data = data["Ok"]
                        if isinstance(data, dict) and "content" in data:
                            data = data["content"]
                        if isinstance(data, list):
                            parts = []
                            for b in data:
                                if isinstance(b, dict) and b.get("text") is not None:
                                    parts.append(str(b.get("text") or ""))
                            text = "\n".join(parts)
                        elif isinstance(data, dict) and data.get("type") == "text":
                            text = str(data.get("text") or "")
                        elif isinstance(data, dict):
                            compact = json.dumps(data, ensure_ascii=False)
                            if len(compact) < 60:
                                return " → %s" % compact
                            lines = []
                            for k, v in list(data.items())[:5]:
                                lines.append("    │ %s: %s" % (k, str(v)[:50]))
                            if len(data) > 5:
                                lines.append("    │ ... (%d more)" % (len(data) - 5))
                            return "\n" + "\n".join(lines)
                        elif isinstance(data, list):
                            return " → [%d items]" % len(data)
                except Exception:
                    pass
            if not text:
                match = re.search(r"'text':\s*'((?:[^'\\]|\\.)*)'", raw)
                if not match:
                    match = re.search(r'"text":\s*"((?:[^"\\]|\\.)*)"', raw, re.DOTALL)
                if match:
                    text = match.group(1)
                    text = text.replace("\\n", "\n").replace("\\'", "'").replace('\\"', '"')
            if not text:
                text = raw
            text = text.strip()
            if not text:
                return ""
            if text[:1] in "{[":
                try:
                    data = json.loads(text)
                    if isinstance(data, dict):
                        compact = json.dumps(data, ensure_ascii=False)
                        if len(compact) < 60:
                            return " → %s" % compact
                        lines = []
                        for k, v in list(data.items())[:5]:
                            lines.append("    │ %s: %s" % (k, str(v)[:50]))
                        if len(data) > 5:
                            lines.append("    │ ... (%d more)" % (len(data) - 5))
                        return "\n" + "\n".join(lines)
                    if isinstance(data, list):
                        return " → [%d items]" % len(data)
                except Exception:
                    pass
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if not lines:
                return ""
            if len(lines) == 1 and len(lines[0]) <= 72:
                return " → %s" % lines[0]
            shown = lines[:6]
            out = "\n" + "\n".join("    │ %s" % ln[:100] for ln in shown)
            if len(lines) > 6:
                out += "\n    │ … (+%d lines)" % (len(lines) - 6)
            return out
        except Exception:
            return ""

    def _format_ask_user_result(self, result: str, question: str) -> str:
        try:
            if '"cancelled": true' in result or '"cancelled":true' in result:
                return "\n    → (cancelled)"
            return ""
        except Exception:
            return ""

    def _find_line_number(self, file_path: str, old: str, new: str) -> Optional[int]:
        if not file_path or not os.path.exists(file_path):
            return None
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            search = new if new else old
            if not search:
                return None
            pos = content.find(search)
            if pos == -1 and old:
                pos = content.find(old)
            if pos == -1:
                return None
            return content[:pos].count("\n") + 1
        except Exception:
            return None

    def _format_edit_diff(self, old: str, new: str) -> str:
        import difflib
        if not old and not new:
            return ""
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        if old_lines and not old_lines[-1].endswith("\n"):
            old_lines[-1] += "\n"
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))
        if not diff:
            return ""
        diff_lines = []
        for line in diff:
            if line.startswith("---") or line.startswith("+++") or line.startswith("@@"):
                continue
            diff_lines.append(line.rstrip("\n"))
        if not diff_lines:
            return ""
        return "\n```diff\n" + "\n".join(diff_lines) + "\n```"

    def _extract_diff_line_num(self, unified: str) -> int:
        m = re.search(r"^@@\s+-(\d+)", unified, re.MULTILINE)
        if m:
            return int(m.group(1))
        return 0

    def _format_unified_diff(self, unified: str) -> str:
        if not unified:
            return ""
        lines = []
        for line in unified.splitlines():
            if line.startswith("---") or line.startswith("+++") or line.startswith("@@"):
                continue
            lines.append(line)
        if not lines:
            return ""
        return "\n```diff\n" + "\n".join(lines) + "\n```"
