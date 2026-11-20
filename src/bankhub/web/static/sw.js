// Network-first shell cache with the pwa-kit update flow, scoped to this app's subpath.
// The API is never cached: it carries personal financial data.
const CACHE = 'bankhub-shell-__BUILD_VERSION__';
const SHELL = ['./', 'index.html', 'app.js', 'style.css', 'pwa-update.js', 'manifest.webmanifest',
               'icons/icon.svg', 'icons/icon-192.png', 'icons/icon-512.png'];
importScripts('sw-update.js');

self.addEventListener('install', (event) => event.waitUntil((async () => {
  await (await caches.open(CACHE)).addAll(SHELL);
  await self.skipWaiting();
})()));

self.addEventListener('activate', (event) => event.waitUntil((async () => {
  const keys = await caches.keys();
  await Promise.all(keys.filter((k) => k.startsWith('bankhub-shell-') && k !== CACHE).map((k) => caches.delete(k)));
  await self.clients.claim();
  await announceUpdate();
})()));

// After one request times out, the network is treated as unreachable for a minute: later requests are
// answered from the copy at once (and still tried in the background), instead of each waiting again.
let slowUntil = 0;

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);
  const base = new URL(self.registration.scope);
  if (request.method !== 'GET' || url.origin !== base.origin || !url.pathname.startsWith(base.pathname)) return;
  const path = url.pathname.slice(base.pathname.length);
  if (path.startsWith('api/') || path === 'callback' || path === 'bust' || path.startsWith('bust.')) return;
  if (url.search || (!SHELL.includes(path || './') && request.mode !== 'navigate')) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const saved = async () => {
      const copy = await cache.match(request)
        || (request.mode === 'navigate' ? (await cache.match('./')) || (await cache.match('index.html')) : null);
      if (!copy || request.mode !== 'navigate') return copy;
      // Tell the page it came from the copy, so it goes straight to the saved data instead of waiting again.
      const html = (await copy.text()).replace('<head>', '<head><meta name="bankhub-from-copy" content="1">');
      return new Response(html, { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
    };
    // Network first, so an online phone never runs old code; but a network that hangs (plane Wi-Fi, a
    // captive portal, a VPN with no route) must not keep the app from opening: after 3 s use the copy.
    const network = fetch(request, { cache: 'no-cache' }).then(async (response) => {
      slowUntil = 0;
      if (response.ok && SHELL.includes(path || './')) await cache.put(request, response.clone());
      return response;
    });
    if (Date.now() < slowUntil) {
      const copy = await saved();
      if (copy) { network.catch(() => {}); return copy; }
    }
    const timeout = new Promise((resolve) => setTimeout(resolve, 2500, 'timeout'));
    try {
      const first = await Promise.race([network, timeout]);
      if (first !== 'timeout') return first;
      slowUntil = Date.now() + 60_000;
      const copy = await saved();
      if (copy) { network.catch(() => {}); return copy; }
      return await network;
    } catch {
      return (await saved()) || new Response('Offline', { status: 503 });
    }
  })());
});

// Alerts arrive as Web Push; tapping one opens the app on the relevant screen.
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = { body: event.data && event.data.text() }; }
  event.waitUntil(self.registration.showNotification(data.title || 'Bank Hub', {
    body: data.body || '',
    tag: data.tag || 'bankhub',
    icon: 'icons/icon-192.png',
    badge: 'icons/icon-192.png',
    data: { url: new URL(data.url || './', self.registration.scope).href },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || self.registration.scope;
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const win = wins.find((w) => w.url.startsWith(self.registration.scope));
    if (win) { await win.focus(); return win.navigate(url); }
    return self.clients.openWindow(url);
  })());
});
