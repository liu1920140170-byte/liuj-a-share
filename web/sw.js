/* 聪明钱复盘台 · A股 —— Service Worker（PWA 离线壳 + 安装支持）
 * 策略：静态壳（首页/图标/manifest）网络优先 + 缓存兜底；/api/ 一律不缓存，保证行情实时。 */
var CACHE = "wyckoff-shell-v3";
var SHELL = [
  "/",
  "/manifest.webmanifest",
  "/icon-192.png",
  "/icon-512.png",
  "/apple-touch-icon.png"
];

self.addEventListener("install", function (event) {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE).then(function (cache) {
      return cache.addAll(SHELL);
    }).catch(function () { /* 首次装壳失败不阻塞安装 */ })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (key) {
        return key === CACHE ? null : caches.delete(key);
      }));
    }).then(function () {
      return self.clients.claim();
    })
  );
});

self.addEventListener("fetch", function (event) {
  var url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.indexOf("/api/") === 0) return;   // 行情接口永不缓存

  event.respondWith(
    fetch(event.request).then(function (response) {
      if (response && response.status === 200 && response.type === "basic") {
        var copy = response.clone();
        caches.open(CACHE).then(function (cache) { cache.put(event.request, copy); }).catch(function () {});
      }
      return response;
    }).catch(function () {
      return caches.match(event.request).then(function (hit) {
        return hit || caches.match("/");
      });
    })
  );
});
