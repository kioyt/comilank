// Comilank Service Worker — Web Push уведомления
// Версия: 1.0.0
// Размещается в: static/sw.js  (отдаётся через роут /sw.js с заголовком Service-Worker-Allowed: /)

self.addEventListener('push', function(e) {
    var data = {};
    try {
        data = e.data.json();
    } catch(err) {
        data = { title: 'Comilank', body: e.data ? e.data.text() : '' };
    }

    var opts = {
        body:  data.body  || '',
        icon:  '/static/favicon.ico',
        badge: '/static/favicon.ico',
        tag:   data.tag   || 'comilank-push',
        renotify: true,
        data:  { url: data.url || '/' }
    };

    e.waitUntil(
        self.registration.showNotification(data.title || 'Comilank', opts)
    );
});

self.addEventListener('notificationclick', function(e) {
    e.notification.close();
    var url = (e.notification.data && e.notification.data.url)
        ? e.notification.data.url
        : '/';

    e.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(cs) {
            for (var i = 0; i < cs.length; i++) {
                if ('focus' in cs[i]) {
                    cs[i].navigate(url);
                    return cs[i].focus();
                }
            }
            if (clients.openWindow) return clients.openWindow(url);
        })
    );
});

// Базовый install / activate — без кэширования (только push)
self.addEventListener('install',  function(e) { self.skipWaiting(); });
self.addEventListener('activate', function(e) { e.waitUntil(clients.claim()); });
