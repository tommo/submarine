"""Agent artifact store. Sublime-free.

The write IS the output: reports live on disk under ~/.submarine/artifacts,
journaled and indexed. The transcript only gets a compact card.

Tool writes and ACP convention writes converge on record_write().
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Union

from .agent_ids import agent_id_folder, agent_id_hex, canon_agent_id

try:
    from plat.constants import ARTIFACTS_DIR
    from plat.jsonio import safe_json_dump, safe_json_load
except ImportError:  # pragma: no cover
    ARTIFACTS_DIR = os.path.join(os.path.expanduser("~"), ".submarine", "artifacts")

    def safe_json_load(file_path, default=None):  # type: ignore
        if default is None:
            default = {}
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def safe_json_dump(data, file_path):  # type: ignore
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            return True
        except Exception:
            return False


DEFAULT_INDEX_CAP = 500
DEFAULT_READ_LIMIT = 20000
JOURNAL_TAIL = 12

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def artifacts_root() -> str:
    """On-disk root. SUBMARINE_ARTIFACTS_DIR overrides for tests."""
    env = os.environ.get("SUBMARINE_ARTIFACTS_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.abspath(str(ARTIFACTS_DIR))


def is_under_artifact_root(path: str, root: Optional[str] = None) -> bool:
    if not path:
        return False
    root = os.path.realpath(root or artifacts_root())
    try:
        ap = os.path.realpath(os.path.expanduser(path))
    except (OSError, ValueError):
        return False
    try:
        common = os.path.commonpath([ap, root])
    except ValueError:
        return False
    return common == root


def is_journal_sidecar(path: str) -> bool:
    name = os.path.basename(path or "")
    return name.endswith(".journal.jsonl") or name == "index.json"


def journal_path_for(artifact_path: str) -> str:
    base, _ext = os.path.splitext(artifact_path)
    return base + ".journal.jsonl"


def sanitize_slug(name: str) -> str:
    raw = (name or "").strip().replace("\\", "/")
    raw = os.path.basename(raw)
    if not raw:
        raw = "artifact"
    base, ext = os.path.splitext(raw)
    if not ext:
        ext = ".md"
    base = _SLUG_RE.sub("-", base).strip(".-") or "artifact"
    ext = _SLUG_RE.sub("", ext) or ".md"
    if not ext.startswith("."):
        ext = "." + ext
    return base + ext.lower()


def format_bytes(n: Any) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 1024:
        return "%d B" % n
    kb = n / 1024.0
    if kb < 1024:
        s = "%.1f" % kb
        if s.endswith(".0"):
            s = s[:-2]
        return "%s KB" % s
    mb = kb / 1024.0
    s = "%.1f" % mb
    if s.endswith(".0"):
        s = s[:-2]
    return "%s MB" % s


def format_card_line(
    name: str,
    nbytes: Any,
    summary: str = "",
) -> str:
    size = format_bytes(nbytes)
    summ = (summary or "").replace("\n", " ").strip()
    if summ:
        return "📄 %s — %s — %s        [open] [path]\n" % (name, size, summ)
    return "📄 %s — %s        [open] [path]\n" % (name, size)


def should_auto_open(
    mode: Any,
    already_opened: bool,
    override: Any = None,
) -> bool:
    """Resolve artifacts_auto_open + per-call override.

    mode/override: "first" | "always" | "never" | True | False.
    """
    chosen = override if override is not None else mode
    if isinstance(chosen, bool):
        return chosen
    text = str(chosen or "first").strip().lower()
    if text in ("always", "true", "yes", "1"):
        return True
    if text in ("never", "false", "no", "0"):
        return False
    return not bool(already_opened)


def file_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _write_bytes(path: str, data: bytes) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _decode_slice(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


class ArtifactStore:
    """Disk store: owner/slug files, index.json MRU, per-file journals."""

    def __init__(
        self,
        root: Optional[str] = None,
        index_cap: Optional[int] = None,
    ) -> None:
        self.root = os.path.abspath(root or artifacts_root())
        try:
            cap = int(index_cap) if index_cap is not None else DEFAULT_INDEX_CAP
        except (TypeError, ValueError):
            cap = DEFAULT_INDEX_CAP
        self.index_cap = max(1, cap)

    @property
    def index_path(self) -> str:
        return os.path.join(self.root, "index.json")

    def owner_dir(self, owner: str) -> str:
        folder = agent_id_folder(owner)
        if folder:
            # `::` is not path-safe; a folder made under the old `agent-<hex>`
            # id keeps its artifacts with the same session.
            legacy = os.path.join(self.root, "agent-" + (agent_id_hex(owner) or ""))
            return legacy if os.path.isdir(legacy) else os.path.join(self.root, folder)
        slug = _SLUG_RE.sub("-", (owner or "unknown").strip()) or "unknown"
        return os.path.join(self.root, slug)

    def load_index(self) -> List[Dict[str, Any]]:
        data = safe_json_load(self.index_path, default=[])
        if isinstance(data, list):
            return data
        return []

    def save_index(self, entries: List[Dict[str, Any]]) -> bool:
        os.makedirs(self.root, exist_ok=True)
        return bool(safe_json_dump(entries[: self.index_cap], self.index_path))

    def unique_path(self, owner: str, slug: str) -> str:
        folder = self.owner_dir(owner)
        os.makedirs(folder, exist_ok=True)
        candidate = os.path.join(folder, slug)
        if not os.path.exists(candidate):
            return candidate
        base, ext = os.path.splitext(slug)
        n = 2
        while True:
            nxt = os.path.join(folder, "%s-%d%s" % (base, n, ext))
            if not os.path.exists(nxt):
                return nxt
            n += 1

    def owner_of_path(self, path: str) -> str:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(self.root))
        parts = rel.replace("\\", "/").split("/")
        if parts and parts[0] not in (".", ".."):
            return parts[0]
        return ""

    def record_write(
        self,
        path: str,
        agent_id: str,
        op: str,
        note: Optional[str] = None,
        title: Optional[str] = None,
        summary: Optional[str] = None,
        session_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Journal + index upsert. File on disk is already current truth."""
        ap = os.path.abspath(os.path.expanduser(path))
        data = b""
        if os.path.isfile(ap):
            data = _read_bytes(ap)
        nbytes = len(data)
        sha = file_sha(data)
        ts = time.time()
        entry = {
            "ts": ts,
            "agent_id": agent_id,
            "op": op,
            "bytes": nbytes,
            "sha": sha,
        }  # type: Dict[str, Any]
        if note:
            entry["note"] = note
        jp = journal_path_for(ap)
        parent = os.path.dirname(jp)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(jp, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        owner = canon_agent_id(self.owner_of_path(ap) or agent_id)
        rec = {
            "path": ap,
            "owner": owner,
            "name": name or os.path.basename(ap),
            "title": title,
            "created": ts,
            "updated": ts,
            "bytes": nbytes,
            "summary": summary,
            "sha": sha,
            "op": op,
        }  # type: Dict[str, Any]
        if session_id:
            rec["session_id"] = session_id
        self._upsert_index(rec)
        return rec

    def _upsert_index(self, rec: Dict[str, Any]) -> None:
        entries = self.load_index()
        path = rec.get("path")
        created = rec.get("created")
        for i, old in enumerate(entries):
            if old.get("path") == path:
                created = old.get("created") or created
                if rec.get("title") is None:
                    rec["title"] = old.get("title")
                if rec.get("summary") is None:
                    rec["summary"] = old.get("summary")
                if not rec.get("session_id"):
                    rec["session_id"] = old.get("session_id")
                entries.pop(i)
                break
        rec["created"] = created
        entries.insert(0, rec)
        self.save_index(entries)

    def write(
        self,
        name: str,
        content: str,
        owner: str,
        mode: str = "write",
        title: Optional[str] = None,
        summary: Optional[str] = None,
        session_id: Optional[str] = None,
        note: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not owner:
            raise ValueError("owner (agent_id) is required")
        slug = sanitize_slug(name)
        folder = self.owner_dir(owner)
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, slug)
        mode = (mode or "write").strip().lower()
        existed = os.path.isfile(dest)
        if mode == "append":
            if existed:
                with open(dest, "a", encoding="utf-8") as f:
                    f.write(content if isinstance(content, str) else str(content))
                op = "append"
            else:
                _write_bytes(
                    dest,
                    (content if isinstance(content, str) else str(content)).encode("utf-8"),
                )
                op = "create"
        else:
            if existed:
                op = "rewrite"
            else:
                dest = self.unique_path(owner, slug)
                op = "create"
            _write_bytes(
                dest,
                (content if isinstance(content, str) else str(content)).encode("utf-8"),
            )
        return self.record_write(
            dest,
            agent_id=agent_id or owner,
            op=op,
            note=note,
            title=title,
            summary=summary,
            session_id=session_id,
            name=os.path.basename(dest),
        )

    def read(
        self,
        path: str,
        offset: int = 0,
        limit: int = DEFAULT_READ_LIMIT,
    ) -> Dict[str, Any]:
        ap = os.path.abspath(os.path.expanduser(path or ""))
        if not os.path.isfile(ap):
            raise FileNotFoundError("artifact not found: %s" % ap)
        try:
            offset = int(offset or 0)
        except (TypeError, ValueError):
            offset = 0
        if offset < 0:
            offset = 0
        try:
            limit = int(limit if limit is not None else DEFAULT_READ_LIMIT)
        except (TypeError, ValueError):
            limit = DEFAULT_READ_LIMIT
        if limit < 0:
            limit = 0
        data = _read_bytes(ap)
        total = len(data)
        chunk = data[offset: offset + limit] if limit else b""
        truncated = offset + len(chunk) < total
        return {
            "content": _decode_slice(chunk),
            "offset": offset,
            "total_bytes": total,
            "truncated": truncated,
            "sha": file_sha(data),
            "journal_tail": self.journal_tail(ap),
            "path": ap,
        }

    def journal_tail(self, path: str, n: int = JOURNAL_TAIL) -> List[Dict[str, Any]]:
        jp = journal_path_for(os.path.abspath(os.path.expanduser(path)))
        if not os.path.isfile(jp):
            return []
        try:
            with open(jp, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            return []
        out = []  # type: List[Dict[str, Any]]
        for line in lines[-max(1, int(n or JOURNAL_TAIL)):]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def list(
        self,
        scope: str = "self",
        agent_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        entries = self.load_index()
        scope = (scope or "self").strip().lower()
        if scope == "all":
            return list(entries)
        if not agent_id:
            return []
        aid = str(canon_agent_id(str(agent_id)))
        return [e for e in entries
                if str(canon_agent_id(str(e.get("owner") or ""))) == aid]

    def _require_artifact(self, path: str) -> str:
        ap = os.path.abspath(os.path.expanduser(path or ""))
        if not is_under_artifact_root(ap, self.root):
            raise ValueError("path is not under the artifact root: %s" % ap)
        if not os.path.isfile(ap):
            raise FileNotFoundError("artifact not found: %s" % ap)
        return ap

    def edit(
        self,
        path: str,
        op: str,
        agent_id: str,
        old: Optional[str] = None,
        new: Optional[str] = None,
        offset: Optional[int] = None,
        length: Optional[int] = None,
        heading: Optional[str] = None,
        note: Optional[str] = None,
        session_id: Optional[str] = None,
        title: Optional[str] = None,
        summary: Optional[str] = None,
    ) -> Dict[str, Any]:
        ap = self._require_artifact(path)
        kind = (op or "").strip().lower()
        if kind == "append":
            with open(ap, "a", encoding="utf-8") as f:
                f.write("" if new is None else str(new))
            journal_op = "append"
        elif kind == "replace_text":
            self.replace_text(ap, old or "", "" if new is None else str(new))
            journal_op = "edit"
        elif kind == "replace_range":
            self.replace_range(
                ap, int(offset or 0), int(length or 0),
                "" if new is None else str(new))
            journal_op = "edit"
        elif kind == "insert_at":
            self.insert_at(ap, int(offset or 0), "" if new is None else str(new))
            journal_op = "edit"
        elif kind == "delete_range":
            self.delete_range(ap, int(offset or 0), int(length or 0))
            journal_op = "edit"
        elif kind == "replace_section":
            self.replace_section(ap, heading or "", "" if new is None else str(new))
            journal_op = "edit"
        else:
            raise ValueError("unknown edit op: %s" % op)
        rec = self.record_write(
            ap,
            agent_id=agent_id,
            op=journal_op,
            note=note,
            title=title,
            summary=summary,
            session_id=session_id,
        )
        rec["edit_op"] = kind
        return rec

    def replace_text(self, path: str, old: str, new: str) -> None:
        data = _read_bytes(path)
        text = _decode_slice(data)
        if old == "":
            raise ValueError("replace_text: old text is empty")
        count = text.count(old)
        if count == 0:
            raise ValueError("replace_text: no match")
        if count > 1:
            raise ValueError("replace_text: %d matches (need exactly 1)" % count)
        _write_bytes(path, text.replace(old, new, 1).encode("utf-8"))

    def replace_range(self, path: str, offset: int, length: int, new: str) -> None:
        data = _read_bytes(path)
        if offset < 0 or length < 0 or offset > len(data) or offset + length > len(data):
            raise ValueError(
                "replace_range: offset/length out of bounds "
                "(offset=%s length=%s total=%s)" % (offset, length, len(data)))
        patch = (new or "").encode("utf-8")
        _write_bytes(path, data[:offset] + patch + data[offset + length:])

    def insert_at(self, path: str, offset: int, new: str) -> None:
        data = _read_bytes(path)
        if offset < 0 or offset > len(data):
            raise ValueError(
                "insert_at: offset out of bounds (offset=%s total=%s)"
                % (offset, len(data)))
        patch = (new or "").encode("utf-8")
        _write_bytes(path, data[:offset] + patch + data[offset:])

    def delete_range(self, path: str, offset: int, length: int) -> None:
        self.replace_range(path, offset, length, "")

    def replace_section(self, path: str, heading: str, new_body: str) -> None:
        data = _read_bytes(path)
        text = _decode_slice(data)
        nxt = _swap_markdown_section(text, heading, new_body)
        _write_bytes(path, nxt.encode("utf-8"))


def _heading_title(raw: str) -> str:
    s = (raw or "").strip()
    m = _HEADING_RE.match(s)
    if m:
        return m.group(2).strip()
    return s.lstrip("#").strip()


def _swap_markdown_section(text: str, heading: str, new_body: str) -> str:
    want = _heading_title(heading)
    if not want:
        raise ValueError("replace_section: heading is empty")
    lines = text.splitlines(True)
    start = None  # type: Optional[int]
    level = 0
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line.rstrip("\n"))
        if not m:
            continue
        if m.group(2).strip() == want:
            start = i
            level = len(m.group(1))
            break
    if start is None:
        raise ValueError("replace_section: heading not found: %s" % want)
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = _HEADING_RE.match(lines[j].rstrip("\n"))
        if m and len(m.group(1)) <= level:
            end = j
            break
    body = new_body if new_body is not None else ""
    if body and not body.endswith("\n"):
        body += "\n"
    # Keep the heading line; replace everything until the next heading.
    new_lines = lines[: start + 1]
    if body:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] = new_lines[-1] + "\n"
        new_lines.append(body)
    new_lines.extend(lines[end:])
    return "".join(new_lines)


# ─── module-level API (default store) ─────────────────────────────────────────

def default_store(root: Optional[str] = None, index_cap: Optional[int] = None) -> ArtifactStore:
    return ArtifactStore(root=root, index_cap=index_cap)


def record_write(
    path: str,
    agent_id: str,
    op: str,
    note: Optional[str] = None,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    session_id: Optional[str] = None,
    name: Optional[str] = None,
    root: Optional[str] = None,
    index_cap: Optional[int] = None,
) -> Dict[str, Any]:
    return default_store(root=root, index_cap=index_cap).record_write(
        path, agent_id, op,
        note=note, title=title, summary=summary,
        session_id=session_id, name=name,
    )


def write(
    name: str,
    content: str,
    owner: str,
    mode: str = "write",
    title: Optional[str] = None,
    summary: Optional[str] = None,
    session_id: Optional[str] = None,
    note: Optional[str] = None,
    agent_id: Optional[str] = None,
    root: Optional[str] = None,
    index_cap: Optional[int] = None,
) -> Dict[str, Any]:
    return default_store(root=root, index_cap=index_cap).write(
        name, content, owner, mode=mode, title=title, summary=summary,
        session_id=session_id, note=note, agent_id=agent_id,
    )


def read(
    path: str,
    offset: int = 0,
    limit: int = DEFAULT_READ_LIMIT,
    root: Optional[str] = None,
) -> Dict[str, Any]:
    return default_store(root=root).read(path, offset=offset, limit=limit)


def list_artifacts(
    scope: str = "self",
    agent_id: Optional[str] = None,
    root: Optional[str] = None,
    index_cap: Optional[int] = None,
) -> List[Dict[str, Any]]:
    return default_store(root=root, index_cap=index_cap).list(scope=scope, agent_id=agent_id)


def edit(
    path: str,
    op: str,
    agent_id: str,
    **kwargs: Any
) -> Dict[str, Any]:
    root = kwargs.pop("root", None)
    index_cap = kwargs.pop("index_cap", None)
    return default_store(root=root, index_cap=index_cap).edit(
        path, op, agent_id, **kwargs)


def publish_to_session(session: Any, rec: Dict[str, Any]) -> None:
    """Emit an artifact_card on the session OutputPort (headless-safe)."""
    if session is None or not rec:
        return
    output = getattr(session, "output", None)
    if output is None:
        return
    fn = getattr(output, "artifact_card", None)
    if fn is None:
        return
    fn(
        path=rec.get("path") or "",
        name=rec.get("name") or os.path.basename(rec.get("path") or ""),
        bytes=rec.get("bytes") or 0,
        summary=rec.get("summary") or "",
        title=rec.get("title"),
    )


def maybe_auto_open_session(
    session: Any,
    path: str,
    override: Any = None,
    mode: Any = None,
) -> bool:
    """Open the artifact in the last session split when the policy says so.

    Returns True if an open was requested. Does not interrupt the session.
    """
    if session is None or not path:
        return False
    if mode is None:
        settings = getattr(session, "settings", None) or {}
        try:
            mode = settings.get("artifacts_auto_open", "first")
        except Exception:
            mode = "first"
    already = bool(getattr(session, "_artifacts_auto_opened", False))
    if not should_auto_open(mode, already, override=override):
        return False
    try:
        session._artifacts_auto_opened = True
    except Exception:
        pass
    window = getattr(session, "window", None)
    try:
        from core.placement import open_file_in_last_session_split
        open_file_in_last_session_split(window, path)
    except Exception:
        pass
    return True


def handle_convention_write(
    path: str,
    session: Any = None,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
    root: Optional[str] = None,
    index_cap: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """ACP/fs write under the artifact root: journal, index, card, auto-open."""
    store = default_store(root=root, index_cap=index_cap)
    if not is_under_artifact_root(path, store.root):
        return None
    if is_journal_sidecar(path):
        return None
    if not os.path.isfile(path):
        return None
    aid = agent_id
    sid = session_id
    if session is not None:
        aid = aid or getattr(session, "agent_id", None)
        sid = sid or getattr(session, "session_id", None)
    op = "rewrite" if os.path.isfile(journal_path_for(path)) else "create"
    rec = store.record_write(
        path,
        agent_id=str(aid or store.owner_of_path(path) or "unknown"),
        op=op,
        session_id=sid,
    )
    if session is not None:
        publish_to_session(session, rec)
        maybe_auto_open_session(session, rec.get("path") or path)
    return rec


def handle_external_save(
    path: str,
    root: Optional[str] = None,
    index_cap: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """User on-save journal. No card (the buffer is the edit)."""
    store = default_store(root=root, index_cap=index_cap)
    if not is_under_artifact_root(path, store.root):
        return None
    if is_journal_sidecar(path):
        return None
    if not os.path.isfile(path):
        return None
    return store.record_write(path, agent_id="user", op="external")
