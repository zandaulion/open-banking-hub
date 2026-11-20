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
    try {
      const response = await fetch(request, { cache: 'no-cache' });
      if (response.ok && SHELL.includes(path || './')) await cache.put(request, response.clone());
      return response;
    } catch {
      return await cache.match(request)
        || (request.mode === 'navigate' ? await cache.match('./') : null)
        || new Response('Offline', { status: 503 });
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
