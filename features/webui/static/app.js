'use strict';

// Console for the plugin socket. Everything it renders comes from /api/*,
// which is `submarine_sessions` with HTTP framing (see features/webui/server.py).
// The socket is request/response with no push channel, so "streaming" here is
// polling: while a turn is running, /api/view is re-fetched to pick up the
// reply as the transcript grows.
//
// The session pane is the Sublime sheet: the live sheet text when the session
// owns a view, else the same grammar rebuilt from the transcript. It is drawn
// by CodeMirror (editor.js: folds per ◎ turn, search, selection) with
// highlight.js providing the sheet's syntax colours; without the CDN the same
// tokenizer paints a <pre> and the composer is a textarea.

const TOKEN_KEY = 'submarine_web_token';
const POLL_WORKING_MS = 1500;
const POLL_IDLE_MS = 5000;
const PENDING_GIVE_UP_MS = 45000;

const $ = (id) => document.getElementById(id);

// Under 761px the page shows one pane at a time (list, then detail), matching
// the CSS; wider than that both panes are visible and the pane state is inert.
const NARROW = window.matchMedia('(max-width: 760px)');
// A touch screen at any width (phone, iPad): Enter is a newline there, tap
// targets grow, and a ◎ line toggles its fold — see style.css and editor.js.
const COARSE = window.matchMedia('(pointer: coarse)');

const EDITOR_WAIT_MS = 6000;

const state = {
  sessions: [],
  ref: null,          // agent id / session id / unique name
  mode: 'sheet',      // sheet | tail | edits
  working: false,
  pending: false,     // a prompt of ours is in flight
  sawWorking: false,
  pendingSince: 0,
  timer: null,
  lastState: null,
  modal: null,        // the pending modal body from /api/pending, if any
  editor: undefined,  // CodeMirror API from editor.js, null when unavailable
  sheet: null,        // the mounted sheet (CodeMirror or <pre>)
  composer: null,     // the CodeMirror composer, when the editor loaded
  source: '',         // what the pane shows: live sheet / transcript
};

// editor.js settles this with the CodeMirror API or null; offline it never
// settles, so the wait is capped and the plain sheet takes over.
const editorReady = Promise.race([
  window.SubmarineEditor ? window.SubmarineEditor.ready : Promise.resolve(null),
  new Promise((r) => setTimeout(() => r(null), EDITOR_WAIT_MS)),
]).then((api) => { state.editor = api || null; return state.editor; });

// ── token ────────────────────────────────────────────────────────────────────
// A link with ?token=… keeps working: remember it, then drop it from the URL.
(function rememberToken() {
  const t = new URLSearchParams(location.search).get('token');
  if (t) {
    try { localStorage.setItem(TOKEN_KEY, t); } catch (e) { /* private mode */ }
    history.replaceState(null, '', location.pathname + location.hash);
  }
})();

function token() {
  try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
}

async function api(path, opts) {
  const o = opts || {};
  const headers = Object.assign({}, o.headers || {});
  const t = token();
  if (t) headers['X-Submarine-Token'] = t;
  if (o.body !== undefined) headers['Content-Type'] = 'application/json';
  let res;
  try {
    res = await fetch(path, { method: o.method || 'GET', headers: headers, body: o.body });
  } catch (e) {
    return { ok: false, error: 'request failed: ' + e.message };
  }
  let body;
  try {
    body = await res.json();
  } catch (e) {
    return { ok: false, error: 'non-JSON reply (HTTP ' + res.status + ')' };
  }
  if (!body || typeof body !== 'object') return { ok: false, error: 'empty reply' };
  if (body.ok !== true && !body.error) body.error = 'HTTP ' + res.status;
  return body;
}

// ── small helpers ───────────────────────────────────────────────────────────

function esc(value) {
  return String(value === null || value === undefined ? '' : value).replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
  );
}

function ago(ts) {
  if (!ts) return '–';
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)));
  if (secs < 60) return secs + 's';
  if (secs < 3600) return Math.floor(secs / 60) + 'm';
  if (secs < 86400) return Math.floor(secs / 3600) + 'h';
  return Math.floor(secs / 86400) + 'd';
}

function when(ts) {
  if (!ts) return '';
  const d = new Date(Number(ts) * 1000);
  return isNaN(d.getTime()) ? '' : d.toLocaleString();
}

function rowRef(row) {
  return String(row.session_id || row.agent_id || row.name || '');
}

// A ref is whatever the user (or a #s= link) gave: session id, agent id or
// name — the same three the socket resolves.
function rowMatches(row, ref) {
  return !!ref && (row.session_id === ref || row.agent_id === ref || row.name === ref);
}

function selectedRow() {
  for (const row of state.sessions) if (rowMatches(row, state.ref)) return row;
  return null;
}

function note(text) { $('note').textContent = text || ''; }

function clearError() {
  const el = $('error');
  el.hidden = true;
  el.textContent = '';
}

// Only ever shows an error. The console polls, so a successful poll must not
// wipe a message the user has not read yet: clearing is explicit and happens
// when they act (select, refresh, tab, send, interrupt).
function showError(env) {
  if (!env || env.ok === true) return;
  const el = $('error');
  const data = env.data || {};
  let msg = String(env.error || 'request failed');
  if (data.code) msg += '  [' + data.code + ']';
  const hint = env.hint || data.hint;
  if (hint) msg += '\n' + hint;
  const candidates = (data.candidates || []).slice(0, 8);
  if (candidates.length) {
    msg += '\n' + candidates.map((c) =>
      [c.state, c.name || '(unnamed)', c.backend, c.agent_id].filter(Boolean).join('  ')).join('\n');
  }
  el.textContent = msg;
  el.hidden = false;
}

// ── navigation ──────────────────────────────────────────────────────────────
// A phone shows the list or the detail, never both. Which one is a body
// attribute the CSS reads; which session is open lives in the URL hash, so a
// detail view is a link and the browser/OS back gesture returns to the list.
// On a wide screen both panes are visible, setPane does nothing visible, and
// the hash still makes a session linkable.

