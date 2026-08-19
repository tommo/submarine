# Identity redesign: agent_id is the only handle

Status: design. Supersedes the view_id parts of `docs/single-view.md`.

## Problem

Two identities exist today:

- `agent_id` — stable string (`agent-<hex12>`), persisted in
  `.sessions.json`, survives restart. Registry docstring already declares it
  "the public identity" (`core/registry.py:1-16`).
- `view_id` — Sublime runtime int. Changes on restart, and **changes meaning
  on every host-view swap** in single-view mode.

Yet view_id is still wired in as if it were identity:

| Where | What breaks |
|---|---|
| `session.view_id` cached copy (`core/session.py:159`, written at `core/registry.py:256`) | goes stale on swap/reattach; readers at `core/session.py:1517`, `commands/session_cmds.py:479-481`, `core/registry.py:428` act on the stale value |
| init params `"view_id"` / `"parent_view_id"` (`core/session.py:444,457`) | a bridge spawned while bound keeps the id after detach; in single-view mode that id then names *another session's* view |
| bridge passes `--view-id=` to its MCP server (`bridge/acp/session.py:460-461`, `bridge/codex_main.py:370-371`) | MCP caller attribution resolves to whatever session is *currently* bound to that view — wrong session, live race |
| agent-facing guide prints "View ID: …" (`bridge/claude_main.py:288-298`) | agents cache and reuse a handle that can silently point elsewhere |
| MCP schemas expose `view_id` / `_caller_view_id` / `fork_from_view_id` (`mcp/tools.py`) | docs warn "never cache view_id" — better to not hand it out at all |
| `session.parent_view_id` + `relink_parent_view` (`core/registry.py:435-443`) | a whole relink pass exists only to repair a cached runtime handle |
| `submarine_active_view` window setting (`core/placement.py:30-43`) | dead pointer after restart; ambiguous in single-view mode |

## Design

**`agent_id` is the only session identity. `view_id` is demoted to a pure
display binding: the answer to "which view currently shows this session",
computed — never stored.**

### 1. Registry shape (`core/registry.py`)

```python
by_agent:  Dict[str, Session]    # agent_id -> Session; ALL live sessions
                                 # (bound, background, sleeping) — one map
binding:   Dict[int, str]        # view_id -> agent_id; display binding only
waits:     child_id -> [waiters] # parent_agent_id only (parent_view_id dropped)
```

- `sessions` + `background` collapse into `by_agent`. "Background" becomes a
  derived predicate: live and not in `binding.values()`.
- `agent_id` is assigned in `Session.__init__` (today lazily in
  `register_session`, `core/registry.py:237-240`) so a session has identity
  before any view exists.
- API:
  - `register(session)` → `by_agent` only.
  - `bind(agent_id, view_id)` / `unbind(agent_id)` — the swap primitives;
    `bind` evicts whatever agent held that view.
  - `by_agent_id(aid)`, `for_view(view) → binding[view.id()] → by_agent[...]`.
  - `bound_view_id(session)` — reverse lookup, computed. Replaces
    `runtime_view_id` and every `session.view_id` read.
- Delete: `session.view_id`, `session.parent_view_id`,
  `relink_parent_view`, the `agents: aid -> view_id` indirection map.

### 2. Bridge protocol

init params (`core/session.py:441-458`):

```python
"agent_id": self.agent_id,                 # was "view_id"
"parent_agent_id": self.parent_agent_id,   # was "parent_view_id"
```

- ACP/codex bridges pass `--agent-id=` to their MCP server subprocess
  (was `--view-id=`, `bridge/acp/session.py:460-461`,
  `bridge/codex_main.py:370-371`).
- `claude_main` session guide prints `Agent ID` / `Parent Agent ID`
  (was View ID). Agents never see a view_id again.
- Plugin and bridges ship together — hard cut, no cross-version compat.

### 3. MCP layer

