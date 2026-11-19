//! Home: the B3 colour flood. Clock and date top left, the current
//! sound output top right, a headline for the current card, and the card row. The screen
//! floods with the colour of the card in focus. On Audio, the blurb gives way to a pill per
//! output; picking one moves the sound there (couchd toasts "Sound now on X"). While a phone
//! plays to Couch (#80), the top right says what's playing instead of the sound output, and
//! OK on Audio goes back to Now Playing (screens/nowplaying.rs).
//!
//! Keys, on top of the arrows and OK:
//!   OK or Up on Audio    into the output pills, on the current output (OK: Now Playing,
//!                        while something plays to Couch)
//!   Down or Back         from the pills, back to the Audio card

use std::rc::Rc;
use std::time::Duration;

use serde_json::{Value, json};
use slint::{ComponentHandle, Model, Timer, TimerMode, VecModel};

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

const CARDS: [Card; 5] = [
    Card {
        id: "youtube",
        name: "YouTube",
        palette: "yt",
        app: Some("youtube"),
        route: None,
        headline: None,
        blurb: "Subscriptions, search and your watch history",
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
const AUDIO: usize = 2;

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

/// The top right: what a phone is playing to Couch, while one is; else the sound output.
pub fn top_line(s: &Value) -> String {
    const LONGEST: usize = 40;
    if state::str_at(s, "speaker.source").is_none() {
        return format!("Sound: {}", sound_label(s));
    }
    let what = if state::bool_at(s, "speaker.playing") == Some(true) { "Now playing" } else { "Paused" };
    match state::str_at(s, "speaker.title").filter(|t| !t.is_empty()) {
        Some(t) if t.chars().count() > LONGEST => {
            // Shortened at a word, unless that's most of it.
            let cut: String = t.chars().take(LONGEST - 1).collect();
            let cut = match cut.rfind(' ') {
                Some(i) if i > LONGEST / 2 => &cut[..i],
                _ => &cut,
            };
            format!("{what}: {}…", cut.trim_end())
        }
        Some(t) => format!("{what}: {t}"),
        None => format!("{what} on Couch"),
    }
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
            current: 0,
            in_outputs: false,
            output_focus: 0,
            outputs: Vec::new(),
            model: Rc::new(VecModel::default()),
            clock: Timer::default(),
        }
    }

    fn current_output(&self, app: &App) -> Option<usize> {
        let current = state::str_at(&app.state, "audio.current")?;
        self.outputs.iter().position(|o| o.id == current)
    }

    fn render(&mut self, app: &App) {
        let data = app.ui.global::<HomeData>();
        data.set_sound(top_line(&app.state).into());

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

        let card = &CARDS[self.current];
        let on_audio = self.current == AUDIO;
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
        let card = &CARDS[self.current];
        if let Some(id) = card.app {
            app.command_or_toast("apps.launch", json!({"app": id}));
            Outcome::Taken
        } else if let Some(route) = card.route {
            app.go(route, json!({}));
            Outcome::Taken
        } else if state::str_at(&app.state, "speaker.source").is_some() {
            app.go(crate::screens::nowplaying::NAME, json!({}));
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
        CARDS[self.current].palette.into()
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
                (Some(Dir::Right), _) if self.current + 1 < CARDS.len() => {
                    self.current += 1;
                    Outcome::Moved
                }
                (Some(Dir::Up), _) if self.current == AUDIO && !self.outputs.is_empty() => self.focus_outputs(app),
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

    #[test]
    fn top_line_says_whats_playing() {
        let mut s = json!({"audio": {"outputs": [{"id": "tv", "name": "TV speakers"}], "current": "tv"},
                           "speaker": {"source": null}});
        assert_eq!(top_line(&s), "Sound: TV speakers");
        s["speaker"] = json!({"source": "spotify", "playing": true, "title": "Slow Lights"});
        assert_eq!(top_line(&s), "Now playing: Slow Lights");
        s["speaker"] = json!({"source": "airplay", "playing": false, "title": null});
        assert_eq!(top_line(&s), "Paused on Couch");
        s["speaker"] =
            json!({"source": "airplay", "playing": true, "title": "A very long title that goes on and on and on"});
        assert_eq!(top_line(&s), "Now playing: A very long title that goes on and on…");
    }
}
