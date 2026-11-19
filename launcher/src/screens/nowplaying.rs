//! Now Playing (#80): what a phone is playing to Couch over Spotify Connect or AirPlay
//! (couchd's `speaker` section, docs/protocol.md). The cover, where it comes from, the title,
//! artist and album, a bar that moves on by itself while it plays (from `position_ms` at
//! `position_at`), and a pill for each thing the source can be asked to do: Previous,
//! Play/Pause and Next for AirPlay, then "Stop playing on Couch" (`speaker.disconnect`). Spotify
//! only has the last: librespot takes no commands.
//!
//! It comes up by itself when music starts (`follow`, with `speaker.take_over` on: couchd has
//! woken the TV and brought the launcher to the front), straight above Home, so Back goes
//! Home and the music plays on; OK on Home's Audio card comes back to it. It goes when the
//! music does (`speaker.source` null).
//!
//! Keys: Left and Right along the pills, OK on one. The remote's media keys are couchd's
//! (couchd/speaker.py), not taken here, or a Play/Pause would reach the phone twice.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::Duration;

use serde_json::{Value, json};
use slint::{ComponentHandle, Image, Timer, TimerMode, VecModel};

use crate::app::{App, Dir, Key, Outcome, Screen, with_app};
use crate::screens::home;
use crate::state;
use crate::{ControlPill, NowPlayingData};

pub const NAME: &str = "nowplaying";
const STOP: &str = "Stop playing on Couch";
// Often enough that the times change within a quarter second of the real one; the screen is
// only drawn again when they do.
const TICK: Duration = Duration::from_millis(250);

thread_local! {
    // The last cover asked for: its URL, and the picture once it's loaded (None while it
    // loads, or if it couldn't be). Shown again at once when the screen comes back.
    static COVER: RefCell<Option<(String, Option<Image>)>> = const { RefCell::new(None) };
}

/// What the router does about Now Playing after couchd's state changed.
#[derive(Debug, PartialEq, Eq)]
pub enum Step {
    Open,
    Close,
    Stay,
}

/// `shown` is the source Now Playing last came up for. It comes up when a source starts
/// playing that it hasn't come up for (not again when the same one resumes after Back), with
/// `speaker.take_over` on, and goes when nothing is on `source`.
pub fn step(shown: &mut Option<String>, s: &Value) -> Step {
    let Some(source) = state::str_at(s, "speaker.source") else {
        *shown = None;
        return Step::Close;
    };
    if state::bool_at(s, "speaker.playing") != Some(true) || shown.as_deref() == Some(source) {
        return Step::Stay;
    }
    *shown = Some(source.to_string());
    if state::setting(s, "speaker.take_over").as_bool() == Some(true) { Step::Open } else { Step::Stay }
}

/// After each state change (App::on_message): bring Now Playing up, above Home, or take it
/// away. Nothing goes above it, so if it's anywhere in the stack, it's on top.
pub fn follow(app: &mut App) {
    let mut shown = app.music_shown.take();
    let step = step(&mut shown, &app.state);
    app.music_shown = shown;
    let here = app.stack_top() == NAME;
    match step {
        Step::Open if !here => {
            app.home();
            app.go(NAME, json!({}));
        }
        Step::Close if here => app.back(),
        _ => {}
    }
}

fn text<'a>(sp: &'a Value, key: &str) -> Option<&'a str> {
    sp.get(key).and_then(Value::as_str).filter(|t| !t.is_empty())
}

fn playing(sp: &Value) -> bool {
    sp.get("playing").and_then(Value::as_bool) == Some(true)
}

fn can(sp: &Value, control: &str) -> bool {
    state::list_at(sp, "controls").iter().any(|c| c.as_str() == Some(control))
}

/// Above the title: where it comes from, and whether it's paused.
pub fn kicker(sp: &Value) -> String {
    let playing = playing(sp);
    let on_couch = |name: &str| format!("{name} · {} on Couch", if playing { "playing" } else { "paused" });
    match (sp.get("source").and_then(Value::as_str), text(sp, "device")) {
        (Some("airplay"), Some(device)) if playing => format!("AirPlay · from {device}"),
        (Some("airplay"), Some(device)) => format!("AirPlay · from {device} · paused"),
        (Some("airplay"), None) => on_couch("AirPlay"),
        (Some("spotify"), _) => on_couch("Spotify"),
        _ => String::new(),
    }
}

