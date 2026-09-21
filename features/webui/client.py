"""The web UI's `op:"sessions"` client.

`features.sessions_cli` owns the wire format (one newline-terminated JSON
request per connection, JSON envelope back) and `features.session_control` owns
the four actions. The web UI is a second consumer of that same surface, so this
module is a thin wrapper rather than a second protocol implementation: it adds
the caller stamp the plugin audits and the error shape the HTTP layer maps.

Thread-safe by construction: `sessions_cli.send` opens one short-lived socket
per call, so a `SessionClient` can be shared by every request thread.
"""
from __future__ import annotations

import os
import stat
from typing import Any, Dict, Optional

from features import sessions_cli

#: Names this surface in the plugin's audit log and in the prompt banner a
#: target sheet shows ("📨 from web UI").
CALLER = {"kind": "webui", "name": "web UI"}

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8787

#: `chat` can hand the prompt to the socket server's wake path, which waits out
#: a bridge init before it answers. The CLI's 30s default is not enough for a
#: sleeping session on a cold start.
CHAT_TIMEOUT = 90.0

#: Refs are agent ids / session ids / unique names; nothing here is huge.
MAX_FIELD_CHARS = 200000


def caller() -> Dict[str, Any]:
    """The caller stamp for a request from this process."""
    out = dict(CALLER)
    out["pid"] = os.getpid()
    try:
        out["cwd"] = os.getcwd()
    except OSError:
        pass
    return out


def _failed(error: str) -> Dict[str, Any]:
    return {"ok": False, "error": error}


class SessionClient(object):
    """One action per method, each returning the plugin's JSON envelope.

    The envelope is passed through untouched (`ok` / `error` / `data` /
    `instance`), so the HTTP layer can map `data.code` to a status code and the
    browser can show the same message the CLI prints.
    """

    def __init__(self, socket_path: str = "",
                 timeout: float = sessions_cli.DEFAULT_TIMEOUT) -> None:
        self.socket_path = socket_path or sessions_cli._socket_path()
        self.timeout = timeout

    def call(self, action: str, timeout: Optional[float] = None,
             **fields: Any) -> Dict[str, Any]:
        env = sessions_cli.call(action, timeout=timeout or self.timeout,
                                path=self.socket_path, caller=caller(), **fields)
        if not isinstance(env, dict):
            return _failed("malformed response from the plugin socket")
        return env

    def list(self, **fields: Any) -> Dict[str, Any]:
        return self.call("list", **fields)

    def view(self, ref: str, **fields: Any) -> Dict[str, Any]:
        return self.call("view", ref=ref, **fields)

    def chat(self, ref: str, prompt: str, **fields: Any) -> Dict[str, Any]:
        return self.call("chat", timeout=CHAT_TIMEOUT, ref=ref, prompt=prompt,
                         **fields)

    def interrupt(self, ref: str) -> Dict[str, Any]:
        return self.call("interrupt", ref=ref)

    def pending(self, ref: str) -> Dict[str, Any]:
        return self.call("pending", ref=ref)

    def backends(self) -> Dict[str, Any]:
        return self.call("backends")

    def create(self, **fields: Any) -> Dict[str, Any]:
        return self.call("create", timeout=CHAT_TIMEOUT, **fields)

    def rename(self, ref: str, name: str) -> Dict[str, Any]:
        return self.call("rename", ref=ref, name=name)

    def open(self, ref: Optional[str], file_path: str, line: Any = None) -> Dict[str, Any]:
        fields = {"file_path": file_path}  # type: Dict[str, Any]
        if ref:
            fields["ref"] = ref
        if line:
            fields["line"] = line
        return self.call("open", **fields)

    def read(self, ref: Optional[str], file_path: str, max_bytes: Any = None) -> Dict[str, Any]:
        fields = {"file_path": file_path}  # type: Dict[str, Any]
        if ref:
            fields["ref"] = ref
        if max_bytes:
            fields["max_bytes"] = max_bytes
        return self.call("read", **fields)

    def close(self, ref: str, remove: bool = False) -> Dict[str, Any]:
        return self.call("close", ref=ref, remove=bool(remove))

    def answer(self, ref: str, **fields: Any) -> Dict[str, Any]:
        return self.call("answer", ref=ref, **fields)

    def health(self) -> Dict[str, Any]:
        """Whether the socket exists. Cheap: no round-trip, no plugin work."""
        present = False
        try:
            present = stat.S_ISSOCK(os.stat(self.socket_path).st_mode)
        except OSError:
            present = False
        out = {"ok": True, "socket": self.socket_path, "present": present}
        if not present:
            out["hint"] = ("launch Sublime Text with Submarine loaded, then "
                           "reload this page")
        return out
