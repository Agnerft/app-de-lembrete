const CACHE_NAME = 'mega-app-v56';
const APP_SHELL = [
  '/',
  '/static/styles.css?v=53',
  '/static/app.js?v=56',
  '/static/assets/inter-latin-variable.woff2',
  '/static/assets/mega-nebula-bg.webp',
  '/static/assets/mega-nebula-bg-mobile.webp',
  '/manifest.webmanifest',
  '/static/assets/favicon.ico',
  '/static/assets/site-icon.svg',
  '/static/assets/site-icon-192.png',
  '/static/assets/site-icon-512.png',
  '/static/assets/mega-app-logo.jpg',
  '/static/assets/mega-app-mark.jpg',
  '/static/assets/mega-app-logo-transparent.png',
  '/static/assets/mega-app-mark-transparent.png',
  '/static/assets/mega-app-lockup.png',
  '/static/assets/mega-app-lockup-dark.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;

  const url = new URL(event.request.url);
  const mustBeFresh =
    event.request.mode === 'navigate' ||
    url.pathname.endsWith('/static/styles.css') ||
    url.pathname.endsWith('/static/app.js') ||
    url.pathname.endsWith('/sw.js');

  if (mustBeFresh) {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          return response;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
});

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = {};
  }

  const title = data.title || 'Mega App';
  const options = {
    body: data.body || 'Voce tem um lembrete do Mega App.',
    icon: data.icon || '/static/assets/site-icon-192.png',
    badge: data.badge || '/static/assets/site-icon-192.png',
    data: { url: data.url || '/' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = event.notification.data?.url || '/';
  event.waitUntil(clients.openWindow(url));
});
