# Submarine web UI — manual

A browser front end for the same session-control socket `submarine_sessions`
uses: list the sessions a running Sublime is holding, read a transcript, send a
prompt, cancel the current turn. Start it where the CLI runs and open
`http://<host>:8787/`.

```
python3 submarine_web.py [--host 0.0.0.0] [--port 8787] [--token S]
python3 -m features.webui [same options]
```

---

## 1. What it shares with the CLI

Nothing is re-implemented for the browser. The UI is a **second client of the
same socket**, exactly like the CLI:

| Layer | Where it lives | Shared? |
|---|---|---|
| Socket transport | `features/sessions_cli.py` (`send`, `call`) | yes — imported by the UI |
| Actions | `features/session_control.py` (`op:"sessions"`) | yes — every button is one of its four actions |
| Server side | `mcp/socket_server.py` routes the op on the plugin's main thread | unchanged |
| Web layer | `features/webui/` (this feature) | new, HTTP only |

So there is no second sessions stack and no path that bypasses the plugin: what
the UI can do, the CLI can do, and both see the same registry, store and
transcript files. Consequences worth knowing:

- **Sublime must be running with Submarine loaded.** The UI does not spawn,
  resume or fork anything by itself; the socket has to be up.
- **`REF` semantics are the CLI's**: an agent id, a session id, or a unique
  session name. Ids win; a name that matches several sessions is refused
  (`ambiguous`) rather than guessed.
- **The UI never evaluates code.** It only uses `op:"sessions"`, not the
  `{"code": …}` namespace that shares the socket.

The caller stamp is `{"kind": "webui", "name": "web UI"}`, so the plugin's audit
log names the surface that acted; the target sheet's prompt line only carries
the mark (`◎ 📨 <prompt> ▶`).

## 2. Run it

The CLI is a script in the plugin tree and so is this one:

```bash
python3 /path/to/submarine/submarine_web.py
```

Optional symlink, next to the other project CLIs:

```bash
ln -s /path/to/submarine/submarine_web.py ~/.local/bin/submarine_web
```

The shim resolves its own symlink, so it works from any directory. Any
Python 3.8+ runs it — the same floor as the plugin's own test suite.

Startup banner:

```
Submarine web UI
  local     http://127.0.0.1:8787/
  all       http://0.0.0.0:8787/  (bound to every interface)
  reachable http://192.168.1.20:8787/
  socket    /var/folders/…/T/submarine_mcp.sock
  token     off — anyone who can reach this port can read and prompt your sessions
Ctrl-C to stop.
```

| Option | Default | Meaning |
|---|---|---|
| `--host` | `0.0.0.0` | bind address — every interface, so a browser on another machine can reach it |
| `--port` | `8787` | TCP port (`--port 0` picks a free one) |
| `--socket` | `$TMPDIR/submarine_mcp.sock` | plugin socket path override |
| `--token` | `$SUBMARINE_WEB_TOKEN` | shared secret for every `/api` route |
| `--quiet` | off | no banner |
| `--verbose` | off | log every request; by default only failures are logged, because the console polls |

## 3. Security

Binding `0.0.0.0` means **anyone who can reach the port can list your sessions,
read their transcripts, and prompt them** — a prompt is arbitrary work on your
machine, run with your agent's permissions. Two things matter:

- Use `--token secret` when the machine is on a shared network. Every `/api`
  route then needs the token, as `X-Submarine-Token: secret` (what the page
  sends) or `?token=secret` (handy for a shared link — the page remembers it in
  `localStorage` and drops it from the URL). The static files themselves are not
  gated: the page holds no data, only the API calls do. Without a token, the
  banner says so.
- Otherwise bind the loopback interface explicitly (`--host 127.0.0.1`) and use
  an SSH tunnel from wherever you want to browse.

There is no TLS: the token travels in clear text on the wire. Treat it as a
LAN-only convenience, not as authentication over the internet.

## 4. HTTP API

The API is the CLI with HTTP framing: one action per route, the plugin's JSON
envelope passed through with an `http` field added, and the status taken from
the envelope's `data.code` (`bad_request` 400, `not_found` 404, `ambiguous` /
`busy` / `no_view` / `no_session` 409, `transcript` / `internal` 500, anything
without a code — the socket is missing — 503).

