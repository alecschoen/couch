//! The on-screen keyboard (ui/keyboard.slint): a sheet at the bottom that types with the
//! remote. A screen opens one (`Keyboard::new`), sends it the keys while it's open, and gets
//! back what was typed when OK is pressed on its Done key.
//!
//!   arrows      move between keys (up from the top row: the Show/Hide chip, if it has one)
//!   OK          press the key
//!   Back        close it, typing nothing
//!   Red         delete the last character
//!   Blue        Shift (on the number pages, the key in Shift's place: #+= or 123)
//!   0–9         type that digit
//!
//! Red and Blue only reach the launcher while couchd's colour keys stand down, so the screen
//! claims the raw keys while the keyboard is open (`RawKeys`).
//!
//! The pages and their keys are the design's (the Password board of the Couch keyboard):
//! letters (Shift is one-shot: a capital, then small letters again), numbers, and symbols,
//! with the same bottom row on each. Keys are laid out here, in px, so focus can move to the
//! key nearest above or below, as on the screen.

use std::time::Duration;

use serde_json::json;
use slint::{ComponentHandle, ModelRc, Timer, TimerMode, VecModel};

use crate::app::{App, Dir, Key, with_app};
use crate::{KbKey, KbRow, KeyboardData};

/// The sheet's inner width: 1920 less 84px each side.
const WIDTH: f32 = 1752.0;
/// Between keys, and between rows.
const GAP: f32 = 15.0;
/// The Show/Hide chip's centre, from the left of the keys: at the right end of the text box
/// (18px in from its edge, and about 196px wide).
const CHIP_X: f32 = WIDTH - 18.0 - 98.0;
/// couchd lets a raw-keys claim lapse 15 s after it was made or after the last key.
const CLAIM_EVERY: Duration = Duration::from_secs(10);

const LETTERS: [&str; 3] = ["qwertyuiop", "asdfghjkl", "zxcvbnm"];
const NUMBERS: [&str; 3] = ["1234567890", "@#$%&*-+()", "!\"':;/?"];
const SYMBOLS: [&str; 3] = ["[]{}<>^=\\|", "~`€£¥°•…¿¡", "_§«»¶¤©"];

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Page {
    Lower,
    /// Letters, the next one a capital.
    Upper,
    Numbers,
    Symbols,
}

impl Page {
    fn letters(self) -> bool {
        matches!(self, Page::Lower | Page::Upper)
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum KeyId {
    Char(char),
    Shift,
    /// The number pages' #+= / 123.
    Alt,
    Delete,
    /// 123 / ABC: letters or numbers.
    Mode,
    Space,
    Done,
    /// Room, not a key (the letters' middle row is set in by half a key).
    Gap,
}

/// A key as laid out: where it is, in px from the left of the keys.
#[derive(Clone, Copy, PartialEq, Debug)]
pub struct Placed {
    pub id: KeyId,
    pub x: f32,
    pub w: f32,
}

impl Placed {
    fn centre(&self) -> f32 {
        self.x + self.w / 2.0
    }
}

/// Where focus is.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Spot {
    /// The Show/Hide chip, beside the text.
    Chip,
    Key(usize, usize),
}

/// What a key did, for the screen that has the keyboard.
#[derive(Clone, PartialEq, Debug)]
pub enum Action {
    /// Focus moved, or a key changed the text or the page.
    Moved,
    /// An arrow with nowhere to go.
    Edge,
    /// Taken; nothing to show for it.
    Taken,
    /// Not a key the keyboard uses.
    No,
    /// Done: here's the text.
    Done(String),
    /// Back: closed without typing anything.
    Close,
}

/// What the keyboard is for: the words around it.
#[derive(Clone, Debug, Default)]
pub struct Purpose {
    /// Above the title ("Wi-Fi · Living room").
    pub kicker: String,
    pub title: String,
    /// In the empty box.
    pub placeholder: String,
    /// The Done key's label ("Join").
    pub done: String,
    /// A password: shown as dots, with a Show/Hide chip.
    pub secret: bool,
    pub max_len: usize,
}

pub struct Keyboard {
    pub purpose: Purpose,
    text: String,
    page: Page,
    focus: Spot,
    shown: bool,
}

impl Keyboard {
    /// Focus starts on the first letter.
    pub fn new(purpose: Purpose, text: &str) -> Self {
        Keyboard { purpose, text: text.to_string(), page: Page::Lower, focus: Spot::Key(0, 0), shown: false }
    }

