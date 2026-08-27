# Single Session View per Window ("host view" mode)

Status: implemented. Default `"ui_mode": "single"` (one host view per
window; session list swaps the bound session). `"tabs"` is the legacy
opt-out (one sheet per session). Tear-off
(`submarine_tear_off_session`) promotes the bound session to its own
sheet; dock (`submarine_dock_session`) returns it to the host. Torn-off
state is in-memory only — it does not persist across ST restarts
(session resumes into host rotation).

## Goal

One output view per window. All sessions in the window share it; the session
list is the switcher. Activating a session row swaps that session into the
host view instead of focusing a per-session sheet.

Mode is `"ui_mode": "single"` (default) or `"tabs"` in
`Submarine.sublime-settings`. Runtime switch collapses/restores sheets
(see §8).

## Why this is cheap in Submarine

The rewrite already built the seams this mode needs:

- `Session` never touches a view — all rendering goes through
  `OutputPort`/`ChromePort` (`core/ports.py`), implemented per session by
  `SubmarineOutputView` (`ui/view.py:28`) whose `.view` is a **settable
  property** (`ui/view.py:45-51`).
- Each session owns its port object (renderer, composer, modals) even though
  the underlying ST view may be shared — per-session state is naturally
  separated.
- Detach/reattach already exists: `registry.detach_session`
  (`core/registry.py:367-390`) moves a live session into
  `registry.background` viewless; `reveal_live_session`
  (`ui/session_list.py:547-610`) re-binds it to a fresh sheet and
  `OutputSheet.show()` repaints from `renderer.conversations`
  (`ui/sheet.py:87-91`, `repaint_from_state` at `ui/renderer.py:580-596`).
- QuickHost (`features/quick.py:231`) already runs one view / many sessions
  with surface swap (`_save_active_surface`, `_bind_session_to_host`,
  `:409-436`, `:629-708`) — the working recipe to generalize.

## Concepts

- **Host view**: the window's single output ST view. Created lazily on first
  session display; scratch, `submarine_output=True`, as today.
- **Bound session**: the one session currently attached to the host view.
  `registry.binding[host_view.id()]` → `by_agent[...]` → bound session
  (unchanged lookup shape for commands/keymaps — everything view-event-keyed
  follows automatically; see `docs/identity.md`).
- **Detached session**: every other live session in the window. Session
  object, bridge process, renderer state all alive; `output.view is None`;
  present in `registry.by_agent` but absent from `binding` ("background" is
  a derived predicate, not a separate map).

At any time: `window sessions = bound (0..1) + detached (0..N) + torn-off (0..N)`.

- **Torn-off session**: a live session that opted out of the host. It has
  its own sheet (tabs-mode behavior) until docked. `session.torn_off` is
  not written to `.sessions.json` and is cleared on single→tabs and on
  ST restart.

## Keep-alive rule (hard requirement)

**Detaching never stops anything.** Switching away from a session:

- never terminates or interrupts its bridge;
- never blocks its event stream — `OutputPort` calls while viewless append
  to the session's renderer state (`conversations`/`current`) and skip
  buffer writes (this headless path is what background sessions already use
  for `reveal_live_session`; if any renderer method writes the buffer
  unconditionally, gate it on `output.view is not None`);
- is exempt from auto-sleep while busy (already true: the sleep timer starts
  at turn end, but make the single-view detach path explicitly refuse to
  sleep a session whose `working`/`turn.active` is set);
- turn completion while detached sets per-session `unread` (see §6) and a
  status message; it never steals host focus.

Sleep/wake semantics are unchanged — sleep is an explicit or idle-timer
action on the session, orthogonal to binding.

## The swap (session list → host)

Moving the caret in the Sessions list onto a CURRENT live row previews
that session in the host and keeps list focus (`follow_current_under_caret`).
Enter (`open_row`) then focuses the host if the row is already bound;
otherwise it calls `HostView.attach(window, target)`:

1. **Save current surface** (bound session A): scroll position, caret, draft
   text + `_input_start`, input-mode flag, expanded tasks, pending modal
   descriptors (permission/plan/question). Store on the session (e.g.
   `session.surface` dict) — not on the view.
2. **Detach A**: `A.output.view = None`, `_input_mode=False`,
   `reset_phantoms_for_new_view()`, `registry.unbind(A.agent_id)`.
   Bridge untouched.
3. **Bind B**: `registry.bind(B.agent_id, host_view.id())`,
   `B.output.view = host_view`, `remember_active_session` (writes
   `submarine_active_agent`).
4. **Paint B**: `repaint_from_state()` (full reprojection from
   `conversations`), re-stamp identity/backend/theme on the view
   (`_persist_view_identity`), re-render pending modals from B's serialized
   modal state, restore B's surface (draft, scroll, input mode if idle).
5. Session list row for B gets the "bound" marker (e.g. `▸`); A's row shows
   its live status (`●` working / `!` unread / `?` input).

First attach when no host view exists: create it via the existing
`OutputSheet.show()` path. Resume of a *saved* (not live) session:
`create_session(...)` as today, but bind to host instead of a new sheet —
reuse `attach_view=` (`main.py:186-188`).

New session (`/new`, provider switch) in single mode: create + attach,
detaching the current one. No new sheet is ever created in this mode.

## Per-session state moves off the view

Anything currently read from view settings that is per-session must become
session state, mirrored to the host view only while bound:

- **unread**: today `submarine_unread` view setting (`ui/view.py:373`).
  Becomes `session.unread`; host view setting mirrors the bound session;
  session list reads the session (fixes detached-completion unread, which a
  shared view setting cannot represent).
- **tab title / provider label / backend / model stamps**: re-stamped on
  every attach (step 4). `submarine_session_id` on the host view always
  means "last bound session" — used by restart restore (§7).
- **phantoms**: `PhantomSet`s are per-view objects; each session's
  `SubmarineOutputView` keeps its own sets but they are constructed against
  the host view at bind time and torn down at detach
  (`clear_phantoms`/`reset_phantoms_for_new_view` already exist).
