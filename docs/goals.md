# Goal harness

Host-owned, backend-agnostic work loop. The user types `/goal`; the plugin
owns planning, continuation, verification, budgets, and complete/unlock. The
model only *claims* via MCP `update_goal` / `goal_verdict`.

`/goal` is **never forwarded** to the engine (would double-drive Grok’s native
harness). Quick Agent sessions reject `/goal` and both MCP tools.

Palette: **Submarine: Goal Status / Pause / Resume / Clear**.
Devtools: `python3 submarine_devtools.py goal … --view-id N`.

---

## Protocol

```
/goal <objective> [--budget N]
/goal status | pause | resume | clear
```

`/goal` and `/goal edit` print status (no query). Trailing `--budget` must be a
positive integer.

```
         /goal <obj>
              │
              v
        [planning] ──── planner writes plan.md
              │ accept_plan (quality + sections + ≥2 AC / verification / checklist)
              v
        [executing] ←── continuation (max 24)
              │ update_goal(completed=true) + preflight
              v
        [verifying] ── goal_verdict / evidence/VERDICT.json
              │
       achieved? ──yes──► [complete]  (also cancels /loop banners)
              │ no (gaps, cap not hit)
              v
        [executing]
```

Plan path: `{project}/.claude/goals/{goal_id}/plan.md`. Accept also writes
`plan.baseline.md`. After accept, disk may only flip checklist `[x]` marks —
rewriting Acceptance criteria / Verification plan is ignored.

---

## Statuses and phases

| Status | Meaning |
|--------|---------|
| `cleared` | No goal (initial / after clear) |
| `active` | Open, host may continue |
| `user_paused` | Esc, `/goal pause`, verify/continue cap, or restore-from-disk |
| `blocked` | 3× `blocked_reason` (`BLOCKED_STREAK_TO_PAUSE`) |
| `budget_limited` | `tokens_used >= token_budget` |
| `infra_paused` | reserved (`pause("infra")`) |
| `complete` | Verified; in-memory plan dropped |

**Phases:** `idle` | `planning` | `executing` | `verifying`.

Resume → planning if no accepted plan, else executing. Esc while active pauses
and skips the next auto-continue / re-plan.

The sticky strip uses `◆` (never `◎`) so it does not collide with the composer.

---

## Plan contract

Required `##` headings (case-insensitive):

- **Acceptance criteria** — ≥2 numbered/bulleted criteria, at least one
  concrete artifact (path / command / test / UI)
- **Verification plan** — ≥2 checks; ≥1 must be substantive (`pytest` / `rg` /
  command exit — not only `test -f` on a file the implementer just wrote)
- **Task checklist** — ≥2 `- [ ]` / `- [x]` items (item *text* frozen at accept)

Optional: Goal kind, Non-goals, Assumed scope, Implementation approach.

`## Goal kind` first token wins for visual-vs-code classification
(`visual` / `render` / `gfx` / `graphics` / `screenshot` / `capture`). An
explicit non-visual kind (e.g. `code-change`) beats keyword soup in the
objective.

Generic host-template plans and objective-only restatements are rejected.

---

## Model tools

```
update_goal(message=str, completed=bool, blocked_reason=str)
goal_verdict(achieved=bool, evidence=[str]|str, gaps=[str]|str, message=str)
```

- Without an open `/goal`, complete/blocked are **rejected** (no invented sticky).
- `completed=true` is deferred to turn end, then host preflight runs
  (open checklist, partial/deferral language, thinned contract vs baseline).
- Skeptic / verify turns must use `goal_verdict`, not `update_goal(completed=true)`.
- `goal_verdict` is valid only while `phase=verifying`.
- Unlock is structured only: `goal_verdict` or `evidence/VERDICT.json` next to
  `plan.md`. Prose cannot complete the goal.
- Fail-closed: no verdict → not achieved.

Default skeptic mode is **task**: the main sheet stays the executor and must
spawn one Task/Agent reviewer. Legacy “open a skeptic session sheet” is gone.

---

## Evidence (host re-validates `achieved=true`)

`validate_evidence_for_achieved` is fail-closed:

- `evidence[]` must be non-empty and cite **on-disk** files (logs / captures
  under `evidence/`, or paths the host can resolve under cwd / plan dir).
- Narrative-only lines (`Structure:…`, `API:…`) do not count.
- Empty / missing / tiny files do not count (logs ≥40 bytes, images ≥200).
- Visual/render goals need ≥1 image capture **and** ≥2 grounded lines —
  green unit tests alone fail.
- Residual/stub language in `message` while `gaps=[]` demotes the claim
  (“non-blocker”, “later”, “v1 scope”, …).
- Technique-name theater (Hi-Z / froxel / SSR / …) without capture/log proof
  is rejected.

---

## Budgets

- Optional `/goal … --budget N` (token-ish). Baseline is session
  `context_k * 1000` at create.
- Enforced at turn end → `budget_limited`.
- Continue cap: 24 implementer continuations without verified complete.
- Verify cap: 5 (`DEFAULT_VERIFY_MAX`). Hitting it pauses the goal.

---

## Persistence (new)

Goal state is written into `.sessions.json` on session save
(`entry["goal"] = GoalTracker.to_json()`). On restore / restart,
`from_json` remaps a mid-flight `active` goal to `user_paused` with
“Restored from disk — /goal resume to continue.” Plan files on disk
(`plan.md` + `plan.baseline.md`) stay; `/goal clear` wipes the tracker
but leaves the plan files.

---

## Pause / resume / clear

| Action | Effect |
|--------|--------|
| `/goal pause` or Esc | `user_paused`; no auto-continue |
| `/goal resume` | Planning kickoff or implementer recap |
| `/goal clear` | Wipe tracker (plan path printed; file left on disk) |
| Palette Goal * | Same four actions |

Verified complete also cancels any `/loop` banner on that session.