/// Where it's got to, in ms, at `now_ms` (Unix time): `position_ms` was the position at
/// `position_at`; while it plays it moves on from there, up to the duration.
pub fn position(sp: &Value, now_ms: f64) -> Option<f64> {
    let mut at = sp.get("position_ms")?.as_f64()?;
    if playing(sp)
        && let Some(since) = sp.get("position_at").and_then(Value::as_f64)
    {
        at += (now_ms - since).max(0.0);
    }
    if let Some(duration) = sp.get("duration_ms").and_then(Value::as_f64) {
        at = at.min(duration);
    }
    Some(at.max(0.0))
}

/// "1:42", or "1:02:03" past an hour.
pub fn time_label(ms: f64) -> String {
    let secs = (ms / 1000.0).floor().max(0.0) as u64;
    let (h, m, s) = (secs / 3600, secs / 60 % 60, secs % 60);
    if h > 0 { format!("{h}:{m:02}:{s:02}") } else { format!("{m}:{s:02}") }
}

/// A pill: `label` "" for an icon on its own.
#[derive(Clone, Debug, PartialEq)]
pub struct Control {
    pub id: &'static str,
    pub label: &'static str,
    pub icon: &'static str,
}

/// The pills for what the source can do now (`controls`).
pub fn controls(sp: &Value) -> Vec<Control> {
    let playing = playing(sp);
    let mut pills = Vec::new();
    if can(sp, "previous") {
        pills.push(Control { id: "previous", label: "", icon: "previous" });
    }
    if can(sp, "play_pause") || can(sp, "play") || can(sp, "pause") {
        let (label, icon) = if playing { ("Pause", "pause") } else { ("Play", "play") };
        pills.push(Control { id: "play_pause", label, icon });
    }
    if can(sp, "next") {
        pills.push(Control { id: "next", label: "", icon: "next" });
    }
    if can(sp, "disconnect") {
        pills.push(Control { id: "disconnect", label: STOP, icon: "" });
    }
    pills
}

/// The command a pill sends.
pub fn command(id: &str, sp: &Value) -> Option<&'static str> {
    match id {
        "previous" => Some("speaker.previous"),
        "next" => Some("speaker.next"),
        "disconnect" => Some("speaker.disconnect"),
        "play_pause" if can(sp, "play_pause") => Some("speaker.play_pause"),
        "play_pause" if playing(sp) && can(sp, "pause") => Some("speaker.pause"),
        "play_pause" if !playing(sp) && can(sp, "play") => Some("speaker.play"),
        _ => None,
    }
}

/// Where focus is: on the pill picked last if it's still there, else Play/Pause, else the
/// first. (AirPlay starts with only Stop, and gets the rest a moment later.)
pub fn focus_of(pills: &[Control], chosen: Option<&str>) -> usize {
    let at = |id: &str| pills.iter().position(|c| c.id == id);
    chosen.and_then(at).or_else(|| at("play_pause")).unwrap_or(0)
}

/// The clock and how far it's got: every TICK, and after each change.
fn tick(app: &App) {
    let d = app.ui.global::<NowPlayingData>();
    let now = chrono::Local::now();
    let time = now.format("%H:%M").to_string();
    if d.get_time() != time.as_str() {
        d.set_time(time.into());
    }
    let sp = state::at(&app.state, "speaker");
    let duration = sp.get("duration_ms").and_then(Value::as_f64).filter(|&d| d > 0.0);
    let progress = duration.zip(position(sp, now.timestamp_millis() as f64));
    d.set_has_progress(progress.is_some());
    let Some((duration, at)) = progress else { return };
    let elapsed = time_label(at);
    let total = time_label(duration);
    if d.get_elapsed() != elapsed.as_str() || d.get_total() != total.as_str() {
        d.set_elapsed(elapsed.into());
        d.set_total(total.into());
        d.set_progress((at / duration) as f32);
    }
}

/// The cover at `speaker.cover`, from the cache or fetched; the note until it's there.
fn draw_cover(app: &App) {
    let d = app.ui.global::<NowPlayingData>();
    let Some(url) = state::str_at(&app.state, "speaker.cover") else {
        d.set_has_cover(false);
        return;
    };
    let cached = COVER.with_borrow(|c| c.as_ref().filter(|(u, _)| u == url).map(|(_, image)| image.clone()));
    match cached {
        Some(Some(image)) => {
            d.set_cover(image);
            d.set_has_cover(true);
        }
        Some(None) => d.set_has_cover(false), // on its way, or it couldn't be had
        None => {
            d.set_has_cover(false);
            let url = url.to_string();
            COVER.set(Some((url.clone(), None)));
            app.fetch(&url.clone(), move |app, result| {
                let image = result.and_then(|bytes| {
                    Image::load_from_data(&bytes, None).map_err(|_| "not a picture it knows".to_string())
                });
                let image = image.map_err(|e| eprintln!("couch-launcher: no cover art: {e}")).ok();
                COVER.with_borrow_mut(|c| {
                    if c.as_ref().is_some_and(|(u, _)| *u == url) {
                        *c = Some((url.clone(), image.clone()));
                    }
                });
                if let Some(image) = image
                    && state::str_at(&app.state, "speaker.cover") == Some(url.as_str())
                {
                    let d = app.ui.global::<NowPlayingData>();
                    d.set_cover(image);
                    d.set_has_cover(true);
                }
            });
        }
    }
}