| Route | Action | Arguments |
|---|---|---|
| `GET /api/health` | — | none; reports the socket path and whether it exists |
| `GET /api/list` | `list` | `scope` (`all` \| `window` \| `children`), `parent`, `window` |
| `GET /api/view` | `view` | `ref` (required), `mode` (`tail` \| `text` \| `edits`), `turns`, `max_chars`, `offset`, `limit`, `file_path` |
| `POST /api/chat` | `chat` | `{"ref", "prompt", "queue", "idem", "display"}` |
| `POST /api/interrupt` | `interrupt` | `{"ref"}` |
| `GET /api/pending` | `pending` | `ref` (required); the question / permission / plan the sheet is waiting on |
| `POST /api/answer` | `answer` | `{"ref", "kind": question\|permission\|plan, "option"\|"options"\|"text"\|"response", "qid"\|"id"}` |
| `GET /api/backends` | `backends` | none; backends (availability, model aliases) and windows (id, project, session count) |
| `POST /api/create` | `create` | `{"backend", "model", "name", "window"\|"project", "prompt", "idem"}` |
| `POST /api/rename` | `rename` | `{"ref", "name"}` |
| `POST /api/close` | `close` | `{"ref", "remove"}` — stop a live session; `remove` drops a history row |

```bash
curl -s localhost:8787/api/list | jq '.data.count'
curl -s "localhost:8787/api/view?ref=GUEST&turns=2" | jq '.data.turns[-1].reply'
curl -s -X POST localhost:8787/api/chat \
     -H 'Content-Type: application/json' \
     -d '{"ref":"GUEST","prompt":"run the tests"}'
curl -s -X POST localhost:8787/api/interrupt -d '{"ref":"GUEST"}'
```

`chat` always carries `queue:"queue"` (the console has no policy picker; the API still takes one), and each send gets
a fresh `idem` key so a retried browser request cannot double-send. The UI does
not use `wait` (`chat --wait`): it polls instead, see below.

## 5. The console