- **modal regions** (permission/plan/question, `ui/modals.py`): serialize
  the pending request on detach; re-render on attach. A permission request
  arriving while detached is **not** shown on the host (it belongs to
  another session's transcript) — the session list row shows `?`, plus a
  one-shot status message. User switches to answer.

## Input commands

Unchanged. All TextCommands resolve via
`get_session_for_view(view)` → `binding[view.id()]` → `by_agent[...]` →
bound session (see `docs/identity.md`). Keymap contexts
(`submarine_caret_in_draft` etc.) gate on the bound session's state.
`submarine_active_agent` window setting (was `submarine_active_view`)
points at the bound session's stable id.

## Busy detached sessions

- Events accumulate headlessly into the session's renderer; on re-attach,
  `repaint_from_state()` shows everything that happened while away. No
  catch-up protocol needed — the state is the buffer's source of truth.
- MCP/tools from a detached session: resolved by the identity redesign
  (`docs/identity.md`) — bridges receive `agent_id` (never `view_id`) in
  init params, so caller attribution is exact regardless of what the host
  view currently shows. The swap cannot invalidate a handle the bridge
  holds, because the bridge holds no runtime handle at all.
- Session list `collect_live` (`ui/session_list.py:273-278`) already falls
  back to `session_id` resolution for viewless rows — keep.

## Restart / reload

ST restores the scratch host view with its settings. On activation,
`_restore_session` (`ui/listeners.py:433-538`) matches
`submarine_session_id` = last bound session → restore bound as today.
Other previously-live sessions are *not* auto-restored (same as background
sessions today); they appear in HISTORY and resume on demand.

Guards: `on_activated`'s restore must not fire while a session is mapped
for the view (already the case) and must not fire mid-swap — swap runs
under the existing `submarine_creating_session` suppression flag
(`main.py:133-147`).

Close of the host view: existing close interception runs
(`ui/listeners.py:239-278`) — bound session detaches per
`keep_running_on_close`; host view is gone; next `open_row` recreates it.
Detached sessions are unaffected.

## Tear-off / dock

- Tear-off: detach the bound session, `reveal_live_session(..., force_sheet=True)`,
  mark `session.torn_off = True`, then attach the most-recent other
  window session (else host placeholder). Never stops the bridge.
- Dock: clear `torn_off`, `HostView.attach` onto the host, soft-close the
  standalone sheet.
- Session list: torn-off rows use `⊡` and open their own sheet (not the
  host). `t` tears off a bound row / docks a torn-off row.
- `ui_mode = "tabs"`: tear-off/dock are hidden no-ops.
- Closing a torn-off sheet follows the normal close intercept; the
  session stays torn-off and the next open recreates its sheet.

## Mode switching at runtime

- tabs → single: pick active session as bound, attach; detach every other
  session in the window and close their (now unbound) sheets. Nothing
  starts torn-off.
- single → tabs: clear every `torn_off` mark; for each detached session,
  run `reveal_live_session` to give it its own sheet. Host view stays as
  the bound session's sheet.

## What changes, by file

| File | Change |
|---|---|
| prerequisite | `docs/identity.md` lands first: registry `by_agent`/`binding`, agent_id-only bridge + MCP protocol, `submarine_active_agent` |
| `ui/host.py` (new) | `HostView` controller: `attach`, `detach_current`, surface save/restore, host view lifecycle, mode switch helpers |
| `core/session.py` | `session.unread`, `session.surface` dict; detach-refuses-while-busy guard for auto-sleep |
| `ui/session_list.py` | single-mode `open_row`/`reveal_row` → `HostView.attach`; bound-row marker; rows read per-session unread |
| `ui/listeners.py` | restore/close guards for host view; unread no longer view-keyed |
| `ui/view.py`, `ui/renderer.py`, `ui/modals.py` | ensure every buffer write is gated on `view is not None`; modal state serialize/re-render on detach/attach |
| `ui/sheet.py` | host view creation path reused; no per-session `new_file` in single mode |
| `main.py` | `create_session` single-mode branch (attach instead of new sheet); `ui_mode` setting read |
| `commands/session_cmds.py` | new/switch/restart flows route through host in single mode |
| `features/quick.py` | untouched — QuickHost keeps its own panel host |
| `Submarine.sublime-settings` | `"ui_mode": "single"` (default) or `"tabs"` |

## Tests

- Swap: bind A → attach B — A unbound (live in `by_agent`, absent from
  `binding`), bridge not stopped, registry lookups correct, commands
  resolve to B.
- Busy keep-alive: detach a working session; feed bridge events; assert
  conversations grew, no buffer writes attempted, no sleep; re-attach →
  `repaint_from_state` called with full state.
- Unread: detached completion sets `session.unread`, appears in list row,
  cleared on attach.
- Modal: permission request while detached → `?` row + no host phantom;
  on attach the modal renders.
- Surface: draft/scroll/input-mode round-trip across two swaps.
- MCP: tool call from detached session attributes to its `agent_id`, not
  the bound session.
- Mode switch both directions with 3 sessions (one busy, one sleeping).
- Guard test stays green: no new per-session `new_file` call in single mode.

## Risks

- **Renderer leakage**: any `TurnRenderer`/phantom code path that writes
  without checking `view` will crash or paint into the wrong transcript.
  Audit + the headless test above.
- **`Conversation.region` offsets** are buffer offsets — only valid while
  bound; never read them from a detached session. `repaint_from_state`
  recomputes on attach, so this holds if no detached path touches regions.
- **Modal phantoms** are the least swap-tested surface (QuickHost never had
  them); the serialize/re-render step needs the most test care.
