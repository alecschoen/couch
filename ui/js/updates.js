// Updates (#24): the "Updated to vX" toast, the next time the launcher is on the TV.
//
// couchd keeps live state `updates.announce` ({id, kind, version, kept}) until the UI says
// it was shown (`updates.announced {id}`), so a toast for an update installed at 3 am, with
// the TV off, waits for the morning. Toasts go through `show`: app.toasts.show today. If the
// overlays (#18) bring a toast API of their own, pass it here; nothing else changes.

export function announceUpdates(app, { show = (text, level) => app.toasts.show(text, level), delay = 2000 } = {}) {
  let timer = null;
  let shown = null; // the id last shown, so a slow couchd doesn't get a second toast

  // Someone can see the launcher: the TV is on, the launcher is in front and done starting,
  // and it isn't dimmed to the idle clock.
  const visible = (s) =>
    s.tv?.power !== "standby" &&
    (s.apps?.foreground ?? "launcher") === "launcher" &&
    s.startup?.done !== false &&
    !s.idle?.idle &&
    document.visibilityState !== "hidden";

  const pending = () => {
    const a = app.couch.state.updates?.announce;
    return a && a.id !== shown ? a : null;
  };

  function check() {
    if (timer || !pending() || !visible(app.couch.state)) return;
    // Give the screen a moment to settle (Startup → Home) before the toast comes in.
    timer = setTimeout(() => {
      timer = null;
      const a = pending();
      if (!a || !visible(app.couch.state)) return;
      shown = a.id;
      show(...message(a));
      app.couch.command("updates.announced", { id: a.id }).catch(() => {});
    }, delay);
  }

  app.couch.addEventListener("state", check);
  document.addEventListener("visibilitychange", check);
  return () => {
    clearTimeout(timer);
    app.couch.removeEventListener("state", check);
    document.removeEventListener("visibilitychange", check);
  };
}

export function message(a) {
  if (a.kind === "rolled-back") {
    const kept = a.kept ? ` Couch stayed on ${a.kept}.` : "";
    return [`Couldn't update to ${a.version}.${kept}`, "warning"];
  }
  return [`Updated to ${a.version}`, "info"];
}
