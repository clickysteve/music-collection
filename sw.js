const CACHE_NAME = 'cover-art-v1';
const COVER_ART_ORIGIN = 'coverartarchive.org';
const ARCHIVE_ORIGIN = 'archive.org'; // CAA redirects here

self.addEventListener('install', (event) => {
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(
                keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
            )
        ).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);

    // Only cache cover art requests
    if (!url.hostname.includes(COVER_ART_ORIGIN) && !url.hostname.includes(ARCHIVE_ORIGIN)) {
        return;
    }

    event.respondWith(
        caches.open(CACHE_NAME).then(cache =>
            cache.match(event.request).then(cached => {
                if (cached) return cached;

                return fetch(event.request).then(response => {
                    // Only cache successful image responses
                    if (response.ok && response.headers.get('content-type')?.startsWith('image')) {
                        cache.put(event.request, response.clone());
                    }
                    return response;
                }).catch(() => {
                    // Network failure — return nothing, let the placeholder SVG show
                    return new Response('', { status: 503 });
                });
            })
        )
    );
});
