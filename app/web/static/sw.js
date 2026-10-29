// Job Monitor service worker. It caches versioned static assets (/static/...?v=) only;
// pages and API responses are never cached, because they contain private data.
"use strict";

const CACHE = "jm-static-v1";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  const versioned =
    event.request.method === "GET" &&
    url.origin === self.location.origin &&
    url.pathname.startsWith("/static/") &&
    url.searchParams.has("v");
  if (!versioned) {
    return;
  }
  event.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const hit = await cache.match(event.request);
      if (hit) {
        return hit;
      }
      const response = await fetch(event.request);
      if (response.ok) {
        // Drop older versions of the same file before storing the new one.
        for (const old of await cache.keys()) {
          if (new URL(old.url).pathname === url.pathname) {
            await cache.delete(old);
          }
        }
        await cache.put(event.request, response.clone());
      }
      return response;
    }),
  );
});
