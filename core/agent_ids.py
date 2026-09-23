"""Agent ids: `submarine::<12 hex>`.

The prefix makes an id unique in free text — a sheet line, a tool call, a
prompt — so it can be found (Cmd+click, orphan harvest, notify dedupe)
without matching some other tool's `agent-…`. Ids minted before the prefix
(`agent-<hex>`) are still accepted everywhere and read back canonical, so
saved records, view stamps, artifacts and agents already running with the
old id keep resolving to the same session.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any, Optional

PREFIX = "submarine::"

# One id in free text, either form. The old form's tail was looser (test
# fixtures and early aliases used words), the new one is hex only.
ID_IN_TEXT_RE = re.compile(
    r"submarine::[0-9a-f]{8,}|agent-[0-9A-Za-z]{6,}", re.I)

_WHOLE_RE = re.compile(r"^(?:submarine::|submarine-|agent-)([0-9a-f]{8,})$", re.I)


def new_agent_id() -> str:
    return PREFIX + uuid.uuid4().hex[:12]


def derived_agent_id(session_id: str) -> str:
    """The stable agent_id of a conversation that has no saved one."""
    return PREFIX + hashlib.sha1(str(session_id).encode("utf-8")).hexdigest()[:12]


def canon_agent_id(value: Any) -> Any:
    """`agent-<hex>` / `submarine-<hex>` → `submarine::<hex>`; anything else
    (None, a subsession alias, a view id) comes back unchanged."""
    if not isinstance(value, str):
        return value
    s = value.strip()
    m = _WHOLE_RE.match(s)
    return (PREFIX + m.group(1).lower()) if m else s


def canon_agent_ids(values: Any) -> list:
    out = []
    for v in values or []:
        c = canon_agent_id(v)
        if c and c not in out:
            out.append(c)
    return out


def agent_id_hex(value: Any) -> Optional[str]:
    c = canon_agent_id(value)
    if isinstance(c, str) and c.startswith(PREFIX):
        return c[len(PREFIX):]
    return None


def agent_id_folder(value: Any) -> Optional[str]:
    """A path-safe name for the id (no `::`)."""
    h = agent_id_hex(value)
    return ("submarine-" + h) if h else None
