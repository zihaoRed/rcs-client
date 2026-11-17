'use strict';

const $ = (sel) => document.querySelector(sel);
const state = {
  contacts: [],
  threads: [],
  messages: [],
  active: null,
  auth: {},
  typingTimer: null,
  es: null,
};

function esc(s) {
  return String(s == null ? '' : s)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

async function api(path, opts) {
  const res = await fetch(path, Object.assign({
    headers: { 'Content-Type': 'application/json' },
  }, opts || {}));
  const text = await res.text();
  let data = {};
  try { data = JSON.parse(text); } catch (_) { data = { raw: text }; }
  if (!res.ok) {
    const err = new Error((data && (data.error || data.message)) || `HTTP ${res.status}`);
    err.data = data;
    throw err;
  }
  return data;
}

/* ---------- formatting ---------- */
function asDate(ts) {
  const d = new Date(ts);
  return isNaN(d.getTime()) ? null : d;
}
function pad(n) { return String(n).padStart(2, '0'); }
function formatShort(ts) {
  const d = asDate(ts);
  if (!d) return '';
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (now - d < 7 * 864e5) return ['周日','周一','周二','周三','周四','周五','周六'][d.getDay()];
  return `${d.getMonth() + 1}/${d.getDate()}`;
}
function formatTime(ts) {
  const d = asDate(ts);
  return d ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : '';
}
function dateLabel(ts) {
  const d = asDate(ts);
  if (!d) return '';
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const that = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const diff = Math.round((today - that) / 864e5);
  if (diff === 0) return '今天';
  if (diff === 1) return '昨天';
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}
function initialOf(name, peer) {
  const n = String(name || '').trim();
  if (n && n !== peer) {
    const parts = n.split(/\s+/).filter(Boolean);
    if (parts.length >= 2 && /^[A-Za-z]/.test(parts[0])) {
      return (parts[0][0] + parts[1][0]).toUpperCase();
    }
    return n.slice(0, 2);
  }
  const digits = String(peer || '').replace(/\D/g, '');
  return digits.slice(-2) || '?';
}
const COLORS = [
  ['#dbe7ff', '#24508f'], ['#dcefe3', '#1e6b3a'], ['#fde8d8', '#8a4b12'],
  ['#f3e2ff', '#6b3f8c'], ['#ffe1e4', '#8c2b35'], ['#e4e8ee', '#3d4b5c'],
];
function avatarStyle(peer) {
  let h = 0;
  for (const ch of String(peer || '')) h = (h * 31 + ch.charCodeAt(0)) | 0;
  const c = COLORS[Math.abs(h) % COLORS.length];
  return `background:${c[0]};color:${c[1]}`;
}

/* ---------- rendering ---------- */
function renderThreads(filter) {
  const list = $('#threadList');
  const q = String(filter || '').trim().toLowerCase();
  const rows = state.threads.filter(t => {
    if (!q) return true;
    return (t.name || '').toLowerCase().includes(q) || (t.peer || '').includes(q);
  });
  if (!rows.length) {
    list.innerHTML = `<div class="empty" style="padding:40px 12px">
      <div class="empty-icon" style="font-size:34px">💬</div>
      <p>${q ? '没有匹配的对话' : '还没有对话'}</p>
    </div>`;
    return;
  }
  list.innerHTML = rows.map(t => {
    const last = t.last || {};
    const preview = last.text ? last.text.replace(/\s+/g, ' ').slice(0, 80) : '暂无消息';
    const out = last.direction === 'out' ? '你: ' : '';
    const lock = String(last.ctype || '').includes('message/mls') ? '🔒 ' : '';
    const active = t.peer === state.active ? ' active' : '';
    return `<div class="thread${active}" data-peer="${esc(t.peer)}">
      <div class="avatar" style="${avatarStyle(t.peer)}">${esc(initialOf(t.name, t.peer))}</div>
      <div class="thread-main">
        <div class="thread-top">
          <div class="thread-name">${esc(t.name || t.peer)}</div>
          <div class="thread-time">${esc(formatShort(last.ts))}</div>
        </div>
        <div class="thread-preview">${esc(out + lock + preview)}</div>
      </div>
      ${t.unread ? `<div class="unread">${t.unread > 99 ? '99+' : t.unread}</div>` : ''}
    </div>`;
  }).join('');
  list.querySelectorAll('.thread').forEach(el => {
    el.addEventListener('click', () => openThread(el.dataset.peer));
  });
}

function messageStatus(m) {
  if (m.direction !== 'out') return '';
  const map = {
    sending: '发送中', sent: '已发送', delivered: '已送达',
    read: '已读', failed: '发送失败', auth: '凭据失效',
  };
  return map[m.status] || m.status || '';
}

function renderMessages() {
  const box = $('#messages');
  if (!state.active) { box.innerHTML = ''; return; }
  box.innerHTML = '';
  let lastDay = '';
  for (const m of state.messages) {
    const day = dateLabel(m.ts);
    if (day && day !== lastDay) {
      lastDay = day;
      const sep = document.createElement('div');
      sep.className = 'date-sep';
      sep.textContent = day;
      box.appendChild(sep);
    }
    const out = m.direction === 'out';
    const row = document.createElement('div');
    row.className = `msg-row ${out ? 'out' : 'in'}`;
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    if (out && (m.status === 'failed' || m.status === 'auth')) bubble.classList.add('failed');
    if (out && m.status === 'sending') bubble.classList.add('sending');
    bubble.textContent = m.text || '';
    bubble.dataset.id = m.rcs_id || String(m._id);
    const meta = document.createElement('div');
    meta.className = 'meta';
    const t = document.createElement('span');
    t.textContent = formatTime(m.ts);
    meta.appendChild(t);
    const lock = document.createElement('span');
    lock.className = 'lock';
    lock.textContent = String(m.ctype || '').includes('message/mls') ? '🔒 E2EE' : 'RCS';
    meta.appendChild(lock);
    if (out) {
      const st = document.createElement('span');
      st.textContent = messageStatus(m);
      meta.appendChild(st);
    }
    // meta should sit under the bubble: wrap bubble+meta in a column
    const wrap = document.createElement('div');
    wrap.style.display = 'flex';
    wrap.style.flexDirection = 'column';
    wrap.style.alignItems = out ? 'flex-end' : 'flex-start';
    wrap.style.maxWidth = 'min(72%, 560px)';
    bubble.style.maxWidth = '100%';
    wrap.appendChild(bubble);
    wrap.appendChild(meta);
    row.appendChild(wrap);
    box.appendChild(row);
  }
  box.scrollTop = box.scrollHeight;
}

function updateHeader() {
  if (!state.active) return;
  const t = state.threads.find(x => x.peer === state.active);
  const c = state.contacts.find(x => x.number === state.active);
  const name = (t && t.name) || (c && c.name) || state.active;
  $('#chatName').textContent = name;
  $('#chatSub').textContent = state.active;
  const av = $('#chatAvatar');
  av.textContent = initialOf(name, state.active);
  av.setAttribute('style', avatarStyle(state.active));
}

function updateConn() {
  const a = state.auth || {};
  const bad = String(a.receiver_last_error || '').includes('AUTH_EXPIRED') || a.send_auth_expired;
  const waiting = !a.recv_frame_mtime && !bad;
  const dot = $('#connDot');
  dot.className = 'dot' + (bad ? ' bad' : waiting ? ' warn' : ' ok');
  $('#connText').textContent = bad ? 'RCS 凭据失效' : waiting ? '等待接收凭据' : 'RCS 就绪';
}

function updateBanner() {
  const a = state.auth || {};
  const bad = String(a.receiver_last_error || '').includes('AUTH_EXPIRED') || a.send_auth_expired;
  const waiting = !a.recv_frame_mtime && !bad;
  const banner = $('#banner');
  if (bad) {
    banner.hidden = false;
    banner.className = 'banner error';
    banner.textContent = 'RCS 凭据失效：当前只能查看历史消息。请等待手机 RCS 注册恢复，或抓取新的 ReceiveMessages 凭据。'
      + (a.receiver_last_connect ? `（最近连接：${a.receiver_last_connect}）` : '');
  } else if (waiting) {
    banner.hidden = false;
    banner.className = 'banner';
    banner.textContent = '正在等待手机 ReceiveMessages 凭据；收到新凭据后会自动开始接收。';
  } else {
    banner.hidden = true;
    banner.textContent = '';
  }
}

/* ---------- data ---------- */
async function loadState() {
  try {
    const s = await api('/api/state');
    state.contacts = s.contacts || [];
    state.threads = s.threads || [];
    state.auth = s.auth || {};
    renderThreads($('#search').value);
    updateConn();
    updateBanner();
    if (!state.active && state.threads.length) {
      await openThread(state.threads[0].peer, true);
    } else if (state.active) {
      updateHeader();
    }
  } catch (e) {
    $('#connText').textContent = '服务未连接';
    $('#connDot').className = 'dot bad';
  }
}

async function loadThreads() {
  try {
    const s = await api('/api/threads');
    state.threads = s.threads || [];
    renderThreads($('#search').value);
  } catch (_) { /* ignore */ }
}

async function openThread(peer, silent) {
  if (!peer) return;
  state.active = peer;
  state.messages = [];
  renderThreads($('#search').value);
  updateHeader();
  $('#empty').hidden = true;
  $('#chatView').hidden = false;
  $('#app').classList.add('chat-open');
  try {
    const data = await api(`/api/messages/${encodeURIComponent(peer)}`);
    state.messages = data.messages || [];
    if (data.contact) {
      const i = state.contacts.findIndex(c => c.number === peer);
      if (i >= 0) state.contacts[i] = data.contact; else state.contacts.push(data.contact);
    }
    renderMessages();
    updateHeader();
    await markRead();
  } catch (e) {
    if (!silent) console.warn(e);
  }
  $('#input').focus();
}

async function markRead() {
  if (!state.active) return;
  try {
    await api(`/api/read/${encodeURIComponent(state.active)}`, { method: 'POST' });
    const t = state.threads.find(x => x.peer === state.active);
    if (t) t.unread = 0;
    renderThreads($('#search').value);
  } catch (_) { /* ignore */ }
}

async function refreshActive() {
  if (!state.active) return;
  try {
    const data = await api(`/api/messages/${encodeURIComponent(state.active)}`);
    state.messages = data.messages || [];
    renderMessages();
    updateHeader();
  } catch (_) { /* ignore */ }
}

async function sendMessage() {
  const input = $('#input');
  const text = input.value.trim();
  if (!text || !state.active) return;
  const peer = state.active;
  const temp = {
    _id: `tmp-${Date.now()}`,
    rcs_id: '',
    peer,
    direction: 'out',
    ctype: 'text/plain',
    text,
    ts: new Date().toISOString(),
    status: 'sending',
  };
  state.messages.push(temp);
  renderMessages();
  input.value = '';
  input.style.height = 'auto';
  $('#sendBtn').disabled = true;
  try {
    const res = await api('/api/send', {
      method: 'POST',
      body: JSON.stringify({ peer, text }),
    });
    state.messages = state.messages.filter(m => m._id !== temp._id);
    if (res.auth_expired) {
      state.auth.send_auth_expired = true;
    } else if (res.ok) {
      state.auth.send_auth_expired = false;
    }
    if (!res.ok && res.error) {
      console.warn('send:', res.error);
    }
    await openThread(peer, true);
    await loadThreads();
    updateConn();
    updateBanner();
  } catch (e) {
    temp.status = 'failed';
    renderMessages();
    console.warn(e);
  } finally {
    $('#sendBtn').disabled = false;
    input.focus();
  }
}

/* ---------- live events ---------- */
function connectEvents() {
  if (state.es) state.es.close();
  const es = new EventSource('/api/events');
  state.es = es;
  es.onmessage = (e) => {
    let ev = null;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    handleEvent(ev);
  };
  es.onerror = () => {
    es.close();
    state.es = null;
    setTimeout(connectEvents, 3000);
  };
}

function handleEvent(ev) {
  if (ev.type === 'message') {
    const m = ev.message;
    if (m && m.peer === state.active) {
      if (!state.messages.some(x => x.rcs_id && x.rcs_id === m.rcs_id)) {
        state.messages.push(m);
        renderMessages();
        markRead();
      }
    }
    loadThreads();
  } else if (ev.type === 'status') {
    const m = state.messages.find(x => x.rcs_id === ev.rcs_id);
    if (m) { m.status = ev.status; renderMessages(); }
    loadThreads();
  } else if (ev.type === 'typing') {
    if (ev.peer === state.active) showTyping();
  } else if (ev.type === 'auth') {
    state.auth.send_auth_expired = true;
    updateConn();
    updateBanner();
  } else if (ev.type === 'contacts') {
    loadState();
  }
}

function showTyping() {
  const el = $('#typing');
  el.hidden = false;
  if (state.typingTimer) clearTimeout(state.typingTimer);
  state.typingTimer = setTimeout(() => { el.hidden = true; }, 5000);
}

/* ---------- modal ---------- */
function renderContacts() {
  const box = $('#contactList');
  if (!state.contacts.length) {
    box.innerHTML = '<div class="contact-number" style="padding:8px">暂无联系人，直接输入号码即可。</div>';
    return;
  }
  box.innerHTML = state.contacts.map(c => `
    <div class="contact-item" data-number="${esc(c.number)}">
      <div class="avatar" style="${avatarStyle(c.number)}">${esc(initialOf(c.name, c.number))}</div>
      <div>
        <div class="contact-name">${esc(c.name)}</div>
        <div class="contact-number">${esc(c.number)}</div>
      </div>
    </div>`).join('');
  box.querySelectorAll('.contact-item').forEach(el => {
    el.addEventListener('click', () => {
      closeModal();
      openThread(el.dataset.number);
    });
  });
}
function openModal() {
  $('#newNumber').value = '';
  $('#newName').value = '';
  renderContacts();
  $('#modal').hidden = false;
  setTimeout(() => $('#newNumber').focus(), 30);
}
function closeModal() { $('#modal').hidden = true; }

async function startChat() {
  const number = $('#newNumber').value.trim();
  const name = $('#newName').value.trim();
  if (!number) { $('#newNumber').focus(); return; }
  try {
    if (name) {
      await api('/api/contacts', {
        method: 'POST',
        body: JSON.stringify({ number, name }),
      });
      await loadState();
    }
    closeModal();
    await openThread(number);
  } catch (e) {
    alert(e.message || '无法开始对话');
  }
}

/* ---------- init ---------- */
function init() {
  $('#newChat').addEventListener('click', openModal);
  $('#modalClose').addEventListener('click', closeModal);
  $('#modal').addEventListener('click', (e) => { if (e.target === $('#modal')) closeModal(); });
  $('#startChat').addEventListener('click', startChat);
  $('#newNumber').addEventListener('keydown', (e) => { if (e.key === 'Enter') startChat(); });

  $('#refreshBtn').addEventListener('click', async () => {
    await loadState();
    await loadThreads();
    $('#connText').textContent = '已刷新 · ' + $('#connText').textContent;
    setTimeout(updateConn, 1200);
  });

  $('#search').addEventListener('input', (e) => renderThreads(e.target.value));
  $('#backBtn').addEventListener('click', () => {
    $('#app').classList.remove('chat-open');
    state.active = null;
    $('#chatView').hidden = true;
    $('#empty').hidden = false;
    renderThreads($('#search').value);
  });

  $('#sendBtn').addEventListener('click', sendMessage);
  const input = $('#input');
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 140) + 'px';
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  loadState().then(connectEvents);
  setInterval(async () => {
    await loadThreads();
    await refreshActive();
    await loadState();
  }, 20000);
}

document.addEventListener('DOMContentLoaded', init);
