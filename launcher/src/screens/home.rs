//! Home: the B3 colour flood. Clock and date top left, the current
//! sound output top right, a headline for the current card, and the card row. The screen
//! floods with the colour of the card in focus. On Audio, the blurb gives way to a pill per
//! output; picking one moves the sound there (couchd toasts "Sound now on X").
//!
//! Keys, on top of the arrows and OK:
//!   OK or Up on Audio    into the output pills, on the current output
//!   Down or Back         from the pills, back to the Audio card

use std::rc::Rc;
use std::time::Duration;

use serde_json::{Value, json};
use slint::{ComponentHandle, Model, SharedString, Timer, TimerMode, VecModel};

use crate::app::{App, Dir, Key, Outcome, Screen};
use crate::state;
use crate::{HomeData, OutputPill};

struct Card {
    id: &'static str,
    name: &'static str,
    palette: &'static str,
    app: Option<&'static str>,
    route: Option<&'static str>,
    headline: Option<&'static str>,
    blurb: &'static str,
}

const CARDS: [Card; 6] = [
    Card {
        id: "youtube",
        name: "YouTube",
        palette: "yt",
        app: Some("youtube"),
        route: None,
        headline: None,
        blurb: "Subscriptions, search and your watch history",
    },
    // In YouTube's red too: it's YouTube's, and a sixth colour would crowd the palette.
    Card {
        id: "ytmusic",
        name: "YouTube Music",
        palette: "yt",
        app: Some("ytmusic"),
        route: None,
        headline: None,
        blurb: "Your mixes, playlists and search",
    },
    Card {
        id: "plex",
        name: "Plex",
        palette: "plex",
        app: Some("plex"),
        route: None,
        headline: None,
        blurb: "Your movies and shows",
    },
    Card {
        id: "audio",
        name: "Audio",
        palette: "audio",
        app: None,
        route: None,
        headline: Some("Play sound through"),
        blurb: "",
    },
    Card {
        id: "bluetooth",
        name: "Bluetooth",
        palette: "bt",
        app: None,
        route: Some("bluetooth"),
        headline: None,
        blurb: "",
    },
    Card {
        id: "settings",
        name: "Settings",
        palette: "settings",
        app: None,
        route: Some("settings"),
        headline: None,
        blurb: "Display, updates and power",
    },
];

pub fn sound_label(s: &Value) -> String {
    let Some(outputs) = state::at(s, "audio.outputs").as_array() else {
        return "…".into(); // not heard from couchd yet
    };
    let current = state::str_at(s, "audio.current");
    outputs
        .iter()
        .find(|o| o.get("id").and_then(Value::as_str) == current && current.is_some())
        .and_then(|o| o.get("name").and_then(Value::as_str))
        .unwrap_or("Off")
        .to_string()
}

pub fn bluetooth_blurb(s: &Value) -> String {
    let connected: Vec<&str> = state::list_at(s, "bluetooth.devices")
        .iter()
        .filter(|d| d.get("connected").and_then(Value::as_bool) == Some(true))
        .filter_map(|d| d.get("name").and_then(Value::as_str))
        .collect();
    let what = if connected.is_empty() {
        "Nothing connected".to_string()
    } else {
        format!("{} connected", connected.join(" and "))
    };
    format!("{what} · OK to pair something new")
}

struct Output {
    id: String,
    name: String,
    available: bool,
}

pub struct Home {
    /// The cards in the row, as indices into CARDS, and the current one (into `cards`).
    cards: Vec<usize>,
    labels: Rc<VecModel<SharedString>>,
    current: usize,
    in_outputs: bool,
    output_focus: usize,
    outputs: Vec<Output>,
    model: Rc<VecModel<OutputPill>>,
    clock: Timer,
}

impl Home {
    pub fn new() -> Self {
        Home {
            cards: (0..CARDS.len()).collect(),
            labels: Rc::new(VecModel::default()),
            current: 0,
            in_outputs: false,
            output_focus: 0,
            outputs: Vec::new(),
            model: Rc::new(VecModel::default()),
            clock: Timer::default(),
        }
    }

