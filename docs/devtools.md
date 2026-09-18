# Submarine Devtools — Usage

Self-debug surface for **agents and humans** while Sublime Text is running with
Submarine loaded. Prefer the CLI from a terminal or agent shell; do not restart
Sublime for normal plugin iteration.

| Surface | Entry |
|---------|--------|
| **CLI** | `python3 submarine_devtools.py <action> …` (from package root) |
| **Socket** | `$TMPDIR/submarine_mcp.sock` — `{"op":"debug","action":…}` |
| **Command Palette** | `Submarine: Devtools …` |

**Not exposed to in-session agents.** Plugin-dev tools are omitted from the
Sublime MCP `tools/list` so product agents do not burn tokens on them. Use the
CLI/socket from outside ST (or Command Palette) only.

Source: `features/devtools/server.py`, `features/devtools/cli.py`,
`features/reload.py`. Entry script: `submarine_devtools.py`.

---

## Install / path

Package should be a symlink into Sublime Packages:

```bash
python3 submarine_devtools.py install
```

macOS target:

`~/Library/Application Support/Sublime Text/Packages/Submarine` → this repo

If install reports “already installed,” you’re fine. Socket appears only while
ST is running with Submarine loaded (`ping` must return `ok`).

---

## CLI quick reference

Run from the package directory (or pass the full path to `submarine_devtools.py`).

```bash
# Health
python3 submarine_devtools.py ping

# Host state
python3 submarine_devtools.py sessions
python3 submarine_devtools.py snapshot              # active / focused sheet
python3 submarine_devtools.py snapshot 28           # by view_id
python3 submarine_devtools.py composer              # sticky ◎ / pad / viewport
python3 submarine_devtools.py composer 28
python3 submarine_devtools.py log --tail 80
python3 submarine_devtools.py log --grep composer
python3 submarine_devtools.py event "note for ring buffer"

# Package reload (no ST restart)
python3 submarine_devtools.py reload                # soft (default)
python3 submarine_devtools.py reload --wait 3       # wait + re-ping
python3 submarine_devtools.py reload --hard         # ignored_packages cycle

# Goal harness (host /goal)
python3 submarine_devtools.py goal status --view-id 28
python3 submarine_devtools.py goal 'your objective' --view-id 28
python3 submarine_devtools.py goal pause --view-id 28
python3 submarine_devtools.py goal resume --view-id 28
python3 submarine_devtools.py goal clear --view-id 28

# Arbitrary main-thread Python in ST
python3 submarine_devtools.py eval 'return list(sublime._submarine_sessions)'
```

**Flags (global):**

| Flag | Meaning |
|------|---------|
| `--view-id N` | Target session view id |
| `--tail N` | Log ring depth (default 80) |
| `--grep SUB` | Filter log messages |
| `--hard` | Reload mode = hard |
| `--mode soft\|hard` | Explicit reload mode |
| `--wait SEC` | After `reload`, sleep and re-ping (default 2) |

Options may appear **before** the action (`goal --view-id 28 'obj'`) or after
(`goal 'obj' --view-id 28`).

---

## Reload (correct, no ST restart)

Use this after editing plugin Python instead of quitting Sublime.

| Mode | What happens |
|------|----------------|
| **soft** (default) | `plugin_unloaded` → unload all `Submarine.*` → purge `sys.modules` → `sublime_plugin.reload_plugin` on every root `.py` with import interception so **submodules** re-exec |
| **hard** | Add package to `Preferences.ignored_packages`, then remove it (~1s later) — full ST package unload/load |

### After reload

1. Socket briefly dies; CLI `--wait` re-pings until `ok`.
2. `plugin_loaded` drops in-memory Session objects (avoids stale class identity).
   Sheets remain; sessions reattach as **sleeping**.
3. Restore registry if you need immediate host access:

```bash
python3 submarine_devtools.py eval '
from ui.listeners import settle_startup_output_views
settle_startup_output_views()
__result__ = list(sublime._submarine_sessions.keys())
'
```

(`settle_startup_claude_views` is kept as an alias.)

