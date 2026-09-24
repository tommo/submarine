"""In-plugin Unix socket server. Sublime imports allowed.

start()/stop() are called from plugin lifecycle and the package reloader.
One newline-terminated JSON request per connection; eval runs on the ST
main thread via sublime.set_timeout.
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import sublime
import sublime_plugin

from .tools import exec_global_names

try:
    from plat.constants import MCP_SOCKET_PATH, PROJECT_SETTINGS_DIR, PROFILES_FILE
except ImportError:  # pragma: no cover - package layout
    import tempfile
    MCP_SOCKET_PATH = os.path.join(tempfile.gettempdir(), "submarine_mcp.sock")
    PROJECT_SETTINGS_DIR = ".claude"
    PROFILES_FILE = "profiles.json"

SOCKET_PATH = MCP_SOCKET_PATH
EVAL_TIMEOUT_S = 30
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MIN_TIMER_S = 60
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "venv", ".venv",
    "env", ".env", "dist", "build", ".cache", ".tox", "target", ".next",
}
SYMBOL_EXTS = {
    ".py", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".nim", ".nims", ".go", ".rs", ".java", ".kt", ".kts", ".swift",
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".cs", ".php", ".rb",
    ".lua", ".sh", ".zsh", ".fish",
}

_SESSIONS_ATTRS = ("_submarine_sessions", "_claude_sessions")
_AGENTS_ATTRS = ("_submarine_agents", "_claude_agents", "_submarine_by_agent")
_EXEC_VIEW_KEYS = ("submarine_executing_view", "claude_executing_view")
_ACTIVE_VIEW_KEYS = ("submarine_active_view", "claude_active_view")
_ACTIVE_AGENT_KEYS = ("submarine_active_agent",)

_server = None  # type: Optional[MCPSocketServer]
_host_waits = []  # type: List[dict]
_timer_table = {}  # type: Dict[str, dict]
_timer_lock = threading.Lock()


def _log(msg: str) -> None:
    try:
        from plat.log import log_plugin
        log_plugin("MCP: %s" % msg)
    except Exception:
        print("[Submarine MCP] %s" % msg)


def start() -> None:
    """Start the MCP socket server (idempotent)."""
    global _server
    if _server:
        return
    _server = MCPSocketServer()
    _server.start()


def stop() -> None:
    """Stop the MCP socket server."""
    global _server
    if _server:
        _server.stop()
        _server = None


def _parent_in_focus(window, parent_session):
    """True when the window's focused view is the parent session's sheet."""
    if window is None or parent_session is None:
        return False
    out = getattr(parent_session, "output", None)
    view = getattr(out, "view", None) if out is not None else None
    if view is None:
        return False              # the parent itself is swapped out
    try:
        if not view.is_valid():
            return False
        active = window.active_view()
        return active is not None and active.id() == view.id()
    except Exception:
        return False


def _try_import(dotted: str) -> Any:
    mod_name, _, attr = dotted.rpartition(".")
    try:
        mod = __import__(mod_name, fromlist=[attr])
        return getattr(mod, attr)
    except (ImportError, AttributeError):
        return None


def _sessions_map() -> dict:
    for attr in _SESSIONS_ATTRS:
        m = getattr(sublime, attr, None)
        if isinstance(m, dict):
            return m
    sublime._submarine_sessions = {}  # type: ignore[attr-defined]
    return sublime._submarine_sessions  # type: ignore[attr-defined]


def _agents_map() -> dict:
    for attr in _AGENTS_ATTRS:
        m = getattr(sublime, attr, None)
        if isinstance(m, dict):
            return m
    sublime._submarine_agents = {}  # type: ignore[attr-defined]
    return sublime._submarine_agents  # type: ignore[attr-defined]


def _window_setting(window, keys: Tuple[str, ...]) -> Any:
    if not window:
        return None
    settings = window.settings()
    for key in keys:
        val = settings.get(key)
        if val is not None:
            return val
    return None


def _runtime_view_id(session) -> Optional[int]:
    fn = _try_import("core.registry.bound_view_id") or _try_import(
        "core.registry.runtime_view_id")
    if fn is not None:
        try:
            return fn(session)
        except Exception:
            pass
    try:
        if session.output and session.output.view:
            return session.output.view.id()
    except Exception:
        pass
    return None


def _is_sleeping(session) -> bool:
    try:
        return bool(session.is_sleeping)
    except Exception:
        return bool(getattr(session, "is_sleeping", False))


def _get_session_by_agent_id(agent_id: str):
    fn = (
        _try_import("core.registry.by_agent_id")
        or _try_import("core.registry.get_session_by_agent_id")
    )
    if fn is not None:
        try:
            return fn(str(agent_id))
        except Exception:
            pass
    agents = _agents_map()
    hit = agents.get(str(agent_id))
    if hit is not None and not isinstance(hit, (int, str)):
        return hit
    if isinstance(hit, int):
        return _sessions_map().get(hit)
    for session in list(agents.values()) + list(_sessions_map().values()):
        if session is None or isinstance(session, (int, str)):
            continue
        if str(getattr(session, "agent_id", "") or "") == str(agent_id):
            return session
        if str(getattr(session, "subsession_id", "") or "") == str(agent_id):
            return session
    return None


def _get_session_for_view_id(view_id):
    if view_id is None:
        return None
    try:
        view_id = int(view_id)
    except (TypeError, ValueError):
        return None
    fn = _try_import("core.registry.get_session_for_view_id")
    if fn is not None:
        try:
            return fn(view_id)
        except Exception:
            pass
    return _sessions_map().get(view_id)


def _get_session_by_ref(ref):
    fn = _try_import("core.registry.get_session_by_ref")
    if fn is not None:
        try:
            return fn(ref)
        except Exception:
            pass
    if ref is None:
        return None
    if isinstance(ref, str) and not ref.isdigit():
        return _get_session_by_agent_id(ref)
    try:
        return _get_session_for_view_id(int(ref))
    except (TypeError, ValueError):
        return _get_session_by_agent_id(str(ref))


def _new_agent_id() -> str:
    fn = _try_import("core.registry.new_agent_id")
    if fn is not None:
        try:
            return fn()
        except Exception:
            pass
    return uuid.uuid4().hex[:12]


def _project_profiles_path(window) -> str:
    if window and window.folders():
        return os.path.join(window.folders()[0], PROJECT_SETTINGS_DIR, PROFILES_FILE)
    return ""


class MCPSocketServer:
    """Unix socket server for MCP eval requests."""

    def __init__(self) -> None:
        self.socket = None  # type: Optional[socket.socket]
        self.running = False
        self.thread = None  # type: Optional[threading.Thread]
        self._caller_agent_id = None  # type: Optional[str]
        self._cached_window = None
        self._cached_window_aid = None

    def start(self) -> None:
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.running = False
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
        try:
            os.unlink(SOCKET_PATH)
        except Exception:
            pass

    def _run(self) -> None:
        if not hasattr(socket, "AF_UNIX"):
            _log("AF_UNIX not available; MCP socket server disabled")
            return
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(SOCKET_PATH)
        self.socket.listen(5)
        self.socket.settimeout(1.0)
        _log("Listening on %s" % SOCKET_PATH)
        while self.running:
            try:
                conn, _ = self.socket.accept()
                self._handle_connection(conn)
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    _log("Error: %s" % e)

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            chunks = []  # type: List[bytes]
            total = 0
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_REQUEST_BYTES:
                    raise ValueError("request too large (>%d bytes)" % MAX_REQUEST_BYTES)
                if b"\n" in chunk:
                    break
            data = b"".join(chunks).decode(errors="replace")
            if not data.strip():
                return
            request = json.loads(data.strip())
            code = request.get("code", "")
            tool = request.get("tool")
            agent_id = request.get("agent_id")
            op = request.get("op")

            result = {"result": None, "error": None}  # type: Dict[str, Any]
            done = threading.Event()

            def do_eval():
                try:
                    if op == "debug":
                        result["result"] = self._debug_op(request)
                    elif op == "sessions":
                        # Session control for callers outside Sublime
                        # (submarine_sessions.py). JSON only: no `code`, no eval.
                        from features.session_control import dispatch as _control
                        result["result"] = _control(request)
                    else:
                        result["result"] = self._eval(
                            code, tool, caller_agent_id=agent_id)
                    done.set()
                except Exception as e:
                    result["error"] = str(e)
                    done.set()

            sublime.set_timeout(do_eval, 0)
            done.wait(timeout=EVAL_TIMEOUT_S)

            eval_result = result.get("result")
            if isinstance(eval_result, dict) and eval_result.get("_wait_for_init"):
                self._finish_wait_for_init(eval_result)
            conn.sendall((json.dumps(result) + "\n").encode())
        except Exception as e:
            import traceback
            _log("_handle_connection error: %s\n%s" % (e, traceback.format_exc()))
            try:
                conn.sendall((json.dumps({"error": str(e)}) + "\n").encode())
            except Exception:
                pass
        finally:
            conn.close()

    def _finish_wait_for_init(self, eval_result: dict) -> None:
        """Poll initialized, then query; wait_for_completion waits for working."""
        session = eval_result.pop("_session")
        prompt = eval_result.pop("_prompt")
        display_prompt = eval_result.pop("_display_prompt", None)
        wait_for_completion = eval_result.pop("_wait_for_completion", False)
        eval_result.pop("_wait_for_init", None)
        tag = "wake" if eval_result.get("waking") else "spawn"
        vid = _runtime_view_id(session) or "?"
        max_wait = 30
        start_t = time.time()
        _log("%s:%s waiting for init (backend=%s)..." % (
            tag, vid, getattr(session, "backend", None)))
        while not getattr(session, "initialized", False) and time.time() - start_t < max_wait:
            time.sleep(0.1)
        if not getattr(session, "initialized", False):
            elapsed = time.time() - start_t
            _log("%s:%s INIT TIMEOUT after %.1fs — prompt dropped" % (tag, vid, elapsed))
            eval_result["error"] = "Session failed to initialize within 30 seconds"
            eval_result.pop("sent", None)
            return
        _log("%s:%s init OK in %.1fs — scheduling prompt" % (
            tag, vid, time.time() - start_t))

        def send_prompt(_session=session, _prompt=prompt, _display=display_prompt,
                        _tag=tag, _vid=vid):
            try:
                if _session.working:
                    _log("%s:%s busy after init — queue_prompt" % (_tag, _vid))
                    _session.queue_prompt(_prompt, display=_display)
                elif _display:
                    _session.query(_prompt, display_prompt=_display)
                else:
                    _session.query(_prompt)
            except Exception as e:
                _log("%s:%s deliver raised: %s: %s" % (
                    _tag, _vid, type(e).__name__, e))

        sublime.set_timeout(send_prompt, 0)

        if wait_for_completion:
            # Wait until the child actually starts working, then until it stops.
            # Polling `working` alone lied while the session was still connecting.
            start_t = time.time()
            while (not getattr(session, "working", False)
                   and getattr(session, "initialized", False)
                   and time.time() - start_t < max_wait):
                time.sleep(0.1)
            while getattr(session, "working", False) and time.time() - start_t < max_wait:
                time.sleep(0.1)
            if getattr(session, "working", False):
                eval_result["warning"] = "Session still processing after 30 seconds"
        eval_result["working"] = bool(getattr(session, "working", False))
        eval_result["initialized"] = True

    def _debug_op(self, request: dict) -> Any:
        action = request.get("action") or "help"
        kwargs = {
            k: v for k, v in request.items()
            if k not in ("op", "action", "code", "tool")
        }
        dispatch = (
            _try_import("features.devtools.server.dispatch")
            or _try_import("features.devtools.dispatch")
        )
        if dispatch is None:
            return {
                "error": (
                    "features.devtools.dispatch is missing "
                    "(expected features.devtools.server.dispatch)"
                ),
                "action": action,
            }
        return dispatch(action, **kwargs)

    def _get_window(self):
        cached = getattr(self, "_cached_window", None)
        cached_aid = getattr(self, "_cached_window_aid", None)
        if cached and cached_aid == self._caller_agent_id:
            try:
                if cached.is_valid():
                    return cached
            except Exception:
                pass
        if self._caller_agent_id:
            session = _get_session_by_agent_id(str(self._caller_agent_id))
            if session is not None:
                try:
                    w = getattr(session, "window", None)
                    if w is not None:
                        self._cached_window = w
                        self._cached_window_aid = self._caller_agent_id
                        return w
                except Exception:
                    pass
                try:
                    v = session.output.view if session.output else None
                    if v is not None and v.is_valid():
                        w = v.window()
                        if w is not None:
                            self._cached_window = w
                            self._cached_window_aid = self._caller_agent_id
                            return w
                except Exception:
                    pass
            if str(self._caller_agent_id).isdigit():
                vid = int(self._caller_agent_id)
                for w in sublime.windows():
                    for v in w.views():
                        if v.id() == vid:
                            self._cached_window = w
                            self._cached_window_aid = self._caller_agent_id
                            return w
        return sublime.active_window()

    def _eval(self, code: str, tool: str = None, caller_agent_id: str = None):
        self._caller_agent_id = caller_agent_id
        if tool:
            window = self._get_window()
            if window and window.folders():
                tool_path = os.path.join(
                    window.folders()[0], ".claude", "sublime_tools", "%s.py" % tool)
                if os.path.exists(tool_path):
                    with open(tool_path, "r") as f:
                        code = f.read()
                else:
                    raise FileNotFoundError("Tool not found: %s" % tool_path)
            else:
                raise RuntimeError("No project folder open")

        exec_globals = self._build_exec_globals()
        window = self._get_window()
        exec_globals["cwd"] = window.folders()[0] if window and window.folders() else None
        exec_globals["AGENT_ID"] = str(caller_agent_id) if caller_agent_id else None

        if "return " in code:
            lines = code.split("\n")
            new_lines = []
            for line in lines:
                stripped = line.lstrip()
                if stripped.startswith("return "):
                    indent = line[:len(line) - len(stripped)]
                    new_lines.append("%s__result__ = %s" % (indent, stripped[7:]))
                else:
                    new_lines.append(line)
            code = "__result__ = None\n" + "\n".join(new_lines)
        else:
            code = "__result__ = None\n%s" % code
        exec(code, exec_globals)
        return exec_globals.get("__result__")

    def _build_exec_globals(self) -> dict:
        impls = {
            "get_window_summary": self._get_window_summary,
            "find_file": self._find_file,
            "get_symbols": self._get_symbols,
            "goto_symbol": self._goto_symbol,
            "read_view": self._read_view,
            "list_backends": self._list_backends,
            "list_profiles": self._list_profiles,
            "spawn_session": self._spawn_session,
            "send_to_session": self._send_to_session,
            "list_sessions": self._list_sessions,
            "read_session_output": self._read_session_output,
            "read_session_edits": self._read_session_edits,
            "list_profile_docs": self._list_profile_docs,
            "read_profile_doc": self._read_profile_doc,
            "lsp": self._lsp,
            "sublime_eval": self._sublime_eval,
            "sublime_tool": self._sublime_tool,
            "list_tools": self._list_tools,
            "quick_done": self._quick_done,
            "update_goal": self._update_goal,
            "goal_verdict": self._goal_verdict,
            "session_info": self._session_info,
            "signal_complete": self._signal_complete,
            "wait_for_subsession": self._wait_for_subsession,
            "set_timer": self._set_timer,
            "cancel_timer": self._cancel_timer,
            "write_artifact": self._write_artifact,
            "edit_artifact": self._edit_artifact,
            "read_artifact": self._read_artifact,
            "list_artifacts": self._list_artifacts,
        }
        required = exec_global_names()
        missing = required - set(impls)
        if missing:
            raise RuntimeError(
                "exec_globals missing implementations for: %s" % sorted(missing))
        g = {
            "sublime": sublime,
            "sublime_plugin": sublime_plugin,
        }
        for name in required:
            g[name] = impls[name]
        return g

    # ─── Editor tools ─────────────────────────────────────────────────────

    def _get_window_summary(self) -> dict:
        window = self._get_window()
        if not window:
            return {"error": "No window"}
        lines = []  # type: List[str]
        folders = window.folders()
        if folders:
            lines.append("Project: %s" % folders[0])
            for f in folders[1:]:
                lines.append("  + %s" % f)
        active_view = window.active_view()
        if active_view and active_view.file_name():
            row, col = (0, 0)
            if active_view.sel():
                row, col = active_view.rowcol(active_view.sel()[0].begin())
            lines.append("Active: %s:%d:%d" % (active_view.file_name(), row + 1, col + 1))
        try:
            sid = _window_setting(window, _ACTIVE_AGENT_KEYS)
            sess = _get_session_by_agent_id(str(sid)) if sid else None
            if sess is None:
                vid = _window_setting(window, _ACTIVE_VIEW_KEYS)
                sess = _get_session_for_view_id(vid) if vid is not None else None
            et = getattr(sess, "edit_target", None) if sess else None
            if et:
                lines.append("Edit target: %s" % et)
        except Exception:
            pass
        all_views = window.views()
        open_files = [v.file_name() for v in all_views if v.file_name()]
        dirty_files = [v.file_name() for v in all_views if v.file_name() and v.is_dirty()]
        lines.append("Open files (%d):" % len(open_files))
        for f in open_files[:20]:
            marker = " *" if f in dirty_files else ""
            lines.append("  • %s%s" % (os.path.basename(f), marker))
        if len(open_files) > 20:
            lines.append("  ... and %d more" % (len(open_files) - 20))
        return {
            "summary": "\n".join(lines),
            "open_count": len(open_files),
            "dirty_count": len(dirty_files),
        }

    def _find_file(self, query: str, pattern: str = None, limit: int = 20) -> list:
        import fnmatch
        window = self._get_window()
        if not window:
            return []
        folders = window.folders()
        if not folders:
            return []
        all_files = []  # type: List[str]
        root_folder = folders[0]
        for folder in folders:
            for dirpath, dirnames, filenames in os.walk(folder):
                dirnames[:] = [d for d in dirnames
                               if not d.startswith(".") and d not in SKIP_DIRS]
                for filename in filenames:
                    if filename.startswith("."):
                        continue
                    full_path = os.path.join(dirpath, filename)
                    rel_path = os.path.relpath(full_path, root_folder)
                    if pattern:
                        if "**" in pattern:
                            if not fnmatch.fnmatch(rel_path, pattern):
                                continue
                        elif not (fnmatch.fnmatch(filename, pattern)
                                  or fnmatch.fnmatch(rel_path, pattern)):
                            continue
                    all_files.append(rel_path)
        if not all_files:
            return []
        query_lower = (query or "").lower()
        scored = []  # type: List[Tuple[int, str]]
        for path in all_files:
            filename = os.path.basename(path).lower()
            path_lower = path.lower()
            if filename == query_lower:
                scored.append((0, path))
            elif filename.startswith(query_lower):
                scored.append((1, path))
            elif query_lower in filename:
                scored.append((2, path))
            elif query_lower in path_lower:
                scored.append((3, path))
            else:
                idx = 0
                for char in query_lower:
                    idx = path_lower.find(char, idx)
                    if idx == -1:
                        break
                    idx += 1
                else:
                    scored.append((4, path))
        scored.sort(key=lambda x: (x[0], x[1]))
        return [path for _, path in scored[:limit]]

    def _get_symbols(self, query, file_path: str = None, limit: int = 10) -> dict:
        window = self._get_window()
        if not window:
            return {"success": False, "error": "No window"}
        if isinstance(query, str):
            query = query.strip()
            if query.startswith("["):
                try:
                    symbols = json.loads(query)
                except Exception:
                    symbols = [query]
            elif "," in query:
                symbols = [s.strip() for s in query.split(",") if s.strip()]
            else:
                symbols = [query]
        elif isinstance(query, list):
            symbols = [str(s).strip() for s in query if str(s).strip()]
        else:
            symbols = []
        if not symbols:
            return {"success": False, "error": "No symbols provided"}
        lines = []  # type: List[str]
        all_locations = []  # type: List[dict]
        for sym in symbols:
            locations = window.lookup_symbol_in_index(sym)
            if file_path:
                locations = [loc for loc in locations
                             if self._symbol_file_matches(loc[0], file_path)]
            max_results = limit if limit > 0 else 50
            if len(locations) < max_results:
                partials = self._find_partial_symbols(
                    sym, file_path, max_results - len(locations))
                locations = self._merge_symbol_locations(locations, partials)
            if not locations:
                lines.append("'%s': not found" % sym)
                continue
            lines.append("'%s' (%d matches):" % (sym, len(locations)))
            shown = locations[:limit] if limit > 0 else locations
            for loc in shown:
                fp, display_name, (row, col) = loc[0], loc[1], loc[2]
                lines.append("  • %s:%s:%s - %s" % (
                    os.path.basename(fp), row, col, display_name))
                all_locations.append({
                    "symbol": sym, "file": fp, "row": row, "col": col,
                })
            if limit > 0 and len(locations) > limit:
                lines.append("  ... and %d more" % (len(locations) - limit))
        return {"summary": "\n".join(lines), "locations": all_locations[:50]}

    def _symbol_file_matches(self, path: str, file_path: str) -> bool:
        if not file_path:
            return True
        if path == file_path:
            return True
        try:
            return os.path.abspath(path) == os.path.abspath(file_path)
        except Exception:
            return False

    def _merge_symbol_locations(self, primary, secondary):
        seen = set()
        merged = []
        for loc in list(primary or []) + list(secondary or []):
            try:
                fp, name, point = loc[0], loc[1], loc[2]
                row, col = point
            except Exception:
                continue
            key = (os.path.abspath(fp), row, col)
            if key in seen:
                continue
            seen.add(key)
            merged.append(loc)
        return merged

    def _find_partial_symbols(self, query: str, file_path: str = None, limit: int = 10) -> list:
        window = self._get_window()
        if not window:
            return []
        q = (query or "").strip().lower()
        if not q or (len(q) < 2 and not file_path):
            return []
        roots = window.folders()
        if not roots:
            return []
        max_files = 1 if file_path else 300
        max_lines = 5000 if file_path else 30000
        max_candidates = max(limit * 5, 25)
        deadline = time.time() + (0.75 if file_path else 0.25)
        candidates = []  # type: List[tuple]
        scanned_files = 0
        scanned_lines = 0
        for path in self._iter_symbol_files(roots, file_path):
            if scanned_files >= max_files or scanned_lines >= max_lines:
                break
            if time.time() >= deadline:
                break
            try:
                if os.path.getsize(path) > 1024 * 1024:
                    continue
                scanned_files += 1
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for row, line in enumerate(f, 1):
                        scanned_lines += 1
                        if scanned_lines >= max_lines:
                            break
                        if row % 200 == 0 and time.time() >= deadline:
                            break
                        for name, col in self._extract_symbol_names(line):
                            score = self._score_partial_symbol(q, name)
                            if score is None:
                                continue
                            candidates.append(
                                (score, name.lower(), path, "%s (partial)" % name, (row, col)))
                            if len(candidates) >= max_candidates:
                                break
                        if len(candidates) >= max_candidates:
                            break
            except Exception:
                continue
            if len(candidates) >= max_candidates:
                break
        candidates.sort(key=lambda item: (item[0], item[1], item[2], item[4][0]))
        return [(path, display, point)
                for _, _, path, display, point in candidates[:max(limit, 0)]]

    def _iter_symbol_files(self, roots, file_path: str = None):
        if file_path:
            yield os.path.abspath(file_path)
            return
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames
                               if d not in SKIP_DIRS and not d.startswith(".")]
                for filename in filenames:
                    if filename.startswith("."):
                        continue
                    ext = os.path.splitext(filename)[1].lower()
                    if ext in SYMBOL_EXTS:
                        yield os.path.join(dirpath, filename)

    def _extract_symbol_names(self, line: str) -> list:
        patterns = (
            r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            r"^\s*(?:proc|func|iterator|template|macro|method)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            r"^\s*(?:type)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\b",
            r"^\s*(?:export\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)\b",
            r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=",
            r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\b",
            r"^\s*(?:public|private|protected|internal|static|final|abstract|\s)*\s*(?:class|interface|enum|struct)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            r"^\s*(?:function|local\s+function)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        )
        for pat in patterns:
            m = re.search(pat, line)
            if m:
                return [(m.group(1), m.start(1) + 1)]
        return []

    def _score_partial_symbol(self, query: str, name: str):
        n = name.lower()
        if n == query:
            return 0
        if n.startswith(query):
            return 1
        if query in n:
            return 2
        return None

    def _list_tools(self) -> list:
        window = self._get_window()
        if not window or not window.folders():
            return []
        tools_dir = os.path.join(window.folders()[0], ".claude", "sublime_tools")
        if not os.path.exists(tools_dir):
            return []
        tools = []  # type: List[dict]
        for filename in os.listdir(tools_dir):
            if not filename.endswith(".py"):
                continue
            name = filename[:-3]
            tool_path = os.path.join(tools_dir, filename)
            try:
                with open(tool_path, "r") as f:
                    src = f.read()
                desc = "No description"
                if src.startswith('"""'):
                    end = src.find('"""', 3)
                    if end > 0:
                        desc = src[3:end].strip()
                elif src.startswith("'''"):
                    end = src.find("'''", 3)
                    if end > 0:
                        desc = src[3:end].strip()
                tools.append({"name": name, "description": desc})
            except Exception:
                tools.append({"name": name, "description": "No description"})
        return tools

    def _goto_symbol(self, query: str) -> dict:
        window = self._get_window()
        if not window:
            return {"error": "No window"}
        locations = window.lookup_symbol_in_index(query)
        if not locations:
            return {"error": "Symbol '%s' not found" % query}
        loc = locations[0]
        path, name, (row, col) = loc[0], loc[1], loc[2]
        window.open_file("%s:%s:%s" % (path, row, col), sublime.ENCODED_POSITION)
        return {"file": path, "name": name, "row": row, "col": col}

    def _read_view(
        self,
        file_path: str = None,
        view_name: str = None,
        head: int = None,
        tail: int = None,
        grep: str = None,
        grep_i: str = None,
        max_chars: int = 50000,
    ) -> dict:
        window = self._get_window()
        if not window:
            return {"error": "No window"}
        view = None
        identifier = None
        if view_name:
            for v in window.views():
                if v.name() == view_name:
                    view = v
                    identifier = view_name
                    break
            if not view:
                return {"error": "View not found: %s" % view_name}
        elif file_path:
            if not os.path.isabs(file_path):
                for folder in window.folders():
                    full_path = os.path.join(folder, file_path)
                    if os.path.exists(full_path):
                        file_path = full_path
                        break
            file_path = os.path.normpath(file_path)
            identifier = file_path
            for v in window.views():
                if v.file_name() and os.path.normpath(v.file_name()) == file_path:
                    view = v
                    break
            if not view:
                if os.path.exists(file_path):
                    view = window.open_file(file_path, sublime.TRANSIENT)
                    deadline = time.time() + 2.0
                    while view.is_loading() and time.time() < deadline:
                        time.sleep(0.05)
                else:
                    return {"error": "File not found: %s" % file_path}
        else:
            return {"error": "Must provide either file_path or view_name"}
        if not view:
            return {"error": "Could not open view for: %s" % identifier}
        content = view.substr(sublime.Region(0, view.size()))
        all_lines = content.split("\n")
        original_line_count = len(all_lines)
        if grep or grep_i:
            pattern = grep if grep else grep_i
            flags = re.IGNORECASE if grep_i else 0
            try:
                regex = re.compile(pattern, flags)
                all_lines = [line for line in all_lines if regex.search(line)]
            except re.error as e:
                return {"error": "Invalid regex pattern: %s" % e}
        if head is not None and tail is not None:
            return {"error": "Cannot specify both head and tail"}
        if head is not None:
            all_lines = all_lines[:head]
        elif tail is not None:
            all_lines = all_lines[-tail:] if tail < len(all_lines) else all_lines
        content = "\n".join(all_lines)
        truncated = False
        if max_chars > 0 and len(content) > max_chars:
            content = content[:max_chars]
            truncated = True
        out = {
            "content": content,
            "size": len(content),
            "line_count": len(all_lines),
            "original_line_count": original_line_count,
            "truncated": truncated,
        }
        if file_path:
            out["file_path"] = file_path
        if view_name:
            out["view_name"] = view_name
        if grep or grep_i:
            out["grep_pattern"] = grep if grep else grep_i
            out["grep_case_insensitive"] = bool(grep_i)
        if head is not None:
            out["head"] = head
        if tail is not None:
            out["tail"] = tail
        return out

    def _sublime_eval(self, code: str = "") -> Any:
        return self._eval(code or "", caller_agent_id=self._caller_agent_id)

    def _sublime_tool(self, name: str = "") -> Any:
        return self._eval("", tool=name, caller_agent_id=self._caller_agent_id)

    # ─── Session tools ────────────────────────────────────────────────────

    def _list_backends(self) -> dict:
        try:
            from backend.specs import BACKENDS, all_backends
        except ImportError:
            return {"error": "backend.specs is not available"}
        default = "claude"
        try:
            settings = sublime.load_settings("Submarine.sublime-settings")
            default = settings.get("default_backend", "claude") or "claude"
        except Exception:
            pass
        rows = []  # type: List[dict]
        summary = ["Backends (● available, ○ not):"]
        for name, spec in all_backends().items():
            avail = spec.available is None or bool(spec.available())
            kind = "builtin" if name in BACKENDS else "custom"
            rows.append({
                "name": name,
                "label": spec.label or name,
                "available": avail,
                "kind": kind,
                "pinned": spec.pinned,
                "bridge": spec.bridge_script,
                "fallback_model": spec.fallback_model,
                "effort": spec.effort,
                "models": [[mid, mlabel] for mid, mlabel in spec.default_models],
            })
            mark = "●" if avail else "○"
            pin = " 📌" if spec.pinned else ""
            summary.append("  %s %s → %s%s  [%s, bridge=%s]" % (
                mark, name, spec.label or name, pin, kind, spec.bridge_script))
        summary.append("")
        summary.append("default backend: %s" % default)
        summary.append(
            "Spawn via spawn_session(backend=<name>, model=<id from models>). "
            "Forking without model= keeps the source session's submodel. "
            "fork_current / fork_from_* only work within the same bridge family."
        )
        return {"summary": "\n".join(summary), "default": default, "backends": rows}

    def _list_profiles(self) -> dict:
        """Profiles map only (other keys in the JSON file are ignored)."""
        try:
            from plat.settings import load_profiles
        except ImportError:
            return {"error": "plat.settings.load_profiles is not available"}
        window = self._get_window()
        project_path = _project_profiles_path(window)
        profiles = load_profiles(project_path or None)
        lines = []  # type: List[str]
        names = []  # type: List[str]
        if profiles:
            lines.append("Profiles:")
            for name, config in profiles.items():
                model = config.get("model", "default") if isinstance(config, dict) else "default"
                desc = ""
                if isinstance(config, dict):
                    desc = config.get("description", "") or ""
                desc_short = ""
                if desc:
                    desc_short = " - %s..." % desc[:40] if len(desc) > 40 else " - %s" % desc
                lines.append("  • %s (%s)%s" % (name, model, desc_short))
                names.append(name)
        else:
            lines.append("No profiles configured")
        return {"summary": "\n".join(lines), "profiles": names}

    @staticmethod
    def _bridge_script_for(backend_name: str):
        try:
            from backend.specs import get
            return get(backend_name).bridge_script
        except Exception:
            return None

    @classmethod
    def _same_backend_family(cls, a: str, b: str) -> bool:
        if not a or not b:
            return False
        if a == b:
            return True
        ba, bb = cls._bridge_script_for(a), cls._bridge_script_for(b)
        return ba is not None and ba == bb

    def _spawn_session(
        self,
        prompt: str,
        name: str = None,
        profile: str = None,
        backend: str = None,
        fork_current: bool = False,
        wait_for_completion: bool = False,
        fork_from_agent_id: str = None,
        model: str = None,
        _caller_agent_id: str = None,
    ) -> dict:
        create_session = (
            _try_import("commands.session_cmds.create_session")
            or _try_import("core.host.create_session")
            or _try_import("core.session.create_session")
        )
        if create_session is None:
            return {
                "error": (
                    "create_session is missing — expected "
                    "commands.session_cmds.create_session(window, resume_id=, "
                    "fork=, profile=, initial_context=, backend=, focus=False)"
                ),
            }
        window = self._get_window()
        if not window:
            return {"error": "No window"}
        fork_srcs = sum(1 for x in (
            fork_current,
            bool(fork_from_agent_id),
        ) if x)
        if fork_srcs > 1:
            return {
                "error": "Pass only one of fork_current or fork_from_agent_id",
            }

        profile_config = None
        if profile:
            try:
                from plat.settings import load_profiles
                profiles = load_profiles(_project_profiles_path(window) or None)
            except Exception:
                profiles = {}
            if profile not in profiles:
                return {"error": "Profile '%s' not found" % profile}
            profile_config = profiles[profile]

        resume_id = None
        fork = False
        fork_source_backend = None
        fork_source_model = None

        def _resolve_fork_source(source_session, label: str):
            nonlocal resume_id, fork, fork_source_backend, fork_source_model, backend
            if not source_session:
                return {"error": "Cannot fork: %s not found" % label}
            sid = getattr(source_session, "session_id", None)
            if not sid:
                return {"error": "Cannot fork: %s has no session_id yet (still starting?)" % label}
            src_backend = getattr(source_session, "backend", None) or "claude"
            fork_source_model = getattr(source_session, "model", None)
            if not backend:
                backend = src_backend
            if not self._same_backend_family(src_backend, backend):
                return {
                    "error": (
                        "Cannot fork %r session into %r; session IDs are not "
                        "portable across bridge families." % (src_backend, backend)
                    ),
                    "source_backend": src_backend,
                    "requested_backend": backend,
                }
            resume_id = sid
            fork = True
            fork_source_backend = src_backend
            return None

        if fork_from_agent_id:
            err = _resolve_fork_source(
                _get_session_by_agent_id(str(fork_from_agent_id)),
                "agent_id %s" % fork_from_agent_id)
            if err:
                return err
        elif fork_current:
            caller_aid = _caller_agent_id or self._caller_agent_id
            caller_session = _get_session_by_agent_id(str(caller_aid)) if caller_aid else None
            if not caller_session:
                get_active = (
                    _try_import("commands.session_cmds.get_active_session")
                    or _try_import("core.host.get_active_session")
                )
                if get_active is not None:
                    try:
                        caller_session = get_active(window)
                    except Exception:
                        caller_session = None
            err = _resolve_fork_source(caller_session, "current session")
            if err:
                return err

        if not backend:
            try:
                settings = sublime.load_settings("Submarine.sublime-settings")
                backend = settings.get("default_backend", "claude") or "claude"
            except Exception:
                backend = "claude"

        agent_id = _new_agent_id()
        subsession_id = agent_id
        parent_session = None
        caller_aid = _caller_agent_id or self._caller_agent_id
        if caller_aid:
            parent_session = _get_session_by_agent_id(str(caller_aid))
        if not parent_session:
            parent_session, _ = self._get_session_for_tool()
        parent_agent_id = None
        parent_session_id = None
        if parent_session:
            parent_agent_id = getattr(parent_session, "agent_id", None)
            parent_session_id = getattr(parent_session, "session_id", None)

        initial_context = {
            "agent_id": agent_id,
            "subsession_id": subsession_id,
            "parent_agent_id": parent_agent_id,
            "parent_session_id": parent_session_id,
        }
        resolve_spawn_model = _try_import("core.registry.resolve_spawn_model")
        spawn_model = None
        if resolve_spawn_model is not None:
            spawn_model = resolve_spawn_model(
                requested=model,
                source_model=fork_source_model,
                forking=fork,
            )
        elif model:
            spawn_model = str(model).strip() or None
        elif fork and fork_source_model:
            spawn_model = str(fork_source_model).strip() or None
        if (backend or "") == "grok" and spawn_model:
            try:
                from backend import grok as grok_backend
                spawn_model = grok_backend.normalize_grok_model(spawn_model)
            except Exception:
                pass
        # A tool-created session takes the screen only when the user is
        # looking at the session that asked for it. Otherwise it starts
        # viewless (it shows in the Sessions list; reveal it from there) —
        # attaching it would swap the shared host sheet out from under
        # whatever the user is reading or typing in.
        show = _parent_in_focus(window, parent_session)
        session = create_session(
            window, resume_id=resume_id, fork=fork, profile=profile_config,
            initial_context=initial_context, backend=backend, focus=False,
            model=spawn_model, show=show,
        )
        if name:
            session.name = name
            try:
                session.output.set_name(name)
            except Exception:
                pass
        try:
            persist = getattr(session, "_persist_view_identity", None)
            if persist:
                persist()
            register = _try_import("core.registry.register_session")
            if register is not None:
                register(session)
            # Note the child on the parent so list_sessions survives a parent
            # sheet recreate that mints a new agent_id.
            if parent_session is not None:
                note_child = _try_import("core.registry.note_child")
                if note_child is not None:
                    note_child(parent_session, agent_id)
                try:
                    ppersist = getattr(parent_session, "_persist_view_identity", None)
                    if ppersist:
                        ppersist()
                    psave = getattr(parent_session, "_save_session", None)
                    if psave:
                        psave()
                except Exception:
                    pass
        except Exception:
            pass

        prompt = self._with_subsession_report_contract(prompt or "")
        out = {
            "_wait_for_init": True,
            "_session": session,
            "_prompt": prompt,
            "_wait_for_completion": wait_for_completion,
            "spawned": True,
            "name": name or "(unnamed)",
            "agent_id": agent_id,
            "subsession_id": subsession_id,
            "parent_agent_id": parent_agent_id,
            "parent_session_id": parent_session_id,
            "backend": backend,
            "model": spawn_model or getattr(session, "model", None),
            "fork": fork,
            "profile": profile,
        }
        if fork_from_agent_id:
            out["forked_from_agent_id"] = fork_from_agent_id
            out["forked_from_backend"] = fork_source_backend
        return out

    @staticmethod
    def _with_subsession_report_contract(prompt: str) -> str:
        body = (prompt or "").rstrip()
        if "signal_complete" in body:
            return body
        return (
            body
            + "\n\nUse this project's knowledge first (list_profile_docs / irr / "
            "existing code) — do not invent APIs or layout. "
            "When fully done, call the Sublime MCP tool signal_complete"
            "(result_summary=…) as its own last step after your final message "
            "— not in parallel with other tools. That call IS the parent "
            "notification; do not also send_to_session the parent with the "
            "same summary, and do not use a CLI/fake complete. "
            "Parent is notified only after this turn idles; "
            "host attaches context_budget for strategy."
        )

    def _stamp_send_prompt(self, prompt: str) -> Tuple[str, str]:
        stamp = _try_import("core.registry.stamp_sender_prompt")
        display = _try_import("core.registry.sender_display_prompt")
        caller = None
        aid = getattr(self, "_caller_agent_id", None)
        if aid is not None:
            caller = _get_session_by_agent_id(str(aid))
        if caller is None:
            ev = _window_setting(self._get_window(), _EXEC_VIEW_KEYS)
            if ev is not None:
                caller = _get_session_for_view_id(ev)
        if caller is None:
            ea = _window_setting(self._get_window(), _ACTIVE_AGENT_KEYS)
            if ea is not None:
                caller = _get_session_by_agent_id(str(ea))
        if stamp is None:
            return prompt, prompt
        stamped = stamp(
            prompt,
            sender_agent_id=getattr(caller, "agent_id", None) or "" if caller else "",
            sender_session_id=getattr(caller, "session_id", None) or "" if caller else "",
            sender_name=(caller.name or "") if caller else "",
        )
        shown = display(stamped) if display is not None else stamped
        return stamped, shown

    def _send_to_session(
        self,
        prompt: str,
        agent_id: str = None,
        _caller_agent_id: str = None,
        session_id: str = None,
        name: str = None,
    ) -> dict:
        if _caller_agent_id is not None:
            self._caller_agent_id = _caller_agent_id
        if not prompt:
            return {"error": "prompt is required"}
        if not (agent_id or session_id or name):
            return {
                "error": "Pass agent_id, session_id or name",
                "hint": 'Call list_sessions(scope="all") to find a session',
            }
        if agent_id:
            session = _get_session_by_agent_id(str(agent_id))
            if not session:
                return {
                    "error": "Session not found for agent_id %r" % agent_id,
                    "hint": 'Call list_sessions(scope="all")',
                }
        else:
            # Same resolution the sessions CLI uses: every window, live only.
            resolve = _try_import("features.session_control._resolve_live")
            if resolve is None:
                return {"error": "cannot resolve by session_id/name here; pass agent_id"}
            ref = {"session_id": str(session_id)} if session_id else {"name": str(name)}
            try:
                session = resolve(ref)
            except Exception as e:
                detail = getattr(e, "data", None) or {}
                return {
                    "error": str(getattr(e, "message", None) or e),
                    "candidates": [
                        {k: c.get(k) for k in ("agent_id", "name", "state") if k in c}
                        for c in (detail.get("candidates") or [])[:20]
                    ],
                    "hint": "Retry with the agent_id of the one you mean",
                }
        prompt, display = self._stamp_send_prompt(prompt)
        aid = getattr(session, "agent_id", None)
        name = session.name or "(unnamed)"
        if session.working or getattr(session, "_compacting", False):
            session.queue_prompt(prompt, display=display)
            return {
                "sent": True, "queued": True, "agent_id": aid,
                "name": name,
                "message": "Target is mid-turn; prompt queued and will run next.",
            }
        sleeping = _is_sleeping(session)
        if sleeping:
            if not getattr(session, "session_id", None):
                return {
                    "error": "Session sleeping but has no session_id — cannot wake",
                    "agent_id": aid,
                }
            session.wake()
            return {
                "_wait_for_init": True, "_session": session, "_prompt": prompt,
                "_display_prompt": display, "_wait_for_completion": False,
                "sent": True, "waking": True, "agent_id": aid,
                "name": name,
            }
        if not session.initialized:
            if session.client or getattr(session, "session_id", None):
                return {
                    "_wait_for_init": True, "_session": session, "_prompt": prompt,
                    "_display_prompt": display, "_wait_for_completion": False,
                    "sent": True, "waking": True, "agent_id": aid,
                    "name": name,
                }
            return {"error": "Session not initialized", "agent_id": aid}
        session.query(prompt, display_prompt=display)
        return {"sent": True, "agent_id": aid, "name": name}

    @staticmethod
    def _session_context_budget(session) -> dict:
        try:
            if hasattr(session, "context_budget_snapshot"):
                return session.context_budget_snapshot() or {}
        except Exception:
            pass
        return {"summary": "ctx:unknown", "has_usage": False}

    def _list_sessions(self, scope: str = "children") -> dict:
        if str(scope or "children").lower() == "all":
            return self._list_all_sessions()
        caller_id = self._caller_agent_id
        caller = _get_session_by_agent_id(str(caller_id)) if caller_id is not None else None
        parent_agent_id = getattr(caller, "agent_id", None) if caller else None
        list_children = _try_import("core.registry.list_children_of")
        if list_children is not None:
            children = list_children(
                parent_agent_id=parent_agent_id, parent=caller)
        else:
            children = []
            iter_fn = _try_import("core.registry.iter_sessions")
            live = list(iter_fn()) if iter_fn is not None else list(_sessions_map().values())
            for session in live:
                if parent_agent_id and getattr(session, "parent_agent_id", None) == parent_agent_id:
                    children.append(session)
        # A parent sheet recreate mints a new agent_id; children stamped with
        # the old one are recovered from the ids named in this transcript.
        if caller is not None:
            text = ""
            try:
                view = caller.output.view if caller.output else None
                if view and view.is_valid():
                    text = view.substr(sublime.Region(0, view.size()))
            except Exception:
                text = ""
            harvest = _try_import("core.registry.harvest_mentioned_orphans")
            if text and harvest is not None:
                relink = _try_import("core.registry.relink_child_to_parent")
                seen = {id(s) for s in children}
                try:
                    found = harvest(caller, text) or []
                except Exception:
                    found = []
                for s in found:
                    if id(s) in seen:
                        continue
                    if relink is not None:
                        try:
                            relink(s, caller)
                        except Exception:
                            pass
                    children.append(s)
                    seen.add(id(s))
            if children:
                try:
                    caller._persist_view_identity()
                    caller._save_session()
                except Exception:
                    pass
        sessions = []  # type: List[dict]
        lines = []  # type: List[str]
        for session in children:
            sleeping = _is_sleeping(session)
            phase = getattr(session, "turn_phase", None) or (
                "waiting" if session.working else "idle")
            if sleeping:
                status = "⏸"
            elif session.working:
                status = {"waiting": "…", "responding": "▸", "tool": "⚙"}.get(phase, "⏳")
            else:
                status = "✓"
            budget = self._session_context_budget(session)
            name = session.name or "(unnamed)"
            aid = getattr(session, "agent_id", None) or ""
            budget_s = budget.get("summary") or ""
            phase_s = " %s" % phase if session.working else ""
            lines.append("%s %s %s%s%s" % (
                status, aid, name, phase_s,
                (" · %s" % budget_s) if budget_s else ""))
            sessions.append({
                "agent_id": aid,
                "name": name,
                "working": bool(session.working),
                "sleeping": sleeping,
                "turn_phase": phase,
                "subsession_id": getattr(session, "subsession_id", None) or aid,
                "parent_agent_id": getattr(session, "parent_agent_id", None),
                "backend": getattr(session, "backend", None),
                "forkable": bool(getattr(session, "session_id", None)),
                "context_budget": budget,
                "context_summary": budget.get("summary"),
                "context_pct": budget.get("context_pct"),
                "headroom": budget.get("headroom"),
            })
        if not lines:
            if caller is None and not parent_agent_id:
                return {
                    "summary": "No caller session for list_sessions "
                               "(agent_id not bound to a live session). "
                               "Spawned children are still listed by the "
                               "agent_id from spawn_session.",
                    "sessions": [],
                    "count": 0,
                }
            return {
                "summary": "No subsessions (use agent_id from spawn)",
                "sessions": [],
                "count": 0,
                "caller_agent_id": parent_agent_id,
            }
        return {"summary": "\n".join(lines), "sessions": sessions, "count": len(sessions)}

    def _list_all_sessions(self) -> dict:
        """Every live session in every window — peers to send_to_session."""
        caller_id = self._caller_agent_id
        iter_fn = _try_import("core.registry.iter_sessions")
        live = list(iter_fn()) if iter_fn is not None else list(_sessions_map().values())
        rows, lines = [], []
        for session in live:
            if getattr(session, "quick_mode", False):
                continue
            aid = getattr(session, "agent_id", None) or ""
            sleeping = _is_sleeping(session)
            state = "sleeping" if sleeping else ("working" if session.working else "idle")
            window = getattr(session, "window", None)
            try:
                wid = window.id() if window is not None else None
            except Exception:
                wid = None
            project = getattr(session, "cwd", None) or ""
            name = " ".join(str(session.name or "(unnamed)").split())
            me = bool(caller_id) and str(aid) == str(caller_id)
            rows.append({
                "agent_id": aid,
                "name": name,
                "state": state,
                "backend": getattr(session, "backend", None),
                "window": wid,
                "project": project or None,
                "parent_agent_id": getattr(session, "parent_agent_id", None),
                "you": me,
            })
            lines.append("%s %-9s %s%s  [win %s · %s]" % (
                aid, state, name[:70], "  (you)" if me else "",
                wid if wid is not None else "?",
                os.path.basename(project.rstrip("/")) or "-"))
        return {"summary": "\n".join(lines) or "No live sessions",
                "sessions": rows, "count": len(rows), "scope": "all"}

    def _read_session_edits(
        self,
        agent_id: str = None,
        offset: int = 0,
        limit: int = 10,
        file_path: str = None,
    ) -> dict:
        """Page Edit/Write diffs from a subsession transcript."""
        from core.session_edits import collect_session_edits, conversations_of, page_edits

        session = _get_session_by_agent_id(str(agent_id)) if agent_id else None
        if not session:
            return {
                "error": "Session not found for %r" % agent_id,
                "hint": "Prefer agent_id from list_sessions / spawn_session",
                "available_agent_ids": list(_agents_map()),
            }
        edits = collect_session_edits(conversations_of(session))
        page = page_edits(
            edits, offset=offset, limit=limit, file_path=file_path)
        page["agent_id"] = getattr(session, "agent_id", None)
        return page

    def _read_session_output(
        self,
        agent_id: str = None,
        lines: int = None,
        max_chars: int = 30000,
    ) -> dict:
        session = _get_session_by_agent_id(str(agent_id)) if agent_id else None
        if not session:
            return {
                "error": "Session not found for %r" % agent_id,
                "hint": "Use agent_id from list_sessions",
                "available_agent_ids": list(_agents_map()),
            }
        aid = getattr(session, "agent_id", None)
        if not session.output or not session.output.view:
            return {"error": "Session output view not found", "agent_id": aid}
        view = session.output.view
        content = view.substr(sublime.Region(0, view.size()))
        if lines:
            all_lines = content.split("\n")
            if len(all_lines) > lines:
                content = "\n".join(all_lines[-lines:])
        truncated = False
        skipped_messages = 0
        if max_chars > 0 and len(content) > max_chars:
            message_pattern = r"\n(?=───|╭|▸|⚠|✓|✗|\n\n)"
            parts = re.split(message_pattern, content)
            kept_parts = []  # type: List[str]
            total_len = 0
            for part in reversed(parts):
                if total_len + len(part) > max_chars and kept_parts:
                    skipped_messages += 1
                    continue
                kept_parts.insert(0, part)
                total_len += len(part) + 1
            content = "\n".join(kept_parts)
            truncated = True
            if skipped_messages > 0:
                content = "[... %d earlier messages truncated ...]\n\n%s" % (
                    skipped_messages, content)
        budget = self._session_context_budget(session)
        return {
            "agent_id": aid,
            "name": session.name or "(unnamed)",
            "working": bool(session.working),
            "sleeping": _is_sleeping(session),
            "backend": getattr(session, "backend", None),
            "output": content,
            "line_count": content.count("\n") + 1 if content else 0,
            "truncated": truncated,
            "context_budget": budget,
            "context_summary": budget.get("summary"),
            "context_pct": budget.get("context_pct"),
            "headroom": budget.get("headroom"),
        }

    def _caller_or_active_session(self):
        session = None
        aid = getattr(self, "_caller_agent_id", None)
        if aid is not None:
            session = _get_session_by_agent_id(str(aid))
        if session is None:
            window = self._get_window()
            ea = _window_setting(window, _ACTIVE_AGENT_KEYS)
            if ea is not None:
                session = _get_session_by_agent_id(str(ea))
        if session is None:
            window = self._get_window()
            vid = _window_setting(window, _EXEC_VIEW_KEYS)
            if vid is not None:
                session = _get_session_for_view_id(vid)
        return session

    def _list_profile_docs(self) -> dict:
        session = self._caller_or_active_session()
        if session is None:
            window = self._get_window()
            ea = _window_setting(window, _ACTIVE_AGENT_KEYS)
            if ea is not None:
                session = _get_session_by_agent_id(str(ea))
            if session is None:
                sid = _window_setting(window, _ACTIVE_VIEW_KEYS)
                if sid is not None:
                    session = _get_session_for_view_id(sid)
        if session is None:
            return {"error": "No active Submarine session"}
        docs = getattr(session, "profile_docs", None) or []
        if not docs:
            return {"docs": [], "count": 0, "note": "No profile docs configured for this session"}
        profile = getattr(session, "profile", None) or {}
        return {
            "docs": docs,
            "count": len(docs),
            "profile": profile.get("description", "") if isinstance(profile, dict) else "",
        }

    def _read_profile_doc(self, path: str) -> dict:
        session = self._caller_or_active_session()
        if session is None:
            window = self._get_window()
            ea = _window_setting(window, _ACTIVE_AGENT_KEYS)
            if ea is not None:
                session = _get_session_by_agent_id(str(ea))
            if session is None:
                sid = _window_setting(window, _ACTIVE_VIEW_KEYS)
                if sid is not None:
                    session = _get_session_for_view_id(sid)
        if session is None:
            return {"error": "No active Submarine session"}
        docs = getattr(session, "profile_docs", None) or []
        if path not in docs:
            return {
                "error": "File '%s' not in profile docset" % path,
                "available": list(docs)[:10],
                "total": len(docs),
            }
        cwd_fn = getattr(session, "_cwd", None)
        cwd = cwd_fn() if cwd_fn else None
        if not cwd:
            window = self._get_window()
            cwd = window.folders()[0] if window and window.folders() else ""
        full_path = os.path.join(cwd, path)
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            json.dumps(content)
            return {"path": path, "content": content, "size": len(content)}
        except Exception as e:
            return {"error": "Failed to read %s: %s" % (path, e)}

    # ─── Quick / goal / timer ─────────────────────────────────────────────

    def _quick_done(self, status: str = "completed", message: str = "") -> dict:
        complete = (
            _try_import("features.quick.complete_quick_from_tool")
            or _try_import("features.quick_agent.complete_quick_from_tool")
        )
        if complete is None:
            return {
                "ok": False,
                "error": (
                    "features.quick.complete_quick_from_tool is missing"
                ),
                "rejected": True,
            }
        vid = None
        if self._caller_agent_id:
            s = _get_session_by_agent_id(str(self._caller_agent_id))
            vid = _runtime_view_id(s) if s else None
        result = complete(
            status=status, message=message, view_id=vid)
        if result.get("ok"):
            st = result.get("status", "completed")
            msg = result.get("message") or ""
            label = {"completed": "done", "blocked": "blocked", "closed": "closed"}.get(st, st)
            out = {
                "ok": True, "status": st, "message": msg,
                "summary": label + (": %s" % msg if msg else ""),
                "ready": bool(result.get("ready", st != "closed")),
            }
            if result.get("closed"):
                out["closed"] = True
            return out
        return result

    def _update_goal(
        self,
        message: str = "",
        completed: bool = False,
        blocked_reason: str = "",
    ) -> dict:
        session = self._caller_or_active_session()
        if session is None or not hasattr(session, "apply_goal_update"):
            return {
                "ok": False,
                "error": "No session for update_goal. Activate /goal first.",
                "rejected": True,
            }
        if getattr(session, "quick_mode", False):
            return {
                "ok": False,
                "error": "update_goal is not for Quick Agent; use quick_done.",
                "rejected": True,
            }
        is_skeptic = _try_import("features.goals.skeptic.is_goal_skeptic")
        if is_skeptic is not None:
            try:
                if is_skeptic(session):
                    return {
                        "ok": False,
                        "error": "Skeptic cannot update_goal.",
                        "rejected": True,
                    }
            except Exception:
                pass
        return session.apply_goal_update(
            message=message or "",
            completed=bool(completed),
            blocked_reason=blocked_reason or "",
        )

    def _goal_verdict(
        self,
        achieved: bool = False,
        evidence=None,
        gaps=None,
        message: str = "",
    ) -> dict:
        session = self._caller_or_active_session()
        resolve = _try_import("features.goals.skeptic.resolve_verdict_session")
        is_skeptic = _try_import("features.goals.skeptic.is_goal_skeptic")
        if session is not None and resolve is not None:
            try:
                owner = resolve(session, _sessions_map())
            except Exception:
                owner = None
            if owner is None:
                paid = getattr(session, "parent_agent_id", None)
                if paid:
                    owner = _get_session_by_agent_id(str(paid))
            if owner is None and is_skeptic is not None:
                try:
                    if is_skeptic(session):
                        return {
                            "ok": False,
                            "error": "Skeptic parent session not found; cannot record verdict.",
                            "rejected": True,
                        }
                except Exception:
                    pass
            if owner is not None:
                session = owner
        if session is None or not hasattr(session, "apply_goal_verdict"):
            return {
                "ok": False,
                "error": (
                    "No session for goal_verdict (caller not bound). "
                    "Ensure the submarine MCP server was started with --agent-id=… "
                    "and the goal is open on that session."
                ),
                "rejected": True,
            }
        try:
            return session.apply_goal_verdict(
                achieved=bool(achieved),
                evidence=evidence,
                gaps=gaps,
                message=message or "",
            )
        except Exception as e:
            return {"ok": False, "error": "goal_verdict failed: %s" % e, "rejected": True}

    def _set_timer(self, seconds, wake_prompt: str = "") -> dict:
        session, error = self._get_session_for_tool()
        if error:
            return error
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return {"error": "seconds must be an integer"}
        if seconds < MIN_TIMER_S:
            seconds = MIN_TIMER_S
        host_set = _try_import("features.scheduler.set_timer")
        if host_set is not None:
            try:
                timer_id = host_set(seconds, wake_prompt, session)
                return {
                    "status": "registered",
                    "timer_id": timer_id,
                    "seconds": seconds,
                    "host": "features.scheduler",
                }
            except Exception as e:
                return {"error": "features.scheduler.set_timer failed: %s" % e}
        return self._set_timer_local(session, seconds, wake_prompt)

    def _cancel_timer(self, timer_id: str = None) -> dict:
        session, error = self._get_session_for_tool()
        if error and timer_id is None:
            return error
        host_cancel = _try_import("features.scheduler.cancel_timer")
        if host_cancel is not None:
            try:
                cancelled = host_cancel(timer_id if timer_id is not None else session)
                return {"status": "cancelled", "cancelled": cancelled,
                        "host": "features.scheduler"}
            except Exception as e:
                return {"error": "features.scheduler.cancel_timer failed: %s" % e}
        return self._cancel_timer_local(session, timer_id)

    def _artifact_index_cap(self) -> int:
        try:
            from plat.constants import SETTINGS_FILE
            val = sublime.load_settings(SETTINGS_FILE).get("artifacts_index_cap", 500)
            return max(1, int(val or 500))
        except Exception:
            return 500

    def _artifact_auto_open_mode(self, session) -> str:
        try:
            from plat.constants import SETTINGS_FILE
            val = sublime.load_settings(SETTINGS_FILE).get(
                "artifacts_auto_open", "first")
            if val:
                return str(val)
        except Exception:
            pass
        settings = getattr(session, "settings", None) or {}
        try:
            return str(settings.get("artifacts_auto_open") or "first")
        except Exception:
            return "first"

    def _write_artifact(
        self,
        name: str,
        content: str,
        mode: str = "write",
        title: str = None,
        summary: str = None,
        auto_open=None,
        _caller_agent_id: str = None,
    ) -> dict:
        if _caller_agent_id is not None:
            self._caller_agent_id = _caller_agent_id
        caller = str(_caller_agent_id or getattr(self, "_caller_agent_id", None) or "")
        if not caller:
            return {"error": "write_artifact requires a caller agent_id"}
        if not name:
            return {"error": "name is required"}
        session = _get_session_by_agent_id(caller)
        try:
            from core.artifacts import write as artifacts_write
            from core.artifacts import maybe_auto_open_session, publish_to_session
            rec = artifacts_write(
                name=name,
                content="" if content is None else str(content),
                owner=caller,
                mode=mode or "write",
                title=title,
                summary=summary,
                session_id=getattr(session, "session_id", None) if session else None,
                agent_id=caller,
                index_cap=self._artifact_index_cap(),
            )
        except Exception as e:
            return {"error": str(e)}
        if session is not None:
            publish_to_session(session, rec)
            maybe_auto_open_session(
                session,
                rec.get("path") or "",
                override=auto_open,
                mode=self._artifact_auto_open_mode(session),
            )
        return {"path": rec.get("path"), "bytes": rec.get("bytes")}

    def _edit_artifact(
        self,
        path: str,
        op: str,
        old: str = None,
        new: str = None,
        offset: int = None,
        length: int = None,
        heading: str = None,
        note: str = None,
        _caller_agent_id: str = None,
    ) -> dict:
        if _caller_agent_id is not None:
            self._caller_agent_id = _caller_agent_id
        caller = str(_caller_agent_id or getattr(self, "_caller_agent_id", None) or "")
        if not caller:
            return {"error": "edit_artifact requires a caller agent_id"}
        if not path:
            return {"error": "path is required"}
        if not op:
            return {"error": "op is required"}
        session = _get_session_by_agent_id(caller)
        try:
            from core.artifacts import edit as artifacts_edit
            from core.artifacts import publish_to_session
            rec = artifacts_edit(
                path=path,
                op=op,
                agent_id=caller,
                old=old,
                new=new,
                offset=offset,
                length=length,
                heading=heading,
                note=note,
                session_id=getattr(session, "session_id", None) if session else None,
                index_cap=self._artifact_index_cap(),
            )
        except Exception as e:
            return {"error": str(e)}
        if session is not None:
            publish_to_session(session, rec)
        return {
            "path": rec.get("path"),
            "bytes": rec.get("bytes"),
            "op": rec.get("edit_op") or op,
        }

    def _read_artifact(
        self,
        path: str,
        offset: int = 0,
        limit: int = 20000,
        _caller_agent_id: str = None,
    ) -> dict:
        if _caller_agent_id is not None:
            self._caller_agent_id = _caller_agent_id
        if not path:
            return {"error": "path is required"}
        try:
            from core.artifacts import read as artifacts_read
            return artifacts_read(path, offset=offset, limit=limit)
        except Exception as e:
            return {"error": str(e)}

    def _list_artifacts(
        self,
        scope: str = "self",
        _caller_agent_id: str = None,
    ) -> dict:
        if _caller_agent_id is not None:
            self._caller_agent_id = _caller_agent_id
        caller = str(_caller_agent_id or getattr(self, "_caller_agent_id", None) or "")
        try:
            from core.artifacts import list_artifacts as artifacts_list
            entries = artifacts_list(
                scope=scope or "self",
                agent_id=caller or None,
                index_cap=self._artifact_index_cap(),
            )
        except Exception as e:
            return {"error": str(e)}
        return {"artifacts": entries, "count": len(entries)}

    def _set_timer_local(self, session, seconds: int, wake_prompt: str) -> dict:
        """Host-local stand-in until features/scheduler.py exists."""
        view_id = _runtime_view_id(session)
        with _timer_lock:
            # Wake-storm invariant: one pending wake per session.
            for tid, entry in list(_timer_table.items()):
                if entry.get("view_id") == view_id:
                    entry["cancelled"] = True
                    _timer_table.pop(tid, None)
            timer_id = "tmr-%s" % uuid.uuid4().hex[:10]
            _timer_table[timer_id] = {
                "timer_id": timer_id,
                "view_id": view_id,
                "session": session,
                "wake_prompt": wake_prompt,
                "fire_at": time.time() + seconds,
                "cancelled": False,
            }

        def _tick(tid=timer_id):
            entry = _timer_table.get(tid)
            if not entry or entry.get("cancelled"):
                return
            sess = entry.get("session")
            if sess is None:
                return
            if getattr(sess, "working", False):
                sublime.set_timeout(_tick, 500)
                return
            if time.time() < entry["fire_at"]:
                remain_ms = int(max(0, (entry["fire_at"] - time.time()) * 1000))
                sublime.set_timeout(_tick, min(remain_ms, 5000) or 50)
                return
            _timer_table.pop(tid, None)
            try:
                sess.query(entry["wake_prompt"])
            except Exception as e:
                _log("timer %s fire failed: %s" % (tid, e))

        sublime.set_timeout(_tick, min(seconds * 1000, 5000))
        return {
            "status": "registered",
            "timer_id": timer_id,
            "seconds": seconds,
            "host": "socket_server.local",
        }

    def _cancel_timer_local(self, session, timer_id: str = None) -> dict:
        cancelled = []  # type: List[str]
        view_id = _runtime_view_id(session) if session is not None else None
        with _timer_lock:
            for tid, entry in list(_timer_table.items()):
                if timer_id is not None and tid != timer_id:
                    continue
                if timer_id is None and entry.get("view_id") != view_id:
                    continue
                entry["cancelled"] = True
                _timer_table.pop(tid, None)
                cancelled.append(tid)
        if timer_id is not None and not cancelled:
            return {"error": "timer_id %r not found" % timer_id}
        return {"status": "cancelled", "cancelled": cancelled, "host": "socket_server.local"}

    # ─── Session helpers / wait / signal ──────────────────────────────────

    def _get_session_for_tool(self, session_id=None):
        if session_id is not None:
            session = _get_session_by_ref(session_id)
            if session is None:
                return None, {
                    "error": "Session not found: %s" % session_id,
                    "available_sessions": list(_agents_map()),
                }
            return session, None
        aid = getattr(self, "_caller_agent_id", None)
        if aid:
            session = _get_session_by_agent_id(str(aid))
            if session is not None:
                return session, None
        window = self._get_window()
        if not window:
            return None, {"error": "No active window"}
        ea = _window_setting(window, _ACTIVE_AGENT_KEYS)
        if ea:
            session = _get_session_by_agent_id(str(ea))
            if session is not None:
                return session, None
        vid = _window_setting(window, _EXEC_VIEW_KEYS)
        if vid is not None:
            session = _get_session_for_view_id(vid)
            if session is not None:
                return session, None
        vid = _window_setting(window, _ACTIVE_VIEW_KEYS)
        if vid is not None:
            session = _get_session_for_view_id(vid)
            if session is not None:
                return session, None
        iter_fn = _try_import("core.registry.iter_sessions")
        live = list(iter_fn()) if iter_fn is not None else list(_sessions_map().values())
        live = [s for s in live if s is not None and not isinstance(s, (int, str))]
        if len(live) == 1:
            return live[0], None
        return None, {
            "error": "No session context available",
            "hint": "Multiple sessions active. Focus the target session window.",
            "available_sessions": [
                getattr(s, "agent_id", None) for s in live
            ],
        }

    def _session_info(self) -> dict:
        aid = getattr(self, "_caller_agent_id", None)
        if aid is None:
            return {"error": "No caller agent_id (MCP not bound to a session)"}
        session = _get_session_by_agent_id(str(aid))
        if not session:
            return {
                "error": "Session %s not found" % aid,
                "agent_id": aid,
                "available_agent_ids": list(_agents_map()),
            }
        parent_agent_id = getattr(session, "parent_agent_id", None)
        subsession_id = getattr(session, "subsession_id", None)
        agent_id = getattr(session, "agent_id", None)
        budget = self._session_context_budget(session)
        return {
            "agent_id": agent_id,
            "parent_agent_id": parent_agent_id,
            "subsession_id": subsession_id or agent_id,
            "is_subsession": bool(parent_agent_id or subsession_id),
            "name": session.name or "(unnamed)",
            "backend": getattr(session, "backend", None) or "claude",
            "initialized": bool(getattr(session, "initialized", False)),
            "working": bool(getattr(session, "working", False)),
            "sleeping": _is_sleeping(session),
            "turn_phase": getattr(session, "turn_phase", None),
            "session_id": getattr(session, "session_id", None),
            "context_budget": budget,
            "context_summary": budget.get("summary"),
            "context_pct": budget.get("context_pct"),
            "headroom": budget.get("headroom"),
        }

    def _wait_for_subsession(
        self,
        subsession_id: str = None,
        agent_id: str = None,
        wake_prompt: str = "",
    ) -> dict:
        child_id = (agent_id or subsession_id or "").strip()
        if not child_id:
            return {"error": "agent_id or subsession_id required"}
        if not wake_prompt:
            wake_prompt = "✅ Subsession %s completed" % child_id
        parent, error = self._get_session_for_tool()
        if error:
            return error
        register = _try_import("core.registry.register_subsession_wait")
        if register is not None:
            wait_id = register(
                child_id=child_id,
                parent_agent_id=getattr(parent, "agent_id", None),
                wake_prompt=wake_prompt,
            )
        else:
            wait_id = "wait-%s" % uuid.uuid4().hex[:10]
            _host_waits.append({
                "wait_id": wait_id,
                "child_id": str(child_id),
                "parent_agent_id": getattr(parent, "agent_id", None),
                "wake_prompt": wake_prompt,
            })
        return {
            "status": "registered",
            "notification_id": wait_id,
            "wait_id": wait_id,
            "agent_id": child_id,
            "subsession_id": child_id,
            "parent_agent_id": getattr(parent, "agent_id", None),
            "host_local": True,
        }

    def _fire_host_waits(self, session, summary, default_body: str) -> int:
        fire = _try_import("core.registry.fire_subsession_waits")
        if fire is not None:
            try:
                return int(fire(session, summary, default_body=default_body) or 0)
            except Exception as e:
                _log("fire_subsession_waits failed: %s" % e)
                return 0
        child_ids = set()
        for key in ("agent_id", "subsession_id"):
            val = getattr(session, key, None)
            if val:
                child_ids.add(str(val))
        n = 0
        remaining = []
        for wait in list(_host_waits):
            if str(wait.get("child_id")) in child_ids:
                parent = _get_session_by_agent_id(str(wait.get("parent_agent_id") or ""))
                body = wait.get("wake_prompt") or default_body
                if summary and summary not in (body or ""):
                    body = "%s\n\n%s" % (body, summary)
                if parent is not None:
                    try:
                        if parent.working:
                            parent.queue_prompt(body, display="📬 Subsession complete")
                        else:
                            parent.query(body, display_prompt="📬 Subsession complete")
                        n += 1
                    except Exception as e:
                        _log("host wait deliver failed: %s" % e)
                        remaining.append(wait)
                else:
                    remaining.append(wait)
            else:
                remaining.append(wait)
        _host_waits[:] = remaining
        return n

    def _signal_complete(self, session_id=None, result_summary: str = None) -> dict:
        if session_id is None:
            session_id = getattr(self, "_caller_agent_id", None)
        if session_id is None:
            return {
                "error": "session_id missing and no MCP caller agent_id — "
                         "cannot route signal_complete",
            }
        session = _get_session_by_ref(session_id)
        if not session:
            return {
                "error": "Session %r not found" % session_id,
                "available_agent_ids": list(_agents_map()),
            }
        resolve_parent = _try_import("core.registry.resolve_parent_session")
        if resolve_parent is not None:
            try:
                parent_session = resolve_parent(session)
            except Exception:
                parent_session = None
        else:
            parent_session = _get_session_by_agent_id(
                str(getattr(session, "parent_agent_id", "") or ""))
        parent_agent_id = getattr(session, "parent_agent_id", None)
        subsession_id = (
            getattr(session, "subsession_id", None)
            or getattr(session, "agent_id", None)
        )
        if not parent_session:
            return {
                "error": (
                    "Session %r is not a subsession or parent not loaded "
                    "(parent_agent_id=%r)" % (
                        getattr(session, "agent_id", session_id), parent_agent_id)
                ),
            }
        if (not getattr(parent_session, "initialized", False)
                and not _is_sleeping(parent_session)):
            if not getattr(parent_session, "client", None):
                return {"error": "Parent session %s has no client connection" % parent_agent_id}
            return {"error": "Parent session %s not initialized" % parent_agent_id}

        budget = self._session_context_budget(session)
        budget_line = budget.get("summary") or "ctx:unknown"
        child_busy = bool(getattr(session, "working", False))
        pending = {
            "result_summary": result_summary,
            "subsession_id": subsession_id,
            "parent_agent_id": parent_agent_id,
            "gen": int(getattr(session, "_signal_complete_gen", 0) or 0) + 1,
            "started": time.time(),
        }
        session._signal_complete_gen = pending["gen"]
        session._pending_signal_complete = pending
        idle_ticks = {"n": 0}
        max_wait_s = 180.0
        child_poll_ms = 250
        parent_poll_ms = 500
        settle_ticks_needed = 2

        def _build_wake():
            b = self._session_context_budget(session)
            line = b.get("summary") or "ctx:unknown"
            sid = pending.get("subsession_id") or subsession_id
            summary = pending.get("result_summary")
            child_aid = getattr(session, "agent_id", None) or sid
            wake = (
                "✅ Subsession %s completed (agent_id=%s)\n"
                "context_budget: %s" % (child_aid, child_aid, line)
            )
            if b.get("headroom"):
                wake += (
                    "\nstrategy_hint: headroom=%s"
                    " — tight/critical → prefer fork_from_agent_id of a lighter base "
                    "or a fresh worker over piling more into this session; "
                    "comfortable → safe to send_to_session(agent_id=…) for follow-ups"
                    % b["headroom"]
                )
            if summary:
                wake += "\n\n%s" % summary
            return wake, b, line

        def try_deliver():
            cur = getattr(session, "_pending_signal_complete", None)
            if not cur or cur.get("gen") != pending["gen"]:
                return
            elapsed = time.time() - pending["started"]
            if elapsed <= max_wait_s:
                if getattr(session, "working", False):
                    idle_ticks["n"] = 0
                    sublime.set_timeout(try_deliver, child_poll_ms)
                    return
                idle_ticks["n"] += 1
                if idle_ticks["n"] < settle_ticks_needed:
                    sublime.set_timeout(try_deliver, child_poll_ms)
                    return
            if _is_sleeping(parent_session):
                try:
                    parent_session.wake()
                except Exception as e:
                    _log("signal_complete: parent wake failed: %s" % e)
                sublime.set_timeout(try_deliver, parent_poll_ms)
                return
            if not getattr(parent_session, "client", None) or not getattr(
                    parent_session, "initialized", False):
                if elapsed > max_wait_s:
                    _log("signal_complete: parent %s never ready — drop" % parent_agent_id)
                    session._pending_signal_complete = None
                    return
                sublime.set_timeout(try_deliver, parent_poll_ms)
                return
            if getattr(session, "_pending_signal_complete", None) is not cur:
                return
            wake_prompt, b, line = _build_wake()
            summary = pending.get("result_summary")
            n_waits = 0
            try:
                n_waits = self._fire_host_waits(session, summary, wake_prompt)
            except Exception as e:
                _log("signal_complete: host waits failed: %s" % e)
            should_inject = _try_import("core.registry.parent_notify_should_inject")
            if should_inject is not None:
                try:
                    inject = bool(should_inject(n_waits, session))
                except Exception:
                    inject = n_waits == 0
            else:
                inject = n_waits == 0
            if not inject:
                session._pending_signal_complete = None
                return
            shown = "📬 Subsession complete"
            label = _try_import("core.registry.subsession_display")
            if label is not None:
                try:
                    shown = label(session)
                except Exception:
                    pass
            try:
                if parent_session.working:
                    parent_session.queue_prompt(wake_prompt, display=shown)
                else:
                    parent_session.query(wake_prompt, display_prompt=shown)
                mark = _try_import("core.registry.mark_child_parent_notified")
                if mark is not None:
                    mark(session)
                session._pending_signal_complete = None
            except Exception as e:
                _log("signal_complete: inject failed: %s" % e)
                sublime.set_timeout(try_deliver, parent_poll_ms)

        sublime.set_timeout(try_deliver, 0)
        status = "queued" if child_busy else "signaled"
        return {
            "status": status,
            "deferred": child_busy,
            "message": (
                "Parent will be notified after this turn finishes "
                "(signal_complete ran while you were still working)."
                if child_busy else "Parent notified when free."
            ),
            "subsession_id": subsession_id,
            # The parent this reaches — its current id, not a stale link.
            "parent_agent_id": getattr(parent_session, "agent_id", None) or parent_agent_id,
            "result_summary": result_summary,
            "context_budget": budget,
            "context_summary": budget_line,
            "context_pct": budget.get("context_pct"),
            "headroom": budget.get("headroom"),
        }

    # ─── LSP ──────────────────────────────────────────────────────────────

    def _lsp(self, cmd: str = "") -> dict:
        raw = (cmd or "").strip()
        parts = raw.split(None, 1)
        action = parts[0] if parts else ""
        rest = parts[1] if len(parts) > 1 else ""
        if action in ("hover", "definition", "references"):
            tokens = rest.rsplit(None, 2)
            if len(tokens) < 3:
                return {"error": "Usage: %s <file> <line> <col>" % action}
            file_path = tokens[0]
            try:
                line, col = int(tokens[1]), int(tokens[2])
            except ValueError:
                return {"error": "line and col must be integers"}
            fn = {
                "hover": self._lsp_hover,
                "definition": self._lsp_definition,
                "references": self._lsp_references,
            }[action]
            return fn(file_path, line, col)
        if action == "symbols":
            file_path = rest.strip()
            if not file_path:
                return {"error": "Usage: symbols <file>"}
            return self._lsp_symbols(file_path)
        if action == "workspace_symbols":
            query = rest.strip()
            if not query:
                return {"error": "Usage: workspace_symbols <query>"}
            return self._lsp_workspace_symbols(query)
        if action == "diagnostics":
            file_path = rest.strip() or None
            return self._lsp_diagnostics(file_path)
        return {
            "error": (
                "Unknown lsp command: %s. Try: hover, definition, references, "
                "symbols, workspace_symbols, diagnostics" % action
            ),
        }

    def _resolve_file_view(self, file_path):
        window = self._get_window()
        if not window:
            return None, "No window"
        if not os.path.isabs(file_path):
            for folder in window.folders():
                full = os.path.join(folder, file_path)
                if os.path.exists(full):
                    file_path = full
                    break
        file_path = os.path.normpath(file_path)
        for v in window.views():
            if v.file_name() and os.path.normpath(v.file_name()) == file_path:
                return v, None
        if os.path.exists(file_path):
            view = window.open_file(file_path, sublime.TRANSIENT)
            deadline = time.time() + 2.0
            while view.is_loading() and time.time() < deadline:
                time.sleep(0.05)
            return view, None
        return None, "File not found: %s" % file_path

    def _get_lsp_session(self, view, capability=None):
        try:
            from LSP.plugin.core.registry import windows as lsp_windows
        except ImportError:
            return None, "LSP package not installed"
        listener = lsp_windows.listener_for_view(view)
        if not listener:
            return None, "No LSP listener for this view"
        if capability:
            session = listener.session_async(capability)
            if not session:
                return None, "No LSP server with %s capability" % capability
            return session, None
        sessions = listener.sessions_async()
        if sessions:
            return sessions[0], None
        return None, "No LSP server for this view"

    def _lsp_request_sync(self, session, method, params, view=None, timeout=5):
        try:
            from LSP.plugin.core.protocol import Request
        except ImportError:
            return None, "LSP package not installed"
        event = threading.Event()
        result = [None]
        error = [None]

        def on_result(r):
            result[0] = r
            event.set()

        def on_error(e):
            error[0] = str(e) if e else "Unknown error"
            event.set()

        request = Request(method, params, view=view)
        session.send_request(request, on_result, on_error)
        event.wait(timeout)
        if error[0]:
            return None, error[0]
        return result[0], None

    def _lsp_hover(self, file_path, line, col):
        try:
            from LSP.plugin.core.views import text_document_position_params
        except ImportError:
            return {"error": "LSP package not installed"}
        view, err = self._resolve_file_view(file_path)
        if err:
            return {"error": err}
        session, err = self._get_lsp_session(view, "hoverProvider")
        if err:
            return {"error": err}
        point = view.text_point(line, col)
        params = text_document_position_params(view, point)
        result, err = self._lsp_request_sync(session, "textDocument/hover", params, view)
        if err:
            return {"error": err}
        if not result:
            return {"result": None, "message": "No hover info at this position"}
        contents = result.get("contents", "")
        if isinstance(contents, dict):
            text = contents.get("value", "")
        elif isinstance(contents, list):
            parts = []
            for item in contents:
                if isinstance(item, dict):
                    parts.append(item.get("value", ""))
                else:
                    parts.append(str(item))
            text = "\n\n".join(parts)
        else:
            text = str(contents)
        return {"content": text}

    def _lsp_definition(self, file_path, line, col):
        try:
            from LSP.plugin.core.views import text_document_position_params
        except ImportError:
            return {"error": "LSP package not installed"}
        view, err = self._resolve_file_view(file_path)
        if err:
            return {"error": err}
        session, err = self._get_lsp_session(view, "definitionProvider")
        if err:
            return {"error": err}
        point = view.text_point(line, col)
        params = text_document_position_params(view, point)
        result, err = self._lsp_request_sync(
            session, "textDocument/definition", params, view)
        if err:
            return {"error": err}
        if not result:
            return {"locations": [], "message": "No definition found"}
        return {"locations": self._parse_locations(result)}

    def _lsp_references(self, file_path, line, col):
        try:
            from LSP.plugin.core.views import text_document_position_params
        except ImportError:
            return {"error": "LSP package not installed"}
        view, err = self._resolve_file_view(file_path)
        if err:
            return {"error": err}
        session, err = self._get_lsp_session(view, "referencesProvider")
        if err:
            return {"error": err}
        point = view.text_point(line, col)
        params = text_document_position_params(view, point)
        params["context"] = {"includeDeclaration": True}
        result, err = self._lsp_request_sync(
            session, "textDocument/references", params, view)
        if err:
            return {"error": err}
        if not result:
            return {"locations": [], "message": "No references found"}
        return {"locations": self._parse_locations(result), "count": len(result)}

    def _lsp_symbols(self, file_path):
        try:
            from LSP.plugin.core.views import text_document_identifier
        except ImportError:
            return {"error": "LSP package not installed"}
        view, err = self._resolve_file_view(file_path)
        if err:
            return {"error": err}
        session, err = self._get_lsp_session(view, "documentSymbolProvider")
        if err:
            return {"error": err}
        params = {"textDocument": text_document_identifier(view)}
        result, err = self._lsp_request_sync(
            session, "textDocument/documentSymbol", params, view)
        if err:
            return {"error": err}
        if not result:
            return {"symbols": []}
        symbols = self._flatten_document_symbols(result)
        return {"symbols": symbols, "count": len(symbols)}

    def _lsp_workspace_symbols(self, query):
        try:
            from LSP.plugin.core.registry import windows as lsp_windows
        except ImportError:
            return {"error": "LSP package not installed"}
        window = self._get_window()
        if not window:
            return {"error": "No window"}
        session = None
        for w in sublime.windows():
            active = w.active_view()
            if not active:
                continue
            listener = lsp_windows.listener_for_view(active)
            if listener:
                s = listener.session_async("workspaceSymbolProvider")
                if s:
                    session = s
                    break
        if not session:
            return {"error": "No LSP server with workspaceSymbolProvider capability"}
        result, err = self._lsp_request_sync(session, "workspace/symbol", {"query": query})
        if err:
            return {"error": err}
        if not result:
            return {"symbols": []}
        symbols = []
        for sym in result:
            location = sym.get("location", {})
            uri = location.get("uri", "")
            range_info = location.get("range", {}).get("start", {})
            symbols.append({
                "name": sym.get("name", ""),
                "kind": self._symbol_kind_name(sym.get("kind", 0)),
                "file": self._uri_to_path(uri),
                "line": range_info.get("line", 0),
                "col": range_info.get("character", 0),
                "container": sym.get("containerName", ""),
            })
        return {"symbols": symbols, "count": len(symbols)}

    def _lsp_diagnostics(self, file_path=None):
        try:
            from LSP.plugin.core.registry import windows as lsp_windows
        except ImportError:
            return {"error": "LSP package not installed"}
        if file_path:
            view, err = self._resolve_file_view(file_path)
            if err:
                return {"error": err}
        else:
            window = self._get_window()
            view = window.active_view() if window else None
            if not view:
                return {"error": "No active view"}
        listener = lsp_windows.listener_for_view(view)
        if not listener:
            return {"error": "No LSP listener for this view"}
        sessions = listener.sessions_async()
        if not sessions:
            return {"error": "No LSP server for this view"}
        all_diagnostics = []
        for session in sessions:
            try:
                from LSP.plugin.core.views import uri_from_view
                uri = uri_from_view(view)
                diags = session.diagnostics.get_diagnostics_for_uri(uri)
                severity_names = {1: "error", 2: "warning", 3: "info", 4: "hint"}
                for d in diags:
                    range_info = d.get("range", {}).get("start", {})
                    all_diagnostics.append({
                        "severity": severity_names.get(d.get("severity", 4), "unknown"),
                        "message": d.get("message", ""),
                        "line": range_info.get("line", 0),
                        "col": range_info.get("character", 0),
                        "source": d.get("source", ""),
                    })
            except Exception as e:
                all_diagnostics.append({
                    "error": "Failed to get diagnostics from %s: %s" % (
                        getattr(getattr(session, "config", None), "name", "?"), e),
                })
        return {
            "file": view.file_name() or "(untitled)",
            "diagnostics": all_diagnostics,
            "count": len(all_diagnostics),
        }

    def _uri_to_path(self, uri):
        if uri.startswith("file://"):
            from urllib.parse import unquote, urlparse
            parsed = urlparse(uri)
            return unquote(parsed.path)
        return uri

    def _parse_locations(self, result):
        if isinstance(result, dict):
            result = [result]
        locations = []
        for loc in result:
            uri = loc.get("uri", loc.get("targetUri", ""))
            range_info = loc.get(
                "range", loc.get("targetRange", loc.get("targetSelectionRange", {}))
            ).get("start", {})
            locations.append({
                "file": self._uri_to_path(uri),
                "line": range_info.get("line", 0),
                "col": range_info.get("character", 0),
            })
        return locations

    def _flatten_document_symbols(self, symbols, parent_name=""):
        result = []
        for sym in symbols:
            name = sym.get("name", "")
            full_name = "%s.%s" % (parent_name, name) if parent_name else name
            if "range" in sym:
                range_info = sym["range"]["start"]
                result.append({
                    "name": full_name,
                    "kind": self._symbol_kind_name(sym.get("kind", 0)),
                    "line": range_info.get("line", 0),
                    "col": range_info.get("character", 0),
                })
            elif "location" in sym:
                range_info = sym["location"].get("range", {}).get("start", {})
                result.append({
                    "name": full_name,
                    "kind": self._symbol_kind_name(sym.get("kind", 0)),
                    "line": range_info.get("line", 0),
                    "col": range_info.get("character", 0),
                })
            children = sym.get("children", [])
            if children:
                result.extend(self._flatten_document_symbols(children, full_name))
        return result

    _SYMBOL_KIND_NAMES = {
        1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class",
        6: "Method", 7: "Property", 8: "Field", 9: "Constructor", 10: "Enum",
        11: "Interface", 12: "Function", 13: "Variable", 14: "Constant",
        15: "String", 16: "Number", 17: "Boolean", 18: "Array", 19: "Object",
        20: "Key", 21: "Null", 22: "EnumMember", 23: "Struct", 24: "Event",
        25: "Operator", 26: "TypeParameter",
    }

    def _symbol_kind_name(self, kind):
        return self._SYMBOL_KIND_NAMES.get(kind, "Unknown(%s)" % kind)
