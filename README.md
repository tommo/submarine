# Submarine

A Sublime Text plugin for [Claude Code](https://claude.ai/claude-code) (Agent SDK
plus custom Anthropic-compatible providers), [Codex CLI](https://github.com/openai/codex),
[Kimi Code](https://moonshotai.github.io/kimi-code/) and [Grok Build](https://x.ai/)
via **Agent Client Protocol (ACP)**, and [Pi](https://github.com/badlogic/pi-mono).

There is no Copilot or DSR backend.

## Requirements

- Sublime Text 4
- Python 3.10+ (for the bridge process)
- One or more CLI backends (authenticated):
  - Claude Code CLI + `claude-agent-sdk`
  - Codex CLI
  - Grok Build CLI (`grok agent stdio` ACP)
  - **Kimi Code CLI** (`kimi acp` ACP) — native path; see below
  - Pi CLI
  - Custom Anthropic-compatible provider — base URL + API key (uses the Claude bridge)

```bash
# Claude Code
npm install -g @anthropic-ai/claude-code
claude  # Follow prompts to authenticate
pip install claude-agent-sdk

# Codex CLI (optional)
npm install -g @openai/codex
codex  # Follow prompts to authenticate

# Grok Build (optional, native ACP)
# Install Grok Build so `grok` is on PATH (or set GROK_BIN).
grok login

# Kimi Code (optional, native ACP)
# Install Kimi Code so `kimi` is on PATH (or set KIMI_BIN).
# Default binary often at ~/.kimi-code/bin/kimi
kimi login
kimi doctor

# Pi (optional)
npm install -g @earendil-works/pi-coding-agent
pi   # Follow /login

# Custom Anthropic-compatible provider (optional) — no extra install,
# configure under settings.custom_providers (see Custom Providers below).
```

**Kimi Code ACP vs custom Moonshot/Kimi provider:** Command Palette
**Kimi Code: New Session** runs the **Agent Client Protocol** agent
(`kimi acp` via `bridge/kimi_main.py`). That is separate from adding a
Moonshot/Kimi entry under `settings.custom_providers` (Anthropic-compatible
`base_url` + API key on the Claude `claude_main.py` bridge). The built-in
`kimi` ACP backend always wins the name; a colliding custom key is remapped
to `kimi_api`. Use ACP for first-class Kimi Code; use `custom_providers` only
when you want the Claude Code SDK talking to an Anthropic-compat endpoint.

**Note:** Authenticate your chosen CLI before using this plugin. If you see
connection errors, run the CLI in a terminal to login.

## Installation

1. Clone or symlink this folder to your Sublime Text `Packages` directory
   as **`Submarine`**:

   ```bash
   # macOS
   ln -s /path/to/submarine ~/Library/Application\ Support/Sublime\ Text/Packages/Submarine

   # Linux
   ln -s /path/to/submarine ~/.config/sublime-text/Packages/Submarine

   # Windows
   mklink /D "%APPDATA%\Sublime Text\Packages\Submarine" C:\path\to\submarine
   ```

   Or: `python3 submarine_devtools.py install`

2. Configure your Python path if needed (see Settings below)

### Plugin Devtools (agent self-debug)

While Sublime is running, agents can inspect live sessions / sticky ◎ / goals,
reload the package without restarting ST, and drive host `/goal` from the CLI:

```bash
python3 submarine_devtools.py install   # ensure Packages/Submarine symlink
python3 submarine_devtools.py ping
python3 submarine_devtools.py sessions
python3 submarine_devtools.py snapshot
python3 submarine_devtools.py composer [view_id]
python3 submarine_devtools.py log --tail 80
python3 submarine_devtools.py reload --wait 3
python3 submarine_devtools.py goal status --view-id N
```

Command Palette: **Submarine: Devtools Snapshot / Sessions / Composer / Log / Reload**.
**Usage doc:** [docs/devtools.md](docs/devtools.md).

### Session control from outside Sublime

The sessions a running Sublime is holding, for a terminal, a script, an agent
that is not itself a session, or a phone. Both clients talk to the plugin
socket (`op:"sessions"`) — no second stack:

```bash
submarine_sessions list | view | chat | interrupt | pending | answer
submarine_sessions create | rename | close | backends
submarine_web.py                                     # browser console, 0.0.0.0:8787
```

The web console is the same actions over HTTP: the Sublime session list
(current above history, by window, ↳ children, `?` waiting first), the sheet
drawn as Sublime draws it (CodeMirror, folds per turn, search), question /
permission / plan cards you can answer, edits with diffs and a code view,
new / rename / close. A prompt sent from outside shows as `◎ 📨 …` on the
sheet. Phone layout included. CodeMirror loads from esm.sh; offline the
console degrades to a `<pre>` and a textarea.

**Usage docs:** [docs/session-control.md](docs/session-control.md) (CLI) and
[docs/web-ui.md](docs/web-ui.md) (browser console, `--host` / `--port` /
`--token`, remote access).

## Usage

### Commands

All commands available via Command Palette (`Cmd+Shift+P`): type "Submarine"

| Command | Keybinding | Description |
|---------|------------|-------------|
| Query | - | Focus / start a session and query |
| Query Selection | - | Query about selected code |
| Query File | - | Query about current file |
| Copy Session ID | - | Copy the current session id |
| Codex: New Session | - | Start a fresh Codex session |
| Pi: New Session | - | Start a fresh Pi session |
| Grok: New Session | - | Start a fresh Grok ACP session |
| Kimi Code: New Session | - | Start a fresh Kimi Code ACP session |
| DeepSeek: New Session | - | Start on the seeded DeepSeek custom provider |
| Add Current File | - | Add file to context |
| Add Selection | - | Add selection to context |
| Add Open Files | - | Add all open files to context |
| Add Current Folder | - | Add folder path to context |
| Clear Context | - | Clear pending context |
| Goal Status | - | Print host goal status |
| Goal Pause | - | Pause the host goal |
| Goal Resume | - | Resume a paused goal |
| Goal Clear | - | Clear the host goal |
| Devtools Snapshot | - | Dump host + focus JSON |
| Devtools Sessions | - | Dump all host sessions |
| Devtools Composer | - | Dump sticky ◎ geometry |
| Devtools Log | - | Dump ring / log tail |
| Devtools Reload Package | - | Soft-reload the package |
| Devtools Reload Package (hard) | - | Full ignored_packages cycle |
| New Session | - | Start a fresh session (default backend) |
| Switch Session… | `Cmd+\` | Quick panel: the active session's actions, new session, backends (`with Grok…` / `with Kimi Code…` show live subscription usage from Service Manager when available). `Cmd+Alt+\` is an alias |
| Session List | - | Scratch list of live + saved sessions |
| Reveal Session from List (keep focus) | - | Show the row's session, focus stays on the list |
| Toggle Session List / View | `Cmd+Shift+\` | Sessions list ↔ session view |
| Next / Previous Session | `Ctrl+]` / `Ctrl+[` | Cycle this window's sessions (sleeping too), from the sheet or the list; the list follows |
| Open Session JSONL | - | Open this session’s transcript |
| Reveal Session JSONL in Finder | - | Reveal the transcript file |
| Hide Session (keep running) | - | Hide the output view; bridge stays up |
| Quick Agent | - | Toggle one-shot Quick Agent sheet |
| Quick Agent New Slot | - | Add a Quick slot (max 3) |
| Quick Agent Config… | - | Configure Quick backend / model / effort |
| Quick Agent Stop | - | Stop all Quick slots |
| Fork Session | - | Fork current session (branch conversation) |
| Fork Session... | - | Fork from a live or saved session |
| Restart Session | - | Restart current session with a profile |
| Restart New | - | Fresh session in this view, same provider/model |
| Change Provider for Current Session… | `Cmd+Ctrl+Alt+P` | Swap provider mid-session (Claude-bridge family) |
| Toggle Tasks Fold | `Cmd+Alt+T` | Expand/collapse the Tasks list in-view |
| Resume Session... | - | Resume a previous session |
| Rename Session... | - | Name the current session |
| Stop Session | - | Disconnect and stop |
| Toggle Output | - | Show/hide output view |
| Clear Output | `Cmd+Ctrl+Alt+C` | Clear output view (`Cmd+Shift+K` in the sheet) |
| Clear Output (Keep Last Round) | `Cmd+K` | Clear older rounds, keep last turn |
| Undo Message | `Cmd+Shift+Z` | Rewind last conversation turn (caret in history) |
| Search Sessions | - | Search all sessions by title |
| Select Effort | - | Set reasoning effort |
| Select Model | - | Set model for the current session |
| Set Default Model | - | Default model for a backend |
| Refresh Models | - | Refresh live model lists |
| Manage Anthropic Providers | - | Add/edit/pin/test providers (wizard) |
| Start Custom Provider Session | - | Start on a pinned Anthropic-compatible provider |
| Generate Provider Model Config | - | Fetch a provider’s live models → alias mapping |
| Set Default Provider | - | Default backend for a plain New Session |
| Copy Conversation | - | Copy the conversation |
| Interrupt | `Alt+Escape` | Stop current query (`Cmd+Shift+Escape`; `Ctrl+C` with empty selection in the sheet) |
| Permission Mode... | - | Change permission settings |
| Add MCP Tools to Project | - | Write `.claude/settings.json` MCP server config |
| Manage Auto-Allowed Tools... | - | Configure tools that skip permission prompts |
| Reset Input Mode | - | Re-enter the sticky ◎ composer |
| Queue Prompt | - | Queue a prompt while a turn is running |
| Send Now (cancel turn) | `Cmd+Enter` | Cancel the in-flight turn and send now (`Ctrl+Enter` too) |
| View Session History... | - | Browse saved history |
| Show Usage | - | Show usage / cost |
| View / Edit / Clear Retain Content | - | Session retain file |
| Edit Project Retain | - | Open `{project}/.claude/RETAIN.md` |
| Open Link at Cursor | `Cmd+click` | Open a link in the output view |
| Toggle Auto-Sleep for This Session | - | Disable/enable auto-sleep on this sheet |
| Toggle Submit Key (Enter / Cmd+Enter) | - | Flip `submit_with_modifier` |
| Sleep Session | - | Put session to sleep (free resources) |
| Wake Session | - | Wake a sleeping session |
| Output Settings | - | Edit `SubmarineOutput.sublime-settings` |

Session list (when that scratch view is focused): `Enter` open, `r` rename,
`v` reveal, `s` star, `j` / `Shift+J` JSONL, `Delete` / `Backspace` close.

### Inline Input Mode

The output view features an inline input area (marked with `◎`) where you type
prompts directly:

- **Enter** — Submit prompt (or wake a sleeping session)
- **Shift+Enter** — Insert newline (multiline prompts)
- **Cmd+Enter** / **Ctrl+Enter** — Send now (cancel in-flight turn + send). When
  `submit_with_modifier` is on, this is also the submit key and Enter inserts a
  newline
- **Alt+Enter** — Queue while busy (idle = normal submit). Queued messages
  sit as `⏳` chips above ◎ with **✎** (pull it back into the composer to
  change it), **↵** (send now) and **×** (drop)
- **@** — Open context menu (browse files, or clear pending context)
- **Cmd+K** — Clear older rounds, keep last turn
- **Cmd+Shift+K** — Clear output (full wipe)
- **Alt+Escape** — Interrupt current query
- **Cmd+A** — Select draft only when the caret is in ◎; select history only
  when browsing above the composer
- **Cmd+Shift+Z** — Conversation undo when the caret is in history
- **Cmd+Shift+V** — Paste image
- **Cmd+W** — Close the session view

`Submarine: Toggle Submit Key` flips Enter vs Cmd+Enter as the submit binding.

### Loop / cron (idle re-prompt)

`/loop` is **not** a plugin slash command — it is forwarded to the backend
engine. The **host** owns the banner and MCP timers (`features/scheduler.py`).

- `/loop <prompt>` — re-fire prompt as soon as the session becomes idle
- `/loop:<duration> <prompt>` — same, with a minimum gap
  (e.g. `/loop:5m check builds`)
- `/loop:cancel` — forwarded to the engine; **reliable cancel is the banner Stop**
  (`cancel_loop` RPC). Sleep, stop, `/clear`, and a verified `/goal` complete
  also cancel
- Same syntax also works as a prefix without slash: `loop:5m check builds`
- Banner / tab show `↻` while a wake is armed (`↻ wake in Ns`)
- Loop only fires when the session is idle — never interrupts an in-flight query

**Host timers (MCP):** `set_timer(seconds, wake_prompt)` is host-owned,
clamped to a **60s minimum**, one pending wake per session (a new timer
replaces the previous). When the timer fires while the session is working,
the wake is **deferred** (500ms poll) until idle. `cancel_timer(timer_id?)`
cancels one timer or every pending timer for this session.

### Permission, plan, and question UI

When a permission prompt appears (input mode is hidden):

```
⚠ Allow Bash: rm file.txt?
  [Y] Allow  [N] Deny  [S] Allow 30s  [A] Always
```

- **Y** — Allow this action
- **N** — Deny (marks tool as error)
- **S** — Allow same tool for 30 seconds
- **A** — Always allow this tool (saves to project Claude settings). Hidden
  for dangerous Bash (`rm`, `git checkout` / `reset` / `clean`, `git stash drop`)
- Prompts are queued one at a time; 30s timeout denies

When viewing plan approval:

- **Y** — Approve plan
- **N** — Reject plan
- **V** — View plan file

When a question is showing:

- **1–4** — Pick an option
- **O** — Other (free text)
- **Enter** — Confirm (multi-select)

All three can also be answered from outside (`submarine_sessions answer`,
the web console); the sheet records the `☑` line either way.

### Menu

Tools > Submarine

### Context Menu

Right-click selected text and choose "Ask Submarine" to query about the selection.

## Settings

`Preferences > Package Settings > Submarine > Settings`

```json
{
    // Path to Python 3.10+ interpreter for the bridge process
    "python_path": "python3",

    // Default model when no profile is selected: "opus", "sonnet", "haiku"
    "default_model": "opus",

    // Default backend for a bare "New Session": "claude", a built-in
    // (codex/kimi/grok/pi), or a custom_providers key.
    // "default_backend": "claude",

    "allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],

    // "default", "acceptEdits", "plan", "bypassPermissions"
    "permission_mode": "acceptEdits",

    // Auto-sleep idle sessions after N minutes (0 = disabled). Timer starts
    // when a turn *ends*. Default if omitted: 60.
    // "auto_sleep_minutes": 60,

    // Service Manager base URL for subscription usage on "with XXX…"
    // switch-panel rows (Grok / Kimi). Empty → http://127.0.0.1:3001
    // "quota_service_url": "http://127.0.0.1:3001",

    // Reasoning effort for fresh sessions (and always for Grok spawn).
    // Claude: low / medium / high / max
    // Grok: low / medium / high / xhigh (max aliases to xhigh)
    "effort": "high",

    // Goal verify skeptic: "task" — main sheet stays executor; spawns one
    // Task/Agent reviewer. (Only task mode is implemented.)
    // "goal_skeptic_mode": "task",

    // submarine MCP read_image. Default "auto" enables it only for Grok;
    // true = all backends, false = off.
    // "mcp_enable_read_image": "auto",

    // ACP (Grok/Kimi) also injects irr MCP (code search) when `irr` is on
    // PATH or at ~/.nimble/bin/irr. Index db: env SUBMARINE_IRR_DB /
    // IRR_MCP_DB, else --db from ~/.claude.json mcpServers.irr, else
    // cwd/.irr, else ~/.irr-pil/db/pil-core. Disable: env SUBMARINE_IRR_MCP=0.

    // Auto-retry a turn that ends in error (provider 503/429 exhausted).
    // 0 = off. Only fires on is_error results (not interrupts).
    // "auto_retry_turns": 0,
    // "auto_retry_backoff_seconds": 20,

    "custom_providers": { /* see Custom Providers */ },

    // Legacy DeepSeek key (read by the seeded deepseek provider).
    // "deepseek_api_key": "",

    // "default_models": { "claude": "opus", "deepseek": "deepseek-v4-pro" },

    "quick_agent": {
        "backend": "deepseek",
        "model": "flash",
        "effort": "low",
        "permission_mode": "bypassPermissions",
        "system_prompt": "You are a Quick Agent: one user message → one short answer."
    },

    // Environment variables passed to the bridge process
    // "env": {},

    // false: Enter submits, Shift+Enter newline.
    // true:  Cmd/Ctrl+Enter submits, Enter newline.
    "submit_with_modifier": false
}
```

Profiles live in separate files — see [docs/profiles.md](docs/profiles.md).
A legacy `"checkpoints"` key is ignored.

### Permission Modes

- `default` — Prompt for all tool actions
- `acceptEdits` — Auto-accept file operations
- `plan` — Read-only until the plan is approved
- `bypassPermissions` — Skip all permission checks
- `auto` — Present in the plugin constants (AI classifier); the palette
  toggle lists the four modes above

### Auto-Allowed Tools

**Command:** `Submarine: Manage Auto-Allowed Tools...`

**Settings:** project `.claude/settings.json` or user `~/.claude.json`
(`autoAllowedMcpTools`, plus `permissions.allow` patterns are merged in):

```json
{
  "autoAllowedMcpTools": [
    "mcp__*__*",
    "mcp__plugin_*",
    "Read",
    "Bash"
  ]
}
```

Supports wildcards (`*`). User-level settings apply to all projects; project
settings override.

### Project settings (`.sublime-project`)

The conventions name these keys. What the plugin **actually reads today**:

| Intended project key | What the code reads |
|----------------------|---------------------|
| `submarine_additional_dirs` | **Package** settings `submarine_additional_dirs`, with fallback `claude_additional_dirs` (`main.py` `construct_session`) — **not** `window.project_data()` |
| `submarine_retain` | **Not read** as a project string. Retain is file-based: `{cwd}/.claude/retain-{sid}.md` (or `~/.submarine/retain-{sid}.md`), plus `{project}/.claude/RETAIN.md` via **Edit Project Retain** |
| `submarine_env` | **Not read**. Bridge env comes from package setting `env` |

Secondary window folders are **not** auto-added as `--add-dir` (the old plugin
did that). Put extra dirs in package settings `submarine_additional_dirs` for now.

## Custom Anthropic-Compatible Providers

Point the Claude bridge at any third-party Anthropic-compatible endpoint (same
env surface as [ccm](https://github.com/nicepkg/ccm) — DeepSeek, GLM, Kimi,
Qwen, OpenRouter, …). Each entry lives under `custom_providers` in settings:

```json
"custom_providers": {
  "glm": {
    "label": "GLM",
    "base_url": "https://open.bigmodel.cn/api/anthropic",
    "auth_env_var": "GLM_API_KEY",
    "opus_model": "glm-5.2[1m]",
    "sonnet_model": "glm-5.2[1m]",
    "haiku_model": "glm-4.7",
    "pinned": true,
    "effort": "high"
  }
}
```

| Field | Role |
|-------|------|
| `base_url` | Required Anthropic-compatible endpoint |
| `auth_token` / `auth_env_var` | Auth (prefer env — never store keys inline) |
| `auth_via_api_key` | `true` → `ANTHROPIC_API_KEY` instead of `AUTH_TOKEN` |
| `opus_model` / `sonnet_model` / `haiku_model` / `subagent_model` | Alias mappings |
| `label` / `abbrev` | Display + tab abbreviation |
| `pinned` | Show in Start Custom Provider / Set Default pickers (default `false`) |
| `effort` | Per-provider override (`low`/`medium`/`high`/`max`); blank → global `effort` |
| `extra_env` | Extra env defaults |

A custom key that collides with a built-in (`kimi`, `grok`, …) is remapped to
`{name}_api` with label suffix ` (API)`.

### Manage UI

**Submarine: Manage Anthropic Providers** — quick-panel wizard: add/edit
providers field-by-field with a review/confirm step, plus per-provider
**Pin/Unpin**, **Test config** (validates base URL + resolved auth),
**Generate Model Config** (fetches `/v1/models` and maps aliases), and
**Duplicate/Delete**. "Edit raw JSON" opens the settings file.

### Change provider on the fly

**Submarine: Select Provider…** (`Cmd+Ctrl+Alt+P`, or
**⇄ Change Provider…** in `Cmd+\`) swaps a running session’s provider
mid-conversation — official Claude ↔ any `(CC) …` provider (DeepSeek, GLM,
StepFun, …). Same bridge, same transcript: the session restarts with
`--resume`.

The model moves only when it can: `opus` / `sonnet` / `haiku` are mapped by
every provider, so they carry over; a concrete id (`deepseek-v4-pro[1m]`,
`claude-opus-5`) means nothing to the other side, so the new provider's
default is used. A refusal says why (mid-turn, unknown provider, or a backend
outside the family — those need a new session).

## Context

Add files, selections, or folders as context before your query:

1. Use **Add Current File**, **Add Selection**, etc., or type `@` in the composer
2. Context shown with 📎 indicator in the output view
3. Context is attached to your next query, then cleared

Requires an active session (use **New Session** first).

## Profiles

Named model / system-prompt / effort configs. User file:
`~/.submarine/profiles.json`. Project file: `{project}/.claude/profiles.json`.
See [docs/profiles.md](docs/profiles.md). Checkpoints are gone — use a profile
or live-fork an open session.

## Goals

Host-owned harness. Type `/goal <objective> [--budget N]` in the sheet (never
forwarded to the engine). The plugin plans → executes → verifies; the model
only claims via MCP `update_goal` / `goal_verdict`.

- Plan lives at `{project}/.claude/goals/{id}/plan.md` (frozen
  `plan.baseline.md` at accept)
- Complete unlocks only with structured `goal_verdict` (or `evidence/VERDICT.json`)
  **and** on-disk evidence. Visual/render goals need an image capture
- 3× `blocked_reason` pauses; optional `--budget N`; continue cap 24; verify cap 5
- Palette: Goal Status / Pause / Resume / Clear
- **Persistence:** goal JSON is saved in `.sessions.json`. After a Sublime
  restart an in-flight `active` goal restores as `user_paused` — `/goal resume`

Full guide: [docs/goals.md](docs/goals.md).

## Sessions

Sessions are automatically saved and can be resumed later. Each session tracks
name (auto from first prompt, or set manually), project directory, and
cumulative cost.

**Multiple sessions per window** — each New Session creates a separate output
view. Switch with `Cmd+\` or **Session List**.

Use **Submarine: Resume Session...** to pick and continue a previous conversation.

After Sublime restarts, orphaned output views are registered as sleeping
sessions. Press Enter or use **Wake Session** to reconnect (`resume_id` is
always passed — never a fresh session).

### Sleep / Wake

- **Sleep** — kills the bridge process; view shows `⏸`
- **Wake** — press Enter in a sleeping view, or **Wake Session**
- The switch panel shows a sleeping active session with `⏸`
- `auto_sleep_minutes` auto-sleeps idle sessions (default 60 if omitted; `0` =
  disabled). Timer starts when a turn *ends*. **Toggle Auto-Sleep for This
  Session** sets `sleep_disabled` on that sheet only
- **Hide Session** closes the view but keeps the bridge running
- The **Session List** shows CURRENT (`▸` bound, `●` working, `?` waiting,
  `!` unread, `✘` halted on an error, `○` ready, `⏸` sleeping) above HISTORY, children under their
  parent with `↳`. Closing a parent asks about its children. Resumed
  transcripts show host-injected prompts as `⚙ …`, never the raw tag block.

### Fork / switch / undo

- **Fork Session** / **Fork Session…** — branch from the current or a saved
  transcript (`resume_id` + `fork=True`)
- MCP `spawn_session(fork_current=…)` / `fork_from_agent_id=…` — same backend
  family only (`list_backends` reports the rule)
- **Undo Message** — Claude jsonl rewind / Grok conversation-only rewind.
  Synthetic turns tagged `channel` / `timer` / `inject` are skipped so old
  transcripts stay clean
- **Search Sessions…** / **Show Session History…** / **Open Session JSONL**

## Output View

The output view shows:

- `◎ prompt ▶` — Your query (multiline supported)
- Spinner — Working indicator
- `☐ Tool` — Tool pending
- `✔ Tool` — Tool completed
- `✘ Tool` — Tool error
- `⚙` — Background tool; flips to `✔`/`✘` with the job's output when it ends.
  The agent runtime reports the completion to the model itself (Claude Code
  runs a follow-up turn, shown as `⚙ …`); the plugin never re-sends it
- Response text with syntax highlighting
- `@done(Xs, ctx, provider/model, effort)` — how the turn ended, and what
  it ran on (recorded per turn, so old turns keep their own provider)
- **Tasks block** — folded by default; `Cmd+Alt+T` or **super+click** the
  banner / "+N more" line to expand
- **Goal strip** — `◆ goal · {phase} · {objective}`
- **Retry hint** — `⚠ 503 retry 3/10` (muted) under the spinner while the
  provider retries a 429/5xx

View title shows session status:

- `◉` / `•` Responding (streaming text) or running a tool
- `◐` / `○` Waiting (turn open but not streaming)
- `◇` Idle
- `⏸` Sleeping
- `❓` Waiting for permission / question / plan
- `⚠` Error-halted
- `↻` Pending self-wake (`/loop` / timer)

Non-Claude sessions show a backend abbreviation in the tab (`CX`, `KM`, `GR`,
`Pi`, …). Codex uses a green-tinted color scheme.

Supports markdown formatting and fenced code blocks with language-specific
syntax highlighting.

## MCP Tools (Sublime Integration)

Allow the agent to query Sublime Text’s editor state via MCP.

### Setup

1. Run **Submarine: Add MCP Tools to Project**
2. This creates `.claude/settings.json` with an `mcpServers.sublime` entry
   pointing at `mcp/server.py`
3. Start a new session

Bridges also inject the Sublime MCP server per session (`--view-id=N`).

### Catalog

From `mcp/tools.py` (`read_image` is advertised only when enabled — default
`auto` = Grok only):

| Tool | Role |
|------|------|
| `get_window_summary` | Open files, active file/selection, folders, layout |
| `find_file` | Fuzzy file find |
| `get_symbols` | Project symbol search |
| `goto_symbol` | Navigate to a definition |
| `read_view` | Read a file or scratch buffer (`head`/`tail`/`grep`) |
| `read_image` | Vision over a local PNG/JPEG/… (gated) |
| `list_backends` | Built-ins + custom providers, availability, fork-family |
| `list_profiles` | Session profiles |
| `spawn_session` | Spawn a child (`profile`, `backend`, live fork) |
| `send_to_session` | Message a worker by `agent_id` (auto-wake; queues if busy) |
| `list_sessions` | This window’s subsessions (`agent_id` is stable) |
| `read_session_output` | Tail a child + `context_budget` |
| `list_profile_docs` / `read_profile_doc` | Profile docset |
| `lsp` | hover / definition / references / symbols / diagnostics |
| `sublime_eval` | Python in Sublime (`sublime`, `sublime_plugin`, named tools) |
| `sublime_tool` | Run `.claude/sublime_tools/<name>.py` |
| `list_tools` | List those saved tools |
| `quick_done` | End a Quick Agent turn (`completed` / `blocked` / `closed`) |
| `update_goal` | Implementer progress / complete / blocked (requires `/goal`) |
| `goal_verdict` | Skeptic unlock during verify only |
| `session_info` | This sheet’s `agent_id`, parent, backend, budget |
| `signal_complete` | Child → parent (after this turn is idle) |
| `wait_for_subsession` | Host-local wait for `signal_complete` |
| `set_timer` | Host-local wake after N seconds (min 60; one pending wake) |
| `cancel_timer` | Cancel one or all timers for this session |

There is no `set_alarm`, `terminal_*`, `order`, `list_personas`, chatroom, or
notalone subscribe/discover API.

**Creating saved tools** — put a `.py` file in `.claude/sublime_tools/` with a
docstring; call it via `sublime_tool(name="…")` or `list_tools()`.

**Multi-agent:** address workers by `agent_id` (stable across Sublime restart).
`view_id` is runtime-only. Prefer `list_sessions` + `send_to_session` over
re-spawning. Parent linkage uses `parent_agent_id`; `signal_complete` looks up
the parent from this sheet.

## Architecture

Layered package. Sublime imports only in `ui/`, `features/`, `commands/`,
listeners, and the entry point. `plat/`, `backend/`, and `core/` run under
plain Python 3 for tests. Bridges are standalone processes (Python 3.10+).

```
┌──────────────┐     JSON-RPC/stdio     ┌──────────────────┐
│ Sublime Text │ ◄────────────────────► │ bridge/          │
│ (Python 3.8) │                        │  claude_main.py  │ Claude Agent SDK
│              │                        │  codex_main.py   │ Codex app-server
│  commands/   │                        │  kimi_main.py    │ Kimi ACP
│  features/   │                        │  grok_main.py    │ Grok ACP
│  ui/         │                        │  pi_main.py      │ Pi
│  core/       │                        └──────────────────┘
│  backend/    │
│  plat/       │
└──────┬───────┘
       │ Unix socket  $TMPDIR/submarine_mcp.sock
       ▼
┌──────────────────┐     stdio     ┌──────────────┐
│ mcp/socket_      │ ◄───────────► │ mcp/server.py│
│ server.py        │               │ (per session)│
└──────────────────┘               └──────────────┘
```

```
submarine/
├── submarine.py           # entry: plugin_loaded / command re-exports
├── main.py                # session factory, auto-sleep, lifecycle
├── plat/                  # constants, log, settings, jsonio, util
├── backend/               # specs, providers, JSON-RPC client
├── core/                  # Session, turn, registry, records, rewind, events
├── ui/                    # output sheet, composer, modals, listeners
├── features/              # goals, scheduler, quick, context, quota, devtools
├── commands/              # thin Window/Text commands
├── mcp/                   # socket server + stdio MCP + tool catalog
└── bridge/                # Python 3.10+ backends (claude/codex/acp/pi)
```

Both bridges emit the same JSON-RPC notifications, so the output view,
permissions, and MCP tools work the same regardless of backend.

Longer snapshot: [docs/architecture.md](docs/architecture.md).

## License

VCL (Vibe-Coded License) — see LICENSE
