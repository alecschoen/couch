// Motion helpers. Timings come from the --dur-* and --ease-* tokens in css/tokens.css, so CSS
// and JS stay in step, and reduced motion (which zeroes the durations) applies to both.
//
// Rule of thumb on the Pi: animate transform and opacity only. Those run on the compositor;
// anything else repaints every frame.

const root = document.documentElement;

export function reduced() {
  return root.dataset.motion === "reduced";
}

export function duration(name) {
  if (reduced()) return 0;
  const value = getComputedStyle(root).getPropertyValue(`--dur-${name}`).trim();
  return value.endsWith("ms") ? parseFloat(value) : parseFloat(value) * 1000 || 0;
}

export function easing(name = "out") {
  return getComputedStyle(root).getPropertyValue(`--ease-${name}`).trim() || "ease-out";
}

// el.animate() with token timings. Returns null (and does nothing) under reduced motion.
export function animate(el, keyframes, { dur = "quick", ease = "out", ...options } = {}) {
  const ms = duration(dur);
  if (!ms) return null;
  return el.animate(keyframes, { duration: ms, easing: easing(ease), ...options });
}

// Cancel every running animation on el and its children.
export function settle(el) {
  for (const a of el.getAnimations({ subtree: true })) a.cancel();
}
