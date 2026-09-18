# Submarine — Architecture

Rewrite of `sublime-claude` (ClaudeCode) into a clean-layered Sublime Text 4 plugin.

## Naming

- Package name: **Submarine** (install dir `Packages/Submarine`)
- Command prefix: `submarine_…` (palette label "Submarine: …")
- Settings: `Submarine.sublime-settings`, project keys `submarine_additional_dirs`,
  `submarine_retain`, `submarine_env`
- Output syntax/scheme: `SubmarineOutput.sublime-syntax` etc.
- Bridge protocol stays JSON-RPC over stdio (contract preserved from research).

## Layers (dependencies point downward only)

```
┌────────────────────────────────────────────────────────────┐
│ L6 commands/        ST command classes (thin, delegate)     │
├────────────────────────────────────────────────────────────┤
│ L5 features/        goals, loop, quick_agent, alarms,       │
│                     context, resume, quota, devtools        │
├────────────────────────────────────────────────────────────┤
│ L4 ui/              output view rendering, composer,        │
│                     formatters, session list, listeners     │
├────────────────────────────────────────────────────────────┤
│ L3 core/            Session, TurnState, Registry,           │
│                     persistence, providers                  │
├────────────────────────────────────────────────────────────┤
│ L2 backend/         backend specs + bridge process mgmt,    │
│                     JSON-RPC client (plugin side)           │
├────────────────────────────────────────────────────────────┤
│ L1 platform/        constants, log, settings, errors, util  │
│                     (NO sublime imports — unit-testable)    │
└────────────────────────────────────────────────────────────┘
   bridge/ (separate process, Python 3.10+): claude, codex,
   acp (kimi, grok), pi — emits identical notifications
   mcp/ socket server (L5 integration) + stdio MCP server
```

Sublime API (`sublime`, `sublime_plugin`) is imported ONLY in L4+ (ui, features,
commands, listeners). L1–L3 are sublime-free so they run under plain python3 for tests.
Bridge scripts are standalone (no package imports beyond stdlib + SDK).

## Package layout (target)

```
Submarine/
├── submarine.py              # entry: plugin_loaded/unloaded, re-export commands
├── package_reloader.py       # dev reload (kept)
├── platform/                 # L1
│   ├── constants.py  log.py  settings.py  errors.py  util.py
├── backend/                  # L2
│   ├── rpc.py                # JSON-RPC stdio client (async reader thread)
│   ├── process.py            # bridge spawn/monitor/restart
│   └── specs.py              # backend registry: claude/codex/acp(kimi,grok)/pi
│                             #   + custom anthropic providers, availability checks
├── core/                     # L3
│   ├── events.py             # typed notification model (dataclasses, py3.8-safe)
│   ├── session.py            # Session: lifecycle/query/permission/queue/interrupt
│   ├── turn.py               # turn state machine (idle/working/waiting/streaming)
│   ├── registry.py           # view↔session map, .sessions.json persistence
│   ├── providers.py          # custom anthropic provider CRUD/model mapping
│   └── alarms.py             # alarm store + firing logic (bridge-driven)
├── ui/                       # L4 (sublime imports allowed)
│   ├── output_view.py        # region-based renderer (split from god-class)
│   ├── models.py             # ToolCall/TodoItem/PermissionRequest/…
│   ├── formatters.py         # per-tool formatters (+sublime-specific)
│   ├── composer.py           # inline input geometry, @ menu, submit handling
│   ├── session_list.py       # switch/search panels
│   └── listeners.py          # ST event listeners
├── features/                 # L5
│   ├── goals/  tracker.py plan.py prompts.py skeptic.py evidence.py
│   ├── loop.py               # /loop idle re-prompt
│   ├── quick_agent.py
│   ├── context.py            # pending-context manager
│   ├── resume.py             # resume preview + history browse
│   ├── quota.py              # subscription usage in panels
│   └── devtools/  server.py  cli.py
├── mcp/
│   ├── socket_server.py      # unix-socket server in Sublime
│   ├── tools.py              # tool catalog + router
│   └── server.py             # stdio MCP server process
├── bridge/                   # Python 3.10+ subprocesses
│   ├── rpc_helpers.py  base.py  acp_base.py
│   ├── claude_main.py  codex_main.py  kimi_main.py  grok_main.py  pi_main.py
├── commands/                 # L6, by domain
│   ├── session_cmds.py  provider_cmds.py  context_cmds.py  ui_cmds.py  text_cmds.py
└── resources: Submarine.sublime-settings, *.sublime-menu/keymap/mousemap,
    SubmarineOutput.sublime-syntax/-settings, schemes, Main/Context menus
```

