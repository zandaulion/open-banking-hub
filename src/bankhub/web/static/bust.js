document.querySelector('#recover').addEventListener('click', async () => {
  document.querySelector('#recovery-status').textContent = 'Clearing app files…';
  const base = new URL('./', location.href).href;
  for (const r of (await navigator.serviceWorker?.getRegistrations()) || []) if (r.scope === base) await r.unregister();
  for (const k of await caches.keys()) if (k.startsWith('bankhub-shell-')) await caches.delete(k);
  sessionStorage.setItem('pwa-kit:updated', '1');
  location.replace(base);
});
