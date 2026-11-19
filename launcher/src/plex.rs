//! Plex's Continue watching (#86), as Home shows it: couchd's live state `plex`
//! (couchd/plex.py), already in words, and where focus is in the row.
//!
//! Nothing shows unless Plex is linked, the `home.plex_continue` setting is on and the list
//! has something in it: Home is then as it was without it.

use serde_json::Value;

use crate::app::{Dir, Outcome};
use crate::state;

/// Home shows five.
pub const ITEMS: usize = 5;

/// One show or film partway through.
#[derive(Clone, Debug, PartialEq)]
pub struct Item {
    pub key: String,
    pub title: String,
    /// "S2 · E5 · 18 min left", "Film · 1 h 12 min left", "S1 · E3 · Next episode".
    pub subtitle: String,
    /// "Season 2 · Episode 5 · Night Shift · 18 min left", for the heading when focused.
    pub detail: String,
    /// How far through, 0 to 1.
    pub progress: f32,
    /// couchd's path for its picture, or None: the flat placeholder.
    pub thumb: Option<String>,
}

/// The list to show, or empty: not linked, the setting off, or nothing partway through.
pub fn items(s: &Value) -> Vec<Item> {
    if state::setting(s, "home.plex_continue").as_bool() == Some(false)
        || state::bool_at(s, "plex.linked") != Some(true)
    {
        return Vec::new();
    }
    let text = |v: &Value, k: &str| v.get(k).and_then(Value::as_str).unwrap_or("").to_string();
    state::list_at(s, "plex.continue")
        .iter()
        .filter_map(|v| {
            let key = text(v, "key");
            let title = text(v, "title");
            if key.is_empty() || title.is_empty() {
                return None;
            }
            Some(Item {
                key,
                title,
                subtitle: text(v, "subtitle"),
                detail: text(v, "detail"),
                progress: v.get("progress").and_then(Value::as_f64).unwrap_or(0.0).clamp(0.0, 1.0) as f32,
                thumb: v.get("thumb").and_then(Value::as_str).filter(|t| crate::thumbs::valid(t)).map(str::to_string),
            })
        })
        .take(ITEMS)
        .collect()
}

/// What Settings › Network says: "Linked ›", "Not linked ›".
pub fn summary(s: &Value) -> String {
    if state::bool_at(s, "plex.linked") == Some(true) { "Linked ›".into() } else { "Not linked ›".into() }
}

/// The row on Home and focus in it. Up from the Plex card goes in, on the first; Left and
/// Right move along; Down or Back come out to the cards again.
#[derive(Debug, Default)]
pub struct Row {
    pub items: Vec<Item>,
    pub inside: bool,
    pub focus: usize,
}

impl Row {
    /// A new list: focus stays on the same show if it's still there, else where it was (or
    /// the last); with nothing left, out to the cards. True if anything changed.
    pub fn update(&mut self, items: Vec<Item>) -> bool {
        if items == self.items {
            return false;
        }
        let key = self.items.get(self.focus).map(|i| i.key.clone());
        self.focus = key
            .and_then(|k| items.iter().position(|i| i.key == k))
            .unwrap_or(self.focus.min(items.len().saturating_sub(1)));
        self.items = items;
        if self.items.is_empty() {
            self.inside = false;
            self.focus = 0;
        }
        true
    }

    /// Up from the Plex card.
    pub fn enter(&mut self) -> Outcome {
        if self.items.is_empty() {
            return Outcome::Edge;
        }
        self.inside = true;
        self.focus = 0;
        Outcome::Moved
    }

    pub fn leave(&mut self) {
        self.inside = false;
    }

    /// An arrow while inside.
    pub fn arrow(&mut self, dir: Dir) -> Outcome {
        match dir {
            Dir::Left if self.focus > 0 => {
                self.focus -= 1;
                Outcome::Moved
            }
            Dir::Right if self.focus + 1 < self.items.len() => {
                self.focus += 1;
                Outcome::Moved
            }
            Dir::Down => {
                self.inside = false;
                Outcome::Moved
            }
            _ => Outcome::Edge,
        }
    }

