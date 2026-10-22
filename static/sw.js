/* HERE service worker — deliberately minimal and safe.
 *
 * The site is fully dynamic (sessions, feeds, posts). This worker NEVER
 * caches HTML pages or API responses: every page load and form action goes
 * straight to the network, exactly as if no worker existed. The only thing
 * it caches is the app icons + manifest (they never change under a versioned
 * cache name), which is what makes HERE installable as an app without any
 * risk of stale pages or broken logins.
 */
var CACHE = "here-static-v1";
var PRECACHE = [
  "/static/manifest.json",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-512-maskable.png",
  "/static/icons/apple-touch-icon.png"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (cache) {
      return cache.addAll(PRECACHE);
    }).then(function () {
      return self.skipWaiting();
    })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(names.map(function (name) {
        if (name !== CACHE) return caches.delete(name);
      }));
    }).then(function () {
      return self.clients.claim();
    })
  );
});

self.addEventListener("fetch", function (event) {
  var req = event.request;
  // Non-GET (posts, uploads, logins) — never intercepted.
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // Only the precached static app assets are served cache-first.
  var isAppAsset = PRECACHE.indexOf(url.pathname) !== -1;
  if (!isAppAsset) return; // everything else: straight to network, untouched
  event.respondWith(
    caches.match(req).then(function (hit) {
      return hit || fetch(req).then(function (res) {
        var copy = res.clone();
        caches.open(CACHE).then(function (cache) { cache.put(req, copy); });
        return res;
      });
    })
  );
});
