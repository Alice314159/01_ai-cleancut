// 最小 Service Worker：只为“添加到主屏幕”和断网时能打开页面壳。
// 页面导航先走网络（总是拿最新版本），失败才用缓存；/api 与 /ws 一律不经过缓存。
const CACHE = 'cleancut-shell-v1'
self.addEventListener('install', (event) => { self.skipWaiting(); event.waitUntil(caches.open(CACHE).then((c) => c.addAll(['./', './manifest.webmanifest', './icon-192.png']).catch(() => undefined))) })
self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()))
})
self.addEventListener('fetch', (event) => {
  const request = event.request
  if (request.method !== 'GET' || request.mode !== 'navigate') return
  event.respondWith(fetch(request).then((response) => {
    const copy = response.clone()
    caches.open(CACHE).then((c) => c.put('./', copy)).catch(() => undefined)
    return response
  }).catch(() => caches.match('./').then((cached) => cached || Response.error())))
})