function setPane(pane) {
  document.body.setAttribute('data-pane', pane);
}

function refFromHash() {
  const m = /^#s=(.+)$/.exec(location.hash);
  if (!m) return null;
  try { return decodeURIComponent(m[1]) || null; } catch (e) { return null; }
}

function setHash(ref) {
  const want = '#s=' + encodeURIComponent(ref);
  if (location.hash !== want) location.hash = want;
}

function showList() {
  setPane('list');
}

// A #s=… deep link needs a list entry behind it, or the phone's back gesture
// would leave the page rather than show the list.
function claimInitialHash() {
  const ref = refFromHash();
  if (!ref) return null;
  history.replaceState(null, '', location.pathname + location.search);
  history.pushState(null, '', '#s=' + encodeURIComponent(ref));
  return ref;
}

function onHashChange() {
  const ref = refFromHash();
  if (!ref) { showList(); return; }
  if (ref === state.ref) { setPane('detail'); return; }
  openSession(ref, { hash: false });
}

function openSession(ref, opts) {
  if (!ref) return;
  clearError();
  state.ref = ref;
  state.pending = false;
  state.sawWorking = false;
  state.lastState = null;
  const row = selectedRow();
  for (const el of $('sessions').querySelectorAll('.row')) {
    el.classList.toggle('on', el.getAttribute('data-ref') === (row ? rowRef(row) : ref));
  }
  renderHead(selectedRow());
  unmountSheet();
  $('pane').innerHTML = '<p class="empty">loading…</p>';
  $('pane').scrollTop = 0;
  setSource('');
  renderModal(null);
  note('');
  setPane('detail');
  if (!opts || opts.hash !== false) setHash(ref);
  refreshPane();
  schedule(0);
}

// ── list ────────────────────────────────────────────────────────────────────
// The Sublime Sessions list, not a dump of the store: CURRENT (live sessions,
// ordered by what needs you — a question or permission, unread, working, then
// idle, then sleeping — and grouped by window) above HISTORY (closed rows,
// collapsed until asked for, searched when there is a query). Children sit
// under their parent (↳), the host-bound session carries ▸, and ids stay off
// the rows: the header shows them for the selected session.

const HISTORY_KEY = 'submarine_web_history';
const HISTORY_PAGE = 40;

const listState = {
  query: '',
  showHistory: false,
  historyLimit: HISTORY_PAGE,
  collapsed: new Set(),   // group keys the user folded
};
try { listState.showHistory = localStorage.getItem(HISTORY_KEY) === '1'; } catch (e) { /* private mode */ }

function isCurrent(row) {
  if (row.kind === 'live') return true;
  return row.state === 'open' || row.state === 'sleeping';   // saved, still current
}

// Sublime's marks: ? input, ! unread, ● working, ⏸ sleeping, ○ ready.
function rowMark(row) {
  if (row.waiting) return ['?', 'waiting'];
  if (row.unread) return ['!', 'unread'];
  const st = String(row.state || '');
  if (st === 'working') return ['●', 'working'];
  if (st === 'error') return ['✘', 'error'];
  if (st === 'sleeping') return ['⏸', 'sleeping'];
  if (row.kind !== 'live') return ['·', 'saved'];
  return ['○', 'idle'];
}

function band(row) {
  if (row.waiting || row.unread) return 0;
  const st = String(row.state || '');
  if (st === 'working' || st === 'error') return 1;
  if (st === 'sleeping') return 3;
  return 2;
}

function basename(path) {
  const p = String(path || '').replace(/\/+$/, '');
  return p.slice(p.lastIndexOf('/') + 1) || p;
}

function shortModel(model) {
  const m = String(model || '');
  // claude-fable-5-1 → fable-5-1; keep vendor-less names as they are.
  return m.replace(/^claude-/, '').replace(/-\d{8}$/, '');
}

function windowId(row) {
  const id = (row.view || {}).window;
  return id === null || id === undefined || id === '' ? '' : String(id);
}

function projectOf(row) {
  return (row.view || {}).project || '';
}

function matchesQuery(row, q) {
  if (!q) return true;
  const hay = [row.name, row.backend, row.model, projectOf(row), row.agent_id, row.session_id, row.state]
    .filter(Boolean).join(' ').toLowerCase();
  return q.split(/\s+/).every((word) => hay.includes(word));
}

// A query keeps a matching row's subtree: the children are what the parent
// spawned, and a match on the parent is usually a search for that work.
function filterTree(rows, q) {
  if (!q) return rows;
  const byAgent = new Map();
  for (const r of rows) if (r.agent_id) byAgent.set(r.agent_id, r);
  const hit = new Set(rows.filter((r) => matchesQuery(r, q)));
  const keep = (r) => {
    let cur = r;
    for (let i = 0; cur && i < 8; i++) {
      if (hit.has(cur)) return true;
      const p = cur.parent_agent_id;
      cur = p && p !== cur.agent_id ? byAgent.get(p) : null;
    }
    return false;
  };
  return rows.filter(keep);
}

// Forest order: roots by (band, recency), each followed by its subtree. A
// child whose parent is not in the same bucket lists as a root.
function treeOrder(rows) {
  const byAgent = new Map();
  for (const r of rows) if (r.agent_id) byAgent.set(r.agent_id, r);
  const kids = new Map();
  const roots = [];
  for (const r of rows) {
    const p = r.parent_agent_id;
    if (p && p !== r.agent_id && byAgent.has(p)) {
      if (!kids.has(p)) kids.set(p, []);
      kids.get(p).push(r);
    } else roots.push(r);
  }
  const sortKey = (a, b) => (band(a) - band(b)) || (Number(b.last_access || 0) - Number(a.last_access || 0));
  roots.sort(sortKey);
  const out = [];
  const seen = new Set();
  const walk = (r, depth) => {
    if (seen.has(r)) return;
    seen.add(r);
    out.push({ row: r, depth: depth });
    for (const k of (kids.get(r.agent_id) || []).sort(sortKey)) walk(k, depth + 1);
  };
  for (const r of roots) walk(r, 0);
  for (const r of rows) walk(r, 0);   // anything a cycle kept out
  return out;
}

