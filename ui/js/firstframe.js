// Tells couchd once the launcher has drawn its first frame (`ui.first_frame`), so it can end
// the session's splash, which until then covers the gap while Chromium starts (#23,
// couchd/splash.py). Two animation frames: the first callback runs before frame one is
// drawn, the second only once it has been.
//
// Sent once per page load, as soon as both the frame is drawn and couchd is connected.
// `at` is the page's own clock (ms since it started loading), for the boot timing.

export function reportFirstFrame(couch, raf = (cb) => requestAnimationFrame(cb)) {
  let drawn = null;
  let sent = false;
  const send = () => {
    if (drawn === null || sent || !couch.connected) return;
    sent = true;
    couch.command("ui.first_frame", { at: Math.round(drawn) }).catch(() => {
      sent = false; // couchd went away mid-send: try again when it's back
    });
  };
  raf(() =>
    raf(() => {
      drawn = performance.now();
      send();
    }),
  );
  couch.addEventListener("connection", send);
}
