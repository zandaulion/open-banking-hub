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
async function api(path, { method = 'GET', body } = {}) {
  const opts = { method, credentials: 'same-origin', headers: {} };
  if (method !== 'GET') { opts.headers['X-Bankhub'] = '1'; }
  if (body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const r = await fetch(`api/${path}`, opts);
  if (!r.ok) {
    let msg = r.statusText || `Server error ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch {}
    if (r.status >= 500) msg = `Server error ${r.status}. ${msg === `Server error ${r.status}` ? '' : msg}`.trim();
    throw new ApiError(r.status, msg);
  }
  return r.json();
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
                                maxOf = (p) => p.value, draw = null, labelOf = null } = {}) {
  // Room above the bars for the year labels, and for value marks when there are any.
  const STEP = 24, top = labelOf ? 32 : 18, bottom = 20, gap = 2, H = height + (labelOf ? 14 : 0);
  const n = points.length, cols = Math.max(n, visible), W = cols * STEP;
  const max = Math.max(1, ...points.map(maxOf));
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': label });
  svg.style.width = `${(cols / visible) * 100}%`;
  const offset = cols - n;   // fewer months than columns: keep them on the right, like the full chart
  const hasSelection = points.some((p) => p.on);
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
    if (p.on) svg.append(s('rect', { x: i * STEP, y: top - 6, width: STEP, height: H - top - bottom + 6, rx: 4, class: 'colsel' }));
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
const state = { month: thisMonth(), categories: null, scroll: {} };

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
    route();
  } else {
    navMode = 'top';
    location.hash = hash.slice(1);
  }
}
function refresh() { navMode = 'keep'; return route(); }
function setTitle(t, ...actions) { titleEl.textContent = t; actionsEl.replaceChildren(...actions); }
function fail(err) {
  if (err.status === 401) return renderActivate();
  view.replaceChildren(h('div', { class: 'card' }, h('p', {}, 'Something went wrong.'),
    h('p', { class: 'muted' }, err.message || 'No details were given.'),
    h('button', { class: 'btn', onclick: () => refresh() }, 'Try again')));
}

const routes = { month: renderMonth, recurring: renderRecurring, tx: renderTransactions, inbox: renderInbox, rules: renderRules, accounts: renderAccounts };
const TAB_OF = { rules: 'inbox' };
async function route() {
  hideTip();
  state.loadMore = null;
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
  try { await fn(params); tabbar.hidden = false; } catch (e) { fail(e); }
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
  route();
});
addEventListener('scroll', () => { if (!routing) scrollPos[currentKey()] = scrollY; }, { passive: true });
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
  const d = await api(`overview?month=${m}`);
  const isCurrent = m === thisMonth();
  const compareTo = d.to_date ? d.to_date.previous_spent_ron : d.previous.spent_ron;
  const compareText = d.to_date ? `vs the same ${d.to_date.day} days of ${monthLabel(d.previous.month)}` : `vs ${monthLabel(d.previous.month)}`;
  const hasIncome = d.has_regular_income;

  const nav = h('div', { class: 'month-nav' },
    h('button', { class: 'chip icon', 'aria-label': 'Previous month', disabled: m <= d.trend[0].month, onclick: () => go('month', { m: shiftMonth(m, -1) }, { replace: true }) }, '‹'),
    h('strong', {}, monthLabel(m) + (isCurrent ? ' · so far' : '')),
    h('button', { class: 'chip icon', 'aria-label': 'Next month', disabled: isCurrent, onclick: () => go('month', { m: shiftMonth(m, 1) }, { replace: true }) }, '›'));

  const over = d.left_ron < 0;
  const hero = h('section', { class: 'card' },
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

  const oneOffs = d.one_offs.length ? h('section', { class: 'card' },
    h('h2', {}, 'One-off this month'),
    h('p', { class: 'faint', style: 'margin:-4px 0 8px;font-size:13px' }, 'Very large payments, kept out of the totals above.'),
    h('ul', { class: 'rows' }, d.one_offs.map((o) => h('li', {},
      h('button', { class: 'rowbtn', onclick: () => go('tx', { m, kind: 'one_off' }) },
        h('div', { class: 'row-head' }, h('span', { class: 'name' }, o.payee || pretty(o.category)),
          h('span', { class: `amt num ${o.amount_ron > 0 ? 'credit' : ''}` }, `${o.amount_ron > 0 ? '+' : ''}${ron(o.amount_ron)}`)),
        h('div', { class: 'row-sub' }, h('span', {}, [pretty(o.category), o.subcategory && pretty(o.subcategory)].filter(Boolean).join(' · ')), h('span', {}, dayLabel(o.booking_date)))))))) : '';

  const k = (v) => (v >= 1000 ? `${Math.round(v / 1000)}k` : nf0.format(v));
  const legend = h('div', { class: 'legend' },
    hasIncome ? h('span', {}, h('i', { class: 'sw income' }), 'Income') : '',
    h('span', {}, h('i', { class: 'sw spent' }), 'Spent'),
    hasIncome ? h('span', {}, h('i', { class: 'sw over' }), 'Over income') : '',
    h('span', {}, h('b', { class: 'mk' }, '+12k'), 'one-off'));
  const trend = h('section', { class: 'card' },
    h('h2', {}, hasIncome ? 'Income and spending per month' : 'Spending per month'),
    legend,
    scrollChart(d.trend.map((t) => ({ ...t, on: t.month === m, short: monthShort(t.month)[0],
      tip: [`${monthLabel(t.month)}${t.partial ? (t.month === thisMonth() ? ' (so far)' : ' (history starts mid-month)') : ''}`,
            hasIncome ? `income ${ron(t.income_ron)}` : null, `spent ${ron(t.outflow_ron)}`,
            hasIncome ? (t.outflow_ron > t.income_ron ? `over by ${ron(t.outflow_ron - t.income_ron)}` : `left ${ron(t.income_ron - t.outflow_ron)}`) : null,
            t.one_off_out_ron ? `one-off out ${ron(t.one_off_out_ron)}` : null,
            t.one_off_in_ron ? `one-off in ${ron(t.one_off_in_ron)}` : null].filter(Boolean).join(' · ') })),
      { id: 'month-trend', label: 'Income and spending per month',
        onTap: (p) => { if (p.month !== m) setTimeout(() => go('month', { m: p.month }, { replace: true }), 250); },
        maxOf: (p) => Math.max(p.outflow_ron, hasIncome ? p.income_ron : 0),
        labelOf: (p) => (p.one_off_out_ron ? `+${k(p.one_off_out_ron)}` : p.one_off_in_ron ? `↓${k(p.one_off_in_ron)}` : ''),
        draw: (svg, p, g) => {
          const dash = p.partial ? ' partial' : '';
          const inc = hasIncome ? g.scale(p.income_ron) : 0, sp = g.scale(p.outflow_ron);
          if (inc > 0) svg.append(s('path', { d: barPath(g.x, g.base - inc, g.bw, inc, 4), class: 'bar income' + dash }));
          const within = hasIncome ? Math.min(sp, inc) : sp;
          if (within > 0) svg.append(s('path', { d: barPath(g.x, g.base - within, g.bw, within, hasIncome && sp > inc ? 0 : 4), class: 'bar spent' + dash }));
          if (hasIncome && sp > inc) {
            svg.append(s('path', { d: barPath(g.x, g.base - sp, g.bw, sp - inc, 4), class: 'bar over' + dash }));
            if (inc > 0) svg.append(s('line', { x1: g.x - 1, x2: g.x + g.bw + 1, y1: g.base - inc, y2: g.base - inc, class: 'income-line' }));
          }
        } }),
    h('p', { class: 'chart-note' }, 'Tap a month to open it · scroll for older months · dashed: incomplete month'),
    tableToggle(d.trend.map((t) => [monthLabel(t.month) + (t.partial ? ' (incomplete)' : ''), nf0.format(t.income_ron), nf0.format(t.outflow_ron),
      nf0.format(t.income_ron - t.outflow_ron), t.one_off_out_ron || t.one_off_in_ron ? nf0.format(t.one_off_in_ron - t.one_off_out_ron) : '—']),
      ['Month', 'Income', 'Spent', 'Left', 'One-off']));

  const maxCat = Math.max(1, ...d.categories.map((c) => c.outflow_ron));
  const cats = h('section', { class: 'card' }, h('h2', {}, 'By category'),
    d.categories.length ? h('ul', { class: 'rows' }, d.categories.map((c) => h('li', {},
      h('button', { class: 'rowbtn', onclick: () => go('tx', { m, category: c.category }) },
        h('div', { class: 'row-head' }, h('span', { class: 'name' }, pretty(c.category)), h('span', { class: 'amt num' }, ron(c.outflow_ron))),
        h('div', { class: 'meter' }, h('span', { style: `width:${(c.outflow_ron / maxCat) * 100}%` })),
        h('div', { class: 'row-sub' }, h('span', {}, `${c.n} payment${c.n === 1 ? '' : 's'}${c.refunds_ron ? ` · ${ron(c.refunds_ron)} refunded` : ''}`),
          h('span', {}, deltaSpan(pctChange(c.outflow_ron, c.previous_ron)), ` vs ${ron(c.previous_ron)}`)))))) : h('p', { class: 'muted' }, 'No spending this month.'));

  const payees = h('section', { class: 'card' }, h('h2', {}, 'Top payees'),
    h('ul', { class: 'rows' }, d.payees.map((p) => h('li', {},
      h('button', { class: 'rowbtn', onclick: () => go('tx', { m, q: p.payee }) },
        h('div', { class: 'row-head' }, h('span', { class: 'name' }, p.payee), h('span', { class: 'amt num' }, ron(p.outflow_ron))),
        h('div', { class: 'row-sub' }, h('span', {}, pretty(p.category)), h('span', {}, `${p.n}×`)))))));

  view.replaceChildren(hero, oneOffs, trend, cats, payees,
    h('p', { class: 'faint center-text' }, `Booked transactions only · newest ${d.newest_transaction || '—'}`));
}

// ---------------------------------------------------------------- recurring
async function renderRecurring() {
  setTitle('Recurring');
  const d = await api('recurring');
  const tm = d.this_month;
  const summary = h('section', { class: 'card' },
    h('p', { class: 'eyebrow' }, 'Regular payments'),
    h('div', { class: 'hero num' }, nf0.format(d.active_monthly_total_ron), h('small', {}, 'RON / month')),
    h('div', { class: 'delta' }, `${ron(d.active_annual_total_ron)} a year`),
    h('div', { class: 'tiles' },
      h('div', { class: 'tile' }, h('div', { class: 'label' }, `${monthShort(tm.month)} paid`), h('div', { class: 'value num' }, nf0.format(tm.paid_ron))),
      h('div', { class: 'tile' }, h('div', { class: 'label' }, `${monthShort(tm.month)} still due`), h('div', { class: 'value num' }, nf0.format(tm.expected_rest_ron))),
      h('div', { class: 'tile' }, h('div', { class: 'label' }, 'Monthly bills MoM'), h('div', { class: 'value' }, deltaSpan(d.bills_change.change_pct)))),
    h('p', { class: 'faint', style: 'margin:10px 0 0;font-size:13px' },
      `Latest ${d.bills_change.bills} monthly bills: ${ron(d.bills_change.latest_total_ron, 2)}, previous bills ${ron(d.bills_change.previous_total_ron, 2)}.`));

  const chart = h('section', { class: 'card' },
    h('h2', {}, 'Recurring payments per month'),
    scrollChart(d.totals_by_month.map((t, i, a) => {
      const prev = i ? a[i - 1].amount_ron : null;
      const ch = t.partial ? null : pctChange(t.amount_ron, prev);
      return { value: t.amount_ron, partial: t.partial, month: t.month, short: monthShort(t.month)[0],
               tip: `${monthLabel(t.month)}${t.partial ? (t.month === thisMonth() ? ' (so far)' : ' (history starts mid-month)') : ''}: ${ron(t.amount_ron)}${ch === null ? '' : ` (${ch > 0 ? '+' : ''}${ch.toFixed(0)}% MoM)`}` };
    }), { id: 'recurring-trend', label: 'Recurring payments per calendar month', allAccent: true }),
    h('p', { class: 'chart-note' }, 'By calendar month · scroll for older months · dashed: incomplete month'),
    tableToggle(d.totals_by_month.map((t, i, a) => {
      const ch = i ? pctChange(t.amount_ron, a[i - 1].amount_ron) : null;
      return [monthLabel(t.month) + (t.partial ? ' (incomplete)' : ''), nf0.format(t.amount_ron), ch === null || t.partial ? '—' : `${ch > 0 ? '+' : ''}${ch.toFixed(0)}%`];
    }), ['Month', 'RON', 'MoM']));

  const incomeCard = h('section', { class: 'card' }, h('h2', {}, 'Regular income'),
    d.income.length ? h('ul', { class: 'rows' }, d.income.map((x) => {
      const c = x.change;
      return h('li', {},
        h('div', { class: 'row-head' }, h('span', { class: 'name' }, x.payee || x.merchant), h('span', { class: 'amt num credit' }, `+${money(x.last_amount, x.currency)}`)),
        h('div', { class: 'row-sub' }, h('span', {}, `${pretty(x.cadence)} · next ${dayLabel(x.next_expected)}`, x.status === 'missed' ? h('span', { class: 'badge warn', style: 'margin-left:6px' }, '⚠ late') : ''),
          c ? h('span', {}, deltaSpan(c.change_pct, { invert: true }), ` ${c.basis}`) : ''));
    })) : h('p', { class: 'muted', style: 'margin:0' }, 'Salary shows here once the account it arrives in is linked.'));
  const list = h('section', { class: 'card' }, h('h2', {}, 'Payments'),
    h('ul', { class: 'rows' }, d.series.map((x) => {
      const c = x.change;
      const changeEl = c ? h('span', {}, deltaSpan(c.change_pct), ` ${c.basis} · was ${ron(c.previous_ron, 2)}`) : h('span', { class: 'faint' }, 'first charge');
      return h('li', {},
        h('div', { class: 'series' },
          h('div', { class: 'row-head' }, h('span', { class: 'name' }, x.payee || x.merchant)),
          h('span', { class: 'amt num' }, money(x.last_amount, x.currency)),
          h('div', { class: 'row-sub', style: 'grid-column:1/-1' },
            h('span', {}, `${pretty(x.cadence)} · next ${dayLabel(x.next_expected)}`, x.status === 'missed' ? h('span', { class: 'badge warn', style: 'margin-left:6px' }, '⚠ missed') : '')),
          h('div', { class: 'row-sub', style: 'grid-column:1/-1;justify-content:flex-start' }, changeEl),
          h('div', { class: 'spark' }, sparkline(x.history.map((p) => ({ value: p.amount_ron, tip: `${monthLabel(p.month)}: ${ron(p.amount_ron, 2)}` }))))));
    })),
    h('p', { class: 'faint', style: 'margin:10px 0 0;font-size:13px' }, 'MoM compares each bill with the previous one; annual payments compare year over year.'));
  view.replaceChildren(summary, incomeCard, chart, list);
}

// ---------------------------------------------------------------- activity
async function renderTransactions(params) {
  setTitle('Activity');
  const filters = { m: params.get('m') || '', category: params.get('category') || '', q: params.get('q') || '', kind: params.get('kind') || '' };
  const search = h('input', { class: 'search', type: 'search', placeholder: 'Name, or amount: 38.68 · 38 · >500 · 100-200', value: filters.q, 'aria-label': 'Search' });
  let timer;
  search.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => go('tx', { ...clean({ ...filters, q: search.value }) }, { replace: true }), 400); });
  const chips = h('div', { class: 'chips' },
    filters.m ? h('button', { class: 'chip', 'aria-pressed': 'true', onclick: () => go('tx', clean({ ...filters, m: '' }), { replace: true }) }, `${monthLabel(filters.m)} ✕`) : '',
    filters.category ? h('button', { class: 'chip', 'aria-pressed': 'true', onclick: () => go('tx', clean({ ...filters, category: '' }), { replace: true }) }, `${pretty(filters.category)} ✕`) : '',
    filters.kind ? h('button', { class: 'chip', 'aria-pressed': 'true', onclick: () => go('tx', clean({ ...filters, kind: '' }), { replace: true }) }, `${KINDS[filters.kind] || filters.kind} ✕`) : '');
  const list = h('ul', { class: 'rows' });
  const more = h('button', { class: 'btn', style: 'width:100%;margin-top:10px' }, 'Load more');
  const count = h('p', { class: 'faint', style: 'margin:0 0 8px' });
  let offset = 0, lastDay = null;
  async function load() {
    const q = new URLSearchParams(clean({ month: filters.m, category: filters.category, q: filters.q, kind: filters.kind, offset, limit: 60 }));
    const d = await api(`transactions?${q}`);
    count.textContent = `${d.total} transaction${d.total === 1 ? '' : 's'}`;
    for (const t of d.transactions) {
      if (t.booking_date !== lastDay) { lastDay = t.booking_date; list.append(h('li', { class: 'daysep' }, dayLabel(t.booking_date))); }
      const tags = [pretty(t.category)];
      if (t.is_internal_transfer) tags.push('own transfer');
      if (t.is_recurring) tags.push('recurring');
      if (t.is_one_off) tags.push('one-off');
      if (t.kind === 'refund') tags.push('refund');
      if (t.status === 'pending') tags.push('pending');
      if (t.overridden) tags.push('changed by you');
      list.append(h('li', { class: 'tx' }, h('button', { class: 'rowbtn', onclick: () => openTxSheet(t, () => refresh()).catch(fail) },
        h('div', { class: 'row-head' }, h('span', { class: 'name' }, t.payee || '—'),
          h('span', { class: `amt num ${t.amount > 0 ? 'credit' : ''}` }, `${t.amount > 0 ? '+' : ''}${money(t.amount, t.currency)}`)),
        t.description && t.description !== t.payee ? h('div', { class: 'desc' }, t.description) : '',
        h('div', { class: 'row-sub' }, h('span', {}, tags.join(' · ')), h('span', {}, t.account_name || t.account_alias)))));
    }
    offset += d.transactions.length;
    more.hidden = offset >= d.total;
  }
  more.addEventListener('click', () => load().catch(fail));
  state.loadMore = async () => { if (more.hidden) return false; await load(); return true; };
  view.replaceChildren(h('section', { class: 'card' }, search, chips, count, list, more));
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
  const list = h('section', { class: 'card' }, h('ul', { class: 'rows' }, d.items.map((it) => h('li', {},
    h('button', { class: 'rowbtn', onclick: () => openRuleSheet(it, d.categories) },
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
function sheetOpen(...children) {
  const sheet = document.getElementById('sheet');
  sheet.replaceChildren(...children);
  sheet.showModal();
  history.pushState({ sheet: true }, '');
  return sheet;
}
// Closing a sheet any way (button, Escape, a save) drops its history entry; Back closes an open sheet.
document.getElementById('sheet').addEventListener('close', () => { if (history.state && history.state.sheet) history.back(); });
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
  const groups = new Map();
  for (const r of d.rules) { if (!groups.has(r.category)) groups.set(r.category, []); groups.get(r.category).push(r); }
  view.replaceChildren(
    h('p', { class: 'muted', style: 'margin:4px 2px 12px' }, 'Your rules are checked before the built-in ones. Tap one to change or delete it; everything is re-sorted straight away.'),
    ...[...groups].map(([cat, rules]) => h('section', { class: 'card' }, h('h2', {}, pretty(cat)),
      h('ul', { class: 'rows' }, rules.map((r) => h('li', {},
        h('button', { class: 'rowbtn', onclick: () => openRuleEditor(r, d.categories) },
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
  const msg = h('p', { class: 'muted', role: 'status' });
  const save = h('button', { class: 'btn primary' }, 'Save');
  const reset = t.overridden ? h('button', { class: 'btn' }, 'Undo my change') : '';
  const finish = () => { sheet.close(); onDone(); };
  save.addEventListener('click', async () => {
    const v = picker.value();
    if (!v.category) { msg.textContent = 'Choose a category.'; return; }
    save.disabled = true; msg.textContent = 'Saving and re-sorting…';
    try {
      await api(`transactions/${t.id}/category`, { method: 'POST', body: { scope: scopeAll.checked ? 'payee' : 'one', ...v } });
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
  sheet = sheetOpen(...head, ...picker.fields,
    h('label', { class: 'toggle' }, scopeAll, isIn ? 'All payments from this payee (spending only)' : 'All payments to this payee (saved as one of your rules)'),
    h('label', { class: 'toggle' }, scopeOne, 'Only this payment'),
    h('label', { class: 'field' }, `One-off${t.is_one_off ? ' (it is now)' : ''}`, oneOff),
    msg,
    h('div', { class: 'actions' }, reset, h('span', { style: 'flex:1' }), h('button', { class: 'btn', onclick: () => { sheet.close(); if (sheetDirty) onDone(); } }, 'Close'), save));
}
function titleCase(s) { return s.replace(/\b\w/g, (c) => c.toUpperCase()); }

// ---------------------------------------------------------------- accounts
function openRenameSheet(a) {
  const input = h('input', { value: a.name === a.alias ? '' : a.name, placeholder: a.alias, maxlength: '40', autocomplete: 'off' });
  const msg = h('p', { class: 'muted', role: 'status' });
  const save = h('button', { class: 'btn primary' }, 'Save');
  let sheet;
  save.addEventListener('click', async () => {
    save.disabled = true;
    try { await api(`accounts/${encodeURIComponent(a.alias)}/name`, { method: 'POST', body: { name: input.value } }); sheet.close(); refresh(); }
    catch (e) { msg.textContent = e.message; save.disabled = false; }
  });
  sheet = sheetOpen(h('h2', {}, a.name),
    h('p', { class: 'muted', style: 'margin:4px 0 0' }, `${a.institution} · ${a.currency}${a.history_from ? ` · history from ${dayLabel(a.history_from)}` : ''}`),
    h('label', { class: 'field' }, 'Name (leave empty for the default)', input), msg,
    h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: () => sheet.close() }, 'Cancel'), save));
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
    h('ul', { class: 'rows', style: 'margin-top:12px' }, b.accounts.map((a) => h('li', {},
      h('button', { class: 'rowbtn', onclick: () => openRenameSheet(a) },
        h('div', { class: 'row-head' }, h('span', { class: 'name' }, a.name), h('span', { class: 'amt num' }, money(a.amount, a.currency))),
        h('div', { class: 'row-sub' }, h('span', {}, `${a.institution}${a.name !== a.alias ? ` · ${a.alias}` : ''}`),
          h('span', {}, a.currency !== 'RON' ? ron(a.amount_ron) : '')))))),
    h('p', { class: 'faint', style: 'margin:8px 0 0;font-size:13px' }, 'Tap an account to rename it. Names stay in this app; AI assistants see the neutral label.'));

  const syncBtn = h('button', { class: 'btn' }, 'Sync now');
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
  if (d.can_link) {
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
  view.replaceChildren(total, banks, link);
}

route();