function contextBar(row) {
  const pct = Number(row.context_pct);
  if (!row.context_pct || isNaN(pct)) return '';
  const lvl = pct >= 85 ? 'hot' : (pct >= 60 ? 'warm' : '');
  return '<span class="ctx ' + lvl + '" title="context ' + Math.round(pct) + '%">' +
    '<i style="width:' + Math.max(4, Math.min(100, pct)) + '%"></i></span>';
}

function rowHtml(entry, section) {
  const row = entry.row;
  const ref = rowRef(row);
  const [mark, markClass] = rowMark(row);
  const view = row.view || {};
  const bound = row.kind === 'live' && view.bound && view.view_id;
  const bits = [];
  const be = row.backend || '';
  const model = shortModel(row.model);
  bits.push(model ? be + ' · ' + model : be);
  if (row.query_count !== null && row.query_count !== undefined) bits.push('Q ' + row.query_count);
  if (row.turn_phase && row.turn_phase !== 'idle' && row.turn_phase !== 'live') bits.push(row.turn_phase);
  if (section === 'history' && projectOf(row)) bits.push(basename(projectOf(row)));
  const waitingText = row.waiting ? ({ question: 'asks you a question', permission: 'wants permission',
                                       plan: 'plan needs approval' }[row.waiting] || row.waiting) : '';
  const tree = entry.depth ? '<span class="tree" style="--d:' + Math.min(entry.depth, 6) + '">↳</span>' : '';
  return '<button type="button" class="row' + (rowMatches(row, state.ref) ? ' on' : '') +
      ' ' + markClass + (row.waiting ? ' waiting' : '') +
    '" data-ref="' + esc(ref) + '" title="' + esc((row.agent_id || '') + '  ' + (row.session_id || '')) + '">' +
      '<span class="r1">' +
        '<span class="cur">' + (bound ? '▸' : '') + '</span>' +
        tree +
        '<span class="mark ' + markClass + '">' + mark + '</span>' +
        '<span class="name">' + esc(row.name || '(unnamed)') + '</span>' +
        '<span class="age dim small">' + esc(ago(row.last_access)) + '</span>' +
      '</span>' +
      '<span class="r2">' +
        (tree ? '<span class="tree-pad" style="--d:' + Math.min(entry.depth, 6) + '"></span>' : '') +
        '<span class="bits">' + esc(bits.join(' · ')) + '</span>' +
        (waitingText ? '<span class="wait">' + esc(waitingText) + '</span>' : '') +
        contextBar(row) +
      '</span>' +
    '</button>';
}

function groupCurrent(rows) {
  const byKey = new Map();
  for (const r of rows) {
    const key = windowId(r) || projectOf(r) || '';
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key).push(r);
  }
  const groups = [...byKey.entries()].map(([key, group]) => {
    const project = group.map(projectOf).find(Boolean);
    const win = group.map(windowId).find(Boolean);
    return {
      key: 'w:' + key,
      title: project ? basename(project) : (win ? 'Window ' + win : 'No window'),
      sub: (win ? 'window ' + win : '') + (project ? (win ? ' · ' : '') + project : ''),
      rows: group,
      // The group with something to attend to floats up; then the busiest.
      rank: Math.min(...group.map(band)),
      recent: Math.max(...group.map((r) => Number(r.last_access || 0))),
    };
  });
  groups.sort((a, b) => (a.rank - b.rank) || (b.recent - a.recent));
  return groups;
}

function groupHtml(g, section) {
  const folded = listState.collapsed.has(g.key);
  const attn = g.rows.filter((r) => r.waiting || r.unread).length;
  const busy = g.rows.filter((r) => r.state === 'working').length;
  const badges = (attn ? '<span class="gb attn">' + attn + ' waiting</span>' : '') +
                 (busy ? '<span class="gb busy">' + busy + ' working</span>' : '');
  return '<div class="group' + (folded ? ' folded' : '') + '" data-group="' + esc(g.key) + '" title="' + esc(g.sub) + '">' +
      '<span class="chev">' + (folded ? '▸' : '▾') + '</span>' +
      '<span class="gt">' + esc(g.title) + '</span>' +
      '<span class="gs dim">' + esc(g.sub) + '</span>' +
      badges +
      '<span class="gn dim">' + g.rows.length + '</span>' +
    '</div>' +
    (folded ? '' : treeOrder(g.rows).map((e) => rowHtml(e, section)).join(''));
}

