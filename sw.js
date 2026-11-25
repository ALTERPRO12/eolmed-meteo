// EolMed MetOcean Live — service worker : réseau d'abord, dernière version en cache si hors connexion
const C = "eolmed-v1";
const CORE = ["./", "./index.html", "./manifest.json", "./icon-192.png", "./apple-touch-icon.png"];
self.addEventListener("install", e => { self.skipWaiting(); e.waitUntil(caches.open(C).then(c => c.addAll(CORE))); });
self.addEventListener("activate", e => e.waitUntil(
  caches.keys().then(ks => Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener("fetch", e => {
  const r = e.request; if (r.method !== "GET") return;
  const u = new URL(r.url);
  const cacheable = u.origin === location.origin || u.hostname.endsWith("open-meteo.com") || u.hostname === "cdnjs.cloudflare.com";
  if (!cacheable) return;                       // carte Windy : toujours en direct
  e.respondWith(fetch(r).then(res => {
    if (res.ok) { const cp = res.clone(); caches.open(C).then(c => c.put(r, cp)); }
    return res;
  }).catch(() => caches.match(r, {ignoreSearch: u.origin === location.origin})
        .then(m => m || (u.origin === location.origin ? caches.match("./") : undefined))));
});