- **Left**: the Sublime *Sessions* list, not the store. **Current** sessions
  (live, plus saved rows still `open`/`sleeping`) come first, grouped by the
  window that owns them — headed by the project folder's name, with the window
  id and full path beside it; click a header to fold the group. Inside a group
  the order is what needs you first, as in Sublime: `?` a question, permission
  or plan waiting, `!` unread, `●` working (pulsing), `○` idle, `⏸` sleeping,
  then most recent; children sit under their parent with `↳`, and the
  host-bound session carries `▸`. A row is the name, age, backend · model,
  `Q` count, the turn phase when it is not idle, a context-usage bar when the
  plugin reports one, and a yellow *asks you a question* / *wants permission*
  line when the sheet is waiting. Ids are off the rows (the header shows them
  for the open session; a row's tooltip has them too). **History** — closed
  rows — is a folded section below, opened on demand (remembered per browser)
  and paged 40 at a time. The **search box** filters everything by name,
  project, backend, model, id or state, searches history without opening it,
  keeps a matching parent's children, `Enter` opens the first hit, `Esc`
  clears. **History** rows group by project too (the saved row's folder),
  most recent group first.
- **＋ on a window's band** opens a form for a session *in that window*:
  backend (unavailable ones greyed), model alias, a name and an optional first
  prompt. A Sublime window with no current session still gets a band, so it
  can be started from. The session starts viewless — in the list,
  not on the host sheet — and the console opens it; the prompt is delivered
  once the bridge is up, like `chat` to a sleeping session.
- **Rename / Close** sit under the selected session's header. Close stops a
  live session the way Cmd+W does (host hands off to a peer; the history row
  stays); on a history row the button reads **Delete** and drops the row.
  Both confirm first.
- **Right**: the selected session's header (state, turn phase, backend, model,
  age, view, project, window, ids) and three read modes, mirroring `view --mode`:

| Tab | `mode` | Reads | Needs a sheet |
|---|---|---|---|
| Sheet (default) | `text`, else `tail` | the rendered output view; without one, the last N turns rebuilt in the sheet's grammar | no — it falls back |
| Transcript | `tail` | the last N turns from the backend transcript, in the sheet's grammar | no — closed sessions work |
| Edits | `edits` | the Edit/Write rows this session made | no |

### The sheet

The session pane **is the Sublime sheet**. Whatever the tab, the text is drawn
by a port of `SubmarineOutput.sublime-syntax` with the `SubmarineOutput`
tmTheme colours (`static/highlight.js`, `static/style.css`): `◎ prompt ▶` in
prompt blue, `⚙` / `✔` / `✘` / `│` tool lines, `@done(model)`, goal and task
strips, markdown (headings, bold, inline code, links, lists, quotes) and fenced
code with the embedded languages' comment / string / number / keyword colours,
plus `diff` blocks. The list says which sessions own a sheet (in single mode
only the host-bound one does); every other session gets the same grammar
rebuilt from its transcript — `◎ prompt ▶`, one `⚙ Tool ×N` per run of a tool,
the reply, `@done` — and the toolbar says `no sheet — transcript`.

The text sits in a **CodeMirror 6 editor** (`static/editor.js`), read-only:

| | |
|---|---|
| Fold / unfold a turn | the `▾` in the gutter on each `◎` line, `Cmd/Ctrl-Alt-[` / `]`, or **Fold all** / **Unfold** in the toolbar — the same per-turn fold the Sublime sheet has |
| Search | `Cmd/Ctrl-F` or **Find**: CodeMirror's panel with regexp / case / whole-word, `Enter` / `Shift-Enter` to step |
| Follow the tail | while a turn is running the pane refreshes every 1.5s; if you were at the end it stays at the end, if you had scrolled up it leaves you there. **⤓ End** jumps to the tail |
| Selection | real text selection with matching-word highlights, so a path or an id copies cleanly |

Refreshes are applied as the smallest edit between the old and the new text,
so folds and the scroll position survive a growing reply.

The **composer is a CodeMirror editor too**: multi-line with undo/redo,
`Enter` sends (`Shift+Enter` newline, `Cmd/Ctrl+Enter` always sends, `Esc`
leaves the box), and a draft is highlighted with the same grammar — a fenced
block you paste reads as one.

CodeMirror comes from **esm.sh** through an import map in `index.html` that
pins every `@codemirror/*` package and externalises their shared dependencies,
so the page holds one `@codemirror/state` (two copies is the classic CM6 CDN
failure). **Offline, nothing breaks**: `editor.js` reports the CDN as
unavailable, the sheet becomes a `<pre>` painted by the same tokenizer, and the
composer is the plain textarea — folding and search are the only things you
lose. No build step either way.

- **Waiting on you**: when the session's sheet is showing a question
  (`AskUserQuestion`), a permission prompt or a plan approval, a card appears
  between the sheet and the composer with the same choices the sheet has —
  numbered options with their descriptions (tap one; a multi-select question
  gets checkboxes and **Confirm**), an *Other…* field for a free-text answer,
  `Y` Allow / `N` Deny / `S` Session / `A` Always for a permission (with the
  command or path it asks about, the rest of the input under *more fields*),
  Approve / Reject for a plan. With the sheet focused the Sublime keys work
  too: `1`–`9`, `Y`/`N`/`S`/`A`. An answer goes through `POST /api/answer`,
  which resolves the sheet's own callback (the `☑ Header → answer` line lands
  in the transcript), is guarded by the request id so a stale card cannot
  answer the wrong prompt, and the card moves to the next question or
  disappears. The list marks such a session `?` and floats it to the top; on
  a phone the composer steps aside while a card is up.
- **Compose**: prompt (see *The sheet* above for the editor) and **Send**
  (Enter; Shift+Enter newlines); a prompt sent mid-turn queues behind the
  turn, as it does in Sublime — there is no policy to pick — and
  **Interrupt** (enabled only while the selected session is `working`; on a
  phone it appears only then): the same cancel as `interrupt`.
