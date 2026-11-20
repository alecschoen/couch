// Runs in every music.youtube.com document before its own scripts (couchd/ytmusic.py).
// YouTube Music is made for a mouse; this makes it work from the sofa with the TV remote:
// bigger, a highlight you can see from across the room, the arrow keys moving it to what's
// on screen in that direction, OK clicking it, Back closing or going back, and the media
// keys driving the player. A prototype: it leans on YouTube Music's element names.
(() => {
  if (window.top !== window) return;

  // Size: couchd shows the tab as 1280x720 at 1.5x (couchd/ytmusic.py), so YouTube Music
  // lays itself out for a small screen and the TV draws it big and sharp.
  const CSS = `
    * { cursor: none !important; }
    /* inside the element, so a carousel or list that clips its overflow can't cut it off */
    .couch-focus {
      outline: 3px solid #fff !important;
      outline-offset: -3px !important;
      border-radius: 8px;
      background-color: rgba(255, 255, 255, 0.14) !important;
    }
    ytmusic-two-row-item-renderer.couch-focus { transform: scale(1.04); transition: transform 0.12s; }
    /* things a remote can't use */
    ytmusic-mealbar-promo-renderer, tp-yt-paper-toast { display: none !important; }
  `;

  // What the highlight stops on. Tiles and rows first: inside one, nothing else is a stop,
  // so a song row is one stop (OK plays it), not its title, artist and album links.
  const TILES = [
    "ytmusic-two-row-item-renderer", // album, playlist, artist and mix cards
    "ytmusic-responsive-list-item-renderer", // song rows
    "ytmusic-chip-cloud-chip-renderer", // the mood chips
    "ytmusic-guide-entry-renderer", // Home, Explore, Library
    "ytmusic-pivot-bar-item-renderer",
    "ytmusic-player-queue-item", // the queue on the player page
  ];
  const STOPS = [
    ...TILES,
    "#play-pause-button", "ytmusic-player-bar .previous-button", "ytmusic-player-bar .next-button",
    "ytmusic-like-button-renderer #button-shape-like button",
    "ytmusic-like-button-renderer #button-shape-dislike button",
    "ytmusic-player-bar .middle-controls", // the song playing: OK opens the player page
    "tp-yt-paper-tab", "yt-button-renderer button", "yt-button-shape button",
    "ytmusic-menu-navigation-item-renderer", "ytmusic-menu-service-item-renderer",
    "ytmusic-toggle-menu-service-item-renderer", "tp-yt-paper-item",
    ".ytp-skip-ad-button", ".ytp-ad-skip-button-modern", "a.yt-simple-endpoint[href]",
  ].join(",");
  const SKIP = [
    ".previous-items-button", ".next-items-button", // carousel arrows: moving does that
    "ytmusic-search-box", "#guide-button", "ytmusic-settings-button", "a.ytmusic-logo",
    "#right-content", // Sign in / cast / account at the top right
  ].join(",");

  const shown = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    const s = getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0";
  };

  // A menu or dialog open: only its items are stops.
  const openLayer = () => {
    const layers = document.querySelectorAll(
      "tp-yt-iron-dropdown:not([aria-hidden='true']), tp-yt-paper-dialog",
    );
    return [...layers].reverse().find((l) => shown(l)) || null;
  };

  // The player page (the big cover and the queue) is up.
  const playerOpen = () => {
    const app = document.querySelector("ytmusic-app-layout");
    return !!app && app.getAttribute("player-page-open") !== null;
  };

  const stops = () => {
    const root = openLayer() || document;
    const found = [];
    for (const el of root.querySelectorAll(STOPS)) {
      if (el.closest(SKIP)) continue;
      const tile = el.parentElement && el.parentElement.closest(TILES.join(","));
      if (tile && root.contains(tile)) continue; // part of a tile: the tile is the stop
      if (!openLayer()) {
        const inPlayer = !!el.closest("ytmusic-player-page");
        const inBar = !!el.closest("ytmusic-player-bar");
        if (playerOpen() ? !(inPlayer || inBar) : inPlayer) continue;
      }
      if (shown(el)) found.push(el);
    }
    return found;
  };

  let current = null;

  const focus = (el) => {
    if (current) current.classList.remove("couch-focus");
    current = el;
    if (!el) return;
    el.classList.add("couch-focus");
    el.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "smooth" });
    // Keep the highlighted row or card clear of the top bar and the player bar.
    const r = el.getBoundingClientRect();
    const top = 70, bottom = innerHeight - 80;
    if (r.top < top || r.bottom > bottom) {
      el.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" });
    }
  };

  const valid = (el) => el && el.isConnected && shown(el) && stops().includes(el);

  // The stop nearest to `from` in direction `dir`: mostly by distance that way, and
  // strongly preferring ones that line up with it (the same row or column).
  const nearest = (from, dir, all) => {
    const a = from.getBoundingClientRect();
    let best = null, bestScore = Infinity;
    for (const el of all) {
      if (el === from || el.contains(from) || from.contains(el)) continue;
      const b = el.getBoundingClientRect();
      let along, across, overlap;
      if (dir === "right" || dir === "left") {
        along = dir === "right" ? b.left - a.right : a.left - b.right;
        if ((dir === "right" ? b.left + b.width / 2 : -(b.left + b.width / 2)) <=
            (dir === "right" ? a.left + a.width / 2 : -(a.left + a.width / 2))) continue;
        overlap = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        across = Math.abs((a.top + a.bottom) / 2 - (b.top + b.bottom) / 2);
      } else {
        along = dir === "down" ? b.top - a.bottom : a.top - b.bottom;
        if ((dir === "down" ? b.top + b.height / 2 : -(b.top + b.height / 2)) <=
            (dir === "down" ? a.top + a.height / 2 : -(a.top + a.height / 2))) continue;
        overlap = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        across = Math.abs((a.left + a.right) / 2 - (b.left + b.right) / 2);
      }
      along = Math.max(along, 0);
      const score = along + across * (overlap > 0 ? 0.5 : 3) + (overlap > 0 ? 0 : 400);
      if (score < bestScore) {
        bestScore = score;
        best = el;
      }
    }
    return best;
  };

  // Where the highlight starts: the first thing in the page's content, not the side menu.
  const SIDE = "ytmusic-guide-renderer, ytmusic-nav-bar, ytmusic-player-bar";
  const first = (all, contentOnly = false) => {
    const content = all.filter((el) => !el.closest(SIDE));
    const pick = (content.length || contentOnly ? content : all).slice();
    pick.sort((x, y) => {
      const a = x.getBoundingClientRect(), b = y.getBoundingClientRect();
      return a.top - b.top || a.left - b.left;
    });
    return pick.find((el) => el.getBoundingClientRect().bottom > 0) || pick[0] || null;
  };

  const move = (dir) => {
    const all = stops();
    if (!valid(current)) {
      focus(first(all));
      return;
    }
    const next = nearest(current, dir, all);
    if (next) focus(next);
  };

  const click = (el) => {
    el.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
    el.click();
  };

  // OK: a song row plays; a card opens; anything else is clicked.
  const activate = () => {
    if (!valid(current)) {
      move("down");
      return;
    }
    const el = current;
    if (el.matches("ytmusic-responsive-list-item-renderer")) {
      const play = el.querySelector("ytmusic-play-button-renderer");
      if (play) return click(play);
    }
    if (el.matches("ytmusic-two-row-item-renderer")) {
      const link = el.querySelector("a.yt-simple-endpoint[href], a[href]");
      if (link) return click(link);
    }
    if (el.matches("ytmusic-player-queue-item")) {
      const play = el.querySelector(".song-info, ytmusic-play-button-renderer");
      if (play) return click(play);
    }
    if (el.matches("ytmusic-guide-entry-renderer, ytmusic-pivot-bar-item-renderer")) {
      const link = el.querySelector("tp-yt-paper-item, a");
      if (link) return click(link);
    }
    click(el);
  };

  const home = () => {
    const send = window.__couchd; // a DevTools binding couchd adds; see couchd/chromium.py
    if (typeof send === "function") send("home");
  };

  // Back: close a menu, then the player page, then go back a page, then leave.
  const back = () => {
    const layer = openLayer();
    if (layer) {
      document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", keyCode: 27, bubbles: true }));
      if (typeof layer.close === "function") layer.close();
      return;
    }
    if (playerOpen()) {
      const toggle = document.querySelector("ytmusic-player-bar .toggle-player-page-button");
      if (toggle) return click(toggle);
    }
    if (location.pathname === "/") return home();
    // The previous page that isn't the player (closing the player page adds an entry, so
    // plain history.back() would open it again). None: leave.
    const nav = window.navigation;
    if (!nav || !nav.currentEntry) return history.back();
    const page = (url) => {
      const u = new URL(url);
      return u.origin !== location.origin || u.pathname === "/watch" ? null : u.pathname + u.search;
    };
    const here = page(nav.currentEntry.url);
    const entries = nav.entries();
    for (let i = nav.currentEntry.index - 1; i >= 0; i--) {
      const there = page(entries[i].url);
      if (there && there !== here) return void nav.traverseTo(entries[i].key);
    }
    home();
  };

  const player = (sel) => {
    const el = document.querySelector(`ytmusic-player-bar ${sel}`);
    if (el) click(el);
  };

  const KEYS = {
    ArrowUp: () => move("up"),
    ArrowDown: () => move("down"),
    ArrowLeft: () => move("left"),
    ArrowRight: () => move("right"),
    Enter: activate,
    Escape: back,
    BrowserBack: back,
    Backspace: back,
    MediaPlayPause: () => player("#play-pause-button"),
    MediaTrackNext: () => player(".next-button"),
    MediaTrackPrevious: () => player(".previous-button"),
    MediaFastForward: () => player(".next-button"),
    MediaRewind: () => player(".previous-button"),
  };

  window.addEventListener(
    "keydown",
    (e) => {
      const act = KEYS[e.key];
      if (!act) return;
      if (e.target && e.target.localName === "input" && e.key.startsWith("Arrow")) {
        if (e.key === "ArrowLeft" || e.key === "ArrowRight") return; // move in the text
      }
      e.preventDefault();
      e.stopImmediatePropagation();
      act();
    },
    true,
  );
  // The page's own scroll and click handling never sees these keys.
  window.addEventListener(
    "keyup",
    (e) => {
      if (KEYS[e.key]) {
        e.preventDefault();
        e.stopImmediatePropagation();
      }
    },
    true,
  );

  const style = () => {
    const el = document.createElement("style");
    el.id = "couch-tv";
    el.textContent = CSS;
    (document.head || document.documentElement).appendChild(el);
  };
  if (document.documentElement) style();
  else document.addEventListener("DOMContentLoaded", style, { once: true });

  // A new page, or the highlighted thing gone (a menu closed, a list redrawn): start
  // again from the top of the content, once there is some.
  let page = location.href;
  let restart = false;
  setInterval(() => {
    if (location.href !== page) {
      page = location.href;
      restart = !!current;
    }
    if (current && !current.isConnected) restart = true;
    if (!restart) return;
    const start = first(stops(), true);
    if (start) {
      restart = false;
      focus(start);
    }
  }, 500);

  // Leaving YouTube Music (window.close) goes back to Couch.
  window.close = home;
})();