function renderList(env) {
  if (env.ok !== true) {
    const noToken = env.http === 401;
    $('conn').textContent = noToken ? 'token required' : 'no socket';
    $('conn').className = 'badge bad';
    $('sessions').innerHTML = '<p class="dim" style="padding:10px">' + esc(env.error || 'cannot list sessions') + '</p>';
    $('counts').textContent = noToken ? 'not authorised' : 'socket missing';
    showError(env);
    return;
  }
  const data = env.data || {};
  const rows = data.sessions || [];
  state.sessions = rows;
  const inst = env.instance || {};
  $('conn').textContent = 'connected' + (inst.pid ? ' · sublime pid ' + inst.pid : '');
  $('conn').className = 'badge ok';

  const q = listState.query.trim().toLowerCase();
  const current = rows.filter(isCurrent);
  const history = rows.filter((r) => !isCurrent(r));
  const curShown = filterTree(current, q);
  const histAll = filterTree(history, q);
  const waiting = current.filter((r) => r.waiting || r.unread).length;
  const working = current.filter((r) => r.state === 'working').length;
  $('counts').innerHTML =
    '<span>' + current.length + ' current</span>' +
    (working ? '<span class="gb busy">' + working + ' working</span>' : '') +
    (waiting ? '<span class="gb attn">' + waiting + ' waiting</span>' : '') +
    '<span class="dim">' + history.length + ' in history</span>';

  let html = '';
  if (!curShown.length) {
    html += '<p class="dim empty-note">' + (q ? 'no current session matches' : 'no current sessions') + '</p>';
  } else {
    html += groupCurrent(curShown).map((g) => groupHtml(g, 'current')).join('');
  }
  // History: hidden until asked for, unless a query is being typed.
  const open = listState.showHistory || !!q;
  const shown = histAll.slice(0, listState.historyLimit);
  html += '<div class="group history' + (open ? '' : ' folded') + '" data-history="1">' +
      '<span class="chev">' + (open ? '▾' : '▸') + '</span>' +
      '<span class="gt">History</span>' +
      '<span class="gn dim">' + (q ? histAll.length + ' of ' : '') + history.length + '</span>' +
    '</div>';
  if (open) {
    html += treeOrder(shown).map((e) => rowHtml(e, 'history')).join('');
    if (histAll.length > shown.length) {
      html += '<button type="button" class="more-rows link" id="more-history">show ' +
        Math.min(HISTORY_PAGE, histAll.length - shown.length) + ' more of ' + histAll.length + '</button>';
    }
    if (!histAll.length) html += '<p class="dim empty-note">nothing in history' + (q ? ' matches' : '') + '</p>';
  }
  const box = $('sessions');
  const keepScroll = box.scrollTop;
  box.innerHTML = html;
  box.scrollTop = keepScroll;
  for (const el of box.querySelectorAll('.row')) {
    el.addEventListener('click', () => openSession(el.getAttribute('data-ref')));
  }
  for (const el of box.querySelectorAll('.group[data-group]')) {
    el.addEventListener('click', () => {
      const key = el.getAttribute('data-group');
      if (listState.collapsed.has(key)) listState.collapsed.delete(key); else listState.collapsed.add(key);
      renderList(env);
    });
  }
  const hist = box.querySelector('.group.history');
  if (hist) hist.addEventListener('click', () => {
    listState.showHistory = !(listState.showHistory || !!q);
    if (!listState.showHistory) listState.historyLimit = HISTORY_PAGE;
    try { localStorage.setItem(HISTORY_KEY, listState.showHistory ? '1' : '0'); } catch (e) { /* private mode */ }
    renderList(env);
  });
  const more = box.querySelector('#more-history');
  if (more) more.addEventListener('click', () => { listState.historyLimit += HISTORY_PAGE; renderList(env); });
  state.lastListEnv = env;
}

// ── detail ──────────────────────────────────────────────────────────────────

function renderHead(row) {
  const el = $('head');
  if (!row) {
    el.className = 'head empty';
    el.textContent = state.ref
      ? 'session ' + state.ref + ' is no longer listed'
      : 'Pick a session on the left, or send a prompt to wake one.';
    return;
  }
  const view = row.view || {};
  const meta = [
    '<span class="m-state">state <code>' + esc(row.state || '?') + '</code></span>',
    row.turn_phase ? '<span>turn <code>' + esc(row.turn_phase) + '</code></span>' : '',
    '<span>backend <code>' + esc(row.backend || '?') + '</code></span>',
    row.model ? '<span>model <code>' + esc(row.model) + '</code></span>' : '',
    '<span>age <code>' + esc(ago(row.last_access)) + '</code></span>',
    row.kind === 'live' && view.bound && view.view_id ? '<span>view <code>' + esc(view.view_id) + '</code></span>' : '',
    view.project ? '<span>project <code>' + esc(view.project) + '</code></span>' : '',
    view.window !== null && view.window !== undefined && view.window !== ''
      ? '<span>window <code>' + esc(view.window) + '</code></span>' : '',
    '<span class="m-id">agent <code>' + esc(row.agent_id || '–') + '</code></span>',
    '<span class="m-id">session <code>' + esc(row.session_id || '–') + '</code></span>',
  ].filter(Boolean).join('');
  el.className = 'head';
  el.innerHTML = '<h2>' + esc(row.name || '(unnamed)') + '</h2><div class="meta">' + meta + '</div>';
}

// The Sublime sheet, rebuilt from the transcript for a session that has no
// sheet (single mode binds one host view; every other session is viewless).
// Same grammar the sheet renderer writes: `◎ prompt ▶`, one `⚙ Tool ×N` per
// run of a tool, the reply, `@done(model)`.
function sheetFromTurns(body, row) {
  const turns = body.turns || [];
  const model = (row && row.model) || (body.session && body.session.backend) || '';
  const out = [];
  for (const t of turns) {
    const prompt = String(t.prompt || '').replace(/\s+$/, '');
    out.push('◎ ' + prompt + (t.prompt_truncated ? ' …' : '') + ' ▶');
    out.push('');
    const tools = t.tools || [];
    if (tools.length) {
      let i = 0;
      while (i < tools.length) {
        let j = i + 1;
        while (j < tools.length && tools[j] === tools[i]) j++;
        out.push('⚙ ' + tools[i] + (j - i > 1 ? ' ×' + (j - i) : ''));
        i = j;
      }
      out.push('');
    }
    const reply = String(t.reply || '').replace(/\s+$/, '');
    if (reply) {
      out.push(reply);
      if (t.reply_truncated) out.push('… truncated');
      out.push('');
    }
    out.push('  @done(' + model + ')');
    out.push('');
  }
  return out.join('\n');
}

function setSource(text) {
  state.source = text || '';
  $('source').textContent = state.source;
  $('source-phone').textContent = state.source;
  // The Sheet tab says when it is really the transcript, for the widths that
  // hide the source label.
  const sheetTab = document.querySelector('.tab[data-mode="sheet"]');
  sheetTab.classList.toggle('fallback', state.mode === 'sheet' && /^no sheet/.test(state.source));
  $('more').hidden = !/transcript/.test(state.source) || !/of \d+ turn/.test(state.source)
    || turnsWanted() >= 50 || !moreTurnsAvailable();
}

