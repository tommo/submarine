# Claude Code background tasks

The claude backend's ⚙ (background) machinery was built on the ACP-era wire:
launch ack → `task_started` → … → `task_notification`. Claude Code's own
background bash does not look like that, and the host mishandled every step.

```bash
python3 sandbox/claude_bg/check_e2e.py            # live capture (model 'haiku')
python3 sandbox/claude_bg/check_e2e.py --mode during
python3 sandbox/claude_bg/check_host_gate.py      # replay captures through the host
python3 -m pytest tests/test_cc_bg_host_gate.py
```

`check_e2e.py` drives the real `bridge/claude_main.py` in a throwaway cwd with a
prompt that forces `Bash(run_in_background: true)`, and writes the capture to
`fixtures/`; `check_host_gate.py` replays that capture, in order, through the
shipped `BridgeEventRouter` + `BackgroundTaskGate` + a headless output view. The
fixtures are committed, so the replay also runs as a pytest.

Two modes, because the turn's end changes what the CLI sends:

- `after` (default) — the model backgrounds the job and ends its turn; the job
  finishes with the session idle. `fixtures/cc_bg_wire.jsonl`.
- `during` — a foreground `sleep` keeps the turn live past the job's completion.
  `fixtures/cc_bg_during.jsonl`.

## Observed (claude 2.1.274, claude-agent-sdk 0.2.154, 2026-09-18)

Tool call: `Bash {command: "sleep 25; echo CC_BG_DONE", run_in_background: true}`
→ bridge emits `tool_use {background: true}`.

Launch ack (the Bash `tool_result`, ~0.1 s later):

```
Command running in background with ID: byfbn40bu. Output is being written to:
/private/tmp/claude-501/<project>/<session>/tasks/byfbn40bu.output. You will be
notified when it completes. To check interim output, use Read on that file path.
```

`task_started` carries `task_id`, `tool_use_id`, `description`, `task_type:
"local_bash"` **and `is_backgrounded`** (true for a backgrounded job, false for a
foreground command — CC opens `task_started` for those too).
`system/background_tasks_changed` carries `tasks: [{task_id, task_type,
description}]`, empty at the end (the host ignores this subtype today).

Completion:

| turn state | what arrives |
|---|---|
| turned ended first (`after`) | `background_tasks_changed []` + **`task_updated {patch: {status: "completed", end_time}}`** — **no `task_notification`**, and `task_updated` has no `tool_use_id`, no `output_file` |
| turn still live (`during`) | `task_updated` **and** `task_notification {status, summary: "Background command \"…\" completed (exit code 0)", output_file}` for the same task, plus a `task_notification` for the foreground command |

After the turn (`after`), the CLI then **enqueues a follow-up turn of its own**
for the finished job: a user message with `origin: {kind: "task-notification"}`
(`promptSource: "sdk"`), answered by the model, closed by a `ResultMessage`
carrying the same `origin`. Mid-turn (`during`) the completion is instead
absorbed into the running turn as a `queued_command` attachment
(`absorbed_mid_turn`) — the model already has it, no separate turn. In both
cases the model is told by Claude Code itself; anything the host sends about the
same job is a duplicate (the real-session transcripts had every host
`<task-notification>` block 2–4 s behind the CLI's own turn, answered "already
accounted for").

The bridge used to read the SDK stream only inside `query()`, so the follow-up
turn was dropped (`_drain_stale` ate it at the next query) and the host then
built its own notification query. Now one persistent reader owns the stream
(`bridge/claude_main.py` `_read_stream`), the bridge stamps its prompts
`origin: human` and runs with `--replay-user-messages`, and every message is
attributed by origin: our turn closes the query RPC; a turn the CLI started is
announced as `injected_turn {origin, summaries}`, streamed, and closed by a
`result {origin}`.

Two consequences of the CLI queue (`queue-operation` lines in the transcript):
a prompt that reaches the CLI while a turn is running is **absorbed** into it
(`absorbed_mid_turn`) and never gets a result of its own — so a host query sent
during the CLI's follow-up turn is closed by *that* turn's result (untagged, as
the host's own; `hq["absorbed"]`). Left open, the host stayed "working" until
Esc and then re-sent the message: the model saw it twice. And `inject_message`
delivers into whichever turn is running (ours or the CLI's); with none running
it answers `idle` and the host keeps the message queued for its next closer —
the bridge no longer holds injects to replay at the end of the next query.

`model` note: this shell maps `haiku` to `deepseek-v4-flash` via
`ANTHROPIC_DEFAULT_HAIKU_MODEL`. Background bash is client-side, so the provider
does not change any of the shapes above; `--clean-env` runs real Anthropic haiku.

## Host contract

- `BackgroundTaskGate.note_launch_ack(tool_use_id, content)` — CC's ack means
  the job is running: `core/events.py` keeps the ⚙ row, and the gate records
  `task_id → tool_use_id` and `task_id → log path` (the only place the log path
  appears on the wire).
- `task_started` with `is_backgrounded: false` and a tool id that is not a ⚙ row
  marks the task foreground: its terminal events are ignored.
- A completion (`task_updated` terminal, or `task_notification`) flips the row
  once — ✓ (✗ on failure) with the job's output read from the log — and marks
  the session unread. **It never starts a turn.** Repeats and aliases are
  no-ops; `killed`/`stopped` are terminal too.
- `injected_turn` is adopted by the session (`Session._adopt_injected_turn`):
  busy without an RPC, a `⚙ <summary>` prompt row, the normal stream, and the
  tagged `result` as closer. Prompts typed meanwhile queue behind it. If it
  arrives while a host query is open (the CLI ran its turn ahead of ours), the
  content paints into that turn and the tagged result is ignored.
- `reconcile(running=[])` (the poll's answer, from `background_tasks_changed`)
  clears a stale ⚙ without surfacing anything.

The whole captured stream is replayed through the real host by
`check_host_gate.py` (`tests/test_cc_bg_host_gate.py`); `check_e2e.py` also
asserts the bridge forwarded exactly one `injected_turn` and one tagged result
for the `after` case.
