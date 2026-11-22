import { installUpdates } from './pwa-update.js';

installUpdates({ appName: 'Bank Hub', scriptUrl: 'sw.js' });

// ---------------------------------------------------------------- helpers
// Bank data (payee names, descriptions) is untrusted text: everything is built
// with textContent, never innerHTML.
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style') el.style.cssText = v;   // CSSOM: allowed by the CSP, unlike style attributes
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) el.append(c.nodeType ? c : String(c));
  return el;
}
const SVG = 'http://www.w3.org/2000/svg';
function s(tag, attrs = {}) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}
const nf0 = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 0 });
const nf2 = new Intl.NumberFormat('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const ron = (v, dec = 0) => `${(dec ? nf2 : nf0).format(v || 0)} RON`;
const money = (v, cur) => `${nf2.format(v)} ${cur}`;
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const monthLabel = (m) => `${MONTHS[+m.slice(5, 7) - 1]} ${m.slice(0, 4)}`;
const monthShort = (m) => MONTHS[+m.slice(5, 7) - 1];
const shiftMonth = (m, d) => { const dt = new Date(+m.slice(0, 4), +m.slice(5, 7) - 1 + d, 1); return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}`; };
const thisMonth = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`; };
const pretty = (cat) => (cat || '').replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
// The year is shown whenever it is not the current one: "Mon 3 Nov 2025", but "Mon 17 Nov" for this year.
const dayLabel = (iso) => {
  const d = new Date(iso + 'T12:00:00');
  const opts = { weekday: 'short', day: 'numeric', month: 'short' };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  return d.toLocaleDateString('en-GB', opts);
};
function pctChange(now, before) { return before ? ((now - before) / before) * 100 : null; }

// "▲ 12%" for spending up, "▼ 8%" down; colour is secondary to the arrow and sign.
function deltaSpan(pct, { invert = false } = {}) {
  if (pct === null || pct === undefined || !isFinite(pct)) return h('span', { class: 'faint' }, '—');
  const up = pct > 0.05, down = pct < -0.05;
  const cls = up ? (invert ? 'down' : 'up') : down ? (invert ? 'up' : 'down') : 'faint';
  return h('span', { class: cls }, `${up ? '▲' : down ? '▼' : '■'} ${Math.abs(pct).toFixed(Math.abs(pct) < 10 ? 1 : 0)}%`);
}

class ApiError extends Error { constructor(status, msg) { super(msg); this.status = status; } }
// Month overviews, kept briefly so a swipe shows the next month at once. Any change made through the
// app (a rule, a category, a sync) clears it.
const monthCache = new Map();
const MONTH_TTL = 60_000;
function monthData(m) {
  const hit = monthCache.get(m);
  if (hit && Date.now() - hit.at < MONTH_TTL) return hit.promise;
  const promise = api(`overview?month=${m}`);
  monthCache.set(m, { at: Date.now(), promise });
  promise.catch(() => monthCache.delete(m));
  return promise;
}

// Offline copy: every answer the app reads is kept on this phone and used when the server can't be
// reached. Copies older than 14 days are deleted, and a device the server no longer accepts wipes them.
const DATA_CACHE = 'bankhub-data-v1';
const OFFLINE_MAX_MS = 14 * 24 * 3600 * 1000;
const offlineKey = (path) => new Request(new URL(`api/${path}`, location.href).href);
async function offlineSave(path, json) {
  try {
    const c = await caches.open(DATA_CACHE);
    await c.put(offlineKey(path), new Response(JSON.stringify(json),
      { headers: { 'Content-Type': 'application/json', 'X-Saved-At': String(Date.now()) } }));
  } catch {}
}
async function offlineRead(path) {
  try {
    const c = await caches.open(DATA_CACHE);
    const r = await c.match(offlineKey(path));
    if (!r) return null;
    const at = Number(r.headers.get('X-Saved-At'));
    if (!at || Date.now() - at > OFFLINE_MAX_MS) { await c.delete(offlineKey(path)); return null; }
    return { at, json: await r.json() };
  } catch { return null; }
}
async function offlineWipe() { try { await caches.delete(DATA_CACHE); } catch {} }
async function offlinePrune() {
  try {
    const c = await caches.open(DATA_CACHE);
    for (const req of await c.keys()) {
      const r = await c.match(req);
      if (!r || Date.now() - Number(r.headers.get('X-Saved-At')) > OFFLINE_MAX_MS) await c.delete(req);
    }
  } catch {}
}
const offlineBar = document.getElementById('offline');
function showOffline(at) {
  state.offline = true;
  state.usedOffline = true;
  offlineBar.replaceChildren(
    h('span', {}, `Offline · data saved ${new Date(at).toLocaleString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}`),
    h('button', { class: 'chip', onclick: () => refresh() }, 'Retry'));
  offlineBar.hidden = false;
}
function showOnline() { state.offline = false; offlineBar.hidden = true; }