4. Wake a sheet before agent turns / goals that need a live bridge.
   `sublime._submarine_sessions` is keyed by `agent_id`, not view id — resolve
   a view id through the registry:

```bash
python3 submarine_devtools.py eval '
from core.registry import default_registry
s = default_registry.for_view_id(28)   # or default_registry.by_agent_id("agent-…")
s.wake()
__result__ = {"wake": True, "session_id": s.session_id, "agent_id": s.agent_id}
'
# poll until initialized
python3 submarine_devtools.py eval '
from core.registry import default_registry
s = default_registry.for_view_id(28)
__result__ = {"initialized": s.initialized, "working": s.working, "sleeping": s.is_sleeping}
'
```

Palette: **Submarine: Devtools Reload Package** / **… (hard)**.

---

## Goal harness via devtools

Same path as typing `/goal …` in the sheet (host-owned planning → execute → verify).

```bash
# After wake + initialized
python3 submarine_devtools.py goal 'Write /tmp/probe.txt with one line ok' --view-id 28
python3 submarine_devtools.py goal status --view-id 28
python3 submarine_devtools.py snapshot --view-id 28   # includes goal phase / plan_path
```

**Lifecycle you should see:**

1. Host materializes plan → `{project}/.claude/goals/{goal_id}/plan.md`
2. `phase=executing`, `status=active`, `working=true`
3. Agent implements; may call `update_goal(completed=true)`
4. Host may enter `phase=verifying`
5. `status=complete`, `phase=idle`

Controls: `status` | `pause` | `resume` | `clear` (same as slash commands).

Goal is **not** available on Quick Agent sessions.

---

## What each dump contains

### `ping`

`ok`, socket path, session count, ring event count, `started`, log path.

### `sessions`

All **host** sessions (not only MCP-spawned subsessions): `view_id`, name,
backend, working/initialized/sleeping, goal strip, view size.

### `snapshot [view_id]`

Windows summary + all sessions + **focus** block: deep session attrs, goal dump,
composer geometry, view settings.

### `composer [view_id]`

Sticky ◎ state: input mode, marker/EOF layout, viewport/layout extent,
trailing empty lines, regions, `scroll_past_end`, sleeping flags.

### `log`

- In-process **ring** (survives soft reload; stored on `sublime._submarine_devtools`)
- File: `$TMPDIR/submarine_devtools.log`
- Bridge tail: `$TMPDIR/submarine_bridge.log`

### `eval`

Runs on the ST main thread with `sublime`, `sublime_plugin`, and named helpers.

**Return values:** prefer assigning `__result__` for multi-statement scripts. A
leading single-line `return …` is rewritten by the MCP host; semicolon-chained
`return` is **not**.

```bash
# Good
python3 submarine_devtools.py eval '__result__ = {"n": len(sublime._submarine_sessions)}'

# Also good (single expression form handled by host)
python3 submarine_devtools.py eval 'return list(sublime._submarine_sessions.keys())'
```

---

## Socket protocol

Newline-terminated JSON on `$TMPDIR/submarine_mcp.sock` (usually
`/tmp/submarine_mcp.sock`).

```json
{"op": "debug", "action": "ping"}
{"op": "debug", "action": "snapshot", "view_id": 28}
{"op": "debug", "action": "composer", "view_id": 28}
{"op": "debug", "action": "log", "tail": 80, "grep": "goal"}
{"op": "debug", "action": "reload", "mode": "soft"}
{"op": "debug", "action": "goal", "args": "status", "view_id": 28}
{"op": "debug", "action": "event", "message": "repro note"}
{"code": "return list(sublime._submarine_sessions.keys())"}
```

Response envelope: `{"result": …, "error": null|string}`.

CLI prefers `op=debug`; if the live plugin predates that handler, it
**hot-imports** `features.devtools.server` via `eval` + `importlib.reload`.

---

## Not in agent MCP

`debug_*` handlers still exist on the host for the CLI/socket path, but they
are **not** advertised in `mcp/tools.py` `tools/list`. In-session agents only
see product tools (`list_sessions`, `update_goal`, `set_timer`, …).