## Kept backends

claude (Agent SDK + custom anthropic-compatible providers), codex (app-server),
acp_base → kimi (`kimi acp`), grok (`grok agent stdio`), pi. Five built-ins only.

## Removed (with their references)

checkpoint (profiles stay), order_table, channel, notalone, persona
(`spawn_session` no longer takes a persona id), and claude-term-mode
(ACP-protocol terminals in acp_base stay).

## Improvements to carry into rewrite (from NOTES/TODO)

- Streaming text rendering (currently batch per response)
- Click to expand/collapse tool sections
- Session search refinement; cost dashboard (stretch)
- MCP saved-tool parameters
- Split Session god-object along seams identified in session-core report
- Keep all documented invariants: resume_id on reconnect, tracked regions for UI,
  permission queueing, ToolResultBlock-in-UserMessage, interrupt drain semantics

## Core layer — decided from session-core report

The old 6800-line `Session` god-object splits into (inside `core/`):

- `records.py` — `SessionRecord` + `.sessions.json` store (cap 200, MRU-front),
  bookmarks, view-stamp keys. Pure data, no sublime.
- `session.py` — thin `Session` façade: identity (agent_id vs view_id vs
  session_id), transport handle, turn controller, chrome handle.
- `transport.py` — bridge spawn + `initialize` params + model/effort resolve
  (`resolve_init_model` chain: profile → session → saved → view stamp → default;
  new sessions ignore stale view stamps).
- `turn.py` — `TurnController` owning `TurnState` as the SINGLE busy bit
  (kills the old 4-clock split-brain: `working`/`_compacting`/`_interrupting`/
  `output.current.working`). Owns query/queue/inject/send-now/interrupt/compact,
  `_query_gen` stale-done guard, interrupt settle timers (450/900/1600ms).
- `events.py` — `BridgeEventRouter`: notification dispatch table → UI calls +
  side effects. Kimi compact-closer rule (stay busy till "Compaction completed"
  or 180s), leftover-stream policy (`inbound_action`: drop/paint/paint_bg).
- `background.py` — bg task registry, poll epochs, notify dedupe,
  `notify_action` (claude=query, kimi/grok=surface).
- `registry.py` — view_id↔Session, agent_id↔view_id, background map,
  parent/child relink, subsession waiters (XOR delivery), sender stamps.
- `placement.py` — pane-group memory (old session_split; renamed — it is NOT
  conversation forking).
- `rewind.py` — undo/rewind service (Claude jsonl + Grok async).

Feature extractions (out of Session): goal harness, workflow panel, plan mode,
retain/compact, context (delete shims, call ContextManager directly), chrome
(composer policy + phantom registry — phantoms must outlive Session via a
module-level registry), status/unread.

Session lifecycle states (replacing dead `session_state.py`, which is deleted):
process: uninitialized/initializing/live/sleeping/backgrounded/closed/error-halted;
turn: idle/live/compacting/interrupting. Sleep is DERIVED:
`session_id and client is None and not initialized` — never a stored flag.

## Features layer — decided from features-mcp report