    fn card(&self) -> &'static Card {
        &CARDS[self.cards[self.current]]
    }

    fn on_audio(&self) -> bool {
        self.card().id == "audio"
    }

    fn current_output(&self, app: &App) -> Option<usize> {
        let current = state::str_at(&app.state, "audio.current")?;
        self.outputs.iter().position(|o| o.id == current)
    }

    fn render(&mut self, app: &App) {
        let data = app.ui.global::<HomeData>();
        data.set_sound(format!("Sound: {}", sound_label(&app.state)).into());

        // The output pills: rebuilt only when the outputs or the current one change.
        let list = state::list_at(&app.state, "audio.outputs");
        let outputs: Vec<Output> = list
            .iter()
            .map(|o| Output {
                id: o.get("id").and_then(Value::as_str).unwrap_or("").into(),
                name: o.get("name").and_then(Value::as_str).unwrap_or("").into(),
                available: o.get("available").and_then(Value::as_bool).unwrap_or(true),
            })
            .collect();
        let current = state::str_at(&app.state, "audio.current").unwrap_or("");
        let pills: Vec<OutputPill> = outputs
            .iter()
            .map(|o| OutputPill {
                id: o.id.as_str().into(),
                name: o.name.as_str().into(),
                selected: o.id == current,
                available: o.available,
            })
            .collect();
        let same = self.model.iter().eq(pills.iter().cloned());
        if !same {
            // Keep focus on the same output if it's still there.
            let focused_id = self.outputs.get(self.output_focus).map(|o| o.id.clone());
            self.outputs = outputs;
            self.model.set_vec(pills);
            self.output_focus = focused_id.and_then(|id| self.outputs.iter().position(|o| o.id == id)).unwrap_or(0);
            if self.outputs.is_empty() {
                self.in_outputs = false;
            }
        }
        data.set_outputs(self.model.clone().into());

        let labels: Vec<SharedString> = self.cards.iter().map(|&i| CARDS[i].name.into()).collect();
        if !self.labels.iter().eq(labels.iter().cloned()) {
            self.labels.set_vec(labels);
        }
        data.set_labels(self.labels.clone().into());

        let card = self.card();
        let on_audio = self.on_audio();
        data.set_current(self.current as i32);
        data.set_headline(card.headline.unwrap_or(card.name).into());
        let blurb = match card.id {
            "bluetooth" => bluetooth_blurb(&app.state),
            "audio" if self.outputs.is_empty() => "No sound outputs right now".into(),
            _ => card.blurb.into(),
        };
        data.set_blurb(blurb.into());
        data.set_show_outputs(on_audio && !self.outputs.is_empty());
        data.set_in_outputs(self.in_outputs);
        data.set_output_focus(self.output_focus as i32);
    }

    fn focus_outputs(&mut self, app: &App) -> Outcome {
        if self.outputs.is_empty() {
            return Outcome::Edge;
        }
        self.in_outputs = true;
        self.output_focus = self
            .current_output(app)
            .filter(|&i| self.outputs[i].available)
            .unwrap_or_else(|| self.outputs.iter().position(|o| o.available).unwrap_or(0));
        Outcome::Moved
    }

    fn choose(&mut self, app: &mut App) -> Outcome {
        let card = self.card();
        if let Some(id) = card.app {
            app.command_or_toast("apps.launch", json!({"app": id}));
            Outcome::Taken
        } else if let Some(route) = card.route {
            app.go(route, json!({}));
            Outcome::Taken
        } else {
            self.focus_outputs(app)
        }
    }
}

fn tick_clock(ui: &crate::MainWindow) {
    let now = chrono::Local::now();
    let data = ui.global::<HomeData>();
    let time = now.format("%H:%M").to_string();
    if data.get_time() != time.as_str() {
        data.set_time(time.into());
        data.set_date(now.format("%A %-d %B").to_string().into());
    }
}

impl Screen for Home {
    fn name(&self) -> &'static str {
        "home"
    }

    fn palette(&self, _app: &App) -> String {
        self.card().palette.into()
    }

    fn show(&mut self, app: &mut App) {
        self.render(app);
        tick_clock(&app.ui);
        let ui = app.ui.as_weak();
        self.clock.start(TimerMode::Repeated, Duration::from_secs(1), move || {
            if let Some(ui) = ui.upgrade() {
                tick_clock(&ui);
            }
        });
    }

    fn destroy(&mut self, _app: &mut App) {
        self.clock.stop();
    }

    fn state(&mut self, app: &mut App, _patch: Option<&Value>) {
        self.render(app);
    }

    fn key(&mut self, key: &Key, app: &mut App) -> Outcome {
        let outcome = if self.in_outputs {
            match (key.arrow(), key.key.as_str()) {
                (Some(Dir::Down), _) | (None, "back") => {
                    self.in_outputs = false;
                    Outcome::Moved
                }
                (Some(Dir::Left), _) => match (0..self.output_focus).rev().find(|&i| self.outputs[i].available) {
                    Some(i) => {
                        self.output_focus = i;
                        Outcome::Moved
                    }
                    None => Outcome::Edge,
                },
                (Some(Dir::Right), _) => {
                    match (self.output_focus + 1..self.outputs.len()).find(|&i| self.outputs[i].available) {
                        Some(i) => {
                            self.output_focus = i;
                            Outcome::Moved
                        }
                        None => Outcome::Edge,
                    }
                }
                (Some(Dir::Up), _) => Outcome::Edge,
                (None, "select") => {
                    if let Some(o) = self.outputs.get(self.output_focus)
                        && o.available
                        && Some(o.id.as_str()) != state::str_at(&app.state, "audio.current")
                    {
                        app.command_or_toast("audio.set_output", json!({"output": o.id}));
                    }
                    Outcome::Taken
                }
                _ => Outcome::No,
            }
        } else {
            match (key.arrow(), key.key.as_str()) {
                (Some(Dir::Left), _) if self.current > 0 => {
                    self.current -= 1;
                    Outcome::Moved
                }
                (Some(Dir::Right), _) if self.current + 1 < self.cards.len() => {
                    self.current += 1;
                    Outcome::Moved
                }
                (Some(Dir::Up), _) if self.on_audio() && !self.outputs.is_empty() => self.focus_outputs(app),
                (Some(_), _) => Outcome::Edge,
                (None, "select") => self.choose(app),
                _ => Outcome::No,
            }
        };
        if outcome == Outcome::Moved || outcome == Outcome::Taken {
            self.render(app);
            app.repaint();
        }
        outcome
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn labels() {
        assert_eq!(sound_label(&json!({})), "…");
        let s = json!({"audio": {"outputs": [{"id": "tv", "name": "TV speakers"}], "current": "tv"}});
        assert_eq!(sound_label(&s), "TV speakers");
        let s = json!({"audio": {"outputs": [{"id": "tv", "name": "TV speakers"}], "current": null}});
        assert_eq!(sound_label(&s), "Off");
        let s = json!({"bluetooth": {"devices": [{"name": "JBL", "connected": true}, {"name": "Old", "connected": false}]}});
        assert_eq!(bluetooth_blurb(&s), "JBL connected · OK to pair something new");
        assert_eq!(bluetooth_blurb(&json!({})), "Nothing connected · OK to pair something new");
    }
}
