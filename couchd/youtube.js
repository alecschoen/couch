// Runs in every youtube.com/tv document before YouTube's own scripts (couchd/youtube.py).
(() => {
  // H.264 only. The Pi 4 decodes H.264 in hardware up to 1080p; VP9 and AV1 would be
  // decoded in software and drop frames. Turned down here, YouTube falls back to H.264.
  // WebM video is always VP8/VP9/AV1. Audio is left alone (Opus in WebM is fine).
  const SOFTWARE = /(^|[^a-z0-9])(vp0?8|vp0?9|av0?1)([^a-z0-9]|$)/i;
  const refused = (type) =>
    typeof type === "string" && (SOFTWARE.test(type) || /^\s*video\/webm/i.test(type));

  for (const cls of [window.MediaSource, window.ManagedMediaSource]) {
    if (!cls || typeof cls.isTypeSupported !== "function") continue;
    const isTypeSupported = cls.isTypeSupported.bind(cls);
    cls.isTypeSupported = (type) => !refused(type) && isTypeSupported(type);
  }

  const canPlayType = HTMLMediaElement.prototype.canPlayType;
  HTMLMediaElement.prototype.canPlayType = function (type) {
    return refused(type) ? "" : canPlayType.call(this, type);
  };

  const caps = navigator.mediaCapabilities;
  if (caps && typeof caps.decodingInfo === "function") {
    const decodingInfo = caps.decodingInfo.bind(caps);
    caps.decodingInfo = (config) =>
      refused(config && config.video && config.video.contentType)
        ? Promise.resolve({ supported: false, smooth: false, powerEfficient: false })
        : decodingInfo(config);
  }

  if (window.top !== window) return;

  // Phones list this TV by what it tells YouTube's lounge (the channel phones control it
  // through): its name, "YouTube on TV", and its device info, a Samsung, which phones show
  // as "Samsung TV". Both say Couch instead. Only the lounge's requests are changed; the
  // rest of YouTube still sees the Samsung it serves the TV interface to.
  const NAME = "Couch";
  const renamed = (url) => {
    const text = url instanceof URL ? url.href : url;
    if (typeof text !== "string" || !text.includes("/api/lounge/")) return url;
    try {
      const u = new URL(text, location.href);
      if (u.searchParams.has("name")) u.searchParams.set("name", NAME);
      const info = u.searchParams.get("deviceInfo");
      if (info) {
        const device = { ...JSON.parse(info), brand: NAME, model: NAME };
        u.searchParams.set("deviceInfo", JSON.stringify(device));
      }
      return u.href;
    } catch (_) {
      return url;
    }
  };
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    return open.call(this, method, renamed(url), ...rest);
  };
  const fetch = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === "string" || input instanceof URL ? renamed(input) : input;
    return fetch.call(this, url, init);
  };

  // Leaving YouTube goes back to Couch, which keeps the tab, so coming back is instant.
  const home = () => {
    const send = window.__couchd; // a DevTools binding couchd adds; see couchd/chromium.py
    if (typeof send === "function") send("home");
  };

  // Back on YouTube's Home tab opens the left-hand guide; Back again would leave, through
  // the "Welcome back" screen or an exit dialog. Go straight home instead. From the guide
  // on any other tab, Back goes to Home first, as YouTube does.
  const BACK = new Set([27, 8, 166, 10009]); // Escape, Backspace, BrowserBack, Tizen Return
  const onHomeTab = () => {
    const hash = location.hash.replace(/^#/, "");
    if (hash === "" || hash === "/") return true;
    const m = /^\/browse(?:\?(.*))?$/.exec(hash);
    return m !== null && [null, "default"].includes(new URLSearchParams(m[1] || "").get("c"));
  };
  let leaving = false; // swallow the keyup of the Back we acted on, too
  const swallow = (e) => {
    e.preventDefault();
    e.stopImmediatePropagation();
  };
  window.addEventListener(
    "keydown",
    (e) => {
      if (!BACK.has(e.keyCode) || !onHomeTab()) return;
      const focus = document.activeElement;
      if (!focus || !focus.closest("ytlr-guide-response")) return;
      swallow(e);
      leaving = true;
      home();
    },
    true,
  );
  window.addEventListener(
    "keyup",
    (e) => {
      if (leaving && BACK.has(e.keyCode)) {
        leaving = false;
        swallow(e);
      }
    },
    true,
  );

  // Anything else that leaves YouTube closes the window: Back on its sign-in screens, or
  // confirming its exit dialog.
  window.close = home;
})();
