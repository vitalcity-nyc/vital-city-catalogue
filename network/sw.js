/* Vital City Contact Master Search — service worker.
   Scope: /network/ only (does not touch the public catalogue at the site root).

   Strategy:
   - Navigations + data.enc  -> NETWORK FIRST. Online always wins, so a fresh
     daily build (and the latest HTML) is never masked by a stale cache; we fall
     back to cache only when the device is offline.
   - Icons + manifest        -> CACHE FIRST (tiny, rarely change).
   The encrypted blob can be cached for offline use; it is useless without the
   passphrase, which is never stored here. */
const VERSION = "vc-net-v1";
const SHELL = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icon-192.png",
  "./icon-512.png",
  "./icon-maskable-512.png",
  "./apple-touch-icon.png",
];

self.addEventListener("install", e => {
  self.skipWaiting();
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL).catch(() => {})));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;   // let cross-origin (fonts, sheet) pass through

  const isStatic = /\.(png|webmanifest)$/.test(url.pathname);

  if (isStatic) {
    // cache-first
    e.respondWith(
      caches.match(req).then(hit => hit || fetch(req).then(res => {
        const copy = res.clone();
        caches.open(VERSION).then(c => c.put(req, copy));
        return res;
      }))
    );
    return;
  }

  // network-first for the page and data.enc
  e.respondWith(
    fetch(req).then(res => {
      const copy = res.clone();
      caches.open(VERSION).then(c => c.put(req, copy));
      return res;
    }).catch(() => caches.match(req).then(hit => hit || caches.match("./index.html")))
  );
});
