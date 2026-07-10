const COVER_CACHE = 'cover-art-v1';
const SHELL_CACHE = 'shell-v1';
const COVER_ART_ORIGIN = 'coverartarchive.org';
const ARCHIVE_ORIGIN = 'archive.org'; // CAA redirects here

// App shell + data: network-first with cache fallback, so the site works
// offline (e.g. in a record shop basement) but always shows fresh data when
// there's a connection.
const SHELL_PATHS = ['index.html', 'albums.json', 'heatmap_data.json', 'manifest.webmanifest',
                     'icon-192.png', 'icon-512.png', 'apple-touch-icon.png'];

self.addEventListener('install', (event) => {
    self.skipWaiting();
    event.waitUntil(
        caches.open(SHELL_CACHE).then(cache =>
            cache.addAll(SHELL_PATHS.map(p => new Request(p, { cache: 'no-cache' }))).catch(() => {})
        )
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(
                keys.filter(k => k !== COVER_CACHE && k !== SHELL_CACHE).map(k => caches.delete(k))
            )
        ).then(() => self.clients.claim())
    );
});

function isShellRequest(url) {
    if (url.origin !== self.location.origin) return false;
    const name = url.pathname.split('/').pop() || 'index.html';
    return SHELL_PATHS.includes(name);
}

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);

    // Shell + data: network-first, fall back to cache when offline
    if (event.request.method === 'GET' && (isShellRequest(url) || event.request.mode === 'navigate')) {
        event.respondWith(
            fetch(event.request).then(response => {
                if (response.ok) {
                    const clone = response.clone();
                    caches.open(SHELL_CACHE).then(cache => cache.put(event.request, clone));
                }
                return response;
            }).catch(() =>
                caches.match(event.request, { ignoreSearch: true }).then(cached =>
                    cached || new Response('Offline', { status: 503 })
                )
            )
        );
        return;
    }

    // Cover art: cache-first (covers never change for a given URL)
    if (!url.hostname.includes(COVER_ART_ORIGIN) && !url.hostname.includes(ARCHIVE_ORIGIN)) {
        return;
    }

    event.respondWith(
        caches.open(COVER_CACHE).then(cache =>
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
