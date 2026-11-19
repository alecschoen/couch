//! Settings › Display › Apps on Home (#89): every card Home can show, each with a switch.
//! OK flips it, and the row on Home follows at once: the switches are couchd's `home.hidden`
//! setting (a list of the cards left off). Settings is listed without one: it always stays on
//! Home, as the way back here.
//!
//! Up and Down move between the cards; Back goes back to Settings.

use std::cell::Cell;
use std::rc::Rc;

use serde_json::{Value, json};
use slint::{ComponentHandle, Model, VecModel};

use crate::app::{App, Dir, Key, Outcome, Screen};
use crate::screens::home;
use crate::{HomeAppRow, HomeAppsData};

const SETTING: &str = "home.hidden";

/// The cards that can be hidden, as (id, name): all but Settings.
fn hideable() -> Vec<(&'static str, &'static str)> {
    home::all_cards().filter(|(id, _)| *id != "settings").collect()
}

/// The cards left off Home now.
fn hidden(app: &App) -> Vec<String> {
    app.setting(SETTING)
        .as_array()
        .map(|a| a.iter().filter_map(Value::as_str).map(str::to_string).collect())
        .unwrap_or_default()
}

/// What Settings › Display says about it: "All shown ›", "2 hidden ›".
pub fn summary(s: &Value) -> String {
    let n = crate::state::setting(s, SETTING).as_array().map_or(0, Vec::len);
    if n == 0 { "All shown ›".into() } else { format!("{n} hidden ›") }
}

/// The setting with `id` shown or hidden, in Home's order (as couchd keeps it).
fn toggled(hidden: &[String], id: &str) -> Vec<String> {
    let hide = !hidden.iter().any(|h| h == id);
    hideable()
        .into_iter()
        .map(|(card, _)| card)
        .filter(|&card| if card == id { hide } else { hidden.iter().any(|h| h == card) })
        .map(str::to_string)
        .collect()
}

pub struct HomeApps {
    /// The focused card, an index into `hideable()`. Shared with a refused change's reply,
    /// which draws the switch back.
    focus: Rc<Cell<usize>>,
    rows: Rc<VecModel<HomeAppRow>>,
}

/// Draw the rows into HomeAppsData.
fn draw(app: &App, focus: usize, model: &Rc<VecModel<HomeAppRow>>) {
    let hidden = hidden(app);
    let cards = hideable();
    let focused = |i: usize| i == focus;
    let mut rows: Vec<HomeAppRow> = cards
        .iter()
        .enumerate()
        .map(|(i, (id, name))| HomeAppRow {
            name: (*name).into(),
            shown: !hidden.iter().any(|h| h == id),
            focused: focused(i),
            fixed: false,
            hairline: i > 0 && !focused(i) && !focused(i - 1),
        })
        .collect();
    let last = cards.len();
    rows.push(HomeAppRow {
        name: "Settings".into(),
        shown: true,
        focused: false,
        fixed: true,
        hairline: !focused(last - 1),
    });
    if !model.iter().eq(rows.iter().cloned()) {
        // Row by row, so a flipped switch slides rather than being built again.
        if model.row_count() == rows.len() {
            for (i, row) in rows.into_iter().enumerate() {
                if model.row_data(i).as_ref() != Some(&row) {
                    model.set_row_data(i, row);
                }
            }
        } else {
            model.set_vec(rows);
        }
    }
    app.ui.global::<HomeAppsData>().set_rows(model.clone().into());
}

impl HomeApps {
    pub fn new() -> Self {
        HomeApps { focus: Rc::new(Cell::new(0)), rows: Rc::new(VecModel::default()) }
    }

    fn render(&self, app: &App) {
        draw(app, self.focus.get(), &self.rows);
    }

    /// Show the change at once; if couchd refuses it, it goes back, with an error toast.
    fn flip(&self, app: &mut App) {
        let Some(&(id, _)) = hideable().get(self.focus.get()) else { return };
        let before = hidden(app);
        let after = toggled(&before, id);
        write_hidden(app, json!(after));
        self.render(app);
        let (focus, rows) = (self.focus.clone(), self.rows.clone());
        app.command(
            "settings.set",
            json!({"key": SETTING, "value": after}),
            Some(Box::new(move |app, result| {
                if let Err(e) = result {
                    write_hidden(app, json!(before));
                    draw(app, focus.get(), &rows);
                    app.toast(&e, "error", None);
                }
            })),
        );
    }
}

/// Write the setting into the local state, as Settings does, until couchd's patch comes.
fn write_hidden(app: &mut App, value: Value) {
    if let Some(settings) = app.state.get_mut("settings").and_then(Value::as_object_mut) {
        settings.insert(SETTING.to_string(), value);
    }
}

impl Screen for HomeApps {
    fn name(&self) -> &'static str {
        "home-apps"
    }

    fn palette(&self, _app: &App) -> String {
        "settings".into()
    }

    fn show(&mut self, app: &mut App) {
        self.render(app);
    }

    fn state(&mut self, app: &mut App, _patch: Option<&Value>) {
        self.render(app);
    }

    fn key(&mut self, key: &Key, app: &mut App) -> Outcome {
        let focus = self.focus.get();
        let outcome = match (key.arrow(), key.key.as_str()) {
            (Some(Dir::Up), _) if focus > 0 => {
                self.focus.set(focus - 1);
                Outcome::Moved
            }
            (Some(Dir::Down), _) if focus + 1 < hideable().len() => {
                self.focus.set(focus + 1);
                Outcome::Moved
            }
            (Some(_), _) => Outcome::Edge,
            (None, "select") => {
                self.flip(app);
                Outcome::Taken
            }
            _ => Outcome::No,
        };
        self.render(app);
        outcome
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strings(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn settings_cant_be_hidden() {
        let ids: Vec<&str> = hideable().into_iter().map(|(id, _)| id).collect();
        assert_eq!(ids, ["youtube", "ytmusic", "plex", "audio", "bluetooth"]);
    }

    #[test]
    fn summaries() {
        assert_eq!(summary(&json!({})), "All shown ›");
        assert_eq!(summary(&json!({"settings": {"home.hidden": ["plex", "audio"]}})), "2 hidden ›");
    }

    #[test]
    fn toggling_keeps_homes_order() {
        assert_eq!(toggled(&[], "plex"), strings(&["plex"]));
        assert_eq!(toggled(&strings(&["plex"]), "youtube"), strings(&["youtube", "plex"]));
        assert_eq!(toggled(&strings(&["youtube", "plex"]), "youtube"), strings(&["plex"]));
        // Anything unknown in the list is dropped on the way back.
        assert_eq!(toggled(&strings(&["netflix"]), "audio"), strings(&["audio"]));
    }
}