pub struct NowPlaying {
    pills: Vec<Control>,
    model: Rc<VecModel<ControlPill>>,
    focus: usize,
    /// The pill focus was moved to: it stays there as pills come and go.
    chosen: Option<&'static str>,
    /// The cover URL drawn (or being fetched).
    cover: Option<String>,
    timer: Timer,
}

impl NowPlaying {
    pub fn new() -> Self {
        NowPlaying {
            pills: Vec::new(),
            model: Rc::new(VecModel::default()),
            focus: 0,
            chosen: None,
            cover: None,
            timer: Timer::default(),
        }
    }

    fn render(&mut self, app: &App) {
        let s = &app.state;
        let sp = state::at(s, "speaker");
        let d = app.ui.global::<NowPlayingData>();
        d.set_sound(format!("Sound: {}", home::sound_label(s)).into());
        d.set_kicker(kicker(sp).into());
        d.set_playing(playing(sp));
        // Title and the rest can be missing for a moment (AirPlay sends them just after).
        d.set_title(text(sp, "title").unwrap_or("Music").into());
        d.set_artist(text(sp, "artist").unwrap_or("").into());
        d.set_album(text(sp, "album").unwrap_or("").into());

        let pills = controls(sp);
        if pills != self.pills {
            let rows: Vec<ControlPill> = pills
                .iter()
                .map(|c| ControlPill { id: c.id.into(), label: c.label.into(), icon: c.icon.into() })
                .collect();
            self.pills = pills;
            self.model.set_vec(rows);
        }
        self.focus = focus_of(&self.pills, self.chosen);
        d.set_focus(self.focus as i32);

        let cover = state::str_at(s, "speaker.cover").map(str::to_string);
        if cover != self.cover {
            self.cover = cover;
            draw_cover(app);
        }
        tick(app);
    }
}

