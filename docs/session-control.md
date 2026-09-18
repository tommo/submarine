# Submarine session control — manual

`submarine_sessions` manages the agent sessions a running Sublime Text is
holding: list them, read them, send a prompt to one, cancel its current turn.
It is a client of the plugin socket, so it works from a terminal, from a script,
or from an agent that is **not** itself a Submarine session.

```
submarine_sessions list [--scope all|window|children]
submarine_sessions view REF [--mode tail|text|edits]
submarine_sessions chat REF "prompt" [--wait]
submarine_sessions interrupt REF
submarine_sessions --manual
```

---

## 1. What it talks to

The plugin runs a Unix socket at `$TMPDIR/submarine_mcp.sock` (usually
`/tmp/submarine_mcp.sock`; on this machine `$TMPDIR` is a per-user directory).
Three namespaces share it: the agent tool calls, the host devtools
(`op:"debug"`, `docs/devtools.md`), and session control:

```json
{"op": "sessions", "action": "list"}
```

Everything the CLI does is one of those requests. Two consequences worth
knowing before you script against it:

- **The control surface never evaluates code.** The `{"code": …}` namespace next
  to it does, and it is for the plugin author, not for agents. `op:"sessions"`
  takes JSON fields only; a `code` field in the same request is ignored.
- **The op ships with the plugin**, so a Sublime that has not reloaded the
  package since it was added answers nothing. The CLI then says
  `hint: reload the Submarine package (the op needs the new code)`.

## 2. Install

Nothing to install: the CLI is a script in the plugin tree.

```bash
python3 /path/to/submarine/submarine_sessions.py list
```

It is normally symlinked onto `PATH` from `~/.local/bin` (where the other
project CLIs live):

```bash
ln -s /path/to/submarine/submarine_sessions.py ~/.local/bin/submarine_sessions
```

The shim resolves its own symlink, so the link works from any directory.
`python3` is all it needs — no plugin import, no Sublime at startup.

## 3. Quick start

```bash
submarine_sessions list                       # what is running, and what is saved
submarine_sessions view GUEST --turns 3       # the tail of that session's transcript
submarine_sessions chat GUEST "run the tests" --wait
submarine_sessions interrupt GUEST            # cancel whatever turn is running
```

`REF` in every command is a session handle (section 6).

---

## 4. Commands

### `list`

```
submarine_sessions list [--scope all|window|children]
                        [--parent REF] [--window ID] [--json]
```

| Option | Meaning |
|---|---|
| `--scope all` | default: every live session in this Sublime, plus the saved rows that have no live session |
| `--scope window` | only sessions whose sheet belongs to one window (with `--window ID`, that window) |
| `--scope children` | only sessions whose `parent_agent_id` is the session named by `--parent` |
| `--parent REF` | parent for `--scope children` |
| `--window ID` | window id for `--scope window` |

Columns: `STATE`, `NAME`, `BACKEND`, `VIEW` (the sheet's view id, `-` when the
session has no sheet), `WINDOW`, `Q` (query count), `AGE` (time since last
activity), and `AGENT / SESSION` (the two ids, truncated). The footer counts the
rows per state.

```
STATE     NAME                   BACKEND VIEW   WINDOW Q     AGE  AGENT / SESSION
----------------------------------------------------------------------------------
idle      GUEST                  grok    59     2      43    1h   agent-0a1b2c3d4e5f 01a0aaaa-bbbb
working   review the dock tabs   kimi    -      2      12    3m   agent-1b2c3d4e5f60 session_0123abcd
sleeping  harness-mcp            grok    12     3      5     2d   agent-2c3d4e5f6071 01a0bbbb-cccc

4 session(s) — idle=1, sleeping=1, working=1, closed=1
```

### `view`

```
submarine_sessions view REF [--mode tail|text|edits] [--turns N]
                            [--max-chars N] [--offset N] [--limit N]
                            [--file PATH] [--json]
```

| Mode | Reads | Needs a sheet |
|---|---|---|
| `tail` (default) | the last `--turns` turns (default 3, max 50) from the backend transcript | no — works for live, sleeping, detached and **closed** sessions |
| `text` | the rendered sheet, last `--max-chars` characters (default 20000) | yes — otherwise `no_view` |
| `edits` | the Edit/Write rows this session made, paged with `--offset`/`--limit` (default 20), filterable with `--file` | no |

`tail` prints the transcript path it read, then each turn as the prompt line
`▌ …`, the tools it used, and the reply. Replies and prompts are cut at
`--max-chars` and marked truncated.

### `chat`

```
submarine_sessions chat REF "prompt" [--queue queue|interrupt|reject]
                                     [--wait] [--idem KEY] [--timeout S]
                                     [--wait-timeout S]
```

| Target state | `--queue queue` (default) | `--queue interrupt` | `--queue reject` |
|---|---|---|---|
| idle, connected | sent now | sent now | sent now |
| mid-turn | queued behind the turn | cancels the turn, then sends | refused with `busy` |
| asleep / not connected | woken, then sent once the bridge is up | same | same |