    #[cfg(test)]
    fn text(&self) -> &str {
        &self.text
    }

    #[cfg(test)]
    fn page(&self) -> Page {
        self.page
    }

    #[cfg(test)]
    fn focus(&self) -> Spot {
        self.focus
    }

    /// The keys on the page, row by row, laid out.
    pub fn rows(&self) -> Vec<Vec<Placed>> {
        layout(self.page)
    }

    /// The text as the box shows it: dots for a password, unless it's shown.
    pub fn shown_text(&self) -> String {
        if self.purpose.secret && !self.shown { "•".repeat(self.text.chars().count()) } else { self.text.clone() }
    }

    pub fn key(&mut self, key: &Key) -> Action {
        if let Some(dir) = key.arrow() {
            return self.go(dir);
        }
        match key.key.as_str() {
            "select" => self.press_focused(),
            "back" => Action::Close,
            "red" => self.press(KeyId::Delete),
            "blue" => {
                let id = if self.page.letters() { KeyId::Shift } else { KeyId::Alt };
                self.press(id)
            }
            k if k.len() == 1 && k.as_bytes()[0].is_ascii_digit() => self.type_char(k.as_bytes()[0] as char),
            _ => Action::No,
        }
    }

    fn press_focused(&mut self) -> Action {
        match self.focus {
            Spot::Chip => {
                self.shown = !self.shown;
                Action::Moved
            }
            Spot::Key(r, i) => match self.rows().get(r).and_then(|row| row.get(i)) {
                Some(k) => self.press(k.id),
                None => Action::Taken,
            },
        }
    }

    /// What a key does when it's pressed.
    pub fn press(&mut self, id: KeyId) -> Action {
        match id {
            KeyId::Char(c) => {
                let c = if self.page == Page::Upper { c.to_uppercase().next().unwrap_or(c) } else { c };
                let action = self.type_char(c);
                if self.page == Page::Upper {
                    self.set_page(Page::Lower);
                }
                action
            }
            KeyId::Space => self.type_char(' '),
            KeyId::Delete => {
                self.text.pop();
                Action::Moved
            }
            KeyId::Shift => {
                self.set_page(if self.page == Page::Upper { Page::Lower } else { Page::Upper });
                Action::Moved
            }
            KeyId::Mode => {
                self.set_page(if self.page.letters() { Page::Numbers } else { Page::Lower });
                Action::Moved
            }
            KeyId::Alt => {
                self.set_page(if self.page == Page::Numbers { Page::Symbols } else { Page::Numbers });
                Action::Moved
            }
            KeyId::Done => Action::Done(self.text.clone()),
            KeyId::Gap => Action::Taken,
        }
    }

    fn type_char(&mut self, c: char) -> Action {
        if self.text.chars().count() >= self.purpose.max_len.max(1) {
            return Action::Edge;
        }
        self.text.push(c);
        Action::Moved
    }

    /// A new page: focus stays where it is, or on the key nearest it in that row.
    fn set_page(&mut self, page: Page) {
        self.page = page;
        if let Spot::Key(r, i) = self.focus {
            let rows = self.rows();
            let keep = rows[r].get(i).is_some_and(|k| k.id != KeyId::Gap);
            if !keep {
                let x = layout_row_centre(r, i, &rows);
                self.focus = Spot::Key(r, nearest(&rows[r], x));
            }
        }
    }

    fn go(&mut self, dir: Dir) -> Action {
        let rows = self.rows();
        let next = match (self.focus, dir) {
            (Spot::Chip, Dir::Down) => Some(Spot::Key(0, nearest(&rows[0], CHIP_X))),
            (Spot::Chip, _) => None,
            (Spot::Key(0, _), Dir::Up) if self.purpose.secret => Some(Spot::Chip),
            (Spot::Key(0, _), Dir::Up) => None,
            (Spot::Key(r, i), Dir::Up | Dir::Down) => {
                let to = if dir == Dir::Up { r - 1 } else { r + 1 };
                rows.get(to).map(|row| Spot::Key(to, nearest(row, rows[r][i].centre())))
            }
            (Spot::Key(r, i), Dir::Left | Dir::Right) => {
                let row = &rows[r];
                let step = |j: usize| if dir == Dir::Left { j.checked_sub(1) } else { Some(j + 1) };
                let mut j = step(i);
                while let Some(k) = j.and_then(|j| row.get(j)) {
                    if k.id != KeyId::Gap {
                        break;
                    }
                    j = step(j.unwrap());
                }
                j.filter(|&j| j < row.len()).map(|j| Spot::Key(r, j))
            }
        };
        match next {
            Some(spot) => {
                self.focus = spot;
                Action::Moved
            }
            None => Action::Edge,
        }
    }

