// CodeMirror 6 front for the console: a read-only sheet that folds per ◎ turn
// and searches with Cmd/Ctrl-F, and a composer with history and a real
// multi-line editing model. Loaded as an ES module from esm.sh through the
// import map in index.html; every @codemirror/* package is externalised there
// so the page holds one @codemirror/state, which CM6 requires.
//
// Offline (no CDN) the import below rejects, `window.SubmarineEditor.ready`
// resolves to null, and app.js keeps its <pre> + <textarea> fallback, painted
// by the same highlight.js tokenizer. The console never depends on the CDN.

const settle = (api) => {
  if (window.__submarineEditorReady) window.__submarineEditorReady(api);
};

let cm;
try {
  const [state, view, language, commands, search] = await Promise.all([
    import('@codemirror/state'),
    import('@codemirror/view'),
    import('@codemirror/language'),
    import('@codemirror/commands'),
    import('@codemirror/search'),
  ]);
  cm = { state, view, language, commands, search };
} catch (e) {
  console.warn('[webui] CodeMirror unavailable, using the plain sheet:', e && e.message);
  settle(null);
}

if (cm) {
  const { EditorState, Compartment, RangeSetBuilder } = cm.state;
  const { EditorView, Decoration, ViewPlugin, keymap, drawSelection, highlightActiveLine,
          lineNumbers, placeholder, scrollPastEnd } = cm.view;
  const { foldGutter, foldService, codeFolding, foldKeymap, foldAll, unfoldAll,
          foldCode, unfoldCode, foldedRanges } = cm.language;
  const { history, historyKeymap, defaultKeymap, insertNewlineAndIndent } = cm.commands;
  const { searchKeymap, highlightSelectionMatches } = cm.search;
  const HL = window.SubmarineHL;

  // ── highlighting: the sheet tokenizer as decorations ────────────────────
  // Recomputed for the whole document on every change. Sheets are a few
  // hundred KB at most and the tokenizer is line-local, so this stays cheap;
  // a diffed dispatch (see setText) keeps the change itself small.

  const markCache = new Map();
  function mark(cls) {
    let d = markCache.get(cls);
    if (!d) { d = Decoration.mark({ class: cls }); markCache.set(cls, d); }
    return d;
  }
  const codeLine = Decoration.line({ class: 'sm-code-line' });

  function buildDecorations(doc, startCtx) {
    const builder = new RangeSetBuilder();
    // startCtx: a context name, or a full tokenizer state (the file view
    // passes HL.codeState(lang) so every line is code of that language).
    const st = (startCtx && typeof startCtx === 'object') ? Object.assign({}, startCtx) : HL.initialState();
    if (typeof startCtx === 'string') st.ctx = startCtx;
    for (let n = 1; n <= doc.lines; n++) {
      const line = doc.line(n);
      const wasCode = st.ctx === 'code';
      const segs = HL.tokenizeLine(line.text, st);
      const nowCode = st.ctx === 'code';
      if ((wasCode || nowCode) && !(startCtx && typeof startCtx === 'object')) builder.add(line.from, line.from, codeLine);
      let pos = line.from;
      for (const [cls, s] of segs) {
        if (!s) continue;
        const end = pos + s.length;
        if (cls) builder.add(pos, end, mark(cls));
        pos = end;
      }
    }
    return builder.finish();
  }

  function highlighter(startCtx) {
    return ViewPlugin.fromClass(class {
      constructor(view) { this.decorations = buildDecorations(view.state.doc, startCtx); }
      update(u) { if (u.docChanged) this.decorations = buildDecorations(u.state.doc, startCtx); }
    }, { decorations: (v) => v.decorations });
  }

  // ── folding: one fold per ◎ … ▶ turn, like Fold.tmPreferences ──────────

  function turnFold(state, lineStart, lineEnd) {
    const doc = state.doc;
    const first = doc.lineAt(lineStart);
    if (!HL.isPromptStart(first.text)) return null;
    // A multi-line prompt folds from its ` ▶` line, so the question stays.
    let head = first;
    if (!HL.isPromptEnd(first.text)) {
      let found = null;
      for (let n = first.number + 1; n <= Math.min(doc.lines, first.number + 200); n++) {
        const line = doc.line(n);
        if (/^◎ /.test(line.text)) break;
        if (HL.isPromptEnd(line.text)) { found = line; break; }
      }
      if (!found) return null;   // the open composer: nothing to fold
      head = found;
    }
    let last = head;
    for (let n = head.number + 1; n <= doc.lines; n++) {
      const line = doc.line(n);
      if (/^◎ /.test(line.text) || /^[┄─━-]{8,}\s*$/.test(line.text)) break;
      if (line.text.trim()) last = line;
    }
    if (last.number <= head.number) return null;
    return { from: head.to, to: last.to };
  }

  // ── themes: the tmTheme's global settings ───────────────────────────────

  const mono = 'ui-monospace, SFMono-Regular, Menlo, "JetBrains Mono", monospace';

  const sheetTheme = EditorView.theme({
    '&': { backgroundColor: '#1f2430', color: '#cbccc6', height: '100%', fontSize: 'calc(12.5px * var(--fs, 1))' },
    '.cm-scroller': { fontFamily: mono, lineHeight: '1.55', overflow: 'auto' },
    '.cm-content': { padding: '8px 0 24px', caretColor: '#ffcc66' },
    '.cm-line': { padding: '0 12px 0 6px' },
    '&.cm-focused': { outline: 'none' },
    '.cm-activeLine': { backgroundColor: '#191e2a' },
    '.cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection': { backgroundColor: '#33415e !important' },
    '.cm-gutters': { backgroundColor: '#1f2430', color: '#707a8c', border: 'none', minWidth: '18px' },
    '.cm-foldGutter .cm-gutterElement': { padding: '0 2px 0 6px', cursor: 'pointer', color: '#5c6773' },
    '.cm-foldGutter .cm-gutterElement:hover': { color: '#cbccc6' },
    '.cm-foldPlaceholder': { backgroundColor: '#262d3a', border: '1px solid #33415e', color: '#8a93a3', margin: '0 4px', padding: '0 6px', borderRadius: '3px' },
    '.cm-panels': { backgroundColor: '#1d2027', color: '#dfe3ea', borderBottom: '1px solid #2f333d' },
    '.cm-panels.cm-panels-top': { borderBottom: '1px solid #2f333d' },
    '.cm-searchMatch': { backgroundColor: '#3a3320', outline: '1px solid #ffcc6644' },
    '.cm-searchMatch.cm-searchMatch-selected': { backgroundColor: '#5a4a1c' },
    '.cm-selectionMatch': { backgroundColor: '#33415e66' },
    '.cm-textfield': { backgroundColor: '#23262e', color: '#dfe3ea', border: '1px solid #2f333d' },
    '.cm-button': { backgroundColor: '#23262e', color: '#dfe3ea', border: '1px solid #2f333d', backgroundImage: 'none' },
    '.cm-panel.cm-search label': { color: '#8b93a1' },
  }, { dark: true });

  const composerTheme = EditorView.theme({
    '&': { backgroundColor: '#1f2430', color: '#cbccc6', fontSize: 'calc(13px * var(--fs, 1))', borderRadius: '5px', border: '1px solid #2f333d' },
    '&.cm-focused': { outline: 'none', borderColor: '#4a5568' },
    '.cm-scroller': { fontFamily: mono, lineHeight: '1.5', maxHeight: '40vh', overflow: 'auto' },
    '.cm-content': { padding: '6px 0', caretColor: '#ffcc66', minHeight: '3.2em' },
    '.cm-line': { padding: '0 8px' },
    '.cm-placeholder': { color: '#8b93a1', fontStyle: 'normal' },
    '.cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection': { backgroundColor: '#33415e !important' },
    '.cm-activeLine': { backgroundColor: 'transparent' },
  }, { dark: true });

  // ── the sheet ───────────────────────────────────────────────────────────

  // Touch: a tap on a `◎ … ▶` line folds or unfolds that turn — the gutter
  // arrow is too small a target for a thumb. Mouse users keep click-to-select.
  const COARSE = window.matchMedia('(pointer: coarse)');

  function isFoldedAt(state, pos) {
    let hit = false;
    foldedRanges(state).between(pos, pos, () => { hit = true; return false; });
    return hit;
  }

  const tapToFold = EditorView.domEventHandlers({
    click(event, view) {
      if (!COARSE.matches) return false;
      const pos = view.posAtCoords({ x: event.clientX, y: event.clientY });
      if (pos === null) return false;
      const line = view.state.doc.lineAt(pos);
      if (!HL.isPromptStart(line.text) && !HL.isPromptEnd(line.text)) return false;
      view.dispatch({ selection: { anchor: line.to } });
      // Folded turns hide their content right after the prompt line's end.
      if (isFoldedAt(view.state, line.to + 1)) unfoldCode(view); else foldCode(view);
      return true;
    },
  });

  function createSheet(parent) {
    const view = new EditorView({
      parent,
      state: EditorState.create({
        doc: '',
        extensions: [
          EditorState.readOnly.of(true),
          EditorView.editable.of(false),
          EditorView.lineWrapping,
          drawSelection(),
          highlightActiveLine(),
          highlightSelectionMatches(),
          codeFolding({ placeholderText: '⋯' }),
          foldGutter({ openText: '▾', closedText: '▸' }),
          foldService.of(turnFold),
          highlighter(null),
          sheetTheme,
          tapToFold,
          keymap.of([...searchKeymap, ...foldKeymap, ...defaultKeymap]),
        ],
      }),
    });

    function atBottom(slack) {
      const el = view.scrollDOM;
      return el.scrollTop + el.clientHeight >= el.scrollHeight - (slack || 24);
    }

    function scrollToEnd() {
      view.dispatch({ effects: EditorView.scrollIntoView(view.state.doc.length, { y: 'end' }) });
    }

    // Replace the text with the smallest change that gets there: folds and
    // the scroll position survive, and a growing reply is an append.
    function setText(text, opts) {
      const o = opts || {};
      const cur = view.state.doc.toString();
      const next = String(text || '');
      const follow = o.follow === true || (o.follow !== false && atBottom());
      if (cur !== next) {
        let a = 0;
        const max = Math.min(cur.length, next.length);
        while (a < max && cur.charCodeAt(a) === next.charCodeAt(a)) a++;
        let b = 0;
        while (b < max - a && cur.charCodeAt(cur.length - 1 - b) === next.charCodeAt(next.length - 1 - b)) b++;
        view.dispatch({
          changes: { from: a, to: cur.length - b, insert: next.slice(a, next.length - b) },
        });
      }
      if (follow) requestAnimationFrame(scrollToEnd);
    }

    return {
      view,
      dom: view.dom,
      setText,
      scrollToEnd,
      atBottom,
      getText: () => view.state.doc.toString(),
      foldAll: () => foldAll(view),
      unfoldAll: () => unfoldAll(view),
      openSearch: () => { for (const k of searchKeymap) if (k.key === 'Mod-f') return k.run(view); },
      focus: () => view.focus(),
      destroy: () => view.destroy(),
    };
  }

  // ── a file (the code view) ──────────────────────────────────────────────
  // Read-only, line numbers, the sheet's code colours for the file's
  // language, opened at a line.

  const fileTheme = EditorView.theme({
    '.cm-gutters': { backgroundColor: '#1a1e29', color: '#5c6773', borderRight: '1px solid #2f333d', minWidth: '40px' },
    '.cm-lineNumbers .cm-gutterElement': { padding: '0 8px 0 10px', minWidth: '36px' },
    '.cm-activeLineGutter': { backgroundColor: '#262d3a', color: '#cbccc6' },
    '.cm-activeLine': { backgroundColor: '#262d3a55' },
  }, { dark: true });

  function createFileView(parent, opts) {
    const o = opts || {};
    const lang = o.lang || HL.langForPath(o.path);
    const view = new EditorView({
      parent,
      state: EditorState.create({
        doc: String(o.text || ''),
        extensions: [
          EditorState.readOnly.of(true),
          EditorView.editable.of(false),
          lineNumbers(),
          drawSelection(),
          highlightActiveLine(),
          highlightSelectionMatches(),
          highlighter(HL.codeState(lang)),
          sheetTheme,
          fileTheme,
          keymap.of([...searchKeymap, ...defaultKeymap]),
        ],
      }),
    });
    function goTo(lineNo) {
      const n = Math.max(1, Math.min(view.state.doc.lines, Number(lineNo) || 1));
      const line = view.state.doc.line(n);
      view.dispatch({
        selection: { anchor: line.from },
        effects: EditorView.scrollIntoView(line.from, { y: 'center' }),
      });
    }
    if (o.line) requestAnimationFrame(() => goTo(o.line));
    return {
      view,
      dom: view.dom,
      goTo,
      openSearch: () => { for (const k of searchKeymap) if (k.key === 'Mod-f') return k.run(view); },
      focus: () => view.focus(),
      destroy: () => view.destroy(),
    };
  }

  // ── the composer ────────────────────────────────────────────────────────

  function createComposer(parent, opts) {
    const o = opts || {};
    const placeholderText = new Compartment();
    const submit = (view) => { if (o.onSubmit) o.onSubmit(view.state.doc.toString()); return true; };
    const enter = (view) => {
      if (o.submitOnEnter && !o.submitOnEnter()) return insertNewlineAndIndent(view);
      return submit(view);
    };
    const view = new EditorView({
      parent,
      state: EditorState.create({
        doc: '',
        extensions: [
          history(),
          drawSelection(),
          EditorView.lineWrapping,
          placeholderText.of(placeholder(o.placeholder || '')),
          highlighter('conversation'),
          composerTheme,
          keymap.of([
            { key: 'Enter', run: enter, shift: insertNewlineAndIndent },
            { key: 'Mod-Enter', run: submit },
            { key: 'Escape', run: (v) => { v.contentDOM.blur(); return true; } },
            ...historyKeymap,
            ...defaultKeymap,
          ]),
          EditorView.updateListener.of((u) => {
            if (u.docChanged && o.onInput) o.onInput(u.state.doc.toString());
            if (u.focusChanged && o.onFocus) o.onFocus(u.view.hasFocus);
          }),
        ],
      }),
    });
    return {
      view,
      dom: view.dom,
      getValue: () => view.state.doc.toString(),
      setValue: (text) => view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: String(text || '') },
      }),
      setPlaceholder: (text) => view.dispatch({
        effects: placeholderText.reconfigure(placeholder(text || '')),
      }),
      focus: () => view.focus(),
      destroy: () => view.destroy(),
    };
  }

  settle({ createSheet, createComposer, createFileView, cm });
}