- **Goals** (`features/goals/`): keep tracker/plan/prompts/skeptic(task mode only)/
  evidence. Improvements: persist `GoalTracker.to_json` on session save + wire
  `from_json` on resume (active→user_paused remap preserved); delete MODE_SESSION
  legacy sheet path, `materialize_plan`, `parse_verifier_verdict` from runtime;
  make "visual requires capture" a plan-declared kind instead of keyword regexes.
  Pre-existing test failures (skeptic ×3, tracker record_and_take) become fix targets.
- **Quick Agent**: keep as-is conceptually (≤3 slots, one-shot contract, narrow tool
  allowlist — do NOT widen), default backend deepseek/haiku/low/bypassPermissions.
- **/loop + alarms**: the old code has THREE overlapping wake systems (bridge
  ScheduleWakeup shadows, an external daemon timer, documented-but-nonexistent set_alarm).
  Submarine unifies: one **host scheduler** (`features/scheduler.py`) owning interval
  timers + child-complete waits; bridges keep their engine-tool shadows
  (ScheduleWakeup clamp [60,3600], one-pending-wake invariant, Grok ACP backup timer)
  but report `loop_scheduled`/`notification_wake` to the same host path. MCP exposes
  `wait_for_subsession` + `signal_complete` plus host-local `set_timer`/`cancel_timer`.
- **MCP**: two-process hop stays (per-session stdio `mcp/server.py` → unix socket →
  in-plugin eval on main thread). Renamed socket `$TMPDIR/submarine_mcp.sock`.
  Tool catalog: KEEP get_window_summary/find_file/get_symbols/goto_symbol/read_view/
  read_image/list_backends/spawn_session (profile + live fork only)/send_to_session/
  list_sessions/read_session_output/lsp/sublime_eval/sublime_tool/list_tools/
  quick_done/update_goal/goal_verdict/session_info/signal_complete/
  wait_for_subsession/list_profiles (profiles only)/list_profile_docs/read_profile_doc
  plus host-local set_timer/cancel_timer. Dropped: emulator shell tools, daemon
  notify/subscribe, group-chat, garage_search, order board, identity listing.
  Keep `normalize_mcp_tool_name` (Grok sends `sublime__goal_verdict`).
- **Devtools + package_reloader**: keep; rename log to `$TMPDIR/submarine_devtools.log`;
  reload must re-start the MCP socket server.
- **Support layer**: constants split (identity/UI/MCP), rename PLUGIN_NAME="Submarine";
  keep ONE bridge file logger (`$TMPDIR/submarine_bridge.log`); error_handler reduces
  to `safe_json_load`/`safe_json_dump` in platform; hooks.py deleted (dead),
  pre_compact folded into session retain gather; keep Claude CLI settings cascade
  (`~/.claude.json` < `.claude/settings.json` < local) + `permissions.allow` →
  autoAllowedMcpTools mapping.
- **Quota/resume_preview/clipboard helpers**: keep as isolated modules.

## Backend layer — decided from bridge-backends report

- **Plugin JSON-RPC contract is frozen** exactly as documented in
  `_research/reports/bridge-backends.md` §"JSON-RPC contract (exhaustive)"
  (requests A + notifications B). Submarine reimplements it verbatim; bridges
  must emit identical shapes so UI/permissions/loop stay backend-agnostic.
- **Plugin side**: `backend/rpc.py` keeps NDJSON + reader thread + main-thread
  marshal + `send_wait` deadlock note. `backend/specs.py` = BackendSpec registry
  (claude/codex/kimi/grok/pi + custom_providers; no extra built-in backends;
  keep kimi→kimi_api collision remap, sibling-auth clear, alias-not-ANTHROPIC_MODEL,
  grok effort/vision gates, kimi bin resolution).