function moreTurnsAvailable() {
  const m = /(\d+) of (\d+) turn/.exec(state.source);
  return !!m && Number(m[1]) < Number(m[2]);
}

// The pane holds either the sheet (CodeMirror, or a <pre> painted by the same
// tokenizer) or plain HTML (edits, errors). One mount per session.
function unmountSheet() {
  if (state.sheet && state.sheet.destroy) state.sheet.destroy();
  state.sheet = null;
  $('pane').classList.remove('is-sheet');
}

function mountSheet() {
  if (state.sheet) return state.sheet;
  const pane = $('pane');
  pane.innerHTML = '';
  pane.classList.add('is-sheet');
  if (state.editor) {
    state.sheet = state.editor.createSheet(pane);
    return state.sheet;
  }
  const pre = document.createElement('pre');
  pre.className = 'sheet';
  pane.appendChild(pre);
  const atBottom = () => pane.scrollTop + pane.clientHeight >= pane.scrollHeight - 24;
  state.sheet = {
    dom: pre,
    text: '',
    setText(text, opts) {
      const o = opts || {};
      const follow = o.follow === true || (o.follow !== false && atBottom());
      if (text !== this.text) {
        this.text = text;
        pre.innerHTML = window.SubmarineHL.toHtml(text);
      }
      if (follow) pane.scrollTop = pane.scrollHeight;
    },
    scrollToEnd() { pane.scrollTop = pane.scrollHeight; },
    atBottom,
    foldAll() {}, unfoldAll() {},
    openSearch() { note('search needs the CodeMirror editor (CDN unreachable)'); },
    destroy() { pre.remove(); },
  };
  return state.sheet;
}

function renderSheetText(text, opts) {
  mountSheet().setText(String(text || ''), opts);
}

function renderEdits(body) {
  const edits = body.edits || [];
  if (!edits.length) return '<p class="empty">no edits</p>';
  let html = '<div class="dim small">' + edits.length + ' of ' + (body.total || edits.length) + ' edit(s)</div>';
  for (const e of edits) {
    html += '<div class="edit"><span class="sm-tool-done">✔</span> ' + esc(e.tool || '?') + '  <span class="path">' + esc(e.file_path || '?') +
      (e.line ? ':' + esc(e.line) : '') + '</span></div>';
  }
  return html;
}

function paneHtml(html) {
  unmountSheet();
  $('pane').innerHTML = html;
}

function turnsWanted() {
  return Math.max(1, Math.min(50, parseInt($('turns').value, 10) || 8));
}

function syncToolbar() {
  const sheetish = state.mode !== 'edits';
  $('turns-wrap').hidden = state.mode === 'edits' || (state.mode === 'sheet' && state.source === 'live sheet');
  for (const id of ('fold unfold find tail').split(' ')) $(id).hidden = !sheetish;
  $('fold').disabled = $('unfold').disabled = $('find').disabled = !state.editor;
}

let paneSeq = 0;

async function refreshPane(opts) {
  if (!state.ref) return;
  const seq = ++paneSeq;
  await editorReady;
  if (seq !== paneSeq || !state.ref) return;   // a newer request took over
  const o = opts || {};
  const ref = 'ref=' + encodeURIComponent(state.ref);
  let env;
  let mode = state.mode;
  if (mode === 'edits') {
    env = await api('/api/view?' + ref + '&mode=edits&limit=50');
    if (seq !== paneSeq) return;
    if (env.ok !== true) { paneHtml('<p class="empty">' + esc(env.error || 'cannot read this session') + '</p>'); showError(env); return; }
    paneHtml(renderEdits(env.data || {}));
    setSource('');
    syncToolbar();
    return;
  }
  // The list says whether the session owns a sheet (single mode binds one
  // host view; the rest are viewless): skip the probe that would only 409.
  const row = selectedRow();
  const hasSheet = !row || (row.kind === 'live' && row.view && row.view.bound && row.view.view_id);
  if (mode === 'sheet' && !hasSheet) mode = 'tail';
  if (mode === 'sheet') {
    env = await api('/api/view?' + ref + '&mode=text');
    if (seq !== paneSeq) return;
    if (env.ok === true) {
      renderSheetText((env.data || {}).text || '', { follow: o.follow });
      setSource('live sheet');
      syncToolbar();
      return;
    }
    const code = (env.data || {}).code;
    if (code !== 'no_view' && code !== 'no_session') {
      paneHtml('<p class="empty">' + esc(env.error || 'cannot read this session') + '</p>');
      showError(env);
      return;
    }
    mode = 'tail';   // no sheet for this session: rebuild it from the transcript
  }
  env = await api('/api/view?' + ref + '&mode=tail&turns=' + turnsWanted());
  if (seq !== paneSeq) return;
  if (env.ok !== true) {
    paneHtml('<p class="empty">' + esc(env.error || 'cannot read this session') + '</p>');
    showError(env);
    return;
  }
  const body = env.data || {};
  const turns = body.turns || [];
  renderSheetText(turns.length
    ? sheetFromTurns(body, selectedRow())
    : '◎ ' + ((body.session || {}).name || 'this session') + ' ▶\n\n  (no turns on disk yet)\n', { follow: o.follow });
  setSource((state.mode === 'sheet' ? 'no sheet — transcript' : 'transcript') +
    ' · ' + turns.length + ' of ' + (body.turn_count || 0) + ' turn(s)');
  syncToolbar();
}

// ── polling ─────────────────────────────────────────────────────────────────

function schedule(ms) {
  clearTimeout(state.timer);
  state.timer = setTimeout(tick, ms === undefined ? POLL_IDLE_MS : ms);
}

