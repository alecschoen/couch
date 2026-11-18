// couchd's on-screen keyboard (#77), the page's side: which text field has focus. Built into
// couchd's overlay script (couchd/overlay_page.py), so every Chromium app has it.
//
// It only watches and reports; it never types and never sets a value. When a text-like field
// gets focus, it tells couchd what it is (through the DevTools binding, window.__couchd), and
// again whenever its value, caret or suggestions change, and when it loses focus. couchd/
// keyboard.py decides what keyboard that is; couchd types into the field over DevTools
// (Input.insertText), as a keyboard would, so pages that check for real typing (sign-in
// pages) take it.
//
//   __couchd('field {"id": "k3x9.2", "focused": true, "tag": "input", "type": "email", ...}')
//   __couchd('field {"id": "k3x9.2", "focused": false}')
//
// couchd calls back into it through the overlay's global: state(id), blur(id), select(id).
const Fields = (() => {
  const TYPES = new Set(["text", "search", "email", "url", "tel", "password", "number"]);
  const POLL_MS = 250; // while a field has focus: its value, caret and suggestions
  const token = Math.random().toString(36).slice(2, 8); // ids from another document never match
  let serial = 0;
  let current = null; // { el, id, sent }
  let timer = 0;

  const send = (message) => {
    if (typeof window.__couchd === "function") window.__couchd("field " + message);
  };
  const words = (s) => (s || "").replace(/\s+/g, " ").trim();

  // The element with focus, inside shadow roots too.
  const active = () => {
    let el = document.activeElement;
    while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
    return el;
  };

  const typable = (el) => {
    if (!el || el.nodeType !== 1 || el.disabled || el.readOnly) return false;
    if (el.localName === "input") return TYPES.has(el.type);
    return el.localName === "textarea" || el.isContentEditable;
  };

  const byId = (el, id) => {
    const root = el.getRootNode();
    return (root.getElementById && root.getElementById(id)) || document.getElementById(id);
  };

  // What the field is called: aria-label, aria-labelledby, a <label>, its title.
  const label = (el) => {
    const aria = words(el.getAttribute("aria-label"));
    if (aria) return aria;
    const by = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
    const named = words(by.map((id) => (byId(el, id) || {}).textContent || "").join(" "));
    if (named) return named;
    const labels = el.labels ? words([...el.labels].map((l) => l.textContent).join(" ")) : "";
    return labels || words(el.getAttribute("title"));
  };

  const shown = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";

  // The field's suggestions: its listbox's options (a combobox's aria-controls or aria-owns),
  // or its <datalist>. null: it has none to show.
  const suggestions = (el) => {
    if (el.list) {
      const typed = el.value.toLowerCase();
      const values = [...el.list.options].map((o) => words(o.value));
      return values.filter((v) => v && v.toLowerCase().includes(typed)).slice(0, 8);
    }
    const ids = `${el.getAttribute("aria-controls") || ""} ${el.getAttribute("aria-owns") || ""}`;
    const box = ids.split(/\s+/).filter(Boolean).map((id) => byId(el, id)).find(Boolean);
    if (!box) return null;
    const out = [];
    for (const option of box.querySelectorAll('[role="option"]')) {
      if (!shown(option)) continue;
      const text = words((option.innerText || "").split("\n")[0]);
      if (text && !out.includes(text)) out.push(text);
      if (out.length >= 8) break;
    }
    return out;
  };

  const report = (field) => {
    const el = field.el;
    const editable = el.localName !== "input" && el.localName !== "textarea";
    let caret = null;
    try {
      caret = editable ? null : el.selectionEnd;
    } catch (e) {} // number inputs have no selection
    return {
      id: field.id,
      focused: true,
      tag: editable ? "editable" : el.localName,
      type: editable ? "" : el.type,
      inputmode: el.getAttribute("inputmode") || "",
      autocomplete: el.getAttribute("autocomplete") || "",
      enterkeyhint: el.getAttribute("enterkeyhint") || "",
      search:
        el.getAttribute("role") === "searchbox" || !!el.closest('[role="search"], search'),
      label: label(el),
      placeholder: words(el.getAttribute("placeholder") || el.getAttribute("aria-placeholder")),
      name: el.getAttribute("name") || "",
      value: (editable ? el.innerText.replace(/\n$/, "") : el.value).slice(0, 2000),
      caret,
      suggestions: suggestions(el),
    };
  };

  // Which field has focus now, and has anything about it changed? Tell couchd.
  const check = () => {
    const el = active();
    if (current && (current.el !== el || !el.isConnected || !typable(el))) {
      send(JSON.stringify({ id: current.id, focused: false }));
      current = null;
    }
    if (!current && typable(el)) current = { el, id: `${token}.${++serial}`, sent: "" };
    if (current) {
      const now = JSON.stringify(report(current));
      if (now !== current.sent) send((current.sent = now));
      if (!timer) timer = setInterval(check, POLL_MS);
    } else if (timer) {
      clearInterval(timer);
      timer = 0;
    }
  };
  const soon = () => setTimeout(check, 0);
  addEventListener("focusin", soon, true);
  addEventListener("focusout", soon, true);
  addEventListener("pageshow", soon);
  if (document.readyState !== "loading") soon();
  else addEventListener("DOMContentLoaded", soon, { once: true });

  const mine = (id) => (current && current.id === id ? current : null);
  return {
    // The field's state now (as reported), or null if that field no longer has focus.
    state(id) {
      check();
      const field = mine(id);
      return field ? report(field) : null;
    },
    blur(id) {
      const field = mine(id);
      if (field) field.el.blur();
      check();
      return !!field;
    },
    // Select all of it, so the next typing replaces it (a suggestion picked).
    select(id) {
      const field = mine(id);
      if (!field) return false;
      const el = field.el;
      if (typeof el.select === "function") el.select();
      else {
        const range = document.createRange();
        range.selectNodeContents(el);
        const selection = getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
      }
      return true;
    },
  };
})();
