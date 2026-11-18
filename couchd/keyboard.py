"""The on-screen keyboard (#77): a sheet that slides up when a text field in a Chromium app
gets focus, and types into it with the TV remote.

This is its state and its keys, and nothing else: couchd/overlay.py shows it and routes the
remote to it, couchd/fields.js tells couchd about the page's text fields, and
ui/js/overlay-view.js draws it. Tests drive it on its own.

    Arrows      move between the keys (Left and Right wrap around a row; Up from the top
                row reaches the suggestions, or a password's Show/Hide)
    OK          presses the key
    Red         deletes a character          Blue   Shift (one capital)
    0-9         type the digit               Back   closes it (the field loses focus)
    Home        closes it, and goes home as always
    Play/pause and the other media keys still go to the app.

The action key (Search, Next, Done...) sends Enter to the field and closes the keyboard; in
a textarea or a contenteditable it only closes it, since Enter there is a new line.

The keys are laid out in rows of ten key widths, as in the design ("Couch on-screen
keyboard"): letters, a 123 page and a #+= page, and a bottom row that depends on the field.
"""

import re
from dataclasses import dataclass
from typing import Any, Literal

Kind = Literal["search", "email", "password", "text"]

LETTERS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
NUMBERS = ("1234567890", "@#$%&*-+()", "!\"':;/?")
SYMBOLS = ("[]{}<>^=\\|", "~`€£¥°•…¿¡", "_§«»¶¤©")
PAGES = {"abc": LETTERS, "123": NUMBERS, "#+=": SYMBOLS}

# The bottom row, by kind of field: (key, width). `s:` keys type what follows the colon.
BOTTOM: dict[str, tuple[tuple[str, float], ...]] = {
    "search": (("mode", 1.5), ("s:-", 1), ("space", 4.5), ("s:.", 1), ("done", 2)),
    "email": (
        ("mode", 1.5),
        ("s:@", 1),
        ("s:.", 1),
        ("s:@gmail.com", 3),
        ("s:.com", 1.5),
        ("done", 2),
    ),
    "other": (("mode", 1.5), ("s:_", 1), ("space", 4.5), ("s:.", 1), ("done", 2)),
}

# The action key's label: the field's enterkeyhint if it has one, else by kind.
HINTS = {"search": "Search", "next": "Next", "go": "Go", "send": "Send", "done": "Done"}
ACTIONS: dict[Kind, str] = {"search": "Search", "email": "Next", "password": "Done", "text": "Done"}
TITLES: dict[Kind, str] = {"search": "Search", "email": "Email", "password": "Password"}

ARROWS = frozenset({"up", "down", "left", "right"})
DIGITS = frozenset(str(n) for n in range(10))
# Still the app's while the keyboard is open.
PASS_THROUGH = frozenset({"play", "pause", "play_pause", "stop", "rewind", "fast_forward"})

EMAIL = r"\be-?mail\b"  # in a field's label, placeholder or name: "Email or phone"
CHIPS = 4  # suggestions shown at most
MAX_TITLE = 60


@dataclass(frozen=True)
class Field:
    """A text field in the page, as couchd/fields.js reports it, read for the keyboard."""

    id: str  # the page's name for this focus of this field
    kind: Kind
    title: str
    placeholder: str
    action: str  # the action key's label
    enter: bool  # the action key sends Enter (not in a textarea or a contenteditable)
    numeric: bool  # starts on the 123 page


@dataclass(frozen=True)
class Op:
    """Something to do to the field, in order: couchd/overlay.py does it over DevTools."""

    kind: Literal["insert", "backspace", "enter", "blur", "replace"]
    text: str = ""  # insert: what to type; replace: what the whole value becomes


@dataclass(frozen=True)
class Key:
    id: str  # "" for the half-key gaps beside the middle row of letters
    w: float  # width, in key widths
    x: float  # left edge, in key widths from the row's start


def _text(value: Any, limit: int = 200) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def read_field(info: Any) -> Field | None:
    """The page's report of a focused field (couchd/fields.js), read leniently."""
    if not isinstance(info, dict) or not isinstance(info.get("id"), str) or not info["id"]:
        return None
    tag, kind_of = _text(info.get("tag")), _text(info.get("type")).lower()
    mode = _text(info.get("inputmode")).lower()
    auto = _text(info.get("autocomplete")).lower().split()
    hint = _text(info.get("enterkeyhint")).lower()
    label, placeholder = _text(info.get("label")), _text(info.get("placeholder"))
    words = f"{label} {placeholder} {_text(info.get('name'))}".lower()
    kind: Kind
    if kind_of == "password":
        kind = "password"
    elif kind_of == "email" or mode == "email" or "email" in auto or re.search(EMAIL, words):
        kind = "email"
    elif kind_of == "search" or mode == "search" or hint == "search" or info.get("search") is True:
        kind = "search"
    else:
        kind = "text"
    title = (label or TITLES.get(kind) or placeholder or _humane(info.get("name")) or "Text")[
        :MAX_TITLE
    ]
    if placeholder.lower() == title.lower():
        placeholder = ""
    elif placeholder.lower().startswith(title.lower() + " "):  # "Search songs, ..." under Search
        rest = placeholder[len(title) + 1 :]
        placeholder = rest[:1].upper() + rest[1:]
    multiline = tag in ("textarea", "editable")
    return Field(
        id=info["id"],
        kind=kind,
        title=title,
        placeholder=placeholder,
        action=HINTS.get(hint) or ACTIONS[kind],
        enter=not multiline,
        numeric=kind_of in ("number", "tel") or mode in ("numeric", "decimal", "tel"),
    )