    // ---- Drawing

    /// Fill KeyboardData (and open the sheet).
    pub fn draw(&self, app: &App) {
        let data = app.ui.global::<KeyboardData>();
        let p = &self.purpose;
        data.set_kicker(p.kicker.as_str().into());
        data.set_title(p.title.as_str().into());
        data.set_placeholder(p.placeholder.as_str().into());
        data.set_secret(p.secret);
        data.set_shown(self.shown);
        data.set_text(self.shown_text().into());
        data.set_empty(self.text.is_empty());
        data.set_chip_focused(self.focus == Spot::Chip);
        let rows: Vec<KbRow> = self
            .rows()
            .iter()
            .enumerate()
            .map(|(r, row)| {
                let keys: Vec<KbKey> = row
                    .iter()
                    .enumerate()
                    .filter(|(_, k)| k.id != KeyId::Gap)
                    .map(|(i, k)| self.kb_key(k, self.focus == Spot::Key(r, i)))
                    .collect();
                KbRow { keys: ModelRc::new(VecModel::from(keys)) }
            })
            .collect();
        data.set_rows(ModelRc::new(VecModel::from(rows)));
        data.set_open(true);
    }

    fn kb_key(&self, k: &Placed, focused: bool) -> KbKey {
        let (label, icon, small, go, dot): (String, &str, bool, bool, &str) = match k.id {
            KeyId::Char(c) => {
                let c = if self.page == Page::Upper { c.to_uppercase().next().unwrap_or(c) } else { c };
                (c.to_string(), "", false, false, "")
            }
            KeyId::Shift => {
                (String::new(), if self.page == Page::Upper { "caps" } else { "shift" }, false, false, "blue")
            }
            KeyId::Alt => ((if self.page == Page::Numbers { "#+=" } else { "123" }).into(), "", true, false, ""),
            KeyId::Delete => (String::new(), "delete", false, false, "red"),
            KeyId::Mode => ((if self.page.letters() { "123" } else { "ABC" }).into(), "", true, false, ""),
            KeyId::Space => (String::new(), "space", true, false, ""),
            KeyId::Done => (self.purpose.done.clone(), "go", true, true, ""),
            KeyId::Gap => (String::new(), "", false, false, ""),
        };
        KbKey { x: k.x, w: k.w, label: label.into(), icon: icon.into(), small, go, dot: dot.into(), focused }
    }
}

/// Close the sheet (what it last showed stays set while it slides away).
pub fn close(app: &App) {
    app.ui.global::<KeyboardData>().set_open(false);
}

/// A page's keys, laid out across WIDTH as the design's flex row does: each key's share of
/// the row (after the gaps between them) is its weight.
pub fn layout(page: Page) -> Vec<Vec<Placed>> {
    let set = match page {
        Page::Lower | Page::Upper => LETTERS,
        Page::Numbers => NUMBERS,
        Page::Symbols => SYMBOLS,
    };
    let chars = |s: &str| s.chars().map(|c| (KeyId::Char(c), 1.0)).collect::<Vec<_>>();
    let middle = if page.letters() {
        [vec![(KeyId::Gap, 0.5)], chars(set[1]), vec![(KeyId::Gap, 0.5)]].concat()
    } else {
        chars(set[1])
    };
    let first = if page.letters() { KeyId::Shift } else { KeyId::Alt };
    let third = [vec![(first, 1.5)], chars(set[2]), vec![(KeyId::Delete, 1.5)]].concat();
    let bottom = vec![
        (KeyId::Mode, 1.5),
        (KeyId::Char('_'), 1.0),
        (KeyId::Space, 4.5),
        (KeyId::Char('.'), 1.0),
        (KeyId::Done, 2.0),
    ];
    [chars(set[0]), middle, third, bottom].into_iter().map(|row| place(&row)).collect()
}

fn place(row: &[(KeyId, f32)]) -> Vec<Placed> {
    let total: f32 = row.iter().map(|(_, w)| w).sum();
    let unit = (WIDTH - GAP * (row.len() as f32 - 1.0)) / total;
    let mut x = 0.0;
    row.iter()
        .map(|&(id, weight)| {
            let w = unit * weight;
            let k = Placed { id, x, w };
            x += w + GAP;
            k
        })
        .collect()
}

fn layout_row_centre(r: usize, i: usize, rows: &[Vec<Placed>]) -> f32 {
    rows[r].get(i).map(Placed::centre).unwrap_or(WIDTH)
}

/// The key in `row` nearest `x` (one it's over, if any).
fn nearest(row: &[Placed], x: f32) -> usize {
    row.iter()
        .enumerate()
        .filter(|(_, k)| k.id != KeyId::Gap)
        .min_by(|(_, a), (_, b)| (a.centre() - x).abs().total_cmp(&(b.centre() - x).abs()))
        .map(|(i, _)| i)
        .unwrap_or(0)
}

/// A screen's claim on the remote's raw keys (remote.raw_keys), so Red and Blue come to the
/// keyboard instead of moving the sound and opening the quick menu. couchd lets a claim lapse
/// after 15 s without a key, so it's made again every 10 s while the keyboard is open; a
/// launcher that goes away mid-typing can't leave the colour keys dead.
#[derive(Default)]
pub struct RawKeys {
    timer: Option<Timer>,
}

impl RawKeys {
    pub fn claim(&mut self, app: &mut App) {
        claim(app, true);
        let timer = Timer::default();
        timer.start(TimerMode::Repeated, CLAIM_EVERY, || {
            with_app(|app| claim(app, true));
        });
        self.timer = Some(timer);
    }

