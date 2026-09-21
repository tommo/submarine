'use strict';

// Sheet highlighter: a line-oriented port of SubmarineOutput.sublime-syntax, so
// the browser paints a session the way the Sublime sheet does. One tokenizer
// feeds both the CodeMirror decorations (editor.js) and the <pre> fallback
// (app.js) — the class names below are the tmTheme scopes, colours live in
// style.css under the same names.
//
//   SubmarineHL.initialState()          -> state for the top of a document
//   SubmarineHL.tokenizeLine(line, st)  -> [[cls|null, text], …]; mutates st
//   SubmarineHL.toHtml(text, ctx?)      -> escaped HTML with <span class=…>
//   SubmarineHL.isPromptLine(line)      -> true for a `◎ … ▶` line (fold roots)
//
// Classes are dotted scope names with `sm-` prefix and dots turned into
// dashes: submarine.tool.done -> sm-tool-done, markup.bold -> sm-markup-bold.

(function (global) {
  const PROMPT_RE = /^◎ (.*?)( ▶)\s*$/;
  const PROMPT_OPEN_RE = /^◎ /;
  const SEP_RE = /^[┄─━-]{8,}\s*$/;
  const DONE_RE = /^\s*@done\(.*\)$/;
  const FENCE_RE = /^(\s*)(```)(\S*)\s*$/;

  // Whole-line rules for the conversation context, in sublime-syntax order.
  const LINE_RULES = [
    [/^\s*[◆◎] goal · (executing|active)\b.*$/, 'sm-goal-active'],
    [/^\s*[◆◎] goal · verifying\b.*$/, 'sm-goal-verifying'],
    [/^\s*[◆◎] goal · planning\b.*$/, 'sm-goal-planning'],
    [/^\s*[◆◎] goal · blocked\b.*$/, 'sm-goal-blocked'],
    [/^\s*[◆◎] goal · (complete|done)\b.*$/, 'sm-goal-done'],
    [/^\s*[◆◎] goal · (paused|budget)\b.*$/, 'sm-goal-paused'],
    [/^\s*▸ .+$/, 'sm-task-active'],
    [/^\s*○ .+$/, 'sm-task-pending'],
    [/^\s*… \+\d+ more\b.*$/, 'sm-task-more'],
    [/^\s*\(super\+click to collapse\)$/, 'sm-task-more'],
    [/^\s*☐ .+$/, 'sm-tool-pending'],
    [/^\s*⚙ .+$/, 'sm-tool-background'],
    [/^\s*✔ .+$/, 'sm-tool-done'],
    [/^\s*✘ .+$/, 'sm-tool-error'],
    [/^\s*☑ .+$/, 'sm-question-answered'],
    [/^\s*⚠ .+retry \d+\/\d+.*$/, 'sm-retry'],
    [/^\s*│ .+$/, 'sm-tool-output'],
    [/^\s*[◇◈◆◇⣾⣽⣻⢿⡿⣟⣯⣷]\s*$/, 'sm-spinner'],
    [/^\s*([-*_])\1{2,}\s*$/, 'sm-punctuation-thematic-break'],
  ];

  // Inline rules for a response-text line. Order matters: code spans first so
  // `**` inside backticks is not bold, links before italic for the `_` in URLs.
  const INLINE_RULES = [
    [/(`+)([^`]+)(\1)/g, ['sm-punctuation-raw', 'sm-markup-raw-inline', 'sm-punctuation-raw']],
    [/(\[)([^\]]+)(\])(\()([^)]+)(\))/g, [
      'sm-punctuation-link', 'sm-string-other-link-title', 'sm-punctuation-link',
      'sm-punctuation-link', 'sm-markup-underline-link', 'sm-punctuation-link']],
    [/(\*\*|__)(.+?)(\1)/g, ['sm-punctuation-bold', 'sm-markup-bold', 'sm-punctuation-bold']],
    [/(?<![*\w])(\*|_)(?!\s)(.+?)(?<!\s)(\1)(?![*\w])/g, ['sm-punctuation-italic', 'sm-markup-italic', 'sm-punctuation-italic']],
    [/ → \d+ (files|matches).*$/g, ['sm-tool-result']],
    [/\*\[interrupted\]\*/g, ['sm-interrupted']],
    [/(⚠)(\s*Allow\s+)(\w+)/g, ['sm-permission-icon', 'sm-permission-label', 'sm-permission-tool']],
    [/\[Y\] Allow/g, ['sm-permission-button-allow']],
    [/\[N\] Deny/g, ['sm-permission-button-deny']],
    [/\[A\] Always/g, ['sm-permission-button-allowall']],
  ];

  // Fenced-code tokenizer: enough of a language's surface to read like the
  // embedded Sublime syntaxes (comments, strings, numbers, keywords, calls).
  const KW = {
    common: 'if else for while return break continue import from as class def struct enum type let var const fn func function new delete try catch except finally throw raise yield await async switch case default do in of not and or is null nil None true false True False proc iterator template macro when elif static pub use mod impl trait match loop where go chan defer select package interface map range',
    shell: 'if then else elif fi for do done while case esac in function return export local echo cd exit set unset source alias test',
    sql: 'select from where insert into values update set delete create table drop alter join left right inner outer on group by order having limit as and or not null primary key index',
  };
  const KW_SET = {};
  for (const k of Object.keys(KW)) KW_SET[k] = new Set(KW[k].split(/\s+/));

  // File extension → fence language, for the code view.
  const EXT_LANG = {
    py: 'python', nim: 'nim', js: 'js', mjs: 'js', ts: 'ts', tsx: 'ts', jsx: 'js', json: 'json',
    sh: 'sh', bash: 'sh', zsh: 'sh', rs: 'rust', go: 'go', c: 'c', h: 'c', cc: 'cpp', cpp: 'cpp',
    hpp: 'cpp', m: 'c', mm: 'cpp', yaml: 'yaml', yml: 'yaml', html: 'html', css: 'css', sql: 'sql',
    rb: 'ruby', lua: 'lua', diff: 'diff', patch: 'diff', xml: 'xml', md: 'md', toml: 'toml',
    txt: 'text', swift: 'common', kt: 'common', java: 'common', cs: 'common', glsl: 'common',
    wgsl: 'common', hlsl: 'common',
  };

  function langForPath(path) {
    const m = /\.([A-Za-z0-9]+)$/.exec(String(path || ''));
    return m ? (EXT_LANG[m[1].toLowerCase()] || '') : '';
  }

  // A state that tokenizes every line as code of `lang` (no fence needed).
  function codeState(lang) {
    return { ctx: 'code', fence: '\u0000', family: codeFamily(lang) };
  }

  function codeFamily(lang) {
    const l = String(lang || '').toLowerCase();
    if (!l) return 'plain';
    if (/^(sh|bash|shell|zsh|fish)$/.test(l)) return 'shell';
    if (/^sql$/.test(l)) return 'sql';
    if (/^(diff|patch)$/.test(l)) return 'diff';
    if (/^(json|yaml|yml|toml|ini|xml|html|css|md|markdown|txt|text)$/.test(l)) return 'data';
    return 'common';
  }

  function tokenizeCode(line, family) {
    if (family === 'diff') {
      if (/^@@/.test(line)) return [['sm-meta-diff-range', line]];
      if (/^\+/.test(line)) return [['sm-markup-inserted', line]];
      if (/^-/.test(line)) return [['sm-markup-deleted', line]];
      return [['sm-diff-context', line]];
    }
    const out = [];
    let i = 0;
    const n = line.length;
    let plain = '';
    const flush = () => { if (plain) { out.push(['sm-code', plain]); plain = ''; } };
    const kws = KW_SET[family] || KW_SET.common;
    while (i < n) {
      const ch = line[i];
      const rest = line.slice(i);
      let m;
      if ((family === 'shell' || family === 'data' || family === 'common') && ch === '#' &&
          (family !== 'common' || !/^#(include|define|if|endif|pragma)/.test(rest))) {
        flush(); out.push(['sm-comment', rest]); break;
      }
      if (family === 'common' && rest.startsWith('//')) { flush(); out.push(['sm-comment', rest]); break; }
      if (family === 'sql' && rest.startsWith('--')) { flush(); out.push(['sm-comment', rest]); break; }
      if ((m = /^\/\*.*?\*\//.exec(rest))) { flush(); out.push(['sm-comment', m[0]]); i += m[0].length; continue; }
      if ((m = /^("""|''')[\s\S]*?\1/.exec(rest)) || (m = /^(["'`])(?:\\.|(?!\1).)*\1/.exec(rest))) {
        flush(); out.push(['sm-string', m[0]]); i += m[0].length; continue;
      }
      if ((m = /^(0x[0-9a-fA-F_]+|\d[\d_]*(\.\d+)?([eE][-+]?\d+)?)\b/.exec(rest))) {
        flush(); out.push(['sm-constant-numeric', m[0]]); i += m[0].length; continue;
      }
      if ((m = /^[A-Za-z_][\w]*/.exec(rest))) {
        const word = m[0];
        const after = rest.slice(word.length);
        flush();
        if (kws.has(word) || (family === 'sql' && kws.has(word.toLowerCase()))) out.push(['sm-keyword', word]);
        else if (/^\s*\(/.test(after)) out.push(['sm-entity-name-function', word]);
        else if (/^[A-Z]/.test(word) && family === 'common') out.push(['sm-entity-name-type', word]);
        else out.push(['sm-code', word]);
        i += word.length;
        continue;
      }
      if ((m = /^[-+*/%=<>!&|^~?:]+/.exec(rest))) { flush(); out.push(['sm-keyword-operator', m[0]]); i += m[0].length; continue; }
      plain += ch;
      i += 1;
    }
    flush();
    return out.length ? out : [['sm-code', line]];
  }

  function tokenizeInline(line, base) {
    // Build a per-character class map, then coalesce runs.
    const cls = new Array(line.length).fill(base);
    const claimed = new Array(line.length).fill(false);
    for (const [re, groups] of INLINE_RULES) {
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(line))) {
        if (m[0].length === 0) { re.lastIndex++; continue; }
        const start = m.index;
        // Skip a match that overlaps something already claimed (code spans win).
        let overlap = false;
        for (let k = start; k < start + m[0].length; k++) if (claimed[k]) { overlap = true; break; }
        if (overlap) continue;
        if (groups.length === 1) {
          for (let k = start; k < start + m[0].length; k++) { cls[k] = groups[0]; claimed[k] = true; }
        } else {
          let pos = start;
          for (let g = 1; g < m.length; g++) {
            const part = m[g] || '';
            for (let k = pos; k < pos + part.length; k++) { cls[k] = groups[g - 1]; claimed[k] = true; }
            pos += part.length;
          }
        }
      }
    }
    const out = [];
    let i = 0;
    while (i < line.length) {
      let j = i + 1;
      while (j < line.length && cls[j] === cls[i]) j++;
      out.push([cls[i], line.slice(i, j)]);
      i = j;
    }
    return out;
  }

  function tokenizeMarkdownLine(line) {
    let m;
    if ((m = /^(#{1,6})(\s+)(.+)$/.exec(line))) {
      return [['sm-punctuation-heading', m[1]], [null, m[2]], ['sm-markup-heading', m[3]]];
    }
    if ((m = /^(\s*)(>)(\s*)(.*)$/.exec(line))) {
      return [[null, m[1]], ['sm-punctuation-blockquote', m[2]], [null, m[3]], ['sm-markup-quote', m[4]]];
    }
    if ((m = /^(\s*)([-*+]|\d+\.)(\s+)(.*)$/.exec(line))) {
      return [[null, m[1]], ['sm-markup-list', m[2]], [null, m[3]]].concat(tokenizeInline(m[4], 'sm-text'));
    }
    return tokenizeInline(line, 'sm-text');
  }

  function initialState() {
    return { ctx: 'main', fence: null, family: null };
  }

  function isPromptLine(line) {
    return PROMPT_RE.test(line);
  }

  // The `◎` line that opens a turn (single- or multi-line prompt).
  function isPromptStart(line) {
    return PROMPT_OPEN_RE.test(line) && line.trim() !== '◎';
  }

  function isPromptEnd(line) {
    return / ▶\s*$/.test(line);
  }

  // Returns segments for `line` and advances `st` (the same object) to the
  // state the next line starts in — the sublime-syntax push/pop, flattened.
  function tokenizeLine(line, st) {
    if (st.ctx === 'code') {
      if (st.fence === '\u0000') return tokenizeCode(line, st.family);   // a whole file: no fence to close
      const closeRe = new RegExp('^' + st.fence.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '```\\s*$');
      if (closeRe.test(line) || /^\s*```\s*$/.test(line)) {
        st.ctx = 'conversation'; st.fence = null; st.family = null;
        return [['sm-punctuation-code', line]];
      }
      return tokenizeCode(line, st.family);
    }
    let m;
    if (SEP_RE.test(line)) { st.ctx = 'main'; return [['sm-input-sep', line]]; }
    if (DONE_RE.test(line)) { st.ctx = 'main'; return [['sm-meta-done', line]]; }
    if ((m = PROMPT_RE.exec(line))) {
      st.ctx = 'conversation';
      return [['sm-prompt-marker', '◎ '], ['sm-prompt', m[1]], ['sm-prompt-marker', m[2]],
              [null, line.slice(2 + m[1].length + m[2].length)]];
    }
    if (PROMPT_OPEN_RE.test(line)) {
      // A prompt that continues on later lines, or the open composer
      // (`◎ draft…`): prompt colour until a ` ▶`, as the Sublime syntax does.
      st.ctx = 'prompt';
      return [['sm-prompt-marker', '◎ '], ['sm-prompt', line.slice(2)]];
    }
    if (st.ctx === 'prompt') {
      if ((m = /^(.*?)( ▶)\s*$/.exec(line))) {
        st.ctx = 'conversation';
        return [['sm-prompt', m[1]], ['sm-prompt-marker', m[2]], [null, line.slice(m[1].length + m[2].length)]];
      }
      return [['sm-prompt', line]];
    }
    if (st.ctx === 'main') {
      return [[null, line]];
    }
    if ((m = FENCE_RE.exec(line))) {
      st.ctx = 'code'; st.fence = m[1]; st.family = codeFamily(m[3]);
      return [[null, m[1]], ['sm-punctuation-code', m[2]], ['sm-constant-language-name', m[3]]];
    }
    for (const [re, cls] of LINE_RULES) {
      if (re.test(line)) return [[cls, line]];
    }
    return tokenizeMarkdownLine(line);
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function toHtml(text, ctx) {
    const st = initialState();
    if (ctx) st.ctx = ctx;           // 'conversation' for a bare fenced block
    const lines = String(text || '').split('\n');
    const out = [];
    for (const line of lines) {
      const segs = tokenizeLine(line, st);
      let html = '';
      for (const [cls, s] of segs) {
        if (!s) continue;
        html += cls ? '<span class="' + cls + '">' + esc(s) + '</span>' : esc(s);
      }
      // A code line carries its block background across the full width.
      const inCode = st.ctx === 'code' || (segs.length === 1 && segs[0][0] === 'sm-punctuation-code');
      out.push(inCode ? '<span class="sm-code-line">' + (html || ' ') + '</span>' : html);
    }
    return out.join('\n');
  }

  global.SubmarineHL = {
    initialState: initialState,
    codeState: codeState,
    langForPath: langForPath,
    tokenizeLine: tokenizeLine,
    tokenizeInline: tokenizeInline,
    toHtml: toHtml,
    isPromptLine: isPromptLine,
    isPromptStart: isPromptStart,
    isPromptEnd: isPromptEnd,
    esc: esc,
  };
})(typeof window !== 'undefined' ? window : globalThis);
