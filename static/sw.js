/* HERE service worker — deliberately minimal and safe.
 *
 * The site is fully dynamic (sessions, feeds, posts). This worker NEVER
 * caches HTML pages or API responses: every page load and form action goes
 * straight to the network, exactly as if no worker existed. The only thing
 * it caches is the app icons + manifest (they never change under a versioned
 * cache name), which is what makes HERE installable as an app without any
 * risk of stale pages or broken logins.
 *
 * It also carries the Web Push handlers (bottom): when the phone's push
 * service delivers a message/call/friend-request notification, the OS
 * wakes this worker just long enough to show it — even with HERE fully
 * closed — and a tap focuses HERE at the right page. Push changes
 * nothing about the caching rules above.
 */
var CACHE = "here-static-v2";
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

/* ------- Web Push -------
 * The server sends a small JSON payload {title, body, url, tag}; we show
 * it verbatim with the HERE icon. Same-tag notifications replace each
 * other (e.g. repeated messages from one person stack as one). */
self.addEventListener("push", function (event) {
  var data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { body: event.data ? event.data.text() : "" };
  }
  var title = data.title || "HERE";
  var options = {
    body: data.body || "",
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/icon-192.png",
    data: { url: data.url || "/" }
  };
  if (data.tag) options.tag = data.tag;
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true })
      .then(function (list) {
        for (var i = 0; i < list.length; i++) {
          var client = list[i];
          if ("focus" in client) {
            if ("navigate" in client) {
              try { client.navigate(url); } catch (e) {}
            }
            return client.focus();
          }
        }
        if (self.clients.openWindow) return self.clients.openWindow(url);
      })
  );
});
