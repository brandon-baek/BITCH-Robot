/* BITCH's service worker: shows notifications (reminders, timers, things BITCH wants to tell you) even when the
   app is closed, and opens the app when you tap one. It caches nothing, so the app is always the latest version. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('push', e => {
  let d = {}; try { d = e.data.json() } catch { d = { title: 'BITCH', body: e.data ? e.data.text() : '' } }
  e.waitUntil(self.registration.showNotification(d.title || 'BITCH', { body: d.body || '', tag: d.tag || 'bitch', icon: '/icon.png', badge: '/icon.png', renotify: true }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(cs => {
    for (const c of cs) if ('focus' in c) return c.focus();
    return self.clients.openWindow('/');
  }));
});