    pub fn focused(&self) -> Option<&Item> {
        if self.inside { self.items.get(self.focus) } else { None }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const THUMB: &str = "/api/plex/thumb/0123456789abcdef01234567";

    fn plex(n: usize) -> Value {
        let list: Vec<Value> = (0..n)
            .map(|i| {
                json!({"key": format!("{i}"), "title": format!("Show {i}"), "subtitle": "S2 \u{b7} E5 \u{b7} 18 min left",
                    "detail": "Season 2 \u{b7} Episode 5 \u{b7} Night Shift \u{b7} 18 min left", "progress": 0.62,
                    "thumb": if i == 0 { Value::from(THUMB) } else { Value::Null }})
            })
            .collect();
        json!({"plex": {"linked": true, "continue": list}})
    }

    #[test]
    fn the_list_from_couchd() {
        let items = items(&plex(7));
        assert_eq!(items.len(), 5);
        assert_eq!(items[0].title, "Show 0");
        assert_eq!(items[0].subtitle, "S2 \u{b7} E5 \u{b7} 18 min left");
        assert_eq!(items[0].thumb.as_deref(), Some(THUMB));
        assert_eq!(items[1].thumb, None);
        assert!((items[0].progress - 0.62).abs() < 1e-6);
    }

    #[test]
    fn nothing_unless_linked_on_and_listed() {
        assert!(items(&json!({})).is_empty());
        let mut s = plex(3);
        s["plex"]["linked"] = json!(false);
        assert!(items(&s).is_empty());
        let mut s = plex(3);
        s["settings"] = json!({"home.plex_continue": false});
        assert!(items(&s).is_empty());
        assert!(items(&plex(0)).is_empty());
        // Bits missing, or a picture that isn't couchd's: dropped, or the placeholder.
        let s = json!({"plex": {"linked": true, "continue": [
            {"key": "", "title": "No key"}, {"key": "1"}, {"key": "2", "title": "Ok", "progress": 7, "thumb": "/etc/passwd"}]}});
        let items = items(&s);
        assert_eq!(items.len(), 1);
        assert_eq!((items[0].progress, items[0].thumb.as_deref()), (1.0, None));
    }

    #[test]
    fn summaries() {
        assert_eq!(summary(&json!({})), "Not linked ›");
        assert_eq!(summary(&json!({"plex": {"linked": true}})), "Linked ›");
    }

    #[test]
    fn up_in_along_and_down_out() {
        let mut row = Row::default();
        assert_eq!(row.enter(), Outcome::Edge); // nothing to go into
        row.update(items(&plex(3)));
        assert_eq!(row.focused(), None);
        assert_eq!(row.enter(), Outcome::Moved);
        assert_eq!(row.focused().unwrap().key, "0");
        assert_eq!(row.arrow(Dir::Left), Outcome::Edge);
        assert_eq!(row.arrow(Dir::Up), Outcome::Edge);
        assert_eq!(row.arrow(Dir::Right), Outcome::Moved);
        assert_eq!(row.arrow(Dir::Right), Outcome::Moved);
        assert_eq!(row.arrow(Dir::Right), Outcome::Edge);
        assert_eq!(row.focused().unwrap().key, "2");
        assert_eq!(row.arrow(Dir::Down), Outcome::Moved);
        assert!(!row.inside);
        // In again: on the first, not where it was.
        row.enter();
        assert_eq!(row.focus, 0);
    }

    #[test]
    fn focus_follows_the_list() {
        let mut row = Row::default();
        row.update(items(&plex(5)));
        row.enter();
        row.arrow(Dir::Right);
        row.arrow(Dir::Right); // on "2"
        // "0" finished: the same show, one along.
        let mut s = plex(5);
        s["plex"]["continue"].as_array_mut().unwrap().remove(0);
        assert!(row.update(items(&s)));
        assert_eq!((row.focus, row.focused().unwrap().key.as_str()), (1, "2"));
        assert!(!row.update(items(&s))); // the same list again: nothing to do
        // The one in focus gone: where it was.
        s["plex"]["continue"].as_array_mut().unwrap().remove(1);
        row.update(items(&s));
        assert_eq!(row.focused().unwrap().key, "3");
        // Fewer than where it was: the last.
        row.arrow(Dir::Right);
        row.arrow(Dir::Right);
        row.update(items(&plex(2)));
        assert_eq!(row.focus, 1);
        // Nothing left (unlinked, the setting off): out to the cards.
        row.update(Vec::new());
        assert!(!row.inside && row.focused().is_none());
    }
}
