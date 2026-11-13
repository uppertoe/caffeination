/* caffeine's service worker. It exists only so that phones, which show
   notifications only through a service worker, can show the buyer's "has
   added a drink" notifications. It caches nothing and handles no fetches. */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      const tab = list.find((c) => "focus" in c);
      return tab ? tab.focus() : self.clients.openWindow("/");
    })
  );
});