async function tick() {
  const env = await api('/api/list?scope=all');
  renderList(env);

  const row = selectedRow();
  const working = !!row && row.state === 'working';
  if (state.pending) {
    if (working) state.sawWorking = true;
    else if (state.sawWorking || Date.now() - state.pendingSince > PENDING_GIVE_UP_MS) {
      state.pending = false;
      state.sawWorking = false;
    }
  }
  if (row && (row.state !== (state.lastState || null))) {
    state.lastState = row.state;
    renderHead(row);
  }
  const busy = working || state.pending;
  state.working = working;
  $('interrupt').disabled = !working;
  setPlaceholder(promptPlaceholder(working));
  if (state.ref && row && row.kind === 'live') await refreshPending();
  else renderModal(null);
  if (state.ref && busy) await refreshPane();
  // A pending question is the thing to notice: poll it at the working rate.
  schedule(busy || state.modal ? POLL_WORKING_MS : POLL_IDLE_MS);
}

// ── pending modal: question / permission / plan ─────────────────────────────

let modalSeq = 0;

async function refreshPending() {
  if (!state.ref) return;
  const seq = ++modalSeq;
  const env = await api('/api/pending?ref=' + encodeURIComponent(state.ref));
  if (seq !== modalSeq) return;
  if (env.ok !== true) {
    // A session that just went to sleep / closed has nothing to answer.
    renderModal(null);
    return;
  }
  const body = env.data || {};
  renderModal(body.modals && body.modals.length ? body : null);
}

function summarizeInput(tool, input) {
  const inp = input || {};
  const first = (keys) => { for (const k of keys) if (inp[k]) return String(inp[k]); return ''; };
  const primary = first(['command', 'file_path', 'path', 'pattern', 'url', 'query', 'prompt', 'description']);
  const rest = Object.keys(inp).filter((k) => !['command', 'file_path', 'path'].includes(k) || !primary);
  let html = '';
  if (primary) html += '<pre class="minput">' + esc(primary) + '</pre>';
  const more = rest.filter((k) => inp[k] !== primary).map((k) => {
    const v = inp[k];
    const text = typeof v === 'string' ? v : JSON.stringify(v, null, 1);
    return '<div class="mk"><span class="dim">' + esc(k) + '</span><pre>' + esc(text) + '</pre></div>';
  });
  if (more.length) {
    html += '<details class="mmore"><summary class="dim small">' + more.length + ' more field' +
      (more.length === 1 ? '' : 's') + '</summary>' + more.join('') + '</details>';
  }
  return html;
}

function renderModal(body) {
  const el = $('modal');
  const same = JSON.stringify(body || null) === JSON.stringify(state.modal || null);
  state.modal = body || null;
  document.body.classList.toggle('has-modal', !!body);
  if (!body) { el.hidden = true; el.innerHTML = ''; return; }
  if (same && !el.hidden) return;         // keep a half-typed "Other…" alive
  const first = body.modals[0];
  const kind = first.kind;
  const p = first.payload || {};
  let html = '';
  if (kind === 'question') {
    const q = p.question || {};
    const idx = Number(p.current_idx || 0);
    const options = q.options || [];
    const multi = !!q.multiSelect;
    const selected = new Set(p.selected || []);
    html += '<div class="mh"><span class="micon">❓</span>' +
      '<span class="mtitle">' + esc(q.header || 'Question') + '</span>' +
      '<span class="dim small">' + (idx + 1) + ' of ' + (p.total || 1) + (multi ? ' · pick any' : '') + '</span></div>';
    html += '<div class="mq">' + esc(q.question || '') + '</div>';
    html += '<div class="opts">' + options.map((o, i) => {
      const label = typeof o === 'string' ? o : (o.label || String(o));
      const desc = typeof o === 'string' ? '' : (o.description || '');
      return '<button type="button" class="opt' + (multi && selected.has(i) ? ' picked' : '') +
        '" data-opt="' + (i + 1) + '"><span class="on">' + (i + 1) + '</span>' +
        '<span class="ol">' + esc(label) + (desc ? '<span class="od">' + esc(desc) + '</span>' : '') + '</span></button>';
    }).join('') + '</div>';
    html += '<div class="mrow">' +
      (multi ? '<button type="button" class="btn primary" id="m-confirm">Confirm selection</button>' : '') +
      '<input id="m-other" type="text" placeholder="Other… (type your own answer)" autocomplete="off">' +
      '<button type="button" class="btn" id="m-other-send">Answer</button></div>';
  } else if (kind === 'permission') {
    const queued = body.modals.filter((m) => m.kind === 'permission').length - 1;
    html += '<div class="mh"><span class="micon warn">⚠</span>' +
      '<span class="mtitle">Allow <code>' + esc(p.tool || '?') + '</code>?</span>' +
      (queued > 0 ? '<span class="dim small">+' + queued + ' more waiting</span>' : '') + '</div>';
    html += summarizeInput(p.tool, p.tool_input);
    html += '<div class="mrow perms">' +
      '<button type="button" class="btn allow" data-perm="allow"><u>Y</u> Allow</button>' +
      '<button type="button" class="btn deny" data-perm="deny"><u>N</u> Deny</button>' +
      '<button type="button" class="btn" data-perm="allow_session" title="Allow this tool for the rest of the session"><u>S</u> Session</button>' +
      '<button type="button" class="btn always" data-perm="allow_all" title="Always allow this tool/command"><u>A</u> Always</button></div>';
  } else if (kind === 'plan') {
    html += '<div class="mh"><span class="micon">📋</span><span class="mtitle">Plan needs approval</span></div>';
    if (p.plan_file) html += '<pre class="minput">' + esc(p.plan_file) + '</pre>';
    if ((p.allowed_prompts || []).length) {
      html += '<div class="dim small">allowed prompts: ' + esc(p.allowed_prompts.map((x) => x.tool || x).join(', ')) + '</div>';
    }
    html += '<div class="mrow perms">' +
      '<button type="button" class="btn allow" data-plan="approve"><u>Y</u> Approve</button>' +
      '<button type="button" class="btn deny" data-plan="reject"><u>N</u> Reject</button></div>';
  } else {
    html += '<div class="mh"><span class="mtitle">' + esc(kind) + ' pending</span></div>';
  }
  el.innerHTML = html;
  el.hidden = false;
  el.className = 'modal ' + kind;
  wireModal(kind, p);
}

