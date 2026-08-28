# Agent artifacts

Status: design. Not yet implemented.

Inspired by Antigravity's artifacts: the agent owns a file cache; reports
are written there once and referenced everywhere — never printed twice.

## The problem

- Agents report by printing into the session transcript. Long reports
  blow context (theirs and the parent's) and are gone from accurate reach
  the moment anything else renders.
- `read_session_output` (`mcp/socket_server.py:1361`) reads the *rendered
  buffer*: tail-biased truncation at 30k chars, no offset/paging, glyphs
  and elisions included, dead when the view is detached. A parent that
  needs "section 3 of the worker's analysis" cannot get it.
- Agents that want to do it properly today must print the report *and*
  write the same text to a file — duplicated effort, duplicated tokens.

## Principle

**The artifact write IS the output.** The agent writes the report once,
into the store. The session transcript gets a compact *artifact card* —
title, path, size, one-line summary — not a copy of the content. Anything
that needs the content (user, parent agent, the writer itself later)
reads the file with accurate ranges. Printing the report text into the
transcript *as well* is an anti-pattern the tool docs actively steer
agents away from.

## Store

```
~/.submarine/artifacts/
  index.json                    # MRU index, like core/records.py
  <owner_agent_id>/
    <slug>.md                   # the artifact
    <slug>.journal.jsonl        # append-only change journal
```

- Root follows the `~/.submarine` convention (`plat/constants.py:20`).
- Slug: tool-provided name, sanitized; collision → `-2`, `-3`.
- `index.json`: `[{path, owner, name, title, created, updated, bytes,
  summary}]`, MRU-front, cap 500. Maintained via `plat.jsonio` helpers.
- Artifacts survive their session. Ownership is an `agent_id`; the index
  also records the originating `session_id` for traceability.

## Writing (tool or convention — both journal)

### Tool: `write_artifact`

```
write_artifact(name, content, mode="write"|"append",
               title=None, summary=None, auto_open=None) -> {path, bytes}
```

- Caller identity via `_caller_agent_id` injection (add to
  `CALLER_INJECT_TOOLS`, `mcp/tools.py:794`) — the agent never names
  itself.
- Host side: `MCPSocketServer._write_artifact` → `core.artifacts.write()`
  → journal → index upsert → emit artifact-card event into the caller's
  session transcript → optional auto-open.
- Response is `{path, bytes}` — deliberately not the content.

## Editing (artifacts are plain files — edit them like files)

An artifact is not a write-once blob. Three modification tiers, all
journaled through the same `record_write` path:

### Tier 1 — native file tools (preferred)

Artifacts are ordinary files at known absolute paths. An agent with
Edit/MultiEdit/Write (claude, codex, kimi, grok all have them) edits the
artifact exactly like project code: surgical `Edit(old, new)`, regex
tools, whatever it already has. The `_acp_fs_write` root hook journals
whole-file writes; a save watcher (below) journals everything else.
**The artifact tools do not try to replace the agent's editor.**

### Tier 2 — `edit_artifact` (for MCP-only callers and precise ops)

Spawned subsessions that talk to the host purely over MCP have no partial
file edit — ACP `fs/write_text_file` rewrites whole files, which is
wasteful for a small fix in a 40 KB report. So:

```
edit_artifact(path, op, ...) -> {path, bytes, op}
  op="replace_text"   old, new                 # unique match, fails on 0 or 2+
  op="replace_range"  offset, length, new      # byte-exact, pairs with read_artifact offsets
  op="insert_at"      offset, new
  op="replace_section" heading, new            # markdown: swap body under ## heading
  op="append"         new                      # (also write_artifact mode="append")
  op="delete_range"   offset, length
  note=None                                    # one-liner into the journal
```

- All ops are journaled as `"edit"` with the note; byte counts and sha
  recorded. `replace_text` failing on ambiguity is a feature — the agent
  must re-read and be precise, same contract as CLI Edit tools.
- `replace_range` composes with `read_artifact(path, offset, limit)`:
  read to find the span, edit by the same coordinates. No guessing.

### Tier 3 — user edits

The user edits the open artifact buffer directly; the on-save listener
journals `{"op": "external", "agent_id": "user"}` (see Journal). Agents
see these in `read_artifact`'s `journal_tail` and must not silently
overwrite user edits — tool docs say: if `journal_tail` shows external
edits after your last write, re-read before rewriting.

### Convention: direct fs write under the root

Agents may also write `~/.submarine/artifacts/<own_agent_id>/<name>.md`
directly (ACP `fs/write_text_file` already allows absolute paths,
`bridge/acp/fs.py:152`). `_acp_fs_write` gains a root-prefix check: writes
under the artifact root are journaled and produce the same card event.
Tool and convention converge on `core.artifacts.record_write()` — one
code path, no behavioral fork.

## The artifact card (transcript rendering)

New renderer event kind (`ui/renderer.py` + `ui/models.py`), rendered as a
one-line row:

```
📄 walkthrough.md — 4.2 KB — auth flow analysis        [open] [path]
```

- Carries hrefs like existing phantom links (`ui/view.py:1088-1111`
  pattern): `open` → open in ST; `path` → copy absolute path.
- State, not chrome: the card is a `Conversation` event, so it survives
  detach/reattach, single-view swaps, and `repaint_from_state` for free.
  No phantoms required.
- The card text carries the agent-supplied `summary` (one line) so the
  transcript alone still tells the user what the artifact *is*.

## Opening

- **Auto-open**: setting `artifacts_auto_open`: `"first"` (default —
  auto-open the first artifact a session creates, later ones stay
  closed), `"always"`, `"never"`. Per-call `auto_open=` overrides.
  Open read-write (users may annotate; see journal), word-wrapped, via the
  `_open_plan_file` pattern (`ui/modals.py:830-852`), in the last session
  split (`core/placement`).
- **Manual**: card `open` link; command palette `Submarine: Artifacts…` →
  quick panel over `index.json` (all owners, MRU); session-list row
  action later if wanted.

## Journal

Per-artifact append-only JSONL:

```json
{"ts": 1788…, "agent_id": "agent-…", "op": "create|rewrite|append|external", "bytes": 4300, "sha": "…", "note": "…"}
```

- Written by `core.artifacts.record_write` for tool writes, convention
  writes, and appends.
- `external`: an on-save listener (`ui/listeners.py`) on open artifact
  views journals *user* edits with `agent_id: "user"` — the agent can then
  see the user annotated its report (readable via `read_artifact` —
  content is the file; journal tail is included in `read_artifact` meta
  so agents know it changed since their write).
- No content snapshots in the journal (events only, sha for integrity);
  the file itself is current truth. Keeps the journal cheap.

## Reading (accurate ranges — the read_session replacement)

```
read_artifact(path, offset=0, limit=20000) ->
    {content, offset, total_bytes, truncated, journal_tail}
list_artifacts(scope="self"|"all") -> [index entries]
```

- Byte-accurate offset/limit paging — the exact thing
  `read_session_output` cannot do. Tool docs say: prefer artifact paths
  over `read_session_output` for anything longer than a screen.
- Any agent can read any artifact (flat trust, same as fs read today);
  `list_artifacts(scope="self")` is the default discovery view.

## Inter-agent flow (the payoff)

Parent spawns worker with: "…write the full analysis to an artifact;
reply with the path and a 3-line summary."

1. Worker: `write_artifact("auth-analysis", …)` → `{path}`.
2. Worker: `signal_complete("done: auth-analysis at
   ~/.submarine/artifacts/agent-x/auth-analysis.md — <3-line summary>")`.
   (Payload rides `result_summary` today — no socket changes needed.
   Structured `artifacts=[paths]` on `signal_complete` /
   `wait_for_subsession` wake text (`_build_wake`,
   `socket_server.py:1931`) is a later refinement.)
3. Parent: `read_artifact(path, offset=…)` — precise sections, no
   transcript scraping, no 30k tail lottery.

The parent's transcript holds two compact rows (📄 card, 📬 wake) instead
of two full report dumps.

## File map

| File | Change |
|---|---|
| `core/artifacts.py` (new, sublime-free) | store write/read/list, edit ops (replace_text/range/section, insert, delete), slugging, index MRU, journal, sha; like `core/records.py` |
| `mcp/tools.py` | TOOL_TABLE: `write_artifact`, `edit_artifact`, `read_artifact`, `list_artifacts` + codegens + `CALLER_INJECT_TOOLS`; tool docs steer away from print-then-write and toward native Edit for surgical changes |
| `mcp/socket_server.py` | `_write_artifact`, `_edit_artifact`, `_read_artifact`, `_list_artifacts`; exec-globals rows; card event emission via the caller session's OutputPort |
| `bridge/acp/fs.py` | `_acp_fs_write` root-prefix hook → `record_write` (convention path) |
| `core/ports.py` | `OutputPort.artifact_card(...)` (headless-safe: state always, chrome when bound) |
| `ui/renderer.py`, `ui/models.py` | card event kind + rendering + href handling |
| `ui/listeners.py` | on-save journal for open artifact views (`external` op) |
| `commands/ui_cmds.py` | `SubmarineArtifactsCommand` quick panel |
| `Default.sublime-commands` | palette entry |
| `Submarine.sublime-settings` | `artifacts_auto_open`, `artifacts_index_cap` |
| `plat/constants.py` | `ARTIFACTS_DIR` |

## Tests

- `core/artifacts`: write/create/append/rewrite, slug collision, index
  MRU + cap, journal lines per op, ranged read offsets, sha integrity.
- Edit ops: replace_text unique-match (fails on 0 and on 2+), range/insert/
  delete byte math vs read offsets, markdown section swap (heading levels,
  missing heading), note lands in journal.
- MCP: caller injection (owner is always the caller), card event emitted
  to the *calling* session (agent_id routing, not bound-view), read/list
  round-trip.
- Convention path: fs write under root journals + cards; outside root
  untouched.
- Renderer: card is a Conversation event — survives detach → fast-path
  swap → reattach (single-view law); headless accumulate works.
- Auto-open: "first" opens once per session; "never" never; per-call
  override wins.
- User edit: on-save journals `external`.
- Guard: `tests/test_removed_features.py` stays green; no `view_id`
  identity anywhere (owner = agent_id).

## Deliberate non-goals

- No content versioning/snapshots (journal is events only).
- No cross-machine sync; store is local.
- No artifact *types* schema (plan/walkthrough/task-list) in v1 — `title`
  and `summary` are free text; typing can come when usage proves the
  categories.
- No cleanup daemon; index cap + manual delete for now.
