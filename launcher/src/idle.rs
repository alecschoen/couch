//! The idle clock (artboard "Screen · Idle clock"): a dark clock and date over
//! the whole launcher once couchd says it's idle (live state `idle.idle`, couchd/idle.py).
//! Whatever screen was up stays underneath, untouched, and is there again on waking.
//!
//! Any button wakes it, and that press does nothing else: couchd sends the `key` message
//! before `idle` turns false, so the clock is still up when the key gets here. A held arrow's
//! repeats are dropped too, until a fresh press.
//!
//! Under the date, the weather for the next hours (src/weather.rs), when there's some to show.
//!
//! Against burn-in the clock drifts between a few spots every DRIFT_MS; the box is kept clear
//! of the screen's edges and the footer (ui/idle.slint), the weather strip included.

use std::cell::RefCell;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};
use slint::{ComponentHandle, ModelRc, Timer, TimerMode, VecModel};

use crate::app::{App, Key};
use crate::state;
use crate::{IdleData, MainWindow, WeatherHour};

const DRIFT_MS: u64 = 3 * 60_000;
/// Where the clock's top-left corner sits, in turn: the artboard's spots ×1.5 for 1080p.
const SPOTS: [(f32, f32); 5] = [(144.0, 144.0), (780.0, 270.0), (270.0, 450.0), (900.0, 495.0), (495.0, 180.0)];

#[derive(Default)]
struct Idle {
    visible: bool,
    drop_repeats: bool,
    spot: usize,
    generation: i32,
    clock: Timer,
    drift: Timer,
    /// couchd's `weather` and the `display.weather` setting, as last heard.
    weather: Value,
}

thread_local! {
    static IDLE: RefCell<Idle> = RefCell::new(Idle::default());
}

/// couchd's state changed: up while couchd says idle and the launcher is in front.
pub fn state(app: &mut App, _patch: Option<&Value>) {
    // Kept for the clock's minutes, which move the weather strip on between patches.
    let kept = json!({
        "weather": state::at(&app.state, "weather"),
        "settings": {"display.weather": state::setting(&app.state, "display.weather")},
    });
    IDLE.with_borrow_mut(|i| i.weather = kept);
    let idle = state::bool_at(&app.state, "idle.idle") == Some(true);
    if idle && app.foreground() == "launcher" {
        show(app);
    } else {
        hide(app);
    }
    if IDLE.with_borrow(|i| i.visible) {
        let footer = footer(&app.state);
        app.ui.global::<IdleData>().set_footer(footer.into());
        IDLE.with_borrow(|i| weather(&app.ui, &i.weather));
    }
}

/// A key while the clock is up wakes it and does nothing else: true if it did (or the key is a
/// repeat of the one that woke it).
pub fn wake(app: &mut App, key: &Key) -> bool {
    if IDLE.with_borrow(|i| i.visible) {
        hide(app);
        IDLE.with_borrow_mut(|i| i.drop_repeats = true);
        return true;
    }
    IDLE.with_borrow_mut(|i| {
        if i.drop_repeats && key.repeat {
            return true;
        }
        i.drop_repeats = false;
        false
    })
}

fn show(app: &mut App) {
    let ui = app.ui.as_weak();
    IDLE.with_borrow_mut(|i| {
        if i.visible {
            return;
        }
        i.visible = true;
        i.generation += 1;
        let now_ms = SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_millis() as u64);
        i.spot = ((now_ms / DRIFT_MS) % SPOTS.len() as u64) as usize;
        let d = app.ui.global::<IdleData>();
        // Put in place, not glided there.
        d.set_drifting(false);
        place(&app.ui, i.spot);
        tick(&app.ui);
        weather(&app.ui, &i.weather);
        d.set_generation(i.generation);
        d.set_shown(true);
        let clock_ui = ui.clone();
        i.clock.start(TimerMode::Repeated, Duration::from_secs(1), move || {
            // A new minute may leave an hour behind: the strip moves on with it.
            if let Some(ui) = clock_ui.upgrade()
                && tick(&ui)
            {
                IDLE.with_borrow(|i| weather(&ui, &i.weather));
            }
        });
        i.drift.start(TimerMode::Repeated, Duration::from_millis(DRIFT_MS), move || {
            let Some(ui) = ui.upgrade() else { return };
            IDLE.with_borrow_mut(|i| {
                i.spot = (i.spot + 1) % SPOTS.len();
                ui.global::<IdleData>().set_drifting(true);
                place(&ui, i.spot);
            });
        });
    });
}