// If the service worker had to serve this page from its copy, the network is not answering right now.
let apiSlowUntil = document.querySelector('meta[name="bankhub-from-copy"]') ? Date.now() + 60_000 : 0;
async function api(path, { method = 'GET', body } = {}) {
  if (method !== 'GET') monthCache.clear();
  const opts = { method, credentials: 'same-origin', headers: {} };
  if (method !== 'GET') { opts.headers['X-Bankhub'] = '1'; }
  if (body instanceof Blob) { opts.headers['Content-Type'] = body.type || 'application/octet-stream'; opts.body = body; }
  else if (body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  let r;
  try {
    // A hanging network (plane Wi-Fi, a VPN with no route) counts as offline after a few seconds.
    if (method === 'GET') {
      // After one timeout, give the network only a moment for the next minute (the copy is ready).
      const wait = Date.now() < apiSlowUntil ? 800 : 4000;
      const abort = new AbortController();
      const timer = setTimeout(() => abort.abort(), wait);
      try { r = await fetch(`api/${path}`, { ...opts, signal: abort.signal }); }
      catch (e) { if (e.name === 'AbortError') apiSlowUntil = Date.now() + 60_000; throw e; }
      finally { clearTimeout(timer); }
      apiSlowUntil = 0;
    } else {
      r = await fetch(`api/${path}`, opts);
    }
    if (method === 'GET' && [502, 503, 504].includes(r.status)) throw new TypeError('server unavailable');
  } catch (err) {
    if (method !== 'GET') throw new ApiError(0, "You're offline. Changes need the server; try again once you're connected.");
    const saved = await offlineRead(path);
    if (saved) { showOffline(saved.at); return saved.json; }
    throw new ApiError(0, "You're offline, and this screen hasn't been saved on this phone yet.");
  }
  if (r.status === 401) await offlineWipe();     // revoked or unregistered: nothing stays on the phone
  if (!r.ok) {
    let msg = r.statusText || `Server error ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch {}
    if (r.status >= 500) msg = `Server error ${r.status}. ${msg === `Server error ${r.status}` ? '' : msg}`.trim();
    throw new ApiError(r.status, msg);
  }
  const json = await r.json();
  if (method === 'GET') { offlineSave(path, json); showOnline(); }
  return json;
}

// On Wi-Fi, at most every six hours, save the screens ahead of time: every month, Recurring, Accounts,
// Sort, My rules and the latest ~600 payments.
let filling = false;
async function fillOfflineCopy(months) {
  // About 0.4 MB, at most every six hours: fine on mobile data too, unless Data Saver is on.
  const conn = navigator.connection;
  if (filling || state.offline || !navigator.onLine || (conn && conn.saveData)) return;
  const last = Number(localStorage.getItem('bankhub:offline-filled') || 0);
  if (Date.now() - last < 6 * 3600 * 1000) return;
  filling = true;
  try { await fillAll(months); } finally { filling = false; }
}
async function fillAll(months) {
  if (!months) months = (await api(`overview?month=${thisMonth()}`)).trend.map((t) => t.month);
  const paths = [...months.map((m) => `overview?month=${m}`), 'recurring', 'accounts', 'alerts', 'inbox', 'rules',
                 ...Array.from({ length: 10 }, (_, i) => `transactions?${new URLSearchParams({ offset: i * 60, limit: 60 })}`)];
  for (const p of paths) {
    if (!navigator.onLine) return;
    try { await api(p); } catch { return; }
    if (state.offline) return;                     // the server went away mid-way: try again later
    await new Promise((r) => setTimeout(r, 150));
  }
  // Only a complete pass counts; an interrupted one (a reload, a lost connection) is retried next time.
  localStorage.setItem('bankhub:offline-filled', String(Date.now()));
}

// ---------------------------------------------------------------- tooltip
const tip = document.getElementById('tooltip');
function showTip(text, x, y) {
  tip.textContent = text;
  tip.hidden = false;
  const w = tip.offsetWidth;
  tip.style.left = `${Math.min(Math.max(8, x - w / 2), innerWidth - w - 8)}px`;
  tip.style.top = `${Math.max(8, y - 44)}px`;
}
function hideTip() { tip.hidden = true; }
addEventListener('scroll', hideTip, { passive: true });

// ---------------------------------------------------------------- charts
// Single-series bars: context bars in grey, the selected period in the accent.
// 4px rounded data end anchored to the baseline, 2px gap between bars.
function barPath(x, y, w, hgt, r) {
  if (hgt <= 0) return '';
  r = Math.min(r, w / 2, hgt);
  return `M${x},${y + hgt}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + hgt}Z`;
}
// Scrollable history: `visible` columns fill the card, older months are reached by scrolling left.
// The chart keeps its scroll position across re-renders; selecting a month never moves it, unless
// the selected month would otherwise be off-screen (then it scrolls just enough to show it).
function scrollChart(points, { id, visible = 14, height = 150, onTap, label = 'Chart', allAccent = false,
                                maxOf = (p) => p.value, draw = null, labelOf = null, dimOthers = true, band = true, defs = null,
                                fitPx = null } = {}) {
  // `fitPx`: the width the chart will have on screen (wide layouts). Columns are sized to it, so text and
  // bars keep their size instead of the whole drawing being scaled up; months beyond what fits scroll.
  const n = points.length;
  if (fitPx) visible = Math.max(1, Math.min(n, Math.floor(fitPx / 26)));
  // Room above the bars for the year labels, and for value marks when there are any.
  const STEP = fitPx ? fitPx / visible : 24, top = labelOf ? 32 : 18, bottom = 20, gap = 2, H = height + (labelOf ? 14 : 0);
  const cols = Math.max(n, visible), W = cols * STEP;
  const max = Math.max(1, ...points.map(maxOf));
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': label });
  svg.style.width = `${(cols / visible) * 100}%`;
  if (defs) svg.append(...defs());
  const offset = cols - n;   // fewer months than columns: keep them on the right, like the full chart
  const hasSelection = dimOthers && points.some((p) => p.on);
  points.forEach((p, k) => {
    const i = k + offset, x = i * STEP + gap / 2, bw = STEP - gap;
    // Everything but the selected month is dimmed, so the selection reads at a glance.
    const g = s('g', { class: hasSelection && !p.on ? 'dim' : '' });
    const hgt = (p.value / max) * (H - top - bottom);
    const y = H - bottom - hgt;
    if (p.month && (p.month.endsWith('-01') || k === 0)) {
      if (k > 0) svg.append(s('line', { x1: i * STEP, x2: i * STEP, y1: 4, y2: H - bottom, class: 'sep' }));
      const yr = s('text', { x: i * STEP + 3, y: 12, class: 'yr' });
      yr.textContent = p.month.slice(0, 4);
      svg.append(yr);
    }
    if (p.on && band) svg.append(s('rect', { x: i * STEP, y: top - 6, width: STEP, height: H - top - bottom + 6, rx: 4, class: 'colsel' }));
    svg.append(g);
    if (draw) {
      draw(g, p, { x, bw, base: H - bottom, scale: (v) => (v / max) * (H - top - bottom) });
    } else {
      const cls = ['bar', (p.on || allAccent) ? 'on' : '', p.partial ? 'partial' : ''].filter(Boolean).join(' ');
      g.append(s('path', { d: barPath(x, y, bw, Math.max(hgt, p.partial ? 1 : 0), 4), class: cls }));
    }
    if (labelOf && labelOf(p)) {
      const last = k === n - 1;   // keep the rightmost label inside the chart
      const t = s('text', { x: last ? x + bw : x + bw / 2, y: Math.max(top - 4, H - bottom - (maxOf(p) / max) * (H - top - bottom) - 4), 'text-anchor': last ? 'end' : 'middle', class: 'mark' });
      t.textContent = labelOf(p);
      g.append(t);
    }
    if (p.short) {
      if (p.on) svg.append(s('rect', { x: x + bw / 2 - 8, y: H - 17, width: 16, height: 15, rx: 7.5, class: 'selpill' }));
      const t = s('text', { x: x + bw / 2, y: H - 6, 'text-anchor': 'middle', class: p.on ? 'sel' : '' });
      t.textContent = p.short;
      svg.append(t);
    }
    const hit = s('rect', { x: i * STEP, y: 0, width: STEP, height: H, class: 'hit' });
    const show = (e) => { const r = hit.getBoundingClientRect(); showTip(p.tip, r.left + r.width / 2, r.top + (y / H) * r.height); e.stopPropagation(); };
    hit.addEventListener('pointerenter', show);
    hit.addEventListener('pointerleave', hideTip);
    hit.addEventListener('click', (e) => { show(e); if (onTap) onTap(p, k); });
    svg.append(hit);
  });
  svg.append(s('line', { x1: 0, x2: W, y1: H - bottom + 0.5, y2: H - bottom + 0.5, class: 'base' }));
  const box = h('div', { class: 'scroller' }, svg);
  // Which year the left edge is in, whatever has been scrolled into view.
  const pin = h('div', { class: 'year-pin', 'aria-hidden': 'true' });
  const updatePin = () => {
    const col = box.scrollWidth / cols;
    const k = Math.min(n - 1, Math.max(0, Math.floor((box.scrollLeft + 2) / col) - offset));
    pin.textContent = points[k]?.month?.slice(0, 4) || '';
  };
  box.addEventListener('scroll', () => { state.scroll[id] = box.scrollLeft; hideTip(); updatePin(); }, { passive: true });
  requestAnimationFrame(() => {
    box.scrollLeft = state.scroll[id] ?? box.scrollWidth;
    const sel = points.findIndex((p) => p.on);
    if (sel >= 0) {
      const col = box.scrollWidth / cols, x0 = (sel + offset) * col, x1 = x0 + col;
      if (x0 < box.scrollLeft) box.scrollLeft = x0;
      else if (x1 > box.scrollLeft + box.clientWidth) box.scrollLeft = x1 - box.clientWidth;
    }
    state.scroll[id] = box.scrollLeft;
    updatePin();
  });
  return h('div', { class: 'chart-wrap' }, pin, box);
}
function sparkline(values, { height = 26 } = {}) {
  const W = 340, n = values.length, max = Math.max(1, ...values.map((v) => v.value));
  const step = W / n, bw = Math.max(2, step - 3);
  let lastIdx = -1;
  values.forEach((v, i) => { if (v.value > 0) lastIdx = i; });
  const svg = s('svg', { viewBox: `0 0 ${W} ${height}`, class: 'chart spark', role: 'img', 'aria-label': 'Last 13 months' });
  values.forEach((v, i) => {
    const hgt = (v.value / max) * (height - 2);
    const x = i * step + 1.5;
    svg.append(s('path', { d: barPath(x, height - hgt, bw, hgt, 3), class: i === lastIdx ? 'bar on' : 'bar' }));
    const hit = s('rect', { x: i * step, y: 0, width: step, height, class: 'hit' });
    const show = (e) => { const r = hit.getBoundingClientRect(); showTip(v.tip, r.left + r.width / 2, r.top); e.stopPropagation(); };
    hit.addEventListener('pointerenter', show);
    hit.addEventListener('pointerleave', hideTip);
    hit.addEventListener('click', show);
    svg.append(hit);
  });
  return svg;
}
// The same numbers as a table, for every chart (accessibility, exact values).
function tableToggle(rows, headers) {
  const box = h('div');
  const btn = h('button', { class: 'chip table-toggle', 'aria-expanded': 'false' }, 'Show as table');
  btn.addEventListener('click', () => {
    const open = btn.getAttribute('aria-expanded') === 'true';
    btn.setAttribute('aria-expanded', String(!open));
    btn.textContent = open ? 'Show as table' : 'Hide table';
    box.replaceChildren(open ? '' : h('table', { class: 'data num' },
      h('tr', {}, headers.map((x) => h('th', {}, x))),
      rows.map((r) => h('tr', {}, r.map((c) => h('td', {}, c))))));
  });
  return h('div', {}, btn, box);
}

// ---------------------------------------------------------------- router
const view = document.getElementById('view');
const titleEl = document.getElementById('title');
const actionsEl = document.getElementById('topbar-actions');
const tabbar = document.getElementById('tabbar');
const state = { month: thisMonth(), categories: null, scroll: {}, offline: false, usedOffline: false };
// Laptop and desktop: a sidebar, cards in columns, and wide layouts for Month and Activity.
const wide = matchMedia('(min-width: 900px)');
// The inner width of a card spanning `span` of the six grid columns of a wide layout (see style.css).
function wideCardPx(span) {
  const content = Math.min(innerWidth >= 1500 ? 1440 : 1240, innerWidth - 200) - 48;
  const col = (content - 5 * 14) / 6;
  return Math.floor(col * span + 14 * (span - 1) - 34);
}
wide.addEventListener('change', () => refresh());
// Charts are sized to their card, so a change of grid (see style.css) redraws.
for (const q of ['(min-width: 1200px)', '(min-width: 1500px)']) matchMedia(q).addEventListener('change', () => refresh());
const chartSpan = () => (innerWidth >= 1200 ? 4 : 3);

function parseHash() {
  const raw = location.hash.slice(1);
  const [name, query] = raw.split('?');
  return { name: name || 'month', params: new URLSearchParams(query || '') };
}
// Screens are history entries; switching month or typing a search replaces the current entry, so
// Back goes to the previous screen. Each screen's scroll position is restored when coming back.
const scrollPos = {};
let navMode = 'restore';          // 'top' for new screens, 'keep' for refreshes, 'restore' for Back
let routing = false;
const currentKey = () => location.hash || '#month';
function go(name, params, { replace = false } = {}) {
  const q = params ? new URLSearchParams(params).toString() : '';
  const hash = '#' + (q ? `${name}?${q}` : name);
  if (replace) {
    navMode = 'keep';
    history.replaceState(history.state, '', hash);
    offlinePrune();
route();
  } else {
    navMode = 'top';
    location.hash = hash.slice(1);
  }
}
function refresh() { navMode = 'keep'; monthCache.clear(); return route(); }
function setTitle(t, ...actions) { titleEl.textContent = t; actionsEl.replaceChildren(...actions); }
function fail(err) {
  if (err.status === 401) return renderActivate();
  view.replaceChildren(h('div', { class: 'card' }, h('p', {}, err.status === 0 ? "You're offline" : 'Something went wrong.'),
    h('p', { class: 'muted' }, err.message || 'No details were given.'),
    h('button', { class: 'btn', onclick: () => refresh() }, 'Try again')));
}

const routes = { month: renderMonth, recurring: renderRecurring, tx: renderTransactions, inbox: renderInbox, rules: renderRules, accounts: renderAccounts };
const TAB_OF = { rules: 'inbox' };
async function route() {
  hideTip();
  state.loadMore = null;
  state.usedOffline = false;
  const openSheet = document.getElementById('sheet');
  if (openSheet.open) openSheet.close();
  const mode = navMode, y = scrollY;
  const target = scrollPos[currentKey()] || 0;   // read before drawing: a redraw briefly shortens the page
  navMode = 'restore';
  routing = true;
  const { name, params } = parseHash();
  if (name === 'invite' || params.has('invite') || location.hash.includes('invite=')) return renderActivate();
  const fn = routes[name] || renderMonth;
  for (const a of tabbar.querySelectorAll('a')) {
    if (a.dataset.tab === (TAB_OF[name] || name)) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  state.panel = false;
  view.dataset.layout = fn === renderMonth ? 'month' : name;
  try { await fn(params); tabbar.hidden = false; slideIn(); } catch (e) { fail(e); }
  if (!state.usedOffline) {
    showOnline();
    if (name !== 'month') setTimeout(() => fillOfflineCopy(null).catch(() => {}), 1000);
  }
  if (mode === 'keep') scrollTo(0, y);
  else if (mode === 'top') scrollTo(0, 0);
  else {
    // A long list may need its later pages again before the old position exists.
    for (let i = 0; i < 10 && state.loadMore && document.documentElement.scrollHeight < target + innerHeight; i++) {
      if (!(await state.loadMore())) break;
    }
    scrollTo(0, target);
  }
  routing = false;
  scrollPos[currentKey()] = scrollY;
}
addEventListener('hashchange', (e) => {
  if (!routing) scrollPos[new URL(e.oldURL).hash || '#month'] = scrollY;
  offlinePrune();
route();
});
addEventListener('scroll', () => { if (!routing) scrollPos[currentKey()] = scrollY; }, { passive: true });

// Month: the page follows your finger. Past a third of the screen (or a quick flick) it is thrown off
// and the next or previous month slides in; otherwise it springs back. At the first or current month
// it only stretches a little. Swipes on the chart scroll the chart; swipes from the screen edges are
// left to the system back gesture; mostly vertical moves stay scrolls.
let drag = null;
const THROW = () => innerWidth * 0.3;
function dragTarget(dx) {
  const b = state.monthBounds;
  if (!b) return null;
  const t = shiftMonth(b.shown, dx < 0 ? 1 : -1);
  return t < b.first || t > b.last ? null : t;
}
view.addEventListener('touchstart', (e) => {
  drag = null;
  if (parseHash().name !== 'month' || e.touches.length !== 1 || document.getElementById('sheet').open) return;
  const t = e.touches[0];
  // Rows and cards are fine to start from (a tap still opens them); form fields and the chart are not.
  if (t.clientX < 28 || t.clientX > innerWidth - 28 || e.target.closest('.scroller, input, select, textarea, .month-nav')) return;
  drag = { x: t.clientX, y: t.clientY, at: Date.now(), mode: null, dx: 0, buzzed: false };
}, { passive: true });
view.addEventListener('touchmove', (e) => {
  if (!drag) return;
  const t = e.touches[0];
  const dx = t.clientX - drag.x, dy = t.clientY - drag.y;
  if (!drag.mode) {
    if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
    drag.mode = Math.abs(dx) > 1.2 * Math.abs(dy) ? 'h' : 'v';
    if (drag.mode === 'h') { view.getAnimations?.().forEach((a) => a.cancel()); hideTip(); }
  }
  if (drag.mode !== 'h') return;
  const target = dragTarget(dx);
  // Nowhere to go: a short rubber band instead of a free drag.
  const moved = target ? dx : Math.sign(dx) * Math.min(Math.abs(dx) * 0.25, 40);
  drag.dx = dx;
  view.style.transform = `translateX(${moved}px)`;
  view.style.opacity = String(1 - Math.min(Math.abs(moved) / innerWidth, 1) * 0.35);
  if (target && !drag.buzzed && Math.abs(dx) > THROW()) { drag.buzzed = true; navigator.vibrate?.(8); }
  if (target && drag.buzzed && Math.abs(dx) < THROW()) drag.buzzed = false;
}, { passive: true });
// A sideways drag that started on a row must not also open that row.
let swallowClick = false;
view.addEventListener('click', (e) => {
  if (swallowClick) { swallowClick = false; e.preventDefault(); e.stopPropagation(); }
}, true);
view.addEventListener('touchend', () => {
  if (!drag || drag.mode !== 'h') { drag = null; return; }
  swallowClick = true;
  setTimeout(() => { swallowClick = false; }, 400);
  const { dx, at, buzzed } = drag;
  drag = null;
  const target = dragTarget(dx);
  const fast = Math.abs(dx) > 40 && Math.abs(dx) / Math.max(1, Date.now() - at) > 0.6;
  const from = view.style.transform || 'translateX(0)';
  const reset = () => { view.style.transform = ''; view.style.opacity = ''; };
  if (!target || (Math.abs(dx) < THROW() && !fast)) {
    if (!view.animate) return reset();
    view.animate([{ transform: from, opacity: view.style.opacity || 1 }, { transform: 'translateX(0)', opacity: 1 }],
      { duration: 220, easing: 'cubic-bezier(.2,.9,.3,1.2)' }).finished.finally(reset);
    return;
  }
  if (!buzzed) navigator.vibrate?.(8);
  const out = dx < 0 ? -innerWidth : innerWidth;
  const go_ = () => { reset(); state.slideFrom = dx < 0 ? 'right' : 'left'; state.slideFar = true; go('month', { m: target }, { replace: true }); };
  if (!view.animate) return go_();
  view.animate([{ transform: from }, { transform: `translateX(${out}px)`, opacity: 0.2 }],
    { duration: 140, easing: 'ease-in', fill: 'forwards' }).finished.then(() => {
      view.getAnimations().forEach((a) => a.cancel());
      view.style.transform = `translateX(${out}px)`;
      go_();
    });
}, { passive: true });
view.addEventListener('touchcancel', () => {
  if (drag && drag.mode === 'h') { view.style.transform = ''; view.style.opacity = ''; }
  drag = null;
});
function slideIn() {
  const from = state.slideFrom, far = state.slideFar;
  state.slideFrom = null;
  state.slideFar = false;
  view.style.transform = '';
  view.style.opacity = '';
  if (!from || matchMedia('(prefers-reduced-motion: reduce)').matches || !view.animate) return;
  const distance = far ? innerWidth : 36;
  view.animate([{ transform: `translateX(${from === 'right' ? distance : -distance}px)`, opacity: far ? 0.2 : 0.35 },
                { transform: 'translateX(0)', opacity: 1 }],
    { duration: far ? 220 : 200, easing: 'cubic-bezier(.2,.8,.2,1)' });
}
tabbar.addEventListener('click', () => { navMode = 'top'; });
document.addEventListener('click', hideTip);

// ---------------------------------------------------------------- activation
function inviteFromUrl() {
  const m = location.hash.match(/invite=([A-Za-z0-9-]+)/);
  return m ? m[1] : '';
}
function renderActivate() {
  tabbar.hidden = true;
  setTitle('Bank Hub');
  const code = inviteFromUrl();
  // Remove the code from the address bar once read.
  if (code) history.replaceState(null, '', location.pathname);
  const input = h('input', { class: 'code-input', value: code, autocomplete: 'off', autocapitalize: 'characters', placeholder: 'XXXX-XXXX-XXXX', 'aria-label': 'Invitation code' });
  const msg = h('p', { class: 'muted', role: 'status' });
  const btn = h('button', { class: 'btn primary' }, 'Activate this device');
  btn.addEventListener('click', async () => {
    btn.disabled = true; msg.textContent = 'Activating…';
    try {
      await api('invites/redeem', { method: 'POST', body: { code: input.value } });
      go('month');
      offlinePrune();
route();
    } catch (e) { msg.textContent = e.message; btn.disabled = false; }
  });
  view.replaceChildren(h('section', { class: 'card narrow' },
    h('p', { class: 'eyebrow' }, 'Private'),
    h('h1', {}, 'Activate this device'),
    h('p', { class: 'muted' }, 'Enter the invitation code from the console. It registers this one device.'),
    h('label', { class: 'field' }, 'Invitation code', input), btn, msg));
}

// ---------------------------------------------------------------- month
async function renderMonth(params) {
  if (params.get('m')) state.month = params.get('m');
  const m = state.month;
  setTitle('Month');
  const d = await monthData(m);
  const isCurrent = m === thisMonth();
  state.monthBounds = { first: d.trend[0].month, last: thisMonth(), shown: m };
  // Fetch the neighbours in the background, so a swipe never waits for the network.
  if (m > d.trend[0].month) monthData(shiftMonth(m, -1)).catch(() => {});
  if (!isCurrent) monthData(shiftMonth(m, 1)).catch(() => {});
  if (!state.offline) setTimeout(() => fillOfflineCopy(d.trend.map((t) => t.month)).catch(() => {}), 1000);
  const compareTo = d.to_date ? d.to_date.previous_spent_ron : d.previous.spent_ron;
  const compareText = d.to_date ? `vs the same ${d.to_date.day} days of ${monthLabel(d.previous.month)}` : `vs ${monthLabel(d.previous.month)}`;
  const hasIncome = d.has_regular_income;

  const nav = h('div', { class: 'month-nav' },
    h('button', { class: 'chip icon', 'aria-label': 'Previous month', disabled: m <= d.trend[0].month, onclick: () => { state.slideFrom = 'left'; go('month', { m: shiftMonth(m, -1) }, { replace: true }); } }, '‹'),
    h('strong', {}, monthLabel(m) + (isCurrent ? ' · so far' : '')),
    h('button', { class: 'chip icon', 'aria-label': 'Next month', disabled: isCurrent, onclick: () => { state.slideFrom = 'right'; go('month', { m: shiftMonth(m, 1) }, { replace: true }); } }, '›'));

  const over = d.left_ron < 0;
  const hero = h('section', { class: 'card m-hero' },
    nav,
    h('p', { class: 'eyebrow' }, 'Spent'),
    h('div', { class: 'hero num' }, nf0.format(d.spent_ron), h('small', {}, 'RON')),
    h('div', { class: 'delta' }, deltaSpan(pctChange(d.spent_ron, compareTo)), ` ${compareText} (${ron(compareTo)})`),
    h('div', { class: 'tiles' },
      h('div', { class: 'tile' }, h('div', { class: 'label' }, 'Income'), h('div', { class: 'value num' }, nf0.format(d.income_ron))),
      h('div', { class: 'tile' }, h('div', { class: 'label' }, over && hasIncome ? 'Over income by' : 'Left'),
        h('div', { class: `value num ${over && hasIncome ? 'up' : ''}` }, hasIncome ? `${over ? '▲ ' : ''}${nf0.format(Math.abs(d.left_ron))}` : '—')),
      h('div', { class: 'tile' }, h('div', { class: 'label' }, 'Other money in'), h('div', { class: 'value num' }, nf0.format(d.other_in_ron)))),
    hasIncome ? '' : h('p', { class: 'faint', style: 'margin:10px 0 0;font-size:13px' }, 'Income shows once the account your salary arrives in is linked.'));

  const oneOffs = d.one_offs.length ? h('section', { class: 'card m-oneoffs' },
    h('h2', {}, 'One-off this month'),
    h('p', { class: 'faint', style: 'margin:-4px 0 8px;font-size:13px' }, 'Very large payments, kept out of the totals above.'),
    h('ul', { class: 'rows' }, d.one_offs.map((o) => h('li', {},
      h('button', { class: 'rowbtn', onclick: () => go('tx', { m, kind: 'one_off' }) },
        h('div', { class: 'row-head' }, h('span', { class: 'name' }, o.payee || pretty(o.category)),
          h('span', { class: `amt num ${o.amount_ron > 0 ? 'credit' : ''}` }, `${o.amount_ron > 0 ? '+' : ''}${ron(o.amount_ron)}`)),
        h('div', { class: 'row-sub' }, h('span', {}, [pretty(o.category), o.subcategory && pretty(o.subcategory)].filter(Boolean).join(' · ')), h('span', {}, dayLabel(o.booking_date)))))))) : '';

  const k = (v) => (v >= 1000 ? `${Math.round(v / 1000)}k` : nf0.format(v));
  const legend = h('div', { class: 'legend' },
    h('span', {}, h('i', { class: 'sw spent' }), 'Spent'),
    hasIncome ? h('span', {}, h('i', { class: 'sw left' }), 'Left over') : '',
    hasIncome ? h('span', {}, h('i', { class: 'sw over' }), 'Over income') : '',
    h('span', {}, h('b', { class: 'mk' }, '+12k'), 'one-off'));
  const trend = h('section', { class: 'card m-trend' },
    h('h2', {}, hasIncome ? 'Income and spending per month' : 'Spending per month'),
    legend,
    scrollChart(d.trend.map((t) => ({ ...t, on: t.month === m, short: monthShort(t.month)[0],
      tip: [`${monthLabel(t.month)}${t.partial ? (t.month === thisMonth() ? ' (so far)' : ' (history starts mid-month)') : ''}`,
            hasIncome ? `income ${ron(t.income_ron)}` : null, `spent ${ron(t.outflow_ron)}`,
            hasIncome ? (t.outflow_ron > t.income_ron ? `over by ${ron(t.outflow_ron - t.income_ron)}` : `left ${ron(t.income_ron - t.outflow_ron)}`) : null,
            t.one_off_out_ron ? `one-off out ${ron(t.one_off_out_ron)}` : null,
            t.one_off_in_ron ? `one-off in ${ron(t.one_off_in_ron)}` : null].filter(Boolean).join(' · ') })),
      { id: 'month-trend', label: 'Income and spending per month', dimOthers: false, band: false, fitPx: wide.matches ? wideCardPx(chartSpan()) : null,
        defs: () => {
          // Money left over is hatched, so it is not told apart from "over income" by colour alone.
          const pat = s('pattern', { id: 'left-hatch', width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' });
          pat.append(s('rect', { width: 6, height: 6, class: 'hatch-bg' }), s('line', { x1: 0, y1: 0, x2: 0, y2: 6, class: 'hatch-line' }));
          return [pat];
        },
        onTap: (p) => { if (p.month !== m) setTimeout(() => go('month', { m: p.month }, { replace: true }), 250); },
        maxOf: (p) => Math.max(p.outflow_ron, hasIncome ? p.income_ron : 0),
        labelOf: (p) => (p.one_off_out_ron ? `+${k(p.one_off_out_ron)}` : p.one_off_in_ron ? `↓${k(p.one_off_in_ron)}` : ''),
        draw: (svg, p, g) => {
          const dash = p.partial ? ' partial' : '';
          // A month with no income in the app is shown as it is: everything spent is over income.
          const known = hasIncome;
          const inc = known ? g.scale(p.income_ron) : 0, sp = g.scale(p.outflow_ron);
          const within = known ? Math.min(sp, inc) : sp;
          if (known && inc > sp) svg.append(s('path', { d: barPath(g.x, g.base - inc, g.bw, inc - sp, 4), class: 'bar left' + dash }));
          if (within > 0) svg.append(s('path', { d: barPath(g.x, g.base - within, g.bw, within, known ? 0 : 4), class: 'bar spent' + dash }));
          if (known && sp > inc) {
            svg.append(s('path', { d: barPath(g.x, g.base - sp, g.bw, sp - inc, 4), class: 'bar over' + dash }));
            if (inc > 0) svg.append(s('line', { x1: g.x - 1, x2: g.x + g.bw + 1, y1: g.base - inc, y2: g.base - inc, class: 'income-line' }));
          }
          // The selected month is outlined, not recoloured: every month keeps the same meaning of its colours.
          if (p.on) {
            const top_ = Math.max(inc, sp);
            svg.append(s('rect', { x: g.x - 2, y: g.base - top_ - 2, width: g.bw + 4, height: top_ + 2, rx: 5, class: 'selbar' }));
          }
        } }),
    h('p', { class: 'chart-note' }, wide.matches
      ? 'Click a month to open it, or use the ← and → keys · scroll the chart for older months · dashed: incomplete month'
      : 'Tap a month to open it, or swipe the page left and right · scroll the chart for older months · dashed: incomplete month'),
    tableToggle(d.trend.map((t) => [monthLabel(t.month) + (t.partial ? ' (incomplete)' : ''), nf0.format(t.income_ron), nf0.format(t.outflow_ron),
      nf0.format(t.income_ron - t.outflow_ron), t.one_off_out_ron || t.one_off_in_ron ? nf0.format(t.one_off_in_ron - t.one_off_out_ron) : '—']),
      ['Month', 'Income', 'Spent', 'Left', 'One-off']));

  const maxCat = Math.max(1, ...d.categories.map((c) => c.outflow_ron));
  const cats = h('section', { class: 'card m-cats' }, h('h2', {}, 'By category'),
    d.categories.length ? h('ul', { class: 'rows' }, d.categories.map((c) => h('li', {},
      h('button', { class: 'rowbtn', onclick: () => go('tx', { m, category: c.category }) },
        h('div', { class: 'row-head' }, h('span', { class: 'name' }, pretty(c.category)), h('span', { class: 'amt num' }, ron(c.outflow_ron))),
        h('div', { class: 'meter' }, h('span', { style: `width:${(c.outflow_ron / maxCat) * 100}%` })),
        h('div', { class: 'row-sub' }, h('span', {}, `${c.n} payment${c.n === 1 ? '' : 's'}${c.refunds_ron ? ` · ${ron(c.refunds_ron)} refunded` : ''}`),
          h('span', {}, deltaSpan(pctChange(c.outflow_ron, c.previous_ron)), ` vs ${ron(c.previous_ron)}`)))))) : h('p', { class: 'muted' }, 'No spending this month.'));

  const payees = h('section', { class: 'card m-payees' }, h('h2', {}, 'Top payees'),
    h('ul', { class: 'rows' }, d.payees.map((p) => h('li', {},
      h('button', { class: 'rowbtn', onclick: () => go('tx', { m, q: p.payee }) },
        h('div', { class: 'row-head' }, h('span', { class: 'name' }, p.payee), h('span', { class: 'amt num' }, ron(p.outflow_ron))),
        h('div', { class: 'row-sub' }, h('span', {}, pretty(p.category)), h('span', {}, `${p.n}×`)))))));

  const people = d.groups && d.groups.length ? h('section', { class: 'card m-groups' }, h('h2', {}, 'Groups'),
    h('ul', { class: 'rows' }, d.groups.map((g) => h('li', {},
      h('div', { class: 'row-head' }, h('span', { class: 'name' }, g.name), h('span', { class: 'amt num' }, ron(g.sent_ron))),
      h('div', { class: 'row-sub' }, h('span', {}, g.behaviour === 'separate' && g.category === 'transfers' ? 'not counted'
          : [pretty(g.category), g.subcategory && pretty(g.subcategory)].filter(Boolean).join(' · ')),
        h('span', {}, g.back_ron ? `${ron(g.back_ron)} back` : `${g.n}×`)))))) : '';

  // The weekly summary: for four days after it is due on the current month, or when opened from the notification.
  let weekCard = '';
  if (isCurrent || params.get('week')) {
    try {
      const w = await api('week');
      const age = (Date.now() - new Date(w.at).getTime()) / 864e5;
      if (params.get('week') || age < 4) weekCard = weekSummaryCard(w.summary);
    } catch { /* offline without a copy: no card */ }
  }
  view.replaceChildren(weekCard, hero, oneOffs, trend, cats, people, payees,
    h('p', { class: 'faint center-text m-foot' }, `Booked transactions only · newest ${d.newest_transaction || '—'}`));
}

function weekSummaryCard(w) {
  const span = `${dayLabel(w.start)} to ${dayLabel(w.end)}`;
  const head = h('div', { class: 'delta' }, `${ron(w.day_to_day_ron)} day to day`,
    w.usual_ron ? h('span', {}, ': ', deltaSpan(w.change_pct), ` vs your usual week (${ron(w.usual_ron)})`) : '',
    w.regular_ron >= 1 ? `, plus ${ron(w.regular_ron)} of regular payments` : '');
  const mo = w.month;
  const section = (title, items) => items.length ? h('div', {}, h('p', { class: 'eyebrow', style: 'margin:14px 0 4px' }, title), h('ul', { class: 'rows' }, items)) : '';
  const row = (name, amount, sub, onclick) => h('li', {}, h(onclick ? 'button' : 'div', { class: onclick ? 'rowbtn' : '', onclick },
    h('div', { class: 'row-head' }, h('span', { class: 'name' }, name), h('span', { class: 'amt num' }, amount)),
    h('div', { class: 'row-sub' }, h('span', {}, sub))));
  const what = { twice: 'Maybe charged twice', bigger: 'Bigger than usual', new: 'New payee', subscription: 'New regular payment' };
  return h('section', { class: 'card m-week' },
    h('p', { class: 'eyebrow' }, `Last week · ${span}`),
    h('div', { class: 'hero num' }, nf0.format(w.spent_ron), h('small', {}, 'RON')), head,
    w.incomplete.length ? h('div', { class: 'notice', style: 'margin:10px 0 0' },
      `Probably not complete: ${w.incomplete.map((x) => `${x.account} has nothing after ${dayLabel(x.newest)}`).join('; ')}. The bank has not sent the rest yet.`) : '',
    h('p', { class: 'faint', style: 'margin:6px 0 0;font-size:13px' },
      `${monthLabel(mo.month)} so far: ${ron(mo.spent_ron)}, against ${ron(mo.previous_ron)} in the same ${mo.days} days of ${monthLabel(mo.previous_month)}.`),
    section('Above usual', w.categories_up.map((c) => row(pretty(c.category), ron(c.spent_ron), `usually ${ron(c.usual_ron)} a week`,
      () => go('tx', { m: w.end.slice(0, 7), category: c.category })))),
    section('Worth a look', w.worth_a_look.map((x) => row(x.payee, x.amount_ron != null ? ron(x.amount_ron, 2) : money(x.amount, x.currency),
      `${what[x.kind]} · ${dayLabel(x.date)}${x.usual_ron ? ` · usually ${ron(x.usual_ron)}` : ''}`, () => go('tx', { q: x.payee })))),
    section('Coming up', w.coming_up.map((x) => row(x.payee, money(x.amount, x.currency),
      `${dayLabel(x.date)}${x.kind === 'card' ? ` · card payment, minimum ${ron(x.minimum, 2)}` : ''}`))),
    w.cards_left_out.length ? h('p', { class: 'faint', style: 'margin:12px 0 0;font-size:12px' },
      `${w.cards_left_out.join(', ')}: not in these weekly figures, because its purchases arrive with the monthly statement.`) : '');
}

// ---------------------------------------------------------------- recurring
async function renderRecurring() {
  setTitle('Recurring');
  const d = await api('recurring');
  const tm = d.this_month;
  const summary = h('section', { class: 'card r-summary' },
    h('p', { class: 'eyebrow' }, 'Regular payments'),
    h('div', { class: 'hero num' }, nf0.format(d.active_monthly_total_ron), h('small', {}, 'RON / month')),
    h('div', { class: 'delta' }, `${ron(d.active_annual_total_ron)} a year`),
    h('div', { class: 'tiles' },
      h('div', { class: 'tile' }, h('div', { class: 'label' }, `${monthShort(tm.month)} paid`), h('div', { class: 'value num' }, nf0.format(tm.paid_ron))),
      h('div', { class: 'tile' }, h('div', { class: 'label' }, `${monthShort(tm.month)} still due`), h('div', { class: 'value num' }, nf0.format(tm.expected_rest_ron))),
      h('div', { class: 'tile' }, h('div', { class: 'label' }, 'Monthly bills MoM'), h('div', { class: 'value' }, deltaSpan(d.bills_change.change_pct)))),
    h('p', { class: 'faint', style: 'margin:10px 0 0;font-size:13px' },
      `Latest ${d.bills_change.bills} monthly bills: ${ron(d.bills_change.latest_total_ron, 2)}, previous bills ${ron(d.bills_change.previous_total_ron, 2)}.`));

  const chart = h('section', { class: 'card r-chart' },
    h('h2', {}, 'Recurring payments per month'),
    scrollChart(d.totals_by_month.map((t, i, a) => {
      const prev = i ? a[i - 1].amount_ron : null;
      const ch = t.partial ? null : pctChange(t.amount_ron, prev);
      return { value: t.amount_ron, partial: t.partial, month: t.month, short: monthShort(t.month)[0],
               tip: `${monthLabel(t.month)}${t.partial ? (t.month === thisMonth() ? ' (so far)' : ' (history starts mid-month)') : ''}: ${ron(t.amount_ron)}${ch === null ? '' : ` (${ch > 0 ? '+' : ''}${ch.toFixed(0)}% MoM)`}` };
    }), { id: 'recurring-trend', label: 'Recurring payments per calendar month', allAccent: true,
          fitPx: wide.matches ? wideCardPx(chartSpan()) : null }),
    h('p', { class: 'chart-note' }, 'By calendar month · scroll for older months · dashed: incomplete month'),
    tableToggle(d.totals_by_month.map((t, i, a) => {
      const ch = i ? pctChange(t.amount_ron, a[i - 1].amount_ron) : null;
      return [monthLabel(t.month) + (t.partial ? ' (incomplete)' : ''), nf0.format(t.amount_ron), ch === null || t.partial ? '—' : `${ch > 0 ? '+' : ''}${ch.toFixed(0)}%`];
    }), ['Month', 'RON', 'MoM']));

  const incomeCard = h('section', { class: 'card r-income' }, h('h2', {}, 'Regular income'),
    d.income.length ? h('ul', { class: 'rows' }, d.income.map((x) => {
      const c = x.change;
      return h('li', {},
        h('div', { class: 'row-head' }, h('span', { class: 'name' }, x.payee || x.merchant), h('span', { class: 'amt num credit' }, `+${money(x.last_amount, x.currency)}`)),
        h('div', { class: 'row-sub' }, h('span', {}, `${pretty(x.cadence)} · next ${dayLabel(x.next_expected)}`, x.status === 'missed' ? h('span', { class: 'badge warn', style: 'margin-left:6px' }, '⚠ late') : ''),
          c ? h('span', {}, deltaSpan(c.change_pct, { invert: true }), ` ${c.basis}`) : ''));
    })) : h('p', { class: 'muted', style: 'margin:0' }, 'Salary shows here once the account it arrives in is linked.'));
  const list = h('section', { class: 'card r-list' }, h('h2', {}, 'Payments'),
    h('div', { class: 'series-head' }, h('span', {}, 'Payment'), h('span', {}, 'When'), h('span', {}, 'Change'),
      h('span', {}, 'By month'), h('span', { class: 'amt' }, 'Last')),
    h('ul', { class: 'rows' }, d.series.map((x) => {
      const c = x.change;
      const changeEl = c ? h('span', {}, deltaSpan(c.change_pct), ` ${c.basis} · was ${ron(c.previous_ron, 2)}`) : h('span', { class: 'faint' }, 'first charge');
      return h('li', {},
        h('div', { class: 'series' },
          h('div', { class: 'row-head' }, h('span', { class: 'name' }, x.payee || x.merchant)),
          h('span', { class: 'amt num' }, money(x.last_amount, x.currency)),
          h('div', { class: 'row-sub series-when' },
            h('span', {}, `${pretty(x.cadence)} · next ${dayLabel(x.next_expected)}`, x.status === 'missed' ? h('span', { class: 'badge warn', style: 'margin-left:6px' }, '⚠ missed') : '')),
          h('div', { class: 'row-sub series-change' }, changeEl),
          h('div', { class: 'spark' }, sparkline(x.history.map((p) => ({ value: p.amount_ron, tip: `${monthLabel(p.month)}: ${ron(p.amount_ron, 2)}` }))))));
    })),
    h('p', { class: 'faint', style: 'margin:10px 0 0;font-size:13px' }, 'MoM compares each bill with the previous one; annual payments compare year over year.'));
  view.replaceChildren(summary, incomeCard, chart, list);
}

// ---------------------------------------------------------------- activity
async function renderTransactions(params) {
  setTitle('Activity');
  const filters = { m: params.get('m') || '', category: params.get('category') || '', q: params.get('q') || '', kind: params.get('kind') || '',
    account: params.get('account') || '' };
  const search = h('input', { class: 'search', type: 'search', placeholder: 'Name, or amount: 38.68 · 38 · >500 · 100-200', value: filters.q, 'aria-label': 'Search' });
  let timer;
  search.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => go('tx', { ...clean({ ...filters, q: search.value }) }, { replace: true }), 400); });
  // Shows the alias until the first page brings the account's name.
  const accountChip = filters.account ? h('button', { class: 'chip', 'aria-pressed': 'true', onclick: () => go('tx', clean({ ...filters, account: '' }), { replace: true }) }, `${filters.account} ✕`) : '';
  const chips = h('div', { class: 'chips' },
    filters.m ? h('button', { class: 'chip', 'aria-pressed': 'true', onclick: () => go('tx', clean({ ...filters, m: '' }), { replace: true }) }, `${monthLabel(filters.m)} ✕`) : '',
    filters.category ? h('button', { class: 'chip', 'aria-pressed': 'true', onclick: () => go('tx', clean({ ...filters, category: '' }), { replace: true }) }, `${pretty(filters.category)} ✕`) : '',
    filters.kind ? h('button', { class: 'chip', 'aria-pressed': 'true', onclick: () => go('tx', clean({ ...filters, kind: '' }), { replace: true }) }, `${KINDS[filters.kind] || filters.kind} ✕`) : '',
    accountChip);
  const list = h('ul', { class: 'rows tx-list' });
  const more = h('button', { class: 'btn', style: 'width:100%;margin-top:10px' }, 'Load more');
  // On a wide screen the list is a table, and a payment opens beside it instead of over it.
  state.panel = wide.matches;
  const head = h('div', { class: 'tx-head' }, h('span', {}, 'Payee'), h('span', {}, 'Description'), h('span', {}, 'Category'),
    h('span', {}, 'Account'), h('span', { class: 'amt' }, 'Amount'));
  const count = h('p', { class: 'faint', style: 'margin:0 0 8px' });
  let offset = 0, lastDay = null;
  async function load() {
    const q = new URLSearchParams(clean({ month: filters.m, category: filters.category, q: filters.q, kind: filters.kind,
      account: filters.account, offset, limit: 60 }));
    const d = await api(`transactions?${q}`);
    if (accountChip && d.transactions.length) accountChip.textContent = `${d.transactions[0].account_name || filters.account} ✕`;
    count.textContent = `${d.total} transaction${d.total === 1 ? '' : 's'}`;
    for (const t of d.transactions) {
      if (t.booking_date !== lastDay) { lastDay = t.booking_date; list.append(h('li', { class: 'daysep' }, dayLabel(t.booking_date))); }
      const tags = [pretty(t.category)];
      if (t.is_internal_transfer) tags.push('own transfer');
      if (t.is_recurring) tags.push('recurring');
      if (t.is_one_off) tags.push('one-off');
      if (t.kind === 'dependent') tags.push('in a group · not counted');
      if (t.kind === 'refund') tags.push('refund');
      if (t.status === 'pending') tags.push('pending');
      if (t.overridden) tags.push('changed by you');
      const li = h('li', { class: 'tx' });
      list.append(li);
      li.append(h('button', { class: 'rowbtn', onclick: (e) => { markSelected(e); openTxSheet(t, () => refresh()).catch(fail); } },
        h('div', { class: 'row-head' }, h('span', { class: 'name' }, t.payee || '—'),
          h('span', { class: `amt num ${t.amount > 0 ? 'credit' : ''}` }, `${t.amount > 0 ? '+' : ''}${money(t.amount, t.currency)}`)),
        t.description && t.description !== t.payee ? h('div', { class: 'desc' }, t.description) : '',
        h('div', { class: 'row-sub' }, h('span', {}, tags.join(' · ')), h('span', {}, t.account_name || t.account_alias))));
    }
    offset += d.transactions.length;
    more.hidden = offset >= d.total;
  }
  more.addEventListener('click', () => load().catch(fail));
  state.loadMore = async () => { if (more.hidden) return false; await load(); return true; };
  view.replaceChildren(h('section', { class: 'card' }, search, chips, count, head, list, more));
  await load();
}
const KINDS = { one_off: 'One-off', income: 'Income', other_in: 'Other money in', refund: 'Refunds' };
function clean(o) { return Object.fromEntries(Object.entries(o).filter(([, v]) => v !== '' && v !== null && v !== undefined)); }

// ---------------------------------------------------------------- sort (inbox)
async function renderInbox() {
  setTitle('Sort', h('button', { class: 'chip', onclick: () => go('rules') }, 'My rules'));
  const d = await api('inbox');
  const intro = h('section', { class: 'card' },
    h('p', { class: 'eyebrow' }, 'Uncategorized'),
    h('div', { class: 'hero num' }, `${d.uncategorized_share_pct}`, h('small', {}, '% of spending')),
    h('p', { class: 'muted', style: 'margin:8px 0 0' }, `${d.remaining} payee${d.remaining === 1 ? '' : 's'} to sort. Tap one to give it a category. Leave "Show name" off for local shops and people: the name then stays on this server and AI assistants only see the category.`));
  state.panel = wide.matches;
  const list = h('section', { class: 'card' }, h('ul', { class: 'rows' }, d.items.map((it) => h('li', {},
    h('button', { class: 'rowbtn', onclick: (e) => { markSelected(e); openRuleSheet(it, d.categories); } },
      h('div', { class: 'row-head' }, h('span', { class: 'name' }, it.example), h('span', { class: 'amt num' }, ron(it.outflow_ron))),
      h('div', { class: 'row-sub' }, h('span', {}, `${it.n}× · last ${dayLabel(it.last_date)}`), h('span', {}, it.bank_transfer ? 'bank transfer' : 'card')))))));
  view.replaceChildren(intro, d.items.length ? list : h('p', { class: 'muted center-text' }, 'Nothing left to sort.'));
}
function categoryPicker(categories, current = '', currentSub = '') {
  const cat = h('select', {}, h('option', { value: '' }, 'Choose…'),
    categories.map((c) => h('option', { value: c, selected: c === current }, pretty(c))),
    h('option', { value: '__new' }, 'New category…'));
  const newCat = h('input', { placeholder: 'e.g. kids_activities', hidden: true, 'aria-label': 'New category name' });
  const sub = h('input', { placeholder: 'optional, e.g. bakery', value: currentSub || '' });
  cat.addEventListener('change', () => {
    newCat.hidden = cat.value !== '__new';
    if (!newCat.hidden) newCat.focus();
    // A subcategory belongs to its category: keep it only when going back to the original one.
    sub.value = cat.value === current ? (currentSub || '') : '';
  });
  const slug = (v) => v.trim().toLowerCase().replace(/[^a-z_ ]/g, '').replace(/\s+/g, '_');
  return {
    fields: [h('label', { class: 'field' }, 'Category', cat, newCat), h('label', { class: 'field' }, 'Subcategory', sub)],
    value: () => ({ category: cat.value === '__new' ? slug(newCat.value) : cat.value, subcategory: slug(sub.value) || null }),
  };
}
// The row whose details are open in the side panel.
function markSelected(e) {
  for (const x of document.querySelectorAll('li.sel')) x.classList.remove('sel');
  e.currentTarget.closest('li')?.classList.add('sel');
}
function sheetOpen(...children) {
  const sheet = document.getElementById('sheet');
  // On a wide Activity screen a payment opens in a panel beside the list, which stays usable: the next
  // payment replaces it in place.
  const panel = state.panel && wide.matches;
  const already = sheet.open && sheet.classList.contains('panel');
  sheet.classList.toggle('panel', panel);
  document.body.classList.toggle('panel-open', panel);
  sheet.replaceChildren(...children);
  if (panel) { if (!sheet.open) sheet.show(); } else sheet.showModal();
  if (!already) history.pushState({ sheet: true }, '');
  return sheet;
}
// Closing a sheet any way (button, Escape, a save) drops its history entry; Back closes an open sheet.
document.getElementById('sheet').addEventListener('close', () => {
  document.body.classList.remove('panel-open');
  for (const x of document.querySelectorAll('li.sel')) x.classList.remove('sel');
  if (history.state && history.state.sheet) history.back();
});
// Keyboard, for laptops: Esc closes, ← and → change month, / searches Activity.
addEventListener('keydown', (e) => {
  const sheet = document.getElementById('sheet');
  if (e.key === 'Escape' && sheet.open) { sheet.close(); return; }
  if (e.metaKey || e.ctrlKey || e.altKey || e.target.closest('input, select, textarea') || (sheet.open && !sheet.classList.contains('panel'))) return;
  const { name } = parseHash();
  if (name === 'month' && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
    const btn = document.querySelector(`.month-nav button[aria-label="${e.key === 'ArrowLeft' ? 'Previous' : 'Next'} month"]`);
    if (btn && !btn.disabled) { e.preventDefault(); btn.click(); }
  } else if (name === 'tx' && e.key === '/') {
    e.preventDefault();
    document.querySelector('input.search')?.focus();
  }
});
addEventListener('popstate', () => {
  const sheet = document.getElementById('sheet');
  if (sheet.open && !(history.state && history.state.sheet)) sheet.close();
});
function openRuleSheet(item, categories) {
  const pattern = h('input', { value: item.pattern, autocomplete: 'off' });
  const picker = categoryPicker(categories);
  const named = h('input', { type: 'checkbox' });
  const name = h('input', { value: titleCase(item.pattern), hidden: true });
  named.addEventListener('change', () => { name.hidden = !named.checked; });
  const msg = h('p', { class: 'muted', role: 'status' });
  const save = h('button', { class: 'btn primary' }, 'Save rule');
  let sheet;
  save.addEventListener('click', async () => {
    const v = picker.value();
    if (!v.category) { msg.textContent = 'Choose a category.'; return; }
    save.disabled = true; msg.textContent = 'Saving and re-sorting…';
    try {
      await api('inbox/rule', { method: 'POST', body: { pattern: pattern.value, ...v, name: named.checked ? name.value : null } });
      sheet.close();
      refresh();
    } catch (e) { msg.textContent = e.message; save.disabled = false; }
  });
  sheet = sheetOpen(
    h('h2', {}, item.example),
    h('p', { class: 'muted', style: 'margin:4px 0 0' }, `${item.n} payments · ${ron(item.outflow_ron)}`),
    h('label', { class: 'field' }, 'Matches payees containing', pattern),
    ...picker.fields,
    h('label', { class: 'toggle' }, named, 'Show name (chains and online services)'),
    h('label', { class: 'field' }, name),
    msg,
    h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: () => sheet.close() }, 'Cancel'), save));
}

// ---------------------------------------------------------------- my rules
async function renderRules() {
  setTitle('My rules', h('button', { class: 'chip', onclick: () => go('inbox') }, 'Sort'));
  const d = await api('rules');
  state.categories = d.categories;
  if (!d.rules.length) {
    view.replaceChildren(h('p', { class: 'muted center-text' }, 'No rules yet. Sort a payee, or recategorize a payment from Activity.'));
    return;
  }
  state.panel = wide.matches;
  const groups = new Map();
  for (const r of d.rules) { if (!groups.has(r.category)) groups.set(r.category, []); groups.get(r.category).push(r); }
  view.replaceChildren(
    h('p', { class: 'muted', style: 'margin:4px 2px 12px' }, 'Your rules are checked before the built-in ones. Tap one to change or delete it; everything is re-sorted straight away.'),
    ...[...groups].map(([cat, rules]) => h('section', { class: 'card' }, h('h2', {}, pretty(cat)),
      h('ul', { class: 'rows' }, rules.map((r) => h('li', {},
        h('button', { class: 'rowbtn', onclick: (e) => { markSelected(e); openRuleEditor(r, d.categories); } },
          h('div', { class: 'row-head' }, h('span', { class: 'name' }, r.name || r.patterns.join(', ')),
            h('span', { class: 'amt num' }, r.matches ? ron(r.outflow_ron) : '')),
          h('div', { class: 'row-sub' },
            h('span', {}, [r.subcategory ? pretty(r.subcategory) : null, r.named ? 'name shown' : 'name hidden'].filter(Boolean).join(' · ')),
            h('span', {}, r.matches ? `${r.matches}×` : 'no matches')),
          r.name ? h('div', { class: 'desc faint', style: 'font-size:13px' }, `matches: ${r.patterns.join(', ')}`) : '')))))));
}
function openRuleEditor(rule, categories) {
  const patterns = h('input', { value: rule.patterns.join(', '), autocomplete: 'off' });
  const picker = categoryPicker(categories, rule.category, rule.subcategory);
  const named = h('input', { type: 'checkbox', checked: rule.named });
  const name = h('input', { value: rule.name || titleCase(rule.patterns[0].replace(/^re:/, '')), hidden: !rule.named });
  named.addEventListener('change', () => { name.hidden = !named.checked; });
  const msg = h('p', { class: 'muted', role: 'status' });
  const save = h('button', { class: 'btn primary' }, 'Save');
  const del = h('button', { class: 'btn' }, 'Delete');
  let sheet;
  const done = () => { sheet.close(); refresh(); };
  save.addEventListener('click', async () => {
    const v = picker.value();
    if (!v.category) { msg.textContent = 'Choose a category.'; return; }
    save.disabled = del.disabled = true; msg.textContent = 'Saving and re-sorting…';
    try {
      await api(`rules/${encodeURIComponent(rule.id)}`, { method: 'PUT', body: { etag: rule.etag, patterns: patterns.value.split(','), ...v, name: named.checked ? name.value : null } });
      done();
    } catch (e) { msg.textContent = e.message; save.disabled = del.disabled = false; }
  });
  del.addEventListener('click', async () => {
    if (del.dataset.armed !== '1') { del.dataset.armed = '1'; del.textContent = 'Tap again to delete'; return; }
    save.disabled = del.disabled = true; msg.textContent = 'Deleting…';
    try { await api(`rules/${encodeURIComponent(rule.id)}?etag=${rule.etag}`, { method: 'DELETE' }); done(); }
    catch (e) { msg.textContent = e.message; save.disabled = del.disabled = false; }
  });
  sheet = sheetOpen(
    h('h2', {}, rule.name || 'Unnamed rule'),
    h('p', { class: 'muted', style: 'margin:4px 0 0' }, rule.matches ? `Catches ${rule.matches} payments · ${ron(rule.outflow_ron)}` : 'Catches no payments at the moment'),
    h('label', { class: 'field' }, 'Matches payees containing (comma-separated)', patterns),
    ...picker.fields,
    h('label', { class: 'toggle' }, named, 'Show name (chains and online services)'),
    h('label', { class: 'field' }, name),
    msg,
    h('div', { class: 'actions' }, del, h('span', { style: 'flex:1' }), h('button', { class: 'btn', onclick: () => sheet.close() }, 'Cancel'), save));
}

// ---------------------------------------------------------------- recategorize a payment
async function openTxSheet(t, onDone) {
  if (!state.categories) state.categories = (await api('rules')).categories;
  const isOwn = !!t.is_internal_transfer, isIn = t.amount > 0;
  const head = [
    h('h2', {}, t.payee || '—'),
    h('p', { class: 'muted', style: 'margin:4px 0 0' }, `${dayLabel(t.booking_date)} · ${money(t.amount, t.currency)} · ${t.account_name || t.account_alias}`),
    t.description && t.description !== t.payee ? h('p', { class: 'faint', style: 'margin:4px 0 0;font-size:13px;word-break:break-word' }, t.description) : '',
    h('p', { style: 'margin:10px 0 0' }, 'Now: ', h('strong', {}, pretty(t.category) + (t.subcategory ? ` · ${pretty(t.subcategory)}` : '')),
      t.overridden ? h('span', { class: 'badge', style: 'margin-left:6px' }, 'changed by you') : ''),
  ];
  let sheet;
  if (isOwn) {
    sheet = sheetOpen(...head, h('p', { class: 'muted' }, 'This is a transfer between your own accounts, so it is left out of spending and has no category.'),
      h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: () => sheet.close() }, 'Close')));
    return;
  }
  const picker = categoryPicker(state.categories, t.category === 'uncategorized' ? '' : t.category, t.subcategory);
  // One-offs and payments already changed by hand are about this payment, not the payee.
  const oneByDefault = isIn || !!t.is_one_off || !!t.overridden;
  const scopeAll = h('input', { type: 'radio', name: 'scope', value: 'payee', checked: !oneByDefault, disabled: isIn });
  const scopeOne = h('input', { type: 'radio', name: 'scope', value: 'one', checked: oneByDefault });
  // "All payments to this payee": show what the rule will match, editable, with a live count.
  const matchInput = h('input', { autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false' });
  const matchInfo = h('p', { class: 'faint', style: 'margin:4px 0 0;font-size:13px' });
  const matchBox = h('div', { hidden: true }, h('label', { class: 'field' }, 'Matches payments containing', matchInput), matchInfo);
  let ruleInfo = null, previewTimer = null;
  const describe = (p, note) => {
    matchInfo.textContent = `${p.count} payment${p.count === 1 ? '' : 's'}, ${ron(p.outflow_ron)} spent` +
      (p.examples.length ? ` · e.g. ${p.examples.slice(0, 3).join(', ')}` : '') + (note ? ` · ${note}` : '');
  };
  const existingNote = () => ruleInfo && ruleInfo.existing && ruleInfo.existing.patterns.includes(matchInput.value.trim().toLowerCase())
    ? `updates your rule "${ruleInfo.existing.name || ruleInfo.existing.patterns[0]}"`
    : (ruleInfo && ruleInfo.existing ? 'a new, more specific rule; it is checked first' : '');
  matchInput.addEventListener('input', () => {
    clearTimeout(previewTimer);
    const v = matchInput.value.trim();
    if (v.length < 3) { matchInfo.textContent = 'At least 3 characters.'; return; }
    previewTimer = setTimeout(async () => {
      try { describe(await api(`rules/preview?${new URLSearchParams({ pattern: v })}`), existingNote()); }
      catch (e) { matchInfo.textContent = e.message; }
    }, 300);
  });
  const showMatch = async () => {
    matchBox.hidden = !scopeAll.checked;
    if (!scopeAll.checked || ruleInfo) return;
    matchInfo.textContent = 'Checking…';
    try {
      ruleInfo = await api(`transactions/${t.id}/payee_rule`);
      matchInput.value = ruleInfo.pattern;
      describe(ruleInfo.preview, existingNote());
    } catch (e) { matchInfo.textContent = e.message; }
  };
  scopeAll.addEventListener('change', showMatch);
  scopeOne.addEventListener('change', showMatch);
  const msg = h('p', { class: 'muted', role: 'status' });
  const save = h('button', { class: 'btn primary' }, 'Save');
  const reset = t.overridden ? h('button', { class: 'btn' }, 'Undo my change') : '';
  const finish = () => { sheet.close(); onDone(); };
  save.addEventListener('click', async () => {
    const v = picker.value();
    if (!v.category) { msg.textContent = 'Choose a category.'; return; }
    save.disabled = true; msg.textContent = 'Saving and re-sorting…';
    try {
      const body = { scope: scopeAll.checked ? 'payee' : 'one', ...v };
      if (scopeAll.checked && matchInput.value.trim()) body.pattern = matchInput.value.trim();
      await api(`transactions/${t.id}/category`, { method: 'POST', body });
      state.categories = null;
      finish();
    } catch (e) { msg.textContent = e.message; save.disabled = false; }
  });
  if (reset) reset.addEventListener('click', async () => {
    reset.disabled = true; msg.textContent = 'Undoing…';
    try { await api(`transactions/${t.id}/category`, { method: 'DELETE' }); finish(); }
    catch (e) { msg.textContent = e.message; reset.disabled = false; }
  });
  const oneOff = h('select', { 'aria-label': 'One-off' },
    h('option', { value: 'auto', selected: t.one_off_setting === null || t.one_off_setting === undefined }, `Automatic (over ${nf0.format(state.oneOffRon || 50000)} RON)`),
    h('option', { value: 'yes', selected: t.one_off_setting === 1 }, 'One-off: keep out of totals'),
    h('option', { value: 'no', selected: t.one_off_setting === 0 }, 'Regular: always count it'));
  oneOff.addEventListener('change', async () => {
    msg.textContent = 'Saving…';
    try {
      await api(`transactions/${t.id}/one_off`, { method: 'POST', body: { value: { auto: null, yes: true, no: false }[oneOff.value] } });
      msg.textContent = 'Saved.';
      sheetDirty = true;
    } catch (e) { msg.textContent = e.message; }
  });
  let sheetDirty = false;
  setTimeout(showMatch, 0);
  sheet = sheetOpen(...head, ...picker.fields,
    h('label', { class: 'toggle' }, scopeAll, isIn ? 'All payments from this payee (spending only)' : 'All payments to this payee (saved as one of your rules)'),
    matchBox,
    h('label', { class: 'toggle' }, scopeOne, 'Only this payment'),
    h('label', { class: 'field' }, `One-off${t.is_one_off ? ' (it is now)' : ''}`, oneOff),
    msg,
    h('div', { class: 'actions' }, reset, h('span', { style: 'flex:1' }), h('button', { class: 'btn', onclick: () => { sheet.close(); if (sheetDirty) onDone(); } }, 'Close'), save));
}
function titleCase(s) { return s.replace(/\b\w/g, (c) => c.toUpperCase()); }

// ---------------------------------------------------------------- alerts
function b64ToBytes(b64) {
  const s = atob((b64 + '='.repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}
function alertsCard() {
  const card = h('section', { class: 'card' }, h('h2', {}, 'Notifications'), h('p', { class: 'muted' }, 'Loading…'));
  const draw = async () => {
    const st = await api('alerts');
    const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
    const msg = h('p', { class: 'muted', role: 'status', style: 'margin:8px 0 0' });
    const actions = h('div', { class: 'chips', style: 'margin-top:10px' });
    if (!supported) {
      actions.append(h('p', { class: 'muted' }, 'This browser cannot receive notifications. Install the app to the home screen and open it from there.'));
    } else if (!st.subscribed) {
      const on = h('button', { class: 'btn primary' }, 'Turn on for this phone');
      on.addEventListener('click', async () => {
        on.disabled = true; msg.textContent = 'Asking for permission…';
        try {
          if ((await Notification.requestPermission()) !== 'granted') throw new Error('Notifications are blocked for this app in the phone settings.');
          const reg = await navigator.serviceWorker.ready;
          const sub = (await reg.pushManager.getSubscription()) ||
            await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(st.public_key) });
          await api('push/subscribe', { method: 'POST', body: sub.toJSON() });
          draw();
        } catch (e) { msg.textContent = e.message; on.disabled = false; }
      });
      actions.append(on);
    } else {
      const test = h('button', { class: 'btn' }, 'Send a test');
      test.addEventListener('click', async () => {
        test.disabled = true;
        try { await api('push/test', { method: 'POST' }); msg.textContent = 'Sent. It should appear in a few seconds.'; }
        catch (e) { msg.textContent = e.message; }
        test.disabled = false;
      });
      const off = h('button', { class: 'btn' }, 'Turn off');
      off.addEventListener('click', async () => {
        const reg = await navigator.serviceWorker.ready;
        const sub = await reg.pushManager.getSubscription();
        if (sub) await sub.unsubscribe().catch(() => {});
        await api('push/unsubscribe', { method: 'POST' });
        draw();
      });
      actions.append(test, off);
    }
    const s = st.settings;
    const num = (key, label, hint) => {
      const input = h('input', { type: 'number', min: '0', step: '100', value: String(s[key] || 0), inputmode: 'numeric' });
      input.dataset.key = key;
      return h('label', { class: 'field' }, `${label}`, input, h('span', { class: 'faint', style: 'font-size:12px' }, hint));
    };
    const tick = (key, label) => {
      const input = h('input', { type: 'checkbox', checked: !!s[key] });
      input.dataset.key = key;
      return h('label', { class: 'toggle' }, input, label);
    };
    const form = h('div', {},
      num('large_payment_ron', 'A payment of at least (RON)', '0 turns it off'),
      num('balance_below_ron', 'Balance of RON accounts below (RON)', '0 turns it off'),
      tick('income_late', 'Regular income is late'),
      tick('price_change', 'A regular payment changes amount'),
      tick('consent_expiring', 'A bank connection is about to end'),
      tick('card_due', 'A credit card payment is due'),
      tick('health', 'A bank stopped syncing, or backups stopped'),
      tick('details', 'Show amounts and payees on the lock screen'),
      tick('weekly', 'Weekly summary'),
      h('div', { class: 'weekly-when' },
        h('label', { class: 'field' }, 'Day', h('select', { 'data-key': 'weekly_day' },
          ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'].map((name, i) => h('option', { value: String(i), selected: s.weekly_day === i }, name)))),
        h('label', { class: 'field' }, 'Time', h('select', { 'data-key': 'weekly_hour' },
          Array.from({ length: 24 }, (_, i) => h('option', { value: String(i), selected: s.weekly_hour === i }, `${String(i).padStart(2, '0')}:00`))))),
      h('span', { class: 'faint', style: 'font-size:12px;display:block;margin-top:-4px' }, 'Covers the seven days before it arrives: spending against your usual week, what changed, what is worth a look and what is due.'));
    const save = h('button', { class: 'btn' }, 'Save alert settings');
    save.addEventListener('click', async () => {
      const body = {};
      for (const el of form.querySelectorAll('input, select')) body[el.dataset.key] = el.type === 'checkbox' ? el.checked : Number(el.value || 0);
      save.disabled = true;
      try { await api('alerts/settings', { method: 'POST', body }); msg.textContent = 'Saved.'; }
      catch (e) { msg.textContent = e.message; }
      save.disabled = false;
    });
    const recent = st.recent.length ? h('div', {}, h('h2', { style: 'margin-top:14px' }, 'Recent alerts'),
      h('ul', { class: 'rows' }, st.recent.map((r) => h('li', {},
        h('div', { class: 'row-head' }, h('span', { class: 'name' }, r.title), h('span', { class: 'faint', style: 'font-size:13px' }, ago(r.at))),
        h('div', { class: 'desc' }, r.body))))) : '';
    card.replaceChildren(h('h2', {}, 'Notifications'),
      h('p', { class: 'muted', style: 'margin-top:0' }, st.subscribed ? 'On for this phone. Checked after every sync.' : 'Get a notification on this phone when something needs a look.'),
      actions, form, save, msg, recent);
  };
  draw().catch((e) => { card.replaceChildren(h('h2', {}, 'Notifications'), h('p', { class: 'muted' }, e.message)); });
  return card;
}

// ---------------------------------------------------------------- health
function ago(iso) {
  if (!iso) return 'never';
  const h = (Date.now() - new Date(iso).getTime()) / 3.6e6;
  if (h < 1) return 'within the hour';
  if (h < 36) return `${Math.round(h)} h ago`;
  return `${Math.round(h / 24)} days ago`;
}
function healthCard(hd) {
  if (!hd) return '';
  const line = (ok, label, detail) => h('li', {},
    h('div', { class: 'row-head' }, h('span', { class: 'name' }, `${ok ? '✓' : '⚠'} ${label}`),
      h('span', { class: ok ? 'faint' : 'up', style: 'font-size:13px;white-space:nowrap' }, detail)));
  const rows = hd.accounts.map((a) => {
    const stale = !a.last_ok || (Date.now() - new Date(a.last_ok).getTime()) > 36 * 3.6e6;
    let detail = a.last_ok ? `synced ${ago(a.last_ok)}` : 'never synced';
    if (a.last_skipped) detail = `skipped ${ago(a.last_skipped_at)}: limit`;
    const row = line(!stale && !a.last_skipped, `${a.institution} · ${a.name}`, detail);
    const bf = a.backfill || {};
    if (a.backfilling) row.append(h('div', { class: 'row-sub' }, h('span', {},
      bf.recent_pending ? `Fetching recent payments: done up to ${dayLabel(bf.recent_pending)}`
      : bf.until && bf.to ? `Filling in older history: ${dayLabel(bf.from)} to ${dayLabel(bf.until)} done, until ${dayLabel(bf.to)}`
      : 'Older history starts at the next sync')));
    return row;
  });
  const localOk = hd.backup_local && (Date.now() - new Date(hd.backup_local).getTime()) < 36 * 3.6e6;
  const offOk = hd.backup_offsite && (Date.now() - new Date(hd.backup_offsite).getTime()) < 36 * 3.6e6;
  rows.push(line(localOk, 'Local backup', ago(hd.backup_local)));
  rows.push(line(offOk, 'Off-site backup', hd.backup_offsite ? ago(hd.backup_offsite) : 'not set up'));
  for (const st of hd.statements || []) {
    const age = (Date.now() - new Date(`${st.period_end}T00:00:00`).getTime()) / 864e5;
    rows.push(line(age <= 40, `${st.name} statement`, age <= 40 ? `up to ${dayLabel(st.period_end)}` : `last one ended ${dayLabel(st.period_end)}: add the new one`));
  }
  if (hd.last_run && hd.last_run.status === 'error') rows.push(line(false, 'Last sync', hd.last_run.error));
  return h('section', { class: 'card' }, h('h2', {}, 'Health'), h('ul', { class: 'rows' }, rows));
}

// ---------------------------------------------------------------- accounts
function openRenameSheet(a, groups) {
  const input = h('input', { value: a.name === a.alias ? '' : a.name, placeholder: a.alias, maxlength: '40', autocomplete: 'off' });
  const msg = h('p', { class: 'muted', role: 'status' });
  const save = h('button', { class: 'btn primary' }, 'Save');
  const group = h('select', {}, h('option', { value: '' }, 'Mine: counted in my totals'),
    groups.map((g) => h('option', { value: String(g.id), selected: g.id === a.group_id }, `In ${g.name}: not in my totals`)),
    h('option', { value: 'new' }, 'In a new group…'));
  let sheet;
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      await api(`accounts/${encodeURIComponent(a.alias)}/name`, { method: 'POST', body: { name: input.value } });
      const was = a.group_id ? String(a.group_id) : '';
      if (group.value === 'new') { sheet.close(); openGroupSheet(null, [a.alias]); return; }
      if (group.value !== was) {
        // Moving the account: take it out of its old group, then add it to the new one.
        const old = groups.find((g) => String(g.id) === was);
        if (old) await api(`groups/${old.id}`, { method: 'POST', body: { ...groupBody(old), accounts: old.accounts.filter((x) => x !== a.alias) } });
        const next = groups.find((g) => String(g.id) === group.value);
        if (next) await api(`groups/${next.id}`, { method: 'POST', body: { ...groupBody(next), accounts: [...next.accounts, a.alias] } });
      }
      sheet.close(); refresh();
    }
    catch (e) { msg.textContent = e.message; save.disabled = false; }
  });
  sheet = sheetOpen(h('h2', {}, a.name),
    h('p', { class: 'muted', style: 'margin:4px 0 0' }, `${a.institution} · ${a.currency}${a.history_from ? ` · history from ${dayLabel(a.history_from)}` : ''}`),
    h('button', { class: 'btn', style: 'margin-top:10px', onclick: () => {
      // Closing steps back over the sheet's history entry; open Activity once that has happened.
      addEventListener('popstate', () => go('tx', { account: a.alias }), { once: true });
      sheet.close();
    } }, 'See its payments'),
    h('label', { class: 'field' }, 'Name (leave empty for the default)', input),
    h('label', { class: 'field' }, 'This account is', group,
      h('span', { class: 'faint', style: 'font-size:12px' }, "Put someone else's accounts in a group (a child, a parent, a business), so their payments are not counted as yours.")), msg,
    h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: () => sheet.close() }, 'Cancel'), save));
}

function groupBody(g) {
  return { name: g.name, behaviour: g.behaviour, category: g.category, subcategory: g.subcategory, money_back: g.money_back,
    sent_as: g.sent_as, payees: g.payees || [], ignored: g.ignored || [] };
}

const BEHAVIOUR = { supported: 'Someone I support', separate: 'Kept separate' };

function groupSummary(g) {
  const cat = [pretty(g.category), g.subcategory && pretty(g.subcategory)].filter(Boolean).join(' · ');
  if (g.behaviour === 'separate') return g.sent_as === 'transfer' ? 'Money between you is a transfer: not counted.' : `What you send counts as ${cat}; what comes back is money in.`;
  return `What you send counts as ${cat}; what comes back ${g.money_back === 'reduce' ? `reduces ${cat}` : 'is money in'}.`;
}

async function openGroupSheet(g, accounts) {
  const d = await api('accounts');
  if (!state.categories) state.categories = (await api('rules')).categories;
  const all = d.balances.accounts.filter((a) => a.role !== 'card');
  const members = new Set(g ? g.accounts : accounts || []);
  const name = h('input', { value: g ? g.name : '', placeholder: 'e.g. Alex, Mum, My company', maxlength: '40', autocomplete: 'off' });
  const behaviour = h('select', {}, Object.entries(BEHAVIOUR).map(([v, label]) => h('option', { value: v, selected: (g ? g.behaviour : 'supported') === v }, label)));
  const current = g ? g.category : 'family';
  const picker = categoryPicker([...new Set([...state.categories, current])].sort(), current, g ? g.subcategory : 'support');
  const back = h('select', {}, h('option', { value: 'money_in', selected: !g || g.money_back === 'money_in' }, 'Other money in'),
    h('option', { value: 'reduce', selected: g && g.money_back === 'reduce' }, 'Reduces the category above'));
  const sent = h('select', {}, h('option', { value: 'transfer', selected: !g || g.sent_as === 'transfer' }, 'A transfer: not counted'),
    h('option', { value: 'spending', selected: g && g.sent_as === 'spending' }, 'Spending, in the category above'));
  const backField = h('label', { class: 'field' }, 'Money they send back counts as', back);
  const sentField = h('label', { class: 'field' }, 'Money you send counts as', sent);
  const catField = h('div', {}, ...picker.fields);
  const sync = () => {
    const sep = behaviour.value === 'separate';
    backField.hidden = sep; sentField.hidden = !sep;
    catField.hidden = sep && sent.value === 'transfer';
  };
  behaviour.addEventListener('change', sync); sent.addEventListener('change', sync); sync();
  const boxes = all.map((a) => {
    const box = h('input', { type: 'checkbox', checked: members.has(a.alias) });
    box.dataset.alias = a.alias;
    const elsewhere = a.group_id && (!g || a.group_id !== g.id) ? ` · now in ${d.groups.find((x) => x.id === a.group_id)?.name}` : '';
    return h('label', { class: 'toggle' }, box, ` ${a.name} `, h('span', { class: 'faint' }, `${a.institution} · ${a.currency}${elsewhere}`));
  });
  // Payee names: found automatically or added by hand count for the group; an unticked one never does.
  const key = (n) => n.toLowerCase().split(/\s+/).filter(Boolean).join(' ');
  const seen = new Map();
  for (const f of (g ? g.found : [])) seen.set(key(f.name), { name: f.name, n: f.n, on: true });
  for (const n of (g ? g.payees : [])) if (!seen.has(key(n))) seen.set(key(n), { name: n, n: 0, on: true });
  for (const n of (g ? g.ignored : [])) seen.set(key(n), { name: n, n: 0, on: false });
  const payeeBoxes = [...seen.values()].map((p) => {
    const box = h('input', { type: 'checkbox', checked: p.on });
    box.dataset.name = p.name;
    return h('label', { class: 'toggle' }, box, ` ${p.name} `, h('span', { class: 'faint' }, p.n ? `${p.n} payment${p.n === 1 ? '' : 's'}` : p.on ? 'added by you' : 'not counted'));
  });
  const extra = h('input', { placeholder: 'Add a payee name, exactly as the bank shows it', maxlength: '80', autocomplete: 'off' });
  const msg = h('p', { class: 'muted', role: 'status' });
  const save = h('button', { class: 'btn primary' }, 'Save');
  let sheet;
  save.addEventListener('click', async () => {
    save.disabled = true; msg.textContent = 'Saving and recounting…';
    const ticks = payeeBoxes.map((l) => l.querySelector('input'));
    const found = new Set((g ? g.found : []).map((f) => key(f.name)));
    // Names ticked that matching would not find on its own are kept as the owner's own list.
    const payees = ticks.filter((x) => x.checked && !found.has(key(x.dataset.name))).map((x) => x.dataset.name);
    if (extra.value.trim()) payees.push(extra.value.trim());
    const cat = picker.value();
    const body = { name: name.value, behaviour: behaviour.value, category: cat.category || 'family', subcategory: cat.subcategory,
      money_back: back.value, sent_as: behaviour.value === 'separate' ? sent.value : 'spending',
      payees: [...new Set([...payees, ...(g ? g.payees : []).filter((n) => ticks.some((x) => x.checked && key(x.dataset.name) === key(n)))])],
      ignored: ticks.filter((x) => !x.checked).map((x) => x.dataset.name),
      accounts: boxes.map((l) => l.querySelector('input')).filter((x) => x.checked).map((x) => x.dataset.alias) };
    try { await api(g ? `groups/${g.id}` : 'groups', { method: 'POST', body }); sheet.close(); refresh(); }
    catch (e) { msg.textContent = e.message; save.disabled = false; }
  });
  const remove = g ? h('button', { class: 'btn', style: 'margin-right:auto' }, 'Remove group') : '';
  if (g) remove.addEventListener('click', async () => {
    if (!confirm(`Remove ${g.name}? Its accounts become yours again.`)) return;
    try { await api(`groups/${g.id}`, { method: 'DELETE' }); sheet.close(); refresh(); }
    catch (e) { msg.textContent = e.message; }
  });
  sheet = sheetOpen(h('h2', {}, g ? g.name : 'New group'),
    h('p', { class: 'muted', style: 'margin:4px 0 0' }, "The group's own payments are not counted as yours, and its balance is shown apart."),
    h('label', { class: 'field' }, 'Name', name),
    h('label', { class: 'field' }, 'This is', behaviour,
      h('span', { class: 'faint', style: 'font-size:12px' }, 'Someone I support: a child or a parent, whose money comes from you. Kept separate: a business, or money you manage for someone.')),
    catField, backField, sentField,
    h('p', { class: 'muted', style: 'margin:14px 0 0;font-size:13px' }, 'Accounts in this group'), ...boxes,
    h('p', { class: 'muted', style: 'margin:14px 0 0;font-size:13px' }, 'Your payments to these names count for this group. Untick one that was matched by mistake.'),
    ...payeeBoxes, h('label', { class: 'field' }, 'Add a name (e.g. a person with no linked account)', extra), msg,
    h('div', { class: 'actions' }, remove, h('button', { class: 'btn', onclick: () => sheet.close() }, 'Cancel'), save));
}

// Adds one or more credit card statements (PDF). Each is sent as it is; the server keeps only what it reads.
function statementPicker(onDone, label = 'Add a statement', accounts = []) {
  const msg = h('p', { class: 'muted', role: 'status', style: 'margin:8px 0 0' });
  const ask = h('div');            // "which account is this?" for files whose account Bank Hub does not recognise
  const input = h('input', { type: 'file', accept: '.pdf,.xml,.ofx,.qfx,application/pdf,text/xml,application/xml', multiple: true, hidden: true });
  const btn = h('button', { class: 'btn primary', disabled: !!state.offline }, state.offline ? 'Needs the server' : label);
  btn.addEventListener('click', () => input.click());
  const send = (f, last, account) => api(`statements?rebuild_after=${last}${account ? `&account=${encodeURIComponent(account)}` : ''}`,
    { method: 'POST', body: new Blob([f], { type: f.type || 'application/octet-stream' }) });
  const line = (r) => `${r.account || 'Statement'} up to ${dayLabel(r.period_end)}: ${r.new} new, ${r.already_had} already there.${r.warnings.length ? ' ' + r.warnings.join(' ') : ''}`;
  input.addEventListener('change', async () => {
    const files = [...input.files];
    if (!files.length) return;
    btn.disabled = true;
    ask.replaceChildren();
    const notes = [], unknown = [];
    let added = 0;
    for (const [i, f] of files.entries()) {
      msg.textContent = `Reading ${i + 1} of ${files.length}…`;
      try {
        const r = await send(f, i === files.length - 1);
        added += r.new;
        notes.push(line(r));
      } catch (e) {
        if (e.status === 409 && accounts.length) unknown.push(f); else notes.push(`${f.name}: ${e.message}`);
      }
    }
    msg.textContent = notes.join(' ');
    btn.disabled = false; input.value = '';
    for (const f of unknown) {
      const pick = h('select', { 'aria-label': 'Account' }, accounts.map((a) => h('option', { value: a.alias }, `${a.name} · ${a.institution}`)));
      const add = h('button', { class: 'btn' }, 'Add to this account');
      const row = h('div', { class: 'field' }, h('span', {}, `${f.name}: which account is it?`), pick, add);
      add.addEventListener('click', async () => {
        add.disabled = true;
        try { const r = await send(f, true, pick.value); row.replaceChildren(line(r)); setTimeout(onDone, 4000); }
        catch (e) { row.append(h('p', { class: 'muted' }, e.message)); add.disabled = false; }
      });
      ask.append(row);
    }
    if (added && !unknown.length) setTimeout(onDone, 4000);
  });
  return h('div', {}, btn, input, msg, ask);
}

// A CSV from any bank: choose the account and the file, confirm which column is which (guessed, and remembered per
// account), and only the payments the account does not have yet are added.
const CSV_DATES = [['%d/%m/%Y', '31/12/2026'], ['%d.%m.%Y', '31.12.2026'], ['%Y-%m-%d', '2026-12-31'], ['%d-%m-%Y', '31-12-2026'],
  ['%m/%d/%Y', '12/31/2026 (month first)'], ['%d/%m/%y', '31/12/26'], ['%d.%m.%y', '31.12.26'], ['%Y/%m/%d', '2026/12/31']];
const CSV_DELIMITERS = [[',', 'comma ,'], [';', 'semicolon ;'], ['\t', 'tab'], ['|', 'bar |']];

async function readTextFile(file) {
  const buf = await file.arrayBuffer();
  try { return new TextDecoder('utf-8', { fatal: true }).decode(buf); }
  catch { return new TextDecoder('windows-1252').decode(buf); }
}

function openCsvSheet(accounts) {
  let sheet, text = '', info = null, current = null;
  const account = h('select', {}, accounts.map((a) => h('option', { value: a.alias }, `${a.name} · ${a.institution} · ${a.currency}`)));
  const input = h('input', { type: 'file', accept: '.csv,.txt,text/csv,text/plain' });
  const mapping = h('div');
  const msg = h('p', { class: 'muted', role: 'status' });
  const go_ = h('button', { class: 'btn primary', disabled: true }, 'Import');

  async function preview(delimiter) {
    msg.textContent = '';
    try {
      info = await api('statements/csv/preview', { method: 'POST', body: { account: account.value, csv: text, delimiter: delimiter || null } });
      const saved = info.saved && info.saved.delimiter === info.delimiter ? info.saved : null;
      render(saved || info.guess);
      go_.disabled = false;
    } catch (e) { mapping.replaceChildren(); go_.disabled = true; msg.textContent = e.message; }
  }

  function render(m) {
    const first = info.rows[Math.min(m.skip || 0, info.rows.length - 1)] || [];
    const sample = (i) => (first[i] || '').slice(0, 18);
    const cols = (value, optional) => {
      const sel = h('select', {}, optional ? h('option', { value: '' }, '—') : '',
        Array.from({ length: info.columns }, (_, i) => h('option', { value: String(i), selected: value === i }, `Column ${i + 1}: ${sample(i)}`)));
      return sel;
    };
    const pick = {
      date: cols(m.date, false), value_date: cols(m.value_date, true), description: cols(m.description, true),
      counterparty: cols(m.counterparty, true), iban: cols(m.iban, true), amount: cols(m.amount, true),
      debit: cols(m.debit, true), credit: cols(m.credit, true),
    };
    const split = h('select', {}, h('option', { value: 'one', selected: m.amount !== null }, 'One signed amount column'),
      h('option', { value: 'two', selected: m.amount === null }, 'Separate debit and credit columns'));
    const delimiter = h('select', {}, CSV_DELIMITERS.map(([v, l]) => h('option', { value: v, selected: v === info.delimiter }, l)));
    const skip = h('input', { type: 'number', min: '0', max: '50', value: String(m.skip ?? 0) });
    const dateFormat = h('select', {}, CSV_DATES.map(([v, l]) => h('option', { value: v, selected: v === m.date_format }, l)));
    const decimal = h('select', {}, h('option', { value: '.', selected: m.decimal === '.' }, '1,234.56 (point)'),
      h('option', { value: ',', selected: m.decimal === ',' }, '1.234,56 (comma)'));
    const invert = h('input', { type: 'checkbox', checked: !!m.invert });
    const oneOrTwo = h('div');
    const field = (label, el) => h('label', { class: 'field' }, label, el);
    const draw = () => oneOrTwo.replaceChildren(...(split.value === 'one'
      ? [field('Amount', pick.amount)] : [field('Money out (debit)', pick.debit), field('Money in (credit)', pick.credit)]));
    split.addEventListener('change', draw); draw();
    delimiter.addEventListener('change', () => preview(delimiter.value));
    const num = (el) => (el.value === '' ? null : Number(el.value));
    current = () => ({
      delimiter: delimiter.value, skip: Number(skip.value) || 0, date: num(pick.date), value_date: num(pick.value_date),
      date_format: dateFormat.value, description: num(pick.description), counterparty: num(pick.counterparty), iban: num(pick.iban),
      amount: split.value === 'one' ? num(pick.amount) : null, debit: split.value === 'two' ? num(pick.debit) : null,
      credit: split.value === 'two' ? num(pick.credit) : null, decimal: decimal.value, invert: invert.checked,
    });
    mapping.replaceChildren(
      h('p', { class: 'faint', style: 'margin:10px 0 4px;font-size:13px' }, `${info.total_rows} rows. The first ones:`),
      h('table', { class: 'csv-preview' }, info.rows.map((r) => h('tr', {}, r.map((c) => h('td', {}, c))))),
      field('Rows to skip at the top (headings)', skip), field('Delimiter', delimiter),
      field('Date', pick.date), field('Date format', dateFormat), field('Description', pick.description),
      field('Counterparty (optional)', pick.counterparty), field('Counterparty IBAN (optional)', pick.iban),
      field('Amounts', split), oneOrTwo, field('Number format', decimal),
      h('label', { class: 'toggle' }, invert, ' Money out is positive in this file (flip the sign)'));
  }

  input.addEventListener('change', async () => {
    if (!input.files.length) return;
    text = await readTextFile(input.files[0]);
    await preview(null);
  });
  account.addEventListener('change', () => { if (text) preview(null); });
  go_.addEventListener('click', async () => {
    go_.disabled = true; msg.textContent = 'Reading…';
    try {
      const r = await api('statements/csv', { method: 'POST', body: { account: account.value, csv: text, mapping: current(), rebuild_after: true } });
      msg.textContent = `${r.account}, ${dayLabel(r.period_start)} to ${dayLabel(r.period_end)}: ${r.new} new, ${r.already_had} already there.${r.warnings.length ? ' ' + r.warnings.join(' ') : ''}`;
      setTimeout(() => { sheet.close(); refresh(); }, 3500);
    } catch (e) { msg.textContent = e.message; go_.disabled = false; }
  });
  sheet = sheetOpen(h('h2', {}, 'Import a CSV'),
    h('p', { class: 'muted', style: 'margin:4px 0 0;font-size:13px' },
      "From your bank's website or app. Only payments the account does not have yet are added. The file is not kept; the column choices are, for the next one."),
    field_('Account', account), field_('File', input), mapping, msg,
    h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: () => sheet.close() }, 'Cancel'), go_));
}
const field_ = (label, el) => h('label', { class: 'field' }, label, el);

function openCardSheet(c) {
  let sheet;
  const used = c.limit_ron ? Math.min(100, (c.owed_ron / c.limit_ron) * 100) : 0;
  const plans = c.plans.length ? h('ul', { class: 'rows', style: 'margin-top:8px' }, c.plans.map((p) => h('li', {},
    h('div', { class: 'row-head' }, h('span', { class: 'name' }, p.merchant), h('span', { class: 'amt num' }, `${ron(p.installment_ron)} / month`)),
    h('div', { class: 'row-sub' }, h('span', {}, `${p.number} · ${ron(p.initial_ron)} from ${dayLabel(p.started)}`), h('span', {}, `${ron(p.remaining_ron)} left`))))) : '';
  sheet = sheetOpen(h('h2', {}, c.name),
    h('p', { class: 'muted', style: 'margin:4px 0 0' }, `${c.institution} · statement up to ${dayLabel(c.period_end)} · ${c.statements} statement${c.statements === 1 ? '' : 's'} added${c.history_from ? `, from ${dayLabel(c.history_from)}` : ''}`),
    h('div', { class: 'hero num', style: 'margin-top:10px' }, nf0.format(c.owed_ron), h('small', {}, `RON owed${c.limit_ron ? ` of ${nf0.format(c.limit_ron)}` : ''}`)),
    c.limit_ron ? h('div', { class: 'meter' }, h('span', { style: `width:${used}%` })) : '',
    h('p', { class: 'faint', style: 'font-size:13px;margin:8px 0 0' },
      [c.due_date ? `Due ${dayLabel(c.due_date)}` : '', c.min_due_ron ? `minimum ${ron(c.min_due_ron, 2)}` : '', c.total_due_ron ? `in full ${ron(c.total_due_ron, 2)}` : ''].filter(Boolean).join(' · ')),
    c.installments_future_ron ? h('p', { class: 'faint', style: 'font-size:13px;margin:4px 0 0' },
      `Instalments: ${ron(c.installments_due_ron)} due with this statement, ${ron(c.installments_future_ron)} still to come.`) : '',
    plans,
    h('div', { style: 'margin-top:14px' }, statementPicker(() => { sheet.close(); refresh(); }, 'Add the next statement')),
    h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: () => {
      addEventListener('popstate', () => go('tx', { account: c.alias }), { once: true });
      sheet.close();
    } }, 'See its payments'), h('button', { class: 'btn', onclick: () => sheet.close() }, 'Close')));
}

async function renderAccounts(params) {
  setTitle('Accounts');
  const d = await api('accounts');
  state.oneOffRon = d.one_off_ron;
  const b = d.balances, st = d.status;
  const ls = d.link_status;
  const linkMsg = params.get('link') === 'importing' || (ls && ls.state === 'importing') ? (
      !ls || ls.state === 'importing' ? `Importing ${ls ? ls.bank : 'the bank'}… ${ls && ls.transactions ? `${ls.transactions} transactions so far` : 'this can take a minute'}`
      : ls.state === 'ok' ? `${ls.bank} linked: ${ls.accounts} account${ls.accounts === 1 ? '' : 's'}, ${ls.transactions} transactions imported.`
      : ls.state === 'no-accounts' ? 'The bank returned no accounts. Link them in the Enable Banking Control Panel first, then try again.'
      : `The import failed: ${ls.error || 'unknown error'}`)
    : { cancelled: 'Linking was cancelled.', invalid: 'That link has expired. Start again.' }[params.get('link')];
  if (ls && ls.state === 'importing') setTimeout(() => { if (parseHash().name === 'accounts') refresh(); }, 3000);
  const total = h('section', { class: 'card' },
    linkMsg ? h('div', { class: 'notice' }, linkMsg) : '',
    h('p', { class: 'eyebrow' }, 'All accounts'),
    h('div', { class: 'hero num' }, nf0.format(b.total_ron), h('small', {}, 'RON')),
    b.total_eur ? h('div', { class: 'delta' }, `≈ ${nf0.format(b.total_eur)} EUR at the BNR rate`) : '',
    h('ul', { class: 'rows', style: 'margin-top:12px' }, b.accounts.filter((a) => !a.group_id && a.role !== 'card').map((a) => h('li', {},
      h('button', { class: 'rowbtn', onclick: () => openRenameSheet(a, d.groups) },
        h('div', { class: 'row-head' }, h('span', { class: 'name' }, a.name), h('span', { class: 'amt num' }, money(a.amount, a.currency))),
        h('div', { class: 'row-sub' }, h('span', {}, `${a.institution}${a.name !== a.alias ? ` · ${a.alias}` : ''}`),
          h('span', {}, a.currency !== 'RON' ? ron(a.amount_ron) : '')))))),
    h('p', { class: 'faint', style: 'margin:8px 0 0;font-size:13px' }, "Tap an account to rename it, or to put it in a group (someone else's money). Names stay in this app; AI assistants see the neutral label."));
  const groupCards = d.groups.map((g) => {
    const list = b.accounts.filter((a) => a.group_id === g.id);
    const total = list.reduce((sum, a) => sum + (a.amount_ron || 0), 0);
    return h('section', { class: 'card' },
      h('div', { class: 'row-head' }, h('h2', {}, g.name), h('button', { class: 'btn', onclick: () => openGroupSheet(g) }, 'Edit')),
      h('p', { class: 'faint', style: 'margin:-4px 0 8px;font-size:13px' }, `${BEHAVIOUR[g.behaviour]} · ${ron(total)}, not part of yours. ${groupSummary(g)}`),
      list.length ? h('ul', { class: 'rows' }, list.map((a) => h('li', {},
        h('button', { class: 'rowbtn', onclick: () => openRenameSheet(a, d.groups) },
          h('div', { class: 'row-head' }, h('span', { class: 'name' }, a.name), h('span', { class: 'amt num' }, money(a.amount, a.currency))),
          h('div', { class: 'row-sub' }, h('span', {}, `${a.institution} · ${a.alias}`)))))) : h('p', { class: 'muted' }, 'No accounts yet.'));
  });
  const newGroup = h('p', { class: 'center-text' }, h('button', { class: 'btn', onclick: () => openGroupSheet(null) }, 'New group'));
  const cardsCard = d.cards.length ? h('section', { class: 'card' }, h('h2', {}, 'Credit cards'),
    h('ul', { class: 'rows' }, d.cards.map((c) => h('li', {},
      h('button', { class: 'rowbtn', onclick: () => openCardSheet(c) },
        h('div', { class: 'row-head' }, h('span', { class: 'name' }, c.name), h('span', { class: 'amt num' }, `${ron(c.owed_ron)} owed`)),
        c.limit_ron ? h('div', { class: 'meter' }, h('span', { style: `width:${Math.min(100, (c.owed_ron / c.limit_ron) * 100)}%` })) : '',
        h('div', { class: 'row-sub' }, h('span', {}, c.due_date ? `Due ${dayLabel(c.due_date)}${c.min_due_ron ? ` · min ${ron(c.min_due_ron)}` : ''}` : `Up to ${dayLabel(c.period_end)}`),
          h('span', {}, c.limit_ron ? `${ron(c.available_ron)} available` : '')))))),
    h('p', { class: 'faint', style: 'margin:8px 0 0;font-size:13px' }, 'Owed on a card is not part of your total above.')) : '';
  const csvAccounts = b.accounts.filter((a) => a.role !== 'card');
  const statementCard = h('section', { class: 'card' }, h('h2', {}, 'Add a statement'),
    h('p', { class: 'faint', style: 'margin:-4px 0 10px;font-size:13px' },
      "Add statements from your bank: BRD PDFs (current accounts and credit cards, which banks often don't share), and CAMT.053 or OFX files from any bank. For anything else, import a CSV and say which column is which. Only payments the account does not have yet are added, and the file itself is not kept."),
    statementPicker(() => refresh(), 'Add statements (PDF, CAMT, OFX)', csvAccounts),
    h('p', { style: 'margin:10px 0 0' }, h('button', { class: 'btn', disabled: !!state.offline || !csvAccounts.length,
      onclick: () => openCsvSheet(csvAccounts) }, 'Import a CSV (any bank)')));

  const syncBtn = h('button', { class: 'btn', disabled: !!state.offline }, state.offline ? 'Sync needs the server' : 'Sync now');
  const syncMsg = h('p', { class: 'muted', role: 'status', style: 'margin:8px 0 0' });
  syncBtn.addEventListener('click', async () => {
    syncBtn.disabled = true; syncMsg.textContent = 'Syncing…';
    try {
      const r = await api('sync', { method: 'POST' });
      const skipped = r.accounts.filter((a) => a.skipped).length;
      const added = r.accounts.reduce((n, a) => n + (a.new || 0), 0);
      syncMsg.textContent = skipped ? `Daily bank limit reached for ${skipped} account(s); the scheduled sync will catch up.` : `Done: ${added} new transaction${added === 1 ? '' : 's'}.`;
    } catch (e) { syncMsg.textContent = e.message; }
    syncBtn.disabled = false;
  });
  const banks = h('section', { class: 'card' }, h('h2', {}, 'Bank connections'),
    h('ul', { class: 'rows' }, st.banks.map((x) => h('li', {},
      h('div', { class: 'row-head' }, h('span', { class: 'name' }, x.institution),
        h('span', {}, x.renew_soon ? h('span', { class: 'badge warn' }, `⚠ ${x.consent_days_left} days left`) : h('span', { class: 'badge' }, `${x.consent_days_left} days left`))),
      h('div', { class: 'row-sub' }, h('span', {}, `Consent until ${x.consent_valid_until}`), h('span', {}, x.last_sync ? `synced ${new Date(x.last_sync).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}` : 'never synced'))))),
    h('div', { style: 'margin-top:12px' }, syncBtn), syncMsg);

  const link = h('section', { class: 'card' }, h('h2', {}, 'Link or renew a bank'));
  if (state.offline) {
    link.append(h('p', { class: 'muted', style: 'margin-top:0' }, 'Linking a bank needs the server. Try again once you are connected.'));
  } else if (d.can_link) {
    const sel = h('select', {}, h('option', { value: '' }, 'Loading banks…'));
    const go_ = h('button', { class: 'btn primary', disabled: true }, 'Continue to the bank');
    const lmsg = h('p', { class: 'muted', role: 'status' });
    api('banks').then((r) => { sel.replaceChildren(h('option', { value: '' }, 'Choose a bank'), ...r.banks.map((n) => h('option', { value: n }, n))); go_.disabled = false; }).catch((e) => { lmsg.textContent = e.message; });
    go_.addEventListener('click', async () => {
      if (!sel.value) return;
      go_.disabled = true; lmsg.textContent = 'Opening the bank…';
      try { const r = await api('link/start', { method: 'POST', body: { bank: sel.value } }); location.href = r.url; }
      catch (e) { lmsg.textContent = e.message; go_.disabled = false; }
    });
    link.append(h('p', { class: 'muted', style: 'margin-top:0' }, 'You approve in the bank\'s app, then come straight back here. The full history is imported right away.'),
      h('label', { class: 'field' }, 'Bank', sel), go_, lmsg);
  } else {
    link.append(h('p', { class: 'muted', style: 'margin-top:0' }, 'Linking from the app needs this return address registered in the Enable Banking Control Panel:'),
      h('p', { class: 'num', style: 'word-break:break-all' }, d.callback_url));
  }
  const yourData = h('section', { class: 'card' }, h('h2', {}, 'Your data'),
    h('p', { class: 'faint', style: 'margin:-4px 0 10px;font-size:13px' }, 'Download everything to this device. The files include names and descriptions, so keep them private.'),
    h('div', { class: 'chips' },
      h('a', { class: 'btn', href: 'api/export.csv', download: '' }, 'Transactions (CSV)'),
      h('a', { class: 'btn', href: 'api/export.json', download: '' }, 'Everything (JSON)')));
  const nw = d.net_worth;
  const netWorth = nw && nw.months.length > 1 ? h('section', { class: 'card' }, h('h2', {}, 'Net worth by month'),
    scrollChart(nw.months.map((p) => ({ value: Math.max(0, p.net_worth_ron), partial: p.partial, month: p.month, short: monthShort(p.month)[0],
      tip: `${monthLabel(p.month)}${p.partial ? ' (so far)' : ''}: ${ron(p.net_worth_ron)}${p.cards_ron ? ` · cards ${ron(p.cards_ron)}` : ''}` })),
      { id: 'net-worth', label: 'Net worth by month', allAccent: true, fitPx: wide.matches ? wideCardPx(innerWidth >= 1200 ? 2 : 3) : null }),
    h('p', { class: 'chart-note' }, `Your accounts minus what is owed on cards, at the end of each month; groups are not included. Worked back from today's balances, so it starts in ${monthLabel(nw.from)}, the first month every account's history fully covers.`),
    tableToggle(nw.months.map((p) => [monthLabel(p.month) + (p.partial ? ' (so far)' : ''), nf0.format(p.net_worth_ron), p.cards_ron ? nf0.format(p.cards_ron) : '—']),
      ['Month', 'Net worth', 'Cards'])) : '';
  // Wide: what you have | other people's money and cards | the connections and the app itself.
  const columns = [[total, netWorth, statementCard], [cardsCard, ...groupCards, newGroup], [banks, healthCard(d.health), alertsCard(), yourData, link]];
  view.replaceChildren(...(wide.matches ? columns.map((c) => h('div', { class: 'col' }, ...c)) : columns.flat()));
}

offlinePrune();
route();
