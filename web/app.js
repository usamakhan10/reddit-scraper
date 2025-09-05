(() => {
  const els = {
    status: document.getElementById('statusBar'),
    newKeyword: document.getElementById('newKeyword'),
    addKeywordBtn: document.getElementById('addKeywordBtn'),
    keywordsList: document.getElementById('keywordsList'),
    filterKeyword: document.getElementById('filterKeyword'),
    filterSubreddit: document.getElementById('filterSubreddit'),
    filterKind: document.getElementById('filterKind'),
    filterFrom: document.getElementById('filterFrom'),
    filterTo: document.getElementById('filterTo'),
    applyFiltersBtn: document.getElementById('applyFiltersBtn'),
    resetFiltersBtn: document.getElementById('resetFiltersBtn'),
    matchesList: document.getElementById('matchesList'),
    repliesList: document.getElementById('repliesList'),
    prevPageBtn: document.getElementById('prevPageBtn'),
    nextPageBtn: document.getElementById('nextPageBtn'),
    pageInfo: document.getElementById('pageInfo'),
    replyPageInfo: document.getElementById('replyPageInfo'),
    authModal: document.getElementById('authModal'),
    apiUser: document.getElementById('apiUser'),
    apiPass: document.getElementById('apiPass'),
    authSaveBtn: document.getElementById('authSaveBtn'),
    authCancelBtn: document.getElementById('authCancelBtn'),
    activityList: document.getElementById('activityList'),
    refreshActivityBtn: document.getElementById('refreshActivityBtn'),
  };

  let page = 1;
  const size = 20;
  let currentFilters = {};
  let replyPage = 1;
  let currentView = 'keywordsView';

  function setStatus(msg, ok=false) {
    els.status.textContent = msg;
    els.status.style.color = ok ? 'var(--ok)' : 'var(--muted)';
  }

  function b64(s) {
    return btoa(unescape(encodeURIComponent(s)));
  }

  function getAuthHeader() {
    const token = sessionStorage.getItem('apiAuth');
    return token ? { 'Authorization': token } : {};
  }

  async function api(path, opts={}) {
    const headers = Object.assign({ 'Content-Type': 'application/json' }, getAuthHeader(), opts.headers || {});
    const resp = await fetch(path, Object.assign({}, opts, { headers }));
    if (resp.status === 401) {
      showAuthModal();
      throw new Error('Unauthorized');
    }
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(text || resp.statusText);
    }
    const ct = resp.headers.get('content-type') || '';
    if (ct.includes('application/json')) return resp.json();
    return resp.text();
  }

  // --- View switching ---
  function switchView(view) {
    currentView = view;
    document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    const active = document.getElementById(view);
    if (active) active.classList.add('active');
    document.querySelector(`.nav-btn[data-view="${view}"]`)?.classList.add('active');
    if (view === 'keywordsView') {
      loadKeywordsSummary();
    } else if (view === 'activityView') {
      loadActivity();
    } else if (view === 'postsView') {
      // default to posts
      els.filterKind.value = 'post';
      page = 1;
      loadPosts();
    } else if (view === 'repliesView') {
      // filter kind optional; use current filterKind
      replyPage = 1;
      loadReplies();
    }
  }
  document.querySelectorAll('.nav-btn').forEach(b => {
    b.addEventListener('click', () => switchView(b.dataset.view));
  });

  function showAuthModal() {
    els.authModal.classList.remove('hidden');
  }
  function hideAuthModal() {
    els.authModal.classList.add('hidden');
  }

  els.authSaveBtn.addEventListener('click', () => {
    const u = els.apiUser.value.trim();
    const p = els.apiPass.value.trim();
    if (u && p) {
      sessionStorage.setItem('apiAuth', 'Basic ' + b64(`${u}:${p}`));
      hideAuthModal();
      refreshAll();
    }
  });
  els.authCancelBtn.addEventListener('click', () => hideAuthModal());

  // --- Keywords ---
  async function loadKeywordsSummary() {
    try {
      const data = await api('/dashboard/keywords');
      renderKeywords(data);
      populateKeywordFilter(await api('/keywords'));
      setStatus('Keywords summary loaded', true);
    } catch (e) {
      setStatus('Failed to load keywords summary');
      console.error(e);
    }
  }

  function renderKeywords(list) {
    els.keywordsList.innerHTML = '';
    list.forEach(k => {
      const li = document.createElement('li');
      const left = document.createElement('span');
      left.textContent = k.keyword;
      const stat = document.createElement('span');
      stat.className = 'kw-stat';
      const posts = k.posts_count ?? 0;
      const replies = k.replies_count ?? 0;
      stat.textContent = `posts: ${posts} · replies: ${replies}`;
      const btn = document.createElement('button');
      btn.textContent = 'Delete';
      btn.className = 'danger';
      btn.addEventListener('click', async () => {
        if (!confirm(`Delete keyword "${k.keyword}"?`)) return;
        try {
          await api(`/keywords/${k.id}`, { method: 'DELETE' });
          await loadKeywordsSummary();
          await loadPosts();
        } catch (e) {
          alert('Failed to delete: ' + e.message);
        }
      });
      li.appendChild(left);
      li.appendChild(stat);
      li.appendChild(btn);
      els.keywordsList.appendChild(li);
    });
  }

  function populateKeywordFilter(list) {
    const sel = els.filterKeyword;
    const val = sel.value;
    sel.innerHTML = '<option value="">All</option>' + list.map(k => `<option value="${k.id}">${k.keyword}</option>`).join('');
    // keep previous selection if still present
    for (const opt of sel.options) {
      if (String(opt.value) === String(val)) opt.selected = true;
    }
  }

  els.addKeywordBtn.addEventListener('click', async () => {
    const kw = els.newKeyword.value.trim();
    if (!kw) return;
    try {
      await api('/keywords', { method: 'POST', body: JSON.stringify({ keyword: kw }) });
      els.newKeyword.value = '';
      await loadKeywordsSummary();
      if (currentView === 'postsView') await loadPosts();
    } catch (e) {
      alert('Failed to add: ' + e.message);
    }
  });

  // --- Activity ---
  async function loadActivity() {
    try {
      const data = await api('/dashboard/activity?limit=20');
      els.activityList.innerHTML = '';
      (data.items || []).forEach(m => {
        els.activityList.appendChild(renderActivityItem(m));
      });
      setStatus('Activity loaded', true);
    } catch (e) {
      setStatus('Failed to load activity');
      console.error(e);
    }
  }
  function renderActivityItem(m) {
    const div = document.createElement('div');
    div.className = 'match';
    const created = new Date((m.created_at || 0) * 1000).toISOString();
    const kw = (m.keywords || []).map(k => `<span class="kw">${escapeHtml(k)}</span>`).join(', ');
    div.innerHTML = `
      <div class="meta">${m.kind.toUpperCase()} · r/${m.subreddit || '-'} · ${created}</div>
      <div class="title">${m.title ? escapeHtml(m.title) : ''}</div>
      ${m.body ? `<div class="body">${escapeHtml(m.body.slice(0, 200))}${m.body.length > 200 ? '…' : ''}</div>` : ''}
      <div class="keywords">Keywords: ${kw} <span class="activity-reply-count">· replies: ${m.reply_count || 0}</span></div>
      <div class="actions">
        <a href="${m.reddit_url}" target="_blank" rel="noreferrer">Open Reddit</a>
      </div>
    `;
    return div;
  }
  els.refreshActivityBtn.addEventListener('click', loadActivity);

  // --- Filters & Matches ---
  function tsFromLocalInput(v) {
    if (!v) return undefined;
    const ms = Date.parse(v);
    if (isNaN(ms)) return undefined;
    return Math.floor(ms / 1000);
  }

  function buildQuery() {
    const params = new URLSearchParams();
    if (els.filterKeyword.value) params.set('keyword_id', els.filterKeyword.value);
    if (els.filterSubreddit.value.trim()) params.set('subreddit', els.filterSubreddit.value.trim());
    if (els.filterKind.value) params.set('kind', els.filterKind.value);
    const fromTs = tsFromLocalInput(els.filterFrom.value);
    const toTs = tsFromLocalInput(els.filterTo.value);
    if (fromTs) params.set('from_ts', fromTs);
    if (toTs) params.set('to_ts', toTs);
    params.set('page', page);
    params.set('size', size);
    currentFilters = Object.fromEntries(params.entries());
    return params.toString();
  }

  function renderMatchItem(m) {
    const div = document.createElement('div');
    div.className = 'match';
    const created = new Date((m.created_at || 0) * 1000).toISOString();
    div.innerHTML = `
      <div class="meta">${m.kind.toUpperCase()} · r/${m.subreddit || '-'} · ${created}</div>
      <div class="title">${m.title ? escapeHtml(m.title) : ''}</div>
      ${m.body ? `<div class="body">${escapeHtml(m.body.slice(0, 240))}${m.body.length > 240 ? '…' : ''}</div>` : ''}
      <div class="keywords">Keywords: ${(m.keywords || []).map(k => `<span class="kw">${escapeHtml(k)}</span>`).join(', ')}</div>
      <div class="actions">
        <a href="${m.reddit_url}" target="_blank" rel="noreferrer">Open Reddit</a>
        <button class="loadReplies">Load replies</button>
      </div>
      <div class="replies hidden"></div>
    `;
    const btn = div.querySelector('button.loadReplies');
    const repliesEl = div.querySelector('.replies');
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const replies = await api(`/replies/${m.id}`);
        repliesEl.classList.remove('hidden');
        repliesEl.innerHTML = replies.length ? replies.map(r => renderReply(r)).join('') : '<div class="reply">No replies</div>';
      } catch (e) {
        alert('Failed to load replies: ' + e.message);
      } finally {
        btn.disabled = false;
      }
    });
    return div;
  }

  function renderReply(r) {
    const created = new Date((r.created_at || 0) * 1000).toISOString();
    const author = r.author_name || r.author_id || 'unknown';
    const content = r.content ? escapeHtml(r.content) : '';
    const link = r.url ? `<a href="${r.url}" target="_blank" rel="noreferrer">open</a>` : '';
    return `
      <div class="reply">
        <div class="meta">${created} · ${escapeHtml(String(author))} · ${link}</div>
        <div class="content">${content}</div>
      </div>
    `;
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  async function loadPosts() {
    try {
      const qs = buildQuery();
      const data = await api(`/posts?${qs}`);
      els.matchesList.innerHTML = '';
      (data.items || []).forEach(m => {
        els.matchesList.appendChild(renderMatchItem(m));
      });
      els.pageInfo.textContent = `Page ${data.page}`;
      setStatus('Posts loaded', true);
    } catch (e) {
      setStatus('Failed to load posts');
      console.error(e);
    }
  }

  els.applyFiltersBtn.addEventListener('click', () => { if (currentView === 'postsView') { page = 1; loadPosts(); } else if (currentView === 'repliesView') { replyPage = 1; loadReplies(); } });
  els.resetFiltersBtn.addEventListener('click', () => {
    els.filterKeyword.value = '';
    els.filterSubreddit.value = '';
    els.filterKind.value = '';
    els.filterFrom.value = '';
    els.filterTo.value = '';
    page = 1;
    if (currentView === 'postsView') loadPosts();
    if (currentView === 'repliesView') loadReplies();
  });
  els.prevPageBtn.addEventListener('click', () => { if (page > 1) { page -= 1; loadPosts(); } });
  els.nextPageBtn.addEventListener('click', () => { page += 1; loadPosts(); });

  // Replies view
  async function loadReplies() {
    try {
      const params = new URLSearchParams();
      if (els.filterKeyword.value) params.set('keyword_id', els.filterKeyword.value);
      if (els.filterSubreddit.value.trim()) params.set('subreddit', els.filterSubreddit.value.trim());
      if (els.filterKind.value) params.set('kind', els.filterKind.value);
      const fromTs = tsFromLocalInput(els.filterFrom.value);
      const toTs = tsFromLocalInput(els.filterTo.value);
      if (fromTs) params.set('reply_from_ts', fromTs);
      if (toTs) params.set('reply_to_ts', toTs);
      params.set('page', replyPage);
      params.set('size', size);
      const data = await api(`/replies?${params.toString()}`);
      els.repliesList.innerHTML = '';
      (data.items || []).forEach(r => {
        els.repliesList.appendChild(renderReplyItem(r));
      });
      els.replyPageInfo.textContent = `Page ${data.page}`;
      setStatus('Replies loaded', true);
    } catch (e) {
      setStatus('Failed to load replies');
      console.error(e);
    }
  }

  function renderReplyItem(r) {
    const div = document.createElement('div');
    div.className = 'match';
    const keywords = (r.keywords || []).map(k => `<span class="kw">${escapeHtml(k)}</span>`).join(', ');
    const mCreated = new Date((r.match_created_at || 0) * 1000).toISOString();
    const replyCreated = new Date((r.reply_created_at || 0) * 1000).toISOString();
    div.innerHTML = `
      <div class="meta">${r.kind.toUpperCase()} · r/${r.subreddit || '-'} · ${mCreated}</div>
      <div class="title">${r.title ? escapeHtml(r.title) : ''}</div>
      <div class="keywords">Keywords: ${keywords}</div>
      <div class="replies">
        <div class="reply">
          <div class="meta">${replyCreated} · ${escapeHtml(String(r.author_name || r.author_id || ''))} · <a href="${r.reply_url}" target="_blank" rel="noreferrer">open reply</a></div>
          <div class="content">${r.reply_content ? escapeHtml(r.reply_content) : ''}</div>
        </div>
      </div>
      <div class="actions">
        <a href="${r.reddit_url}" target="_blank" rel="noreferrer">Open Reddit</a>
      </div>
    `;
    return div;
  }

  async function refreshAll() {
    await loadKeywordsSummary();
    switchView('keywordsView');
  }

  // Kick off
  refreshAll();
})();