    pub fn release(&mut self, app: &mut App) {
        if self.timer.take().is_some() {
            claim(app, false);
        }
    }

    pub fn held(&self) -> bool {
        self.timer.is_some()
    }
}

fn claim(app: &mut App, on: bool) {
    app.command("remote.raw_keys", json!({"on": on}), Some(Box::new(|_, _| {})));
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(name: &str) -> Key {
        Key { key: name.into(), repeat: false, hold: false }
    }

    fn password() -> Keyboard {
        Keyboard::new(
            Purpose {
                kicker: "Wi-Fi · Upstairs".into(),
                title: "Password".into(),
                placeholder: "Password".into(),
                done: "Join".into(),
                secret: true,
                max_len: 64,
            },
            "",
        )
    }

    fn ids(row: &[Placed]) -> Vec<KeyId> {
        row.iter().map(|k| k.id).collect()
    }

    #[test]
    fn rows_fill_the_width_by_weight() {
        let rows = layout(Page::Lower);
        assert_eq!(rows.len(), 4);
        for row in &rows {
            let last = row.last().unwrap();
            assert!((last.x + last.w - WIDTH).abs() < 0.01);
        }
        // Ten keys, nine gaps: (1752 - 135) / 10 each.
        assert!((rows[0][0].w - 161.7).abs() < 0.01);
        // The middle row is set in by half a key.
        assert_eq!(rows[1][0].id, KeyId::Gap);
        assert!((rows[1][0].w - rows[1][1].w / 2.0).abs() < 0.01);
        assert_eq!(ids(&rows[2])[0], KeyId::Shift);
        assert_eq!(*ids(&rows[2]).last().unwrap(), KeyId::Delete);
        assert_eq!(ids(&rows[3]), [KeyId::Mode, KeyId::Char('_'), KeyId::Space, KeyId::Char('.'), KeyId::Done]);
        assert_eq!(ids(&layout(Page::Numbers)[2])[0], KeyId::Alt);
    }

    #[test]
    fn typing_with_a_one_shot_shift() {
        let mut kb = password();
        assert_eq!(kb.key(&key("blue")), Action::Moved);
        assert_eq!(kb.page(), Page::Upper);
        assert_eq!(kb.press(KeyId::Char('q')), Action::Moved);
        kb.press(KeyId::Char('w'));
        assert_eq!(kb.text(), "Qw");
        assert_eq!(kb.page(), Page::Lower);
        kb.key(&key("4"));
        kb.key(&key("2"));
        kb.press(KeyId::Space);
        assert_eq!(kb.text(), "Qw42 ");
        kb.key(&key("red"));
        assert_eq!(kb.text(), "Qw42");
        assert_eq!(kb.shown_text(), "••••");
    }

    #[test]
    fn pages_and_their_keys() {
        let mut kb = password();
        kb.press(KeyId::Mode);
        assert_eq!(kb.page(), Page::Numbers);
        kb.press(KeyId::Char('@'));
        // Blue on the number pages is the key in Shift's place.
        kb.key(&key("blue"));
        assert_eq!(kb.page(), Page::Symbols);
        kb.press(KeyId::Char('€'));
        kb.key(&key("blue"));
        assert_eq!(kb.page(), Page::Numbers);
        kb.press(KeyId::Mode);
        assert_eq!(kb.page(), Page::Lower);
        assert_eq!(kb.text(), "@€");
    }

    #[test]
    fn ok_presses_the_focused_key_and_done_hands_back_the_text() {
        let mut kb = password();
        assert_eq!(kb.focus(), Spot::Key(0, 0));
        kb.key(&key("select"));
        kb.key(&key("right"));
        kb.key(&key("select"));
        assert_eq!(kb.text(), "qw");
        for _ in 0..3 {
            kb.key(&key("down"));
        }
        assert_eq!(kb.key(&key("down")), Action::Edge);
        for _ in 0..4 {
            kb.key(&key("right"));
        }
        assert_eq!(kb.key(&key("right")), Action::Edge);
        assert_eq!(kb.key(&key("select")), Action::Done("qw".into()));
        assert_eq!(kb.key(&key("back")), Action::Close);
    }

    #[test]
    fn up_and_down_go_to_the_key_above() {
        let mut kb = password();
        let on = |kb: &Keyboard| {
            let Spot::Key(r, i) = kb.focus() else { panic!("on the chip") };
            kb.rows()[r][i].id
        };
        // From 'g' (middle row) down to 'v', right under it, and back.
        kb.focus = Spot::Key(1, 5);
        assert_eq!(on(&kb), KeyId::Char('g'));
        kb.key(&key("down"));
        assert_eq!(on(&kb), KeyId::Char('v'));
        kb.key(&key("up"));
        assert_eq!(on(&kb), KeyId::Char('g'));
        // From 'p' down to 'l', then Delete.
        kb.focus = Spot::Key(0, 9);
        kb.key(&key("down"));
        assert_eq!(on(&kb), KeyId::Char('l'));
        kb.key(&key("down"));
        assert_eq!(on(&kb), KeyId::Delete);
        // Left from 'a' stops at the gap: nothing that way.
        kb.focus = Spot::Key(1, 1);
        assert_eq!(kb.key(&key("left")), Action::Edge);
        // Space, from 'c' and 'b' above it.
        kb.focus = Spot::Key(2, 3);
        kb.key(&key("down"));
        assert_eq!(kb.focus(), Spot::Key(3, 2));
    }

    #[test]
    fn the_show_chip_is_above_the_keys() {
        let mut kb = password();
        kb.press(KeyId::Char('a'));
        kb.key(&key("up"));
        assert_eq!(kb.focus(), Spot::Chip);
        assert_eq!(kb.key(&key("up")), Action::Edge);
        kb.key(&key("select"));
        assert_eq!(kb.shown_text(), "a");
        kb.key(&key("down"));
        assert_eq!(kb.focus(), Spot::Key(0, 9)); // 'p', under the chip
        // Not a password: no chip, nothing above the keys.
        let mut plain = Keyboard::new(Purpose { max_len: 10, ..Purpose::default() }, "x");
        assert_eq!(plain.key(&key("up")), Action::Edge);
        assert_eq!(plain.shown_text(), "x");
    }

    #[test]
    fn a_page_change_keeps_focus_near() {
        let mut kb = password();
        // On Shift, Mode... Shift becomes #+= in the same place.
        kb.focus = Spot::Key(3, 0);
        kb.key(&key("select"));
        assert_eq!(kb.page(), Page::Numbers);
        assert_eq!(kb.focus(), Spot::Key(3, 0));
        // On the numbers' middle row, its last key: letters have a gap there.
        kb.focus = Spot::Key(1, 9);
        kb.press(KeyId::Mode);
        assert_eq!(kb.focus(), Spot::Key(1, 9));
        assert_eq!(kb.rows()[1][9].id, KeyId::Char('l'));
    }

    #[test]
    fn it_stops_at_the_longest() {
        let mut kb = Keyboard::new(Purpose { max_len: 2, ..Purpose::default() }, "ab");
        assert_eq!(kb.press(KeyId::Char('c')), Action::Edge);
        assert_eq!(kb.text(), "ab");
        assert_eq!(kb.key(&key("play_pause")), Action::No);
    }
}