- `mcp/server.py`: `--view-id` CLI arg → `--agent-id`;
  `CALLER_VIEW_ID` → `CALLER_AGENT_ID`.
- Socket request JSON: `"view_id"` → `"agent_id"`
  (`mcp/socket_server.py:282-294`); `_get_session_for_view_id` →
  `by_agent_id` direct (no indirection through the view map).
- `mcp/tools.py`: codegens emit `_caller_agent_id`; tool schemas drop
  `view_id` / `fork_from_view_id` params.
- Backward compat for in-flight agents: `resolve_ref`
  (`core/registry.py:308-320`) keeps accepting an int / digit-string and
  resolves it through `binding` (legacy view_id), so a long-running agent
  mid-session with a cached view_id still lands on the right session *while
  the binding is unchanged* — best-effort, documented as deprecated.

### 4. UI layer

- Commands/listeners keep resolving by view — that is a binding query and
  stays one: `session_for_view(view)` → `binding` → `by_agent`. In
  single-view mode this returns the bound session automatically.
- `submarine_active_view` window setting → `submarine_active_agent`
  (`core/placement.py`): write the new key, read old key once as fallback.
  Active-session tracking now survives restarts and host swaps.
- Session list rows keyed by `agent_id` (replacing the
  `view_id`-or-`id(session)` fallback, `ui/session_list.py:273-278`).
- View stamps `submarine_session_id` / `submarine_agent_id` stay — they are
  view metadata for restart restore, which is legitimate view-keyed data.
- Devtools `goal_command(view_id=...)` → `agent_id`
  (`features/devtools/server.py:269`, `features/devtools/cli.py:206`).

### 5. Session

- `Session.__init__`: `self.agent_id = new_agent_id()` (or from saved entry
  on resume); `view_id` param and attribute removed.
- `core/session.py:1517` construct-time `if s.view_id is not None` block
  becomes unconditional register; persist/bind happens in
  `main.create_session` as today.
- `parent_agent_id` already exists and is the only parent pointer.

## What this buys

- **Single-view swap becomes one line**: `bind(b, host_view.id())` after
  `unbind(a)` — no cached handle anywhere to repair (the entire
  `relink_parent_view` class of machinery disappears).
- **MCP attribution race eliminated**: a detached busy session's tool calls
  carry its own agent_id; resolution is exact regardless of what the host
  view currently shows. This was the biggest correctness risk in the
  single-view design.
- **Restart**: active session, parent links, and waiters all keyed by
  stable ids — no dead view pointers.
- **Simpler agent contract**: tool docs stop warning about view_id caching
  because agents never receive one.

## Migration notes

- `.sessions.json` already persists `agent_id` — no data migration.
- Old `submarine_active_view` window settings: one-read fallback, then only
  the new key is written.
- Running bridges from before the upgrade die with their plugin reload —
  no protocol compat window needed.
- `resolve_ref` legacy int path is the only kept compat shim; log a
  deprecation warning when it fires.

## Test plan

- Registry: bind/unbind eviction, `for_view` follows rebind, background
  predicate, no `view_id` attribute on Session (guard test).
- Init params: `agent_id` present, `view_id`/`parent_view_id` absent for
  every backend (param-capture fakes).
- MCP: caller resolution by agent_id; legacy int ref resolves through
  binding with deprecation log; subsession spawn attribution exact after a
  simulated swap.
- Placement: active-agent setting round-trip; old key read once.
- Full suite green; `tests/test_removed_features.py` unaffected.
- Update any test constructing `Session(view_id=...)` or faking
  `session.view_id` — expected churn, mechanical.

## Sequencing

This redesign is **phase 1** of the single-view work and lands in tabs mode
first (behavior-neutral there: still 1 session : 1 view, so binding is a
bijection and nothing user-visible changes). `docs/single-view.md` then
builds on `bind`/`unbind` instead of hand-rolling map moves.