- Send is refused for saved rows: `chat` needs a live session (a sleeping one is
  woken by the plugin's own hand-off, and the console says so).

### On a phone (≤ 760px)

The two panes become one at a time, the way the rest of the phone navigates —
a list you tap into, and a way back out:

- **The list is the whole screen** to start with (the page title, connection badge and ⟳ live in its head — there is no separate top bar); tapping a session swaps in
  the transcript and compose full-screen. The tapped row stays marked, so
  coming back shows where you were.
- **The open session lives in the URL** as `#s=<ref>`, so a session is a link:
  a bookmarked or sent URL opens straight into it. The browser's back gesture
  (or the Android back button) returns to the list, and the **← Sessions**
  button in the detail header is the same move — it pops the entry the tap
  pushed, so both routes stay in step. Going *forward* reopens the session.
- **Compose is pinned to the bottom**, under a transcript that scrolls. On a
  phone Enter inserts a newline and the **Send** button delivers, so a soft
  keyboard cannot send a half-typed prompt by accident; wide screens keep
  Enter-to-send and `Shift+Enter` for newlines. The prompt box starts one line
  tall and grows with the draft, so a long prompt does not eat the transcript.
- **Safe areas and the keyboard**: the viewport is `viewport-fit=cover` and the
  header and compose use `env(safe-area-inset-*)`, so the notch and the home
  indicator do not sit over a control. The app height follows
  `visualViewport.height`, because a soft keyboard shrinks the visual viewport
  and not the layout one — without that the compose row would be under the
  keys.
- **Tap targets** — rows, tabs, the back control and the compose buttons — are
  at least 44px tall, and the compose controls stack at true phone widths.
- **The sheet gets the screen.** In the detail pane the top bar is gone (⟳ in
  the toolbar reloads), the header is one line of name + state, the id chips
  are dropped, the toolbar is one row of tabs + **⤓ End**, and a thin caption
  says what the pane shows with **+8 older turns** in place of the number
  field. While the composer has focus the header, toolbar and caption step
  aside too, so above the keyboard you still see what you are replying to.
- **Folding by touch**: tap a `◎` line to fold or unfold that turn; the
  gutter arrow is a bigger target as well.

### Any touch screen (iPad included)

Width is not the only thing that matters: `(pointer: coarse)` — a finger on an
iPad in the two-pane layout — gets 40px+ controls, a wider fold gutter, a
larger sheet font, tap-to-fold on `◎` lines, and **Enter as a newline** in the
composer (`⌘/Ctrl+Enter` or **Send** deliver), because a soft keyboard's
return key must not send half a prompt. Between 761px and 1000px the toolbar
drops *Fold all* / *Unfold* and the source label; the Sheet tab shows `·T`
when it is really the transcript.

Everything above 760px is untouched: same two-pane layout, same Enter-to-send,
same header. The socket, the API and the polling described here are identical
on both.

### Why it polls instead of streaming

The plugin socket is one request and one reply per connection, with no push
channel — the CLI's `chat --wait` is already "send, then ask again". The console
does the same thing on a timer: `/api/list` every 5s, plus `/api/view` every
1.5s
**while the selected session is working**, and it backs off to idle polling as
soon as the turn ends. So a long reply appears in Transcript once the backend
has written it to the transcript file; the Sheet tab shows the rendered output
view, which is where mid-turn text shows up first for a live session; the
sheet editor keeps your place (or the tail) across those refreshes.

A sleeping target is a special case: `chat` wakes it and only then answers, once
the plugin's hand-off has waited out the bridge init (that wait has a ~30s
ceiling and runs off the editor's main thread). So the reply can take a few
seconds and the console says `woke the session…` instead of treating the delay
as a failure. If the bridge never comes up, the plugin returns `ok` **with** an
`error` string — the console reports that and keeps the prompt text so it can be
retried; it never claims a delivery that did not happen.

## 6. Files

```
features/webui/
├── __init__.py      exports for the package
├── client.py        op:"sessions" over features/sessions_cli's transport
├── server.py        ThreadingHTTPServer: routes, status mapping, token gate
├── cli.py           argument parsing + startup banner
├── __main__.py      `python3 -m features.webui`
└── static/
    ├── index.html   the console + the CodeMirror import map
    ├── app.js       list, polling, actions, which text the pane shows
    ├── highlight.js the sheet tokenizer (port of SubmarineOutput.sublime-syntax)
    ├── editor.js    CodeMirror sheet + composer (ES module; optional at runtime)
    └── style.css    layout, phone rules, the tmTheme scopes as classes
submarine_web.py     shim: `python3 submarine_web.py`
tests/test_webui.py            the HTTP layer against a fake client (no Sublime needed)
tests/test_webui_highlight.py  the tokenizer under node (skipped without node)
```