async function answer(fields) {
  if (!state.ref) return;
  clearError();
  for (const b of $('modal').querySelectorAll('button')) b.disabled = true;
  const env = await api('/api/answer', {
    method: 'POST',
    body: JSON.stringify(Object.assign({ ref: state.ref }, fields)),
  });
  showError(env);
  if (env.ok === true) {
    const d = env.data || {};
    note('answered ' + (d.answered || fields.kind));
    renderModal(d.modals && d.modals.length ? d : null);
    await refreshPane({ follow: true });
  } else {
    // stale / not_found: whatever we showed is gone, re-read.
    await refreshPending();
  }
  schedule(0);
}

function wireModal(kind, p) {
  const el = $('modal');
  if (kind === 'question') {
    const q = p.question || {};
    const multi = !!q.multiSelect;
    const picked = new Set(p.selected || []);
    for (const b of el.querySelectorAll('.opt')) {
      b.addEventListener('click', () => {
        const n = Number(b.getAttribute('data-opt'));
        if (!multi) { answer({ kind: 'question', option: n, qid: p.qid }); return; }
        if (picked.has(n - 1)) picked.delete(n - 1); else picked.add(n - 1);
        b.classList.toggle('picked', picked.has(n - 1));
      });
    }
    const confirm = el.querySelector('#m-confirm');
    if (confirm) confirm.addEventListener('click', () => {
      answer({ kind: 'question', options: [...picked].sort().map((i) => i + 1), qid: p.qid });
    });
    const other = el.querySelector('#m-other');
    const sendOther = () => {
      const text = other.value.trim();
      if (text) answer({ kind: 'question', text: text, qid: p.qid });
    };
    el.querySelector('#m-other-send').addEventListener('click', sendOther);
    other.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); sendOther(); } });
  } else if (kind === 'permission') {
    for (const b of el.querySelectorAll('[data-perm]')) {
      b.addEventListener('click', () => answer({ kind: 'permission', response: b.getAttribute('data-perm'), id: p.id }));
    }
  } else if (kind === 'plan') {
    for (const b of el.querySelectorAll('[data-plan]')) {
      b.addEventListener('click', () => answer({ kind: 'plan', response: b.getAttribute('data-plan'), id: p.id }));
    }
  }
}