---

## Command Palette

| Command | Role |
|---------|------|
| Submarine: Devtools Snapshot | Scratch JSON + clipboard |
| Submarine: Devtools Sessions | Scratch JSON + clipboard |
| Submarine: Devtools Composer | Scratch JSON + clipboard |
| Submarine: Devtools Log | Scratch JSON |
| Submarine: Devtools Reload Package | Soft reload |
| Submarine: Devtools Reload Package (hard) | Hard reload |

---

## Agent playbooks

### Iterate on plugin code

```bash
# edit files…
python3 submarine_devtools.py reload --wait 3
python3 submarine_devtools.py eval '
from ui.listeners import settle_startup_output_views
settle_startup_output_views()
__result__ = len(sublime._submarine_sessions)
'
python3 submarine_devtools.py ping
```

### Debug sticky ◎ / empty rows / focus

```bash
python3 submarine_devtools.py sessions
python3 submarine_devtools.py composer 28
python3 submarine_devtools.py snapshot 28
python3 submarine_devtools.py log --grep composer
```

Check: `input_mode`, tail marker, trailing empty lines, layout vs viewport,
`scroll_past_end`, `submarine_sleeping`.

### Debug goal stuck “Active”

```bash
python3 submarine_devtools.py goal status --view-id 28
python3 submarine_devtools.py snapshot --view-id 28
# look at focus.goal.phase / status / plan_path
python3 submarine_devtools.py log --grep goal
```

### Dogfood host goal product

```bash
# wake target session, wait for initialized, then:
python3 submarine_devtools.py goal 'small objective with clear evidence' --view-id 28
# poll
python3 submarine_devtools.py goal status --view-id 28
# plan lives under {project}/.claude/goals/{id}/plan.md
```

### Bridge / spawn hang

```bash
python3 submarine_devtools.py log --tail 100
# inspect bridge_tail + ring
python3 submarine_devtools.py sessions
```

---

## When to use what

| Symptom | Action |
|---------|--------|
| Code changed, classes still old | `reload` (soft); hard if soft fails |
| Empty rows under ◎ / scroll range | `composer` + `snapshot` |
| Focus thrash / no composer after turn | `sessions` → composer flags, `input_mode` |
| Goal stuck active after done | `goal status` / `snapshot` → phase |
| Bridge / spawn hang | `log` + bridge tail |
| Need arbitrary host state | `eval` with `__result__ = …` |
| Want agent-visible note in ring | `event "…"` |

---

## Files / sockets

| Path | Role |
|------|------|
| `features/devtools/server.py` | Host ring, snapshots, `dispatch`, goal wrapper |
| `features/devtools/cli.py` | Outside-ST CLI |
| `submarine_devtools.py` | Thin entry that calls the CLI |
| `features/reload.py` | Soft + hard reload implementation |
| `$TMPDIR/submarine_mcp.sock` | MCP + debug socket |
| `$TMPDIR/submarine_devtools.log` | Durable event log |
| `$TMPDIR/submarine_bridge.log` | Bridge process log |

---

## Gotchas

1. **Sublime must be running** — no socket ⇒ `ping` fails.
2. **Soft reload clears live Session objects** — settle + wake as needed.
3. **`list_sessions` MCP tool ≠ CLI `sessions`** — MCP lists spawned
   subsessions; CLI `sessions` dumps all host sheets.
4. **Goal needs a live bridge** — wake and wait for `initialized` before
   `goal 'objective'`.
5. **Hot-reload of only `devtools` via CLI** is fine for dump helpers;
   **product code** (session, listeners, goal) needs full `reload`.
6. **Hard reload** briefly disables the whole package (menus/commands go away
   until re-enable).
7. **Sleep banner after reload** — process-global phantom registry +
   `view.erase_phantoms(key)` on clear/show/wake/reload. If you still see
   stacked banners, run `python3 submarine_devtools.py reload` once (or focus
   the sheet → Enter to wake).