def _humane(name: Any) -> str:
    words = _text(name).replace("_", " ").replace("-", " ").split()
    return " ".join(words).capitalize()


def layout(page: str, bottom: str) -> list[list[Key]]:
    """The four rows of keys on a page, with each key's place."""
    chars = PAGES[page]
    spec: list[list[tuple[str, float]]] = [
        [(f"c:{c}", 1) for c in chars[0]],
        [(f"c:{c}", 1) for c in chars[1]],
        [("shift" if page == "abc" else "alt", 1.5), *((f"c:{c}", 1) for c in chars[2])],
        list(BOTTOM[bottom]),
    ]
    if page == "abc":
        spec[1] = [("", 0.5), *spec[1], ("", 0.5)]
    spec[2].append(("back", 1.5))
    rows = []
    for row in spec:
        keys, x = [], 0.0
        for key_id, w in row:
            keys.append(Key(key_id, w, x))
            x += w
        rows.append(keys)
    return rows


class Keyboard:
    """The keyboard's state and its keys. Knows nothing of pages or couchd."""

    def __init__(self) -> None:
        self.open = False
        self.field: Field | None = None  # kept after it closes: the last field typed into
        self.app: str | None = None
        self.text = ""  # the field's value, as the page last said
        self.caret: int | None = None  # where typing goes, None: at the end
        self.suggestions: list[str] = []
        self.suggests = False  # the field has a list of suggestions (a combobox, a datalist)
        self.page = "abc"
        self.shift = False
        self.shown = False  # a password, shown instead of dots
        self.row, self.col = 0, 0  # row -1: the suggestions, or Show/Hide
        self._x = 0.5  # where Up and Down aim, so a column stays a column

    # --- opening and closing ---------------------------------------------------------------

    def show(self, field: Field, app: str) -> None:
        self.open, self.field, self.app = True, field, app
        self.text, self.caret, self.suggestions, self.suggests = "", None, [], False
        self.page = "123" if field.numeric else "abc"
        self.shift = self.shown = False
        self.row, self.col, self._x = 0, 0, 0.5

    def hide(self) -> None:
        self.open = False

    def mirror(self, info: dict[str, Any]) -> bool:
        """The field's value, caret and suggestions, as the page says now. True if any of it
        changed what the keyboard shows."""
        before = (self.text, self.caret, self.suggestions, self.suggests)
        value = info.get("value")
        self.text = value if isinstance(value, str) else ""
        caret = info.get("caret")
        ok = isinstance(caret, int) and not isinstance(caret, bool) and 0 <= caret <= len(self.text)
        self.caret = caret if ok else None
        raw = info.get("suggestions")
        self.suggests = isinstance(raw, list)
        seen, chips = {self.text.strip().lower()}, []
        for s in raw if isinstance(raw, list) else ():
            text = _text(s, 120)
            if text and text.lower() not in seen:
                seen.add(text.lower())
                chips.append(text)
        self.suggestions = chips[:CHIPS]
        if self.row == -1 and not self._extras():
            self._to_row(0)
        elif self.row == -1 and self.col >= self._extras():
            self.col = self._extras() - 1
        return before != (self.text, self.caret, self.suggestions, self.suggests)

    # --- keys --------------------------------------------------------------------------------

    def key(self, key: str) -> tuple[bool, list[Op]]:
        """(taken, ops). An open keyboard takes every key but Home and the media keys."""
        if not self.open:
            return False, []
        if key == "home":
            self.hide()
            return False, [Op("blur")]
        if key in PASS_THROUGH:
            return False, []
        if key == "back":
            self.hide()
            return True, [Op("blur")]
        if key in ARROWS:
            self._move(key)
            return True, []
        if key == "select":
            return True, self._press()
        if key == "red":
            return True, [Op("backspace")]
        if key == "blue":
            if self.page != "abc":
                self.page = "abc"
                self._settle()
                self.shift = True
            else:
                self.shift = not self.shift
            return True, []
        if key in DIGITS:
            return True, [Op("insert", key)]
        return True, []  # Green, Yellow: nothing

    def rows(self) -> list[list[Key]]:
        assert self.field is not None
        bottom = self.field.kind if self.field.kind in BOTTOM else "other"
        return layout(self.page, bottom)

    def focused(self) -> str:
        """The focused key's id, as the view knows it: "row:col", "chip:N" or "reveal"."""
        if self.row == -1:
            return "reveal" if self._reveal() else f"chip:{self.col}"
        return f"{self.row}:{self.col}"

    def _press(self) -> list[Op]:
        assert self.field is not None
        if self.row == -1:
            if self._reveal():
                self.shown = not self.shown
                return []
            chosen = self.suggestions[self.col]
            self.hide()
            return [Op("replace", chosen), Op("enter"), Op("blur")]
        key = self.rows()[self.row][self.col].id
        kind, _, arg = key.partition(":")
        match kind:
            case "c":
                if self.shift and self.page == "abc":
                    self.shift = False
                    return [Op("insert", arg.upper())]
                return [Op("insert", arg)]
            case "s":
                return [Op("insert", arg)]
            case "space":
                return [Op("insert", " ")]
            case "back":
                return [Op("backspace")]
            case "shift":
                self.shift = not self.shift
            case "mode":
                self.page = "123" if self.page == "abc" else "abc"
                self.shift = False
                self._settle()
            case "alt":
                self.page = "#+=" if self.page == "123" else "123"
                self._settle()
            case "done":
                self.hide()
                return [Op("enter"), Op("blur")] if self.field.enter else [Op("blur")]
        return []

    # --- moving --------------------------------------------------------------------------------

    def _reveal(self) -> bool:
        return self.field is not None and self.field.kind == "password"

    def _extras(self) -> int:
        """How many stops the row above the keys has."""
        return 1 if self._reveal() else len(self.suggestions)

    def _move(self, key: str) -> None:
        if self.row == -1:
            if key in ("left", "right"):
                n = self._extras()
                self.col = (self.col + (1 if key == "right" else -1)) % n
            elif key == "down":
                self._to_row(0)
            return
        rows = self.rows()
        if key in ("left", "right"):
            row, step = rows[self.row], 1 if key == "right" else -1
            col = self.col
            for _ in row:
                col = (col + step) % len(row)
                if row[col].id:
                    break
            self.col = col
            self._x = row[col].x + row[col].w / 2
        elif key == "up" and self.row == 0:
            if self._extras():
                self.row, self.col = -1, 0
        elif key == "up" or (key == "down" and self.row < len(rows) - 1):
            self._to_row(self.row + (1 if key == "down" else -1))

    def _to_row(self, row: int) -> None:
        """Into `row`, on the key under where Up and Down aim (or the nearest one)."""
        keys = self.rows()[row]
        x = self._x
        under = [i for i, k in enumerate(keys) if k.id and k.x <= x < k.x + k.w]
        if under:
            self.row, self.col = row, under[0]
            return
        stops = [i for i, k in enumerate(keys) if k.id]
        self.row = row
        self.col = min(stops, key=lambda i: abs(keys[i].x + keys[i].w / 2 - x))

    def _settle(self) -> None:
        """After the page changed: the same place, or the nearest key to it."""
        if self.row >= 0:
            row = self.rows()[self.row]
            if self.col >= len(row) or not row[self.col].id:
                self._to_row(self.row)

    # --- what the view draws -----------------------------------------------------------------

    def view(self) -> dict[str, Any]:
        """The keyboard for ui/js/overlay-view.js (couchd/overlay.py adds the app's colour).
        A password's text is only ever dots here unless it's shown."""
        assert self.field is not None
        f = self.field
        masked = f.kind == "password" and not self.shown
        rows = []
        for r, row in enumerate(self.rows()):
            rows.append([self._key_view(r, c, k) for c, k in enumerate(row)])
        return {
            "open": True,
            "kind": f.kind,
            "title": f.title,
            "placeholder": f.placeholder,
            "text": "•" * len(self.text) if masked else self.text,
            "caret": self.caret,
            "masked": masked,
            "reveal": ("Hide" if self.shown else "Show") if f.kind == "password" else None,
            "chips": list(self.suggestions) if self.suggests and f.kind != "password" else None,
            "rows": rows,
            "focus": self.focused(),
        }

    def _key_view(self, r: int, c: int, key: Key) -> dict[str, Any]:
        if not key.id:
            return {"gap": True, "w": key.w}
        assert self.field is not None
        view: dict[str, Any] = {"id": f"{r}:{c}", "w": key.w}
        kind, _, arg = key.id.partition(":")
        match kind:
            case "c":
                view["label"] = arg.upper() if self.shift and self.page == "abc" else arg
            case "s":
                view["label"] = arg
                view["fn"] = len(arg) > 1
            case "space":
                view.update(icon="space", label="", aria="Space", fn=True)
            case "back":
                view.update(icon="backspace", label="", aria="Delete", dot="red")
            case "shift":
                view.update(icon="caps" if self.shift else "shift", label="", aria="Shift")
                view["dot"] = "blue"
            case "mode":
                view.update(label="123" if self.page == "abc" else "ABC", fn=True)
            case "alt":
                view.update(label="#+=" if self.page == "123" else "123", fn=True)
            case "done":
                icon = "find" if self.field.kind == "search" else "go"
                view.update(label=self.field.action, icon=icon, fn=True, go=True)
        return view