fn hide(app: &mut App) {
    IDLE.with_borrow_mut(|i| {
        if !i.visible {
            return;
        }
        i.visible = false;
        i.clock.stop();
        i.drift.stop();
        app.ui.global::<IdleData>().set_shown(false);
    });
}

fn place(ui: &MainWindow, spot: usize) {
    let (x, y) = SPOTS[spot];
    let d = ui.global::<IdleData>();
    d.set_x(x);
    d.set_y(y);
}

/// The clock, to the minute: true if it turned.
fn tick(ui: &MainWindow) -> bool {
    let now = chrono::Local::now();
    let d = ui.global::<IdleData>();
    let time = now.format("%H:%M").to_string();
    if d.get_time() == time.as_str() {
        return false;
    }
    d.set_time(time.into());
    d.set_date(now.format("%A %-d %B").to_string().into());
    true
}

/// The weather strip for now, from `s` (`weather`, and the setting): none, no strip.
fn weather(ui: &MainWindow, s: &Value) {
    let hours: Vec<WeatherHour> = crate::weather::strip(s, chrono::Local::now().timestamp())
        .into_iter()
        .map(|h| WeatherHour { label: h.label.into(), icon: h.icon.into(), temp: h.temp.into() })
        .collect();
    ui.global::<IdleData>().set_weather(ModelRc::new(VecModel::from(hours)));
}

/// "Sound: TV speakers · the TV turns off after an hour"
fn footer(s: &Value) -> String {
    let current = state::str_at(s, "audio.current");
    let output = state::list_at(s, "audio.outputs")
        .iter()
        .find(|o| current.is_some() && o.get("id").and_then(Value::as_str) == current)
        .and_then(|o| o.get("name").and_then(Value::as_str))
        .filter(|n| !n.is_empty());
    let mut parts = Vec::new();
    if let Some(output) = output {
        parts.push(format!("Sound: {output}"));
    }
    let off = state::num_at(s, "idle.tv_off_after").unwrap_or(0.0);
    if truthy(state::setting(s, "tv.off_when_idle")) && off != 0.0 {
        parts.push(format!("the TV turns off after {}", hours(off)));
    }
    parts.join(" \u{b7} ")
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|n| n != 0.0),
        Value::String(s) => !s.is_empty(),
        _ => true,
    }
}

fn hours(seconds: f64) -> String {
    let n = (seconds / 3600.0).round();
    if n >= 1.0 {
        return if n == 1.0 { "an hour".into() } else { format!("{n} hours") };
    }
    let m = (seconds / 60.0).round().max(1.0);
    if m == 1.0 { "a minute".into() } else { format!("{m} minutes") }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn footer_says_where_the_sound_goes() {
        let audio = json!({"outputs": [{"id": "tv", "name": "TV speakers"}], "current": "tv"});
        assert_eq!(footer(&json!({"audio": audio})), "Sound: TV speakers");
        let s = json!({"audio": audio, "idle": {"tv_off_after": 3600},
            "settings": {"tv.off_when_idle": true}});
        assert_eq!(footer(&s), "Sound: TV speakers \u{b7} the TV turns off after an hour");
        let s = json!({"audio": {"outputs": [], "current": null}, "idle": {"tv_off_after": 7200},
            "settings": {"tv.off_when_idle": true}});
        assert_eq!(footer(&s), "the TV turns off after 2 hours");
        let s = json!({"idle": {"tv_off_after": 3600}});
        assert_eq!(footer(&s), "");
    }

    #[test]
    fn hours_in_words() {
        assert_eq!(hours(3600.0), "an hour");
        assert_eq!(hours(5400.0), "2 hours");
        assert_eq!(hours(1200.0), "20 minutes");
        assert_eq!(hours(60.0), "a minute");
        assert_eq!(hours(5.0), "a minute");
    }
}