- **Bridge side is PORT-AND-CLEAN, not rewritten** (production-proven): copy
  `rpc_helpers.py`, `base.py`, `acp_base.py`, `main.py`→`claude_main.py`,
  `codex_main.py`, `kimi_main.py`, `kimi_bg.py`, `grok_main.py`, `pi_main.py`;
  delete the unused extra-backend and daemon-client scripts; strip leftover
  dispatch stubs from claude_main; rename log paths/env (`SUBLIME_CLAUDE_*`→`SUBMARINE_*`) and
  shared bridge-support imports to the new `bridge/support/` package
  (settings cascade, logger, constants — must stay sublime-free).
- **50 production invariants** in the report §9 are acceptance criteria
  (cancel-as-notification, agent_busy retry, q0 AskUser mapping, plan outcome
  shape, foreign-session filter, load-replay no-paint, bundled remap, busy-state
  leftovers, ACP terminal rules, concurrent dispatch, stdout writer thread).

## UI layer — decided from output-ui report

- **Port nearly unchanged**: `composer_geometry` (policy kernel, tested),
  `output_models`, `output_pending`, `context_manager`, formatter registries,
  `command_parser` (`/loop` stays forwarded — engine owns it), the syntax/theme
  machinery (renamed `SubmarineOutput.*`, scope `text.submarine`).
- **OutputView split**: `sheet.py` (lifecycle/title/theme), `composer.py`
  (sticky ◎, caret ownership via composer_geometry, modal hiding),
  `renderer.py` (turn render), `tools.py` (tool rows/tasks strip),
  `modals.py` (permission/plan/question UI + key handling), `phantoms.py`
  (media/context/pad). Contracts preserved: glyph alphabet (◎▶☐⚙✔✘◆▸○),
  Y/N/S/A/V + 1–4/O/Enter modal keys, title icons, tasks fold, retry hint.
- **Render improvement**: `Conversation` events are source of truth; renderer
  does INCREMENTAL APPEND when the turn only grew text; full region rewrite
  only on structural change (tool upsert/meta/tasks fold). Caret policy stays
  governed by `composer_geometry.stream_tick_actions`. meta() renders
  synchronously (race fix preserved). History cap 20 preserved.
- **Circular dep broken**: Session owns chrome; OutputView exposes a narrow
  `OutputPort` protocol (prompt/tool/text/meta/modals); OutputView never
  imports the entry point — Session pushes session-state in.
- **Commands**: full inventory in report §"Command inventory". Naming map
  `claude_*`→`submarine_*`. Single entry `submarine.py` re-exports ALL command
  classes (fix the stale/missing exports: ClaudeGoal* etc.). Dedup
  insert/replace TextCommands (one copy). Palette generated from one list.
- **Resources**: split keymap (output/list/global), drop Terminus dump,
  drop `ClaudeOutput.tmLanguage` + unused color-scheme; themes renamed
  (`SubmarineOutput*.hidden-tmTheme`; codex green kept, unused scheme dropped,
  quick kept).

## Removal — final decisions (from removal-map report)

- Embedded TTY emulator (`terminal/`): **re-ported** as the vendored
  Terminus-derived package (`SubmarineTerminal*` commands via the ROOT shim
  `submarine_terminal_plugin.py`). The hidden-PTY engine (`cc_pty_session`)
  + launch/transcript helpers + pty MCP tools stay **deleted**
  (subscription-billing CLI POC is an explicit product decision).
  ACP-protocol terminals in acp_base KEEP (not the emulator).
- `chatroom` MCP tool: deleted with notalone (broken wiring anyway).
- `set_timer`: reimplemented host-local in the new `features/scheduler.py`
  (small; replaces the old notalone daemon timers).
  `wait_for_subsession`/`signal_complete` host-local plane kept as the ONLY
  notification plane.
- `_is_synthetic_turn` keeps recognizing `channel`/`timer`/`inject` tags so
  Undo stays clean on old transcripts.
- persona → users use profiles instead. checkpoint → live fork / preload_docs.
- Full deletion checklist: `_research/reports/removal-map.md` §12 — use as
  acceptance list; add a "these symbols must not exist" guard test.