- `--wait` follows the turn and then prints the reply (via a `view --turns 1`
  request). Without `--wait`, the command returns as soon as the prompt is
  accepted, which for a sleeping target means "woken, delivery pending".
- `--idem KEY` makes a retry safe: the same key is answered from a small replay
  cache instead of sending the prompt a second time. Use it from anything that
  retries on timeout.
- `--timeout` is how long the CLI waits for the socket reply (default 30s);
  `--wait-timeout` is how long the follow-up `--wait` read may take (600s).
- The prompt is delivered to the model as written. Only the *display* line in
  the transcript is stamped (`📨 from outside agent`, or the caller's name when
  a wire client sends one).
- `chat` never stops a session and never opens a sheet.

### `interrupt`

```
submarine_sessions interrupt REF [--json]
```

Cancels the current turn — the same path the `Esc` key uses. The reply is
honest about the two-step nature of that:

```
GUEST: interrupted
turn_phase=interrupting settling=True user_cancelled=True
the bridge acknowledge is still outstanding; the host's settle timer reconciles it if it never arrives
```

`settling: true` means the cancellation was requested and the session is still
`working` while the bridge acknowledges; the host settles a lost acknowledgement
on its own timer. Nothing is queued for the session by an interrupt.

### Global options

Accepted before or after the command:

| Option | Meaning |
|---|---|
| `--json` | print the raw envelope instead of the human rendering |
| `--manual` | print this manual and exit; no socket, no Sublime needed |
| `--socket PATH` | talk to a socket other than `$TMPDIR/submarine_mcp.sock` |
| `--timeout S` | how long to wait for the socket reply (default 30) |

### `--manual` and `help`

`submarine_sessions --manual` prints this file. It is read from the plugin tree,
needs no socket, and works whether or not Sublime is running.
`submarine_sessions help` prints the short usage instead.

## 5. Reading the output

Human output is for a terminal; `--json` prints the raw envelope instead, for
scripts and agents:

```json
{
  "ok": true,
  "data": { "scope": "all", "count": 4, "states": {"idle": 1}, "sessions": [ … ] },
  "error": null,
  "ref_resolved": { "agent_id": "agent-0a1b2c3d4e5f", "session_id": "01a0…", "name": "GUEST", "backend": "grok", "view_id": 59 },
  "instance": { "pid": 37679, "plugin_dir": "/…/Packages/Submarine" },
  "ts": 1789692785.05
}
```

- `ref_resolved` echoes what your `REF` actually resolved to. Pin it (pass the
  `agent_id`) on the next call instead of a name.
- `instance` says which Sublime answered. Any instance rebinds the same socket
  path, so a client that cares should compare it between calls.
- Exit codes: `0` success, `1` an error envelope (message on stderr), `2` bad
  usage.

Error codes in `data.code`:

| Code | Meaning |
|---|---|
| `not_found` | no live or saved session matches `REF`; `candidates` lists what exists |
| `ambiguous` | more than one session matches the name; `candidates` lists them |
| `busy` | target is mid-turn and `--queue reject` was asked for |
| `no_view` | `--mode text` on a session with no sheet |
| `no_session` | the action needs a running session (e.g. `edits` on a saved row) |
| `transcript` | the backend transcript could not be read |
| `bad_request` | unknown scope, mode, or empty prompt |
| `unknown_action` | not one of `list`, `view`, `chat`, `interrupt` |
| `internal` | a bug in the host side; the message carries the exception |

## 6. Addressing a session (`REF`)

Accepted, in this order:

1. `agent_id` — the stable owner id (`agent-0a1b2c3d4e5f`). Always correct.
2. `session_id` — the backend transcript id (`01a0aaaa-…`, `session_0123abcd-…`).
3. `subsession_id` — an alias of the agent id for spawned children.
4. A view id (`59`) — the sheet currently showing the session, live only.
5. A `name` (`GUEST`) — only if exactly one session matches, case-insensitively.
   Two matches is `ambiguous`, never a guess.

A wire client may instead send a typed ref, which removes all doubt:

```json
{"ref": {"agent_id": "agent-0a1b2c3d4e5f"}}
{"ref": {"session_id": "01a0aaaa-bbbb-7ccc-8ddd-eeeeffff0000"}}
{"ref": {"name": "GUEST", "backend": "grok"}}
```

The resolver never falls back to "the active view" or "the only working
session": a control surface that acts on the wrong agent is worse than one that
refuses.

## 7. What the fields mean

`state` for a live session:

| Value | Meaning |
|---|---|
| `idle` | connected, waiting for input |
| `working` | a turn is running (see `turn_phase`) |
| `sleeping` | no bridge process; `chat` wakes it, `view` still reads it |
| `error` | the session halted on a failure |
| `closed` | registered but with nothing behind it (no id, no connection) |