// Sublime's keys, when the sheet has focus and a modal is up: 1-4 pick an
// option, Y/N/S/A answer a permission, Y/N a plan. Ignored while typing.
document.addEventListener('keydown', (e) => {
  if (!state.modal || e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
  const first = state.modal.modals[0];
  const p = first.payload || {};
  const k = e.key.toLowerCase();
  if (first.kind === 'question' && /^[1-9]$/.test(k)) {
    const b = $('modal').querySelector('.opt[data-opt="' + k + '"]');
    if (b) { b.click(); e.preventDefault(); }
  } else if (first.kind === 'permission') {
    const map = { y: 'allow', n: 'deny', s: 'allow_session', a: 'allow_all' };
    if (map[k]) { answer({ kind: 'permission', response: map[k], id: p.id }); e.preventDefault(); }
  } else if (first.kind === 'plan') {
    const map = { y: 'approve', n: 'reject' };
    if (map[k]) { answer({ kind: 'plan', response: map[k], id: p.id }); e.preventDefault(); }
  }
});

// ── actions ─────────────────────────────────────────────────────────────────

function idemKey() {
  return 'webui-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
}

function actionText(data) {
  const verbs = {
    sent: 'sent',
    queued: 'queued behind the turn',
    woke: 'woke the session, prompt delivered once connected',
    send_now: 'interrupted the turn and sent',
    pending: 'accepted',
  };
  let out = verbs[data.action] || data.action || 'accepted';
  if (data.duplicate) out += ' (duplicate replay key: not sent again)';
  if (data.note) out += ' — ' + data.note;
  return out;
}

function getPrompt() {
  return state.composer ? state.composer.getValue() : $('prompt').value;
}

function setPrompt(text) {
  if (state.composer) state.composer.setValue(text);
  else { $('prompt').value = text; sizeCompose(); }
}

function setPlaceholder(text) {
  if (state.composer) state.composer.setPlaceholder(text);
  $('prompt').placeholder = text;
}

async function send() {
  const prompt = getPrompt();
  if (!state.ref || !prompt.trim()) return;
  const row = selectedRow();
  if (row && row.kind !== 'live') {
    note('this row is not a running session — pick a live/sleeping one');
    return;
  }
  $('send').disabled = true;
  clearError();
  // A sleeping target answers only once the plugin has waited out its bridge
  // init, which can take seconds: say so instead of looking hung.
  note('sending…');
  const env = await api('/api/chat', {
    method: 'POST',
    body: JSON.stringify({
      ref: state.ref,
      prompt: prompt,
      queue: $('queue').value,
      idem: idemKey(),
    }),
  });
  $('send').disabled = false;
  showError(env);
  if (env.ok !== true) { note('not sent'); return; }
  const data = env.data || {};
  if (env.error) {
    // The socket server accepts the hand-off and reports the delivery failure
    // in the same reply (e.g. a bridge that never came up). Nothing was sent:
    // keep the text so it can be retried.
    note('accepted, but not delivered: ' + env.error);
    return;
  }
  setPrompt('');
  note(actionText(data));
  if (data.action === 'sent' || data.action === 'send_now' || data.action === 'woke') {
    state.pending = true;
    state.sawWorking = false;
    state.pendingSince = Date.now();
  }
  await tick();
}

async function interrupt() {
  if (!state.ref) return;
  $('interrupt').disabled = true;
  clearError();
  const env = await api('/api/interrupt', {
    method: 'POST',
    body: JSON.stringify({ ref: state.ref }),
  });
  showError(env);
  if (env.ok === true) {
    const d = env.data || {};
    note(d.interrupted
      ? 'interrupted' + (d.settling ? ' (the bridge is still settling)' : '')
      : 'nothing was running');
    state.pending = false;
    state.sawWorking = false;
  }
  await tick();
}

// ── wiring ──────────────────────────────────────────────────────────────────

// Publish the visual viewport height as --app-h; the phone stylesheet uses it
// as the app height.
function syncAppHeight() {
  const vv = window.visualViewport;
  const h = Math.round(vv ? vv.height : window.innerHeight);
  if (h > 0) document.documentElement.style.setProperty('--app-h', h + 'px');
}

// The phone's compose box starts small so the transcript keeps the screen, and
// grows with what is typed. The cap is a share of the visual viewport too, so
// an open keyboard still leaves the transcript room.
function sizeCompose() {
  if (state.composer) return;
  const box = $('prompt');
  box.style.height = '';
  box.rows = NARROW.matches ? 1 : 3;
  if (!NARROW.matches) return;
  const vv = window.visualViewport;
  const cap = Math.round(((vv && vv.height) || window.innerHeight) * 0.34);
  box.style.height = 'auto';
  box.style.height = Math.max(52, Math.min(box.scrollHeight + 2, cap)) + 'px';
}

// A phone's Enter key is the way out of the field, not a send, so only a wide
// screen advertises Enter-to-send.
function promptPlaceholder(working) {
  if (working) return 'Session is mid-turn — queue, interrupt, or wait…';
  if (NARROW.matches) return 'Prompt this session…';
  if (COARSE.matches) return 'Prompt this session… (Send or ⌘/Ctrl+Enter; Enter is a newline)';
  return 'Prompt this session… (Enter sends, Shift+Enter newline)';
}

// True while the composer has focus. On a phone the CSS then hides the session
// header and toolbar, so the sheet keeps room above the keyboard.
function setComposing(on) {
  document.body.classList.toggle('composing', !!on);
  if (on && NARROW.matches && state.sheet && state.sheet.atBottom && state.sheet.atBottom(80)) {
    // Keep the tail in view as the pane shrinks under the keyboard.
    setTimeout(() => { if (state.sheet) state.sheet.scrollToEnd(); }, 150);
  }
}

// A phone soft keyboard shrinks the visual viewport and not the layout one, so
// the app height and the compose box have to follow the visual viewport rather
// than the window, or the keys would cover the compose row.
function onViewportChange() {
  syncAppHeight();
  sizeCompose();
  setPlaceholder(promptPlaceholder(state.working));
}

// Swap the textarea for the CodeMirror composer once the editor is in. The
// textarea stays in the DOM (hidden) as the offline fallback.
async function mountComposer() {
  const api = await editorReady;
  if (!api) return;
  const draft = $('prompt').value;
  state.composer = api.createComposer($('composer'), {
    placeholder: promptPlaceholder(state.working),
    submitOnEnter: () => !NARROW.matches && !COARSE.matches,
    onSubmit: () => { send(); },
    onFocus: setComposing,
  });
  if (draft) state.composer.setValue(draft);
  $('prompt').hidden = true;
  $('composer').hidden = false;
  syncToolbar();
}

function wire() {
  $('refresh').addEventListener('click', () => { clearError(); refreshPane(); schedule(0); });

  $('back').addEventListener('click', () => {
    // The entry we pushed for the open session, when there is one, so this is
    // the same move as the browser's own back.
    if (refFromHash()) history.back();
    else showList();
  });
  window.addEventListener('hashchange', onHashChange);

  for (const tab of document.querySelectorAll('.tab')) {
    tab.addEventListener('click', () => {
      for (const other of document.querySelectorAll('.tab')) other.classList.remove('on');
      tab.classList.add('on');
      state.mode = tab.getAttribute('data-mode');
      clearError();
      refreshPane();
    });
  }

  $('turns').addEventListener('change', () => { clearError(); refreshPane(); });
  $('reload').addEventListener('click', () => { clearError(); refreshPane(); schedule(0); });
  let searchTimer = null;
  $('search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      listState.query = $('search').value;
      listState.historyLimit = HISTORY_PAGE;
      if (state.lastListEnv) renderList(state.lastListEnv);
    }, 80);
  });
  $('search').addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { $('search').value = ''; listState.query = ''; if (state.lastListEnv) renderList(state.lastListEnv); $('search').blur(); }
    if (e.key === 'Enter') {
      const first = $('sessions').querySelector('.row');
      if (first) openSession(first.getAttribute('data-ref'));
    }
  });
  $('more').addEventListener('click', () => {
    $('turns').value = String(Math.min(50, turnsWanted() + 8));
    clearError();
    refreshPane({ follow: false });
  });
  $('prompt').addEventListener('focus', () => setComposing(true));
  $('prompt').addEventListener('blur', () => setComposing(false));
  $('fold').addEventListener('click', () => { if (state.sheet) state.sheet.foldAll(); });
  $('unfold').addEventListener('click', () => { if (state.sheet) state.sheet.unfoldAll(); });
  $('find').addEventListener('click', () => { if (state.sheet) state.sheet.openSearch(); });
  $('tail').addEventListener('click', () => { if (state.sheet) state.sheet.scrollToEnd(); });
  $('composer').hidden = true;
  mountComposer();

  $('compose').addEventListener('submit', (e) => { e.preventDefault(); send(); });
  $('prompt').addEventListener('input', sizeCompose);
  $('prompt').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send(); return; }
    if (e.key === 'Enter' && !e.shiftKey && !e.altKey) {
      if (NARROW.matches || COARSE.matches) return;   // newline; Send delivers
      e.preventDefault();
      send();
    }
  });
  $('interrupt').addEventListener('click', interrupt);

  if (window.visualViewport) window.visualViewport.addEventListener('resize', onViewportChange);
  window.addEventListener('resize', onViewportChange);
  onViewportChange();

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) schedule(0);
  });
}

const deepRef = claimInitialHash();
wire();
if (deepRef) openSession(deepRef, { hash: false });
else showList();
tick();
