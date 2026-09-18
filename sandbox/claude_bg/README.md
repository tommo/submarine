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

The bridge's post-result drain stops as soon as its `_pending_bg_tasks` empties,
which a terminal `task_updated` does — so in the `after` case the drain ends on
the `task_updated` line and the CLI's `task_notification` (if it was queued next)
is left in the SDK buffer, where the *next* query's `_drain_stale` swallows it
(the bridge log prints `pre-drain: consumed N stale messages`). `poll_bg_tasks`
then reports `pending: 0, checked: 0` and never drains it either.

`model` note: this shell maps `haiku` to `deepseek-v4-flash` via
`ANTHROPIC_DEFAULT_HAIKU_MODEL`. Background bash is client-side, so the provider
does not change any of the shapes above; `--clean-env` runs real Anthropic haiku.

## What the host did with that (before the fix)

1. `tool_result` did not recognize the ack as "still running" (it looked for
   `status: running`), so `finalize_tool(keep=True)` closed the ⚙ row ~0.1 s
   after launch: an empty ⚙ strip, nothing to reconcile, no way to see what was
   running.
2. The turn's own `result` closed it again in the ACP shape.
3. The completion (`task_updated`) only flipped the row: no notification turn —
   the job's output was never shown and the agent was never woken with the
   result. The user saw a ✓ row and silence.
4. A *foreground* command's completion could start a notification turn for work
   the model had already answered inside that turn.

## Host contract after the fix

- `BackgroundTaskGate.note_launch_ack(tool_use_id, content)` — CC's ack means
  the job is running: `core/events.py` keeps the ⚙ row, and the gate records
  `task_id → tool_use_id` and `task_id → log path` (the only place the log path
  appears on the wire).
- `task_started` with `is_backgrounded: false` and a tool id that is not a ⚙ row
  marks the task foreground: its terminal events are ignored (no turn). Backends
  that do not send the flag (kimi/acp) are unaffected.
- A terminal `task_updated` surfaces the completion exactly like
  `task_notification` (buffered, debounced, then `notify_action` — for claude a
  query). The block is keyed by `tool_use_id`, so the CLI's richer
  `task_notification` for the same job **replaces** the pending block instead of
  adding a second turn, and a repeat never notifies twice.
- `reconcile(running=[])` (the poll's answer, and the `background_tasks_changed`
  set when someone wires it) still clears a stale ⚙ without waking the session.

Open, not done: the drain break loses the CLI's own `task_notification` in the
`after` case (the host reconstructs the completion from `task_updated` + the ack
instead, which is why the ack's log path is parsed); `background_tasks_changed`
is still ignored; `note_task_poll_delivery` matches CC task ids only via the ids
we already track, not via a `bash-*` pattern.
