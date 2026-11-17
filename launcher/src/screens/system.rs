//! The system screens (startup, opening, error): the takeovers
//! App::update_system puts up when couchd's state asks for them.
//!
//!   startup   the Couch lockup, a busy bar and "Turning on the TV", while couchd starts its
//!             services and wakes the TV. Nothing to do: it takes every key.
//!   opening   the app's name, big, in its colour, while it loads. Back gives up (couchd stops
//!             waiting and stays on the launcher); Home does too, through couchd.
//!   error     an app couldn't open, or stopped (`apps.error`, whose `kind` picks the words).
//!             Try again launches it again; Go home (or Back) dismisses it.

use std::time::{Duration, Instant};

use serde_json::{Value, json};
use slint::{ComponentHandle, Timer, TimerMode, VecModel};

use crate::SystemData;
use crate::app::{App, Dir, Key, Outcome, Screen, with_app};
use crate::state;

const STEP_TV: &str = "Turning on the TV";
const STEP_SERVICES: &str = "Getting ready";
// Shown when couchd hasn't answered for SLOW.
const SLOW_LABEL: &str = "Taking longer than usual";
const SLOW: Duration = Duration::from_secs(30);

const WIFI: &str = "The Wi-Fi isn’t connected. Check that your router is on, then try again.";

// Where the web breaks the error screens' known lines (a title at 1350px, a body at 1140px).
// Slint's wrapped text can't be given CSS's line-height, so these are drawn a line each.
const BREAKS: [(&str, &str); 2] = [
    ("YouTube stopped unexpectedly", "YouTube stopped"),
    (
        "Make sure the computer running Plex is on and connected to the same network.",
        "Make sure the computer running Plex is on and connected to the same",
    ),
];

/// Startup's line, from couchd's `startup.step`.
pub fn startup_label(startup: &Value) -> &'static str {
    match startup.get("step").and_then(Value::as_str) {
        Some("services") => STEP_SERVICES,
        _ => STEP_TV,
    }
}

/// An app's name and palette (opening.js appLook).
pub fn app_look(app_id: &str, s: &Value) -> (String, &'static str) {
    match app_id {
        "youtube" => ("YouTube".into(), "yt"),
        "plex" => ("Plex".into(), "plex"),
        _ => {
            let listed = state::list_at(s, "apps.list")
                .iter()
                .find(|a| a.get("id").and_then(Value::as_str) == Some(app_id))
                .and_then(|a| a.get("name").and_then(Value::as_str));
            (listed.unwrap_or(app_id).to_string(), "system")
        }
    }
}

/// What an error screen says (error.js errorCopy).
#[derive(Debug, PartialEq)]
pub struct ErrorCopy {
    pub app: String,
    pub palette: &'static str,
    pub title: String,
    pub body: String,
}

pub fn error_copy(error: &Value, s: &Value) -> ErrorCopy {
    let text = |k: &str| error.get(k).and_then(Value::as_str).unwrap_or("");
    let id = text("app");
    let (name, look_palette) = app_look(id, s);
    let (title, body, palette) = match text("kind") {
        "offline" => {
            let title =
                if id == "plex" { format!("{name} needs the network") } else { format!("{name} needs the internet") };
            (title, WIFI.to_string(), "settings")
        }
        "server" => (
            "Can’t reach your Plex server".to_string(),
            "Make sure the computer running Plex is on and connected to the same network.".to_string(),
            look_palette,
        ),
        "crashed" => (
            format!("{name} stopped unexpectedly"),
            "It closed on its own. Try again to open it, or go home.".to_string(),
            look_palette,
        ),
        "failed" => (
            format!("{name} didn’t start"),
            "Something went wrong while opening it. Give it a moment, then try again.".to_string(),
            look_palette,
        ),
        "repeated" => (
            format!("{name} keeps stopping"),
            "It has closed a few times in a row. Give it a minute, then try again.".to_string(),
            look_palette,
        ),
        _ => (text("message").to_string(), String::new(), look_palette),
    };
    ErrorCopy { app: name, palette, title, body }
}

/// A text's lines, where the web breaks it if it's one we know; else the one line, which the
/// screen wraps itself.
pub fn lines(text: &str) -> Vec<String> {
    if text.is_empty() {
        return Vec::new();
    }
    match BREAKS.iter().find(|(full, _)| *full == text) {
        Some((full, first)) => vec![first.to_string(), full[first.len()..].trim_start().to_string()],
        None => vec![text.to_string()],
    }
}

/// Startup's line now: couchd's step, or while it isn't connected, a word on how long it's been.
fn startup_text(app: &App, started: Instant) -> &'static str {
    if app.connected {
        startup_label(state::at(&app.state, "startup"))
    } else if started.elapsed() >= SLOW {
        SLOW_LABEL
    } else {
        STEP_TV
    }
}

pub struct System {
    kind: &'static str,
    palette: &'static str,
    /// Opening and errors: the app.
    app_id: String,
    name: String,
    title: String,
    body: String,
    /// Errors: the focused pill, 0 Try again, 1 Go home.
    focus: usize,
    started: Instant,
    timer: Timer,
}

