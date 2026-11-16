// A stand-in for a screen that hasn't landed yet, so Home's cards have somewhere to go.
// Bluetooth (#16) and Settings (#17) replace their registrations in index.html; this file goes
// once neither uses it.
//
//   router.register("bluetooth", stubScreen({ title: "Bluetooth", palette: "bt" }));

import { h } from "../components.js";

export function stubScreen({ title, palette }) {
  return () => ({
    el: h(
      "section",
      {},
      h("span", { class: "breadcrumb" }, `Home › ${title}`),
      h("span", { class: "t-title", style: { marginTop: "27px" } }, title),
      h("span", { class: "t-md muted", style: { marginTop: "36px" } }, "Not built yet. Back to go home."),
    ),
    palette,
  });
}