impl Screen for NowPlaying {
    fn name(&self) -> &'static str {
        NAME
    }

    fn palette(&self, _app: &App) -> String {
        "audio".into()
    }

    fn show(&mut self, app: &mut App) {
        app.ui.global::<NowPlayingData>().set_pills(self.model.clone().into());
        self.cover = None;
        draw_cover(app);
        self.cover = state::str_at(&app.state, "speaker.cover").map(str::to_string);
        self.render(app);
        self.timer.start(TimerMode::Repeated, TICK, || {
            with_app(|app| tick(app));
        });
    }

    fn hide(&mut self, _app: &mut App) {
        self.timer.stop();
    }

    fn state(&mut self, app: &mut App, patch: Option<&Value>) {
        let touched =
            patch.and_then(Value::as_object).is_none_or(|p| p.contains_key("speaker") || p.contains_key("audio"));
        if touched {
            self.render(app);
        }
    }

    fn key(&mut self, key: &Key, app: &mut App) -> Outcome {
        let outcome = match (key.arrow(), key.key.as_str()) {
            (Some(Dir::Left), _) if self.focus > 0 => {
                self.focus -= 1;
                Outcome::Moved
            }
            (Some(Dir::Right), _) if self.focus + 1 < self.pills.len() => {
                self.focus += 1;
                Outcome::Moved
            }
            (Some(_), _) => Outcome::Edge,
            (None, "select") => {
                let sp = state::at(&app.state, "speaker");
                if let Some(name) = self.pills.get(self.focus).and_then(|c| command(c.id, sp)) {
                    app.command_or_toast(name, json!({}));
                }
                Outcome::Taken
            }
            _ => Outcome::No,
        };
        if outcome == Outcome::Moved {
            self.chosen = self.pills.get(self.focus).map(|c| c.id);
            app.ui.global::<NowPlayingData>().set_focus(self.focus as i32);
        }
        outcome
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ids(sp: &Value) -> Vec<&'static str> {
        controls(sp).iter().map(|c| c.id).collect()
    }

    #[test]
    fn pills_follow_the_controls() {
        let airplay = json!({"source": "airplay", "playing": true,
            "controls": ["play_pause", "play", "pause", "next", "previous", "disconnect"]});
        assert_eq!(ids(&airplay), ["previous", "play_pause", "next", "disconnect"]);
        assert_eq!(controls(&airplay)[1].label, "Pause");
        let spotify = json!({"source": "spotify", "playing": true, "controls": ["disconnect"]});
        assert_eq!(controls(&spotify), [Control { id: "disconnect", label: STOP, icon: "" }]);
        assert!(ids(&json!({})).is_empty());

        let paused = json!({"source": "airplay", "playing": false, "controls": ["play", "pause", "disconnect"]});
        assert_eq!(controls(&paused)[0], Control { id: "play_pause", label: "Play", icon: "play" });
        assert_eq!(command("play_pause", &paused), Some("speaker.play"));
        assert_eq!(command("play_pause", &json!({"playing": true, "controls": ["pause"]})), Some("speaker.pause"));
        assert_eq!(command("play_pause", &airplay), Some("speaker.play_pause"));
        assert_eq!(command("play_pause", &spotify), None);
        assert_eq!(command("disconnect", &spotify), Some("speaker.disconnect"));
    }

    #[test]
    fn focus_stays_where_it_was_put() {
        let only_stop = controls(&json!({"controls": ["disconnect"]}));
        let all = controls(&json!({"controls": ["play_pause", "next", "previous", "disconnect"]}));
        assert_eq!(focus_of(&only_stop, None), 0);
        assert_eq!(focus_of(&all, None), 1);
        assert_eq!(focus_of(&all, Some("next")), 2);
        assert_eq!(focus_of(&only_stop, Some("next")), 0);
    }

    #[test]
    fn position_moves_on_while_playing() {
        let sp = json!({"playing": true, "position_ms": 60_000, "position_at": 1_000_000, "duration_ms": 245_000});
        assert_eq!(position(&sp, 1_042_500.0), Some(102_500.0));
        assert_eq!(position(&sp, 2_000_000.0), Some(245_000.0)); // not past the end
        assert_eq!(position(&sp, 900_000.0), Some(60_000.0)); // a clock behind couchd's
        let paused = json!({"playing": false, "position_ms": 60_000, "position_at": 1_000_000});
        assert_eq!(position(&paused, 1_042_500.0), Some(60_000.0));
        assert_eq!(position(&json!({"playing": true, "position_ms": null}), 0.0), None);
        assert_eq!(time_label(102_500.0), "1:42");
        assert_eq!(time_label(245_000.0), "4:05");
        assert_eq!(time_label(3_723_000.0), "1:02:03");
        assert_eq!(time_label(0.0), "0:00");
    }

    #[test]
    fn kickers() {
        let k = |v: Value| kicker(&v);
        assert_eq!(k(json!({"source": "spotify", "playing": true, "device": "Pixel"})), "Spotify · playing on Couch");
        assert_eq!(k(json!({"source": "spotify", "playing": false})), "Spotify · paused on Couch");
        assert_eq!(
            k(json!({"source": "airplay", "playing": true, "device": "Alec’s iPhone"})),
            "AirPlay · from Alec’s iPhone"
        );
        assert_eq!(k(json!({"source": "airplay", "playing": false, "device": "Mac"})), "AirPlay · from Mac · paused");
        assert_eq!(k(json!({"source": "airplay", "playing": true, "device": null})), "AirPlay · playing on Couch");
        assert_eq!(k(json!({"source": null})), "");
    }

    #[test]
    fn comes_up_when_music_starts() {
        let mut shown = None;
        let playing = |source: &str, on: bool| json!({"speaker": {"source": source, "playing": on}});
        // Paused at first (an AirPlay phone that's connected): not yet.
        assert_eq!(step(&mut shown, &playing("airplay", false)), Step::Stay);
        assert_eq!(step(&mut shown, &playing("airplay", true)), Step::Open);
        // Back, then a pause and play again: it stays away.
        assert_eq!(step(&mut shown, &playing("airplay", false)), Step::Stay);
        assert_eq!(step(&mut shown, &playing("airplay", true)), Step::Stay);
        // Another phone starts: again.
        assert_eq!(step(&mut shown, &playing("spotify", true)), Step::Open);
        // The music stops: it goes, and the next time it comes up again.
        assert_eq!(step(&mut shown, &json!({"speaker": {"source": null}})), Step::Close);
        assert_eq!(step(&mut shown, &playing("spotify", true)), Step::Open);
        // With take-over off it doesn't, and not later either.
        let mut shown = None;
        let mut s = playing("airplay", true);
        s["settings"] = json!({"speaker.take_over": false});
        assert_eq!(step(&mut shown, &s), Step::Stay);
        s["settings"] = json!({});
        assert_eq!(step(&mut shown, &s), Step::Stay);
    }
}