impl System {
    /// `kind` is "startup", "opening" or "error".
    pub fn new(kind: &'static str, params: &Value, app: &mut App) -> Self {
        let mut s = System {
            kind,
            palette: "system",
            app_id: String::new(),
            name: String::new(),
            title: String::new(),
            body: String::new(),
            focus: 0,
            started: Instant::now(),
            timer: Timer::default(),
        };
        match kind {
            "opening" => {
                s.app_id = params.get("app").and_then(Value::as_str).unwrap_or("").to_string();
                let (name, palette) = app_look(&s.app_id, &app.state);
                (s.name, s.palette) = (name, palette);
            }
            "error" => {
                let error = params.get("error").cloned().unwrap_or(Value::Null);
                s.app_id = error.get("app").and_then(Value::as_str).unwrap_or("").to_string();
                let copy = error_copy(&error, &app.state);
                (s.name, s.palette, s.title, s.body) = (copy.app, copy.palette, copy.title, copy.body);
            }
            _ => {}
        }
        s
    }

    fn go_home(app: &mut App) {
        app.command("apps.clear_error", json!({}), None);
    }

    fn error_key(&mut self, key: &Key, app: &mut App) -> Outcome {
        match (key.arrow(), key.key.as_str()) {
            (Some(Dir::Left), _) if self.focus == 1 => self.focus = 0,
            (Some(Dir::Right), _) if self.focus == 0 => self.focus = 1,
            (Some(_), _) => return Outcome::Edge,
            (None, "back") => {
                Self::go_home(app);
                return Outcome::Taken;
            }
            // couchd goes home and clears the error; nothing for focus to do.
            (None, "home") => return Outcome::Taken,
            (None, "select") => {
                if self.focus == 0 {
                    // A failure shows as the error screen again: no toast.
                    app.command("apps.launch", json!({"app": self.app_id}), None);
                } else {
                    Self::go_home(app);
                }
                return Outcome::Taken;
            }
            _ => return Outcome::No,
        }
        app.ui.global::<SystemData>().set_focus(self.focus as i32);
        Outcome::Moved
    }
}

impl Screen for System {
    fn name(&self) -> &'static str {
        self.kind
    }

    fn palette(&self, _app: &App) -> String {
        self.palette.into()
    }

    fn still(&self) -> bool {
        self.kind == "startup"
    }

    fn show(&mut self, app: &mut App) {
        let data = app.ui.global::<SystemData>();
        match self.kind {
            "startup" => {
                data.set_startup_label(startup_text(app, self.started).into());
                // It follows the connection too, which isn't a state change.
                let started = self.started;
                self.timer.start(TimerMode::Repeated, Duration::from_millis(500), move || {
                    with_app(|app| {
                        app.ui.global::<SystemData>().set_startup_label(startup_text(app, started).into());
                    });
                });
            }
            "opening" => data.set_app_name(self.name.as_str().into()),
            _ => {
                data.set_app_name(self.name.as_str().into());
                let model = |text: &str| -> slint::ModelRc<slint::SharedString> {
                    let v: Vec<slint::SharedString> = lines(text).into_iter().map(Into::into).collect();
                    std::rc::Rc::new(VecModel::from(v)).into()
                };
                data.set_title_lines(model(&self.title));
                data.set_body_lines(model(&self.body));
                self.focus = 0;
                data.set_focus(0);
            }
        }
    }

    fn destroy(&mut self, _app: &mut App) {
        self.timer.stop();
    }

    fn state(&mut self, app: &mut App, _patch: Option<&Value>) {
        if self.kind == "startup" {
            app.ui.global::<SystemData>().set_startup_label(startup_text(app, self.started).into());
        }
    }

    fn key(&mut self, key: &Key, app: &mut App) -> Outcome {
        match self.kind {
            "error" => self.error_key(key, app),
            "opening" => {
                if key.is("back") {
                    app.command("apps.home", json!({}), None);
                }
                Outcome::Taken
            }
            // Nothing to do yet; couchd moves on by itself.
            _ => Outcome::Taken,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn startup_labels() {
        assert_eq!(startup_label(&Value::Null), "Turning on the TV");
        assert_eq!(startup_label(&json!({"step": "tv"})), "Turning on the TV");
        assert_eq!(startup_label(&json!({"step": "services"})), "Getting ready");
    }

    #[test]
    fn looks() {
        let s = json!({"apps": {"list": [{"id": "kodi2", "name": "Films"}]}});
        assert_eq!(app_look("plex", &s), ("Plex".into(), "plex"));
        assert_eq!(app_look("kodi2", &s), ("Films".into(), "system"));
        assert_eq!(app_look("nope", &s), ("nope".into(), "system"));
    }

    #[test]
    fn error_words() {
        let s = json!({});
        let c = error_copy(&json!({"app": "plex", "kind": "offline", "message": "x"}), &s);
        assert_eq!((c.title.as_str(), c.palette), ("Plex needs the network", "settings"));
        let c = error_copy(&json!({"app": "youtube", "kind": "offline"}), &s);
        assert_eq!(c.title, "YouTube needs the internet");
        let c = error_copy(&json!({"app": "plex", "kind": "crashed"}), &s);
        assert_eq!((c.app.as_str(), c.palette, c.title.as_str()), ("Plex", "plex", "Plex stopped unexpectedly"));
        let c = error_copy(&json!({"app": "youtube", "kind": "weird", "message": "Oh no"}), &s);
        assert_eq!((c.title.as_str(), c.body.as_str(), c.palette), ("Oh no", "", "yt"));
    }

    #[test]
    fn known_lines_break_where_the_web_does() {
        assert_eq!(lines("YouTube stopped unexpectedly"), ["YouTube stopped", "unexpectedly"]);
        assert_eq!(lines("Plex stopped unexpectedly"), ["Plex stopped unexpectedly"]);
        assert!(lines("").is_empty());
    }
}