`state` for a saved row is what the store recorded: `open`, `sleeping` or
`closed`.

`turn_phase` is the session's turn kind: `idle`, `live`, `interrupting`,
`compacting`.

## 8. Guarantees

- **Nothing here stops a session.** `interrupt` cancels a turn, which is what
  `Esc` does; no action sleeps, closes or detaches a session.
- **Nothing opens a sheet.** `text` mode reads one if it exists and reports
  `no_view` if it does not, so a background client never steals your window.
- **Everything works without a sheet** except `text`: `list`, `view tail`,
  `view edits`, `chat` and `interrupt` run from registry, store and transcript
  state alone.
- **A timed-out `chat --wait` does not drop the work.** The prompt keeps
  running; the command says so instead of implying failure. Follow it with
  `view`.
- **Retries are safe with `--idem`.** The replay cache is small (64 keys, on the
  plugin host) and survives a package reload.

## 9. Limitations

- One editor answers: the socket path is per user, and the last Sublime to load
  the plugin owns it. `instance` in the envelope tells you which one replied.
- There is no authentication on the socket yet (see `docs/devtools.md`,
  "hardening"): anything that can connect can also use the `code` namespace.
  Do not expose this socket beyond your user account.
- `view tail` reads the backend's own store, so a session whose transcript the
  backend has pruned or moved can only be read through `text` while it is open.
- The Claude CLI's transcripts live per project directory; sessions resumed
  across projects are resolved through the plugin's resume chain, which is what
  makes `view tail` work for closed sessions.
- No stdio MCP façade yet: an external agent reaches this through the CLI or its
  own socket client.

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `error: socket missing: /tmp/submarine_mcp.sock` | Sublime is not running, or the plugin is not loaded | start Sublime with Submarine; the `hint` line says so too |
| `error: Sublime answered nothing for op:sessions` | the running plugin predates the op | reload the package (`submarine_devtools reload`) or restart Sublime |
| `error: no live or saved session 'X'` | wrong handle | run `list` and use the `agent_id` it prints |
| `error: 2 live sessions named 'hello'` | a shared name | add the backend or the full agent id |
| `error: GUEST is mid-turn` | `--queue reject` on a busy session | use the default `--queue queue`, or `interrupt` first |
| `error: session GUEST has no sheet` | `--mode text` while the session is detached, sleeping or backgrounded | use `--mode tail`, or open the session in Sublime |
| `chat --wait` returns without a reply | the follow-up `view` found no turn yet | re-run `view REF --turns 1`, or raise `--wait-timeout` |

## 11. Worked example

```bash
$ submarine_sessions list --scope window --window 2
STATE     NAME     BACKEND VIEW   WINDOW Q     AGE  AGENT / SESSION
--------------------------------------------------------------------------------
idle      GUEST    grok    59     2      43    1h   agent-0a1b2c3d4e5f 01a0aaaa-bbbb

2 session(s) — idle=1, closed=1

$ submarine_sessions view GUEST --turns 1
GUEST (grok) — 24 turn(s) on disk, showing 1
~/.grok/sessions/%2Fpath%2Fto%2Fproject/01a0bbbb-…/chat_history.jsonl

▌ <user_query>
Selection from /path/to/project/src/impl.nim:L1
…642 characters of reply…

$ submarine_sessions chat GUEST "rebase onto master and run the suite" --wait \
      --idem rebase-1
queued behind the turn → GUEST (agent-0a1b2c3d4e5f)

tests: 924 passed

$ submarine_sessions interrupt GUEST
GUEST: nothing running
turn_phase=idle settling=False user_cancelled=False
```

## 12. Implementing another client

The CLI is a thin wrapper; anything that can write one JSON line to the socket
can do the same job. The request shape:

```json
{"op": "sessions", "action": "view", "ref": {"name": "GUEST"},
 "mode": "tail", "turns": 3,
 "caller": {"kind": "external", "name": "my-agent", "pid": 4711, "cwd": "/w"}}
```

`caller` is optional and only used for the display stamp and the audit log in
`docs/devtools.md`. The reply is the envelope from section 5, wrapped by the
socket's own `{"result": …, "error": …}` layer.

One `chat` subtlety for other clients: a sleeping target answers immediately
with `data.action: "woke"` while the socket thread wakes the bridge, waits for
it and then delivers the prompt. That hand-off is why `chat` must not be
treated as synchronous unless you passed `wait`.

## 13. Where the code is

| Piece | File |
|---|---|
| CLI client | `features/sessions_cli.py`, shim `submarine_sessions.py` |
| Host-side control logic | `features/session_control.py` |
| Socket namespace | `mcp/socket_server.py` (`op == "sessions"`) |
| Tests | `tests/test_session_control.py` |
| Host devtools manual | `docs/devtools.md` |

See also `docs/architecture.md` for how sessions, bridges and the registry fit
together.
