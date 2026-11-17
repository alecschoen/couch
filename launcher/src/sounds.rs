//! UI sounds (#49): short, quiet sounds that confirm what the remote did.
//!
//! Which sound, for a key the launcher handled (App::on_key):
//!   move    an arrow moved focus (or a screen used it: a stepper, a slider)
//!   tick    the same, for a held arrow's repeats: lighter, and at most one per 70 ms
//!   edge    an arrow with nowhere to go
//!   select  OK
//!   back    Back or Home that went somewhere (Back on Home makes no sound: it does nothing)
//! and, without a key:
//!   toast   a message pops up (`error` for an error toast)
//!   error   an error screen goes up
//!
//! Only the launcher makes these, and only while it's in front: not over YouTube or Plex
//! (Kodi has its own, which couchd/kodi_sounds.py switches with the same setting), and not
//! for the key that wakes the idle clock. The setting `audio.ui_sounds` says where: "on" (any
//! output, the default), "tv" (only while the sound is on the TV speakers: the jack and
//! Bluetooth can lag behind) or "off".
//!
//! The synth (sounds/synth.rs) is the web's Web Audio graph in Rust; sounds/output.rs plays
//! it on its own thread through PipeWire, as Chromium did.

mod output;
mod synth;

use std::cell::{Cell, OnceCell};
use std::time::{Duration, Instant};

use crate::app::{App, Key, Outcome};
use crate::state;

const REPEAT_GAP: Duration = Duration::from_millis(70);

pub struct Sounds {
    // The audio thread, started with the first sound (None if it couldn't be).
    out: OnceCell<Option<output::Output>>,
    last_repeat: Cell<Option<Instant>>,
}

impl Sounds {
    pub fn new() -> Self {
        Sounds { out: OnceCell::new(), last_repeat: Cell::new(None) }
    }

    /// Play a sound by name, if sounds are on now. Returns at once.
    pub fn play(&self, app: &App, name: &str) {
        let Some((name, _)) = synth::sound(name) else { return };
        let mode = app.setting("audio.ui_sounds").as_str();
        if !enabled(mode, app.foreground(), state::str_at(&app.state, "audio.current")) {
            return;
        }
        if let Some(out) = self.out.get_or_init(output::Output::start) {
            out.play(name);
        }
    }

    /// The sound for a key the launcher handled: `outcome` from the screen, `moved` if
    /// focus, the screen on top or the back stack changed.
    pub fn for_key(&self, app: &App, key: &Key, outcome: Outcome, moved: bool) {
        let Some(name) = for_key(&key.key, key.repeat, outcome, moved) else { return };
        if name == "tick" && !self.repeat_due(Instant::now()) {
            return;
        }
        self.play(app, name);
    }

    /// At most one tick per REPEAT_GAP, so a held arrow doesn't buzz.
    fn repeat_due(&self, now: Instant) -> bool {
        if self.last_repeat.get().is_some_and(|t| now.duration_since(t) < REPEAT_GAP) {
            return false;
        }
        self.last_repeat.set(Some(now));
        true
    }
}

/// Would a sound play now? The setting, and the launcher being in front.
fn enabled(mode: Option<&str>, foreground: &str, output: Option<&str>) -> bool {
    match mode {
        Some("off") => false,
        _ if foreground != "launcher" => false,
        Some("tv") => output == Some("tv"),
        _ => true,
    }
}

/// Which sound a key makes (the web's UiSounds.forKey), or none.
fn for_key(key: &str, repeat: bool, outcome: Outcome, moved: bool) -> Option<&'static str> {
    // A screen that took the key used it, even if nothing visible moved.
    let taken = matches!(outcome, Outcome::Moved | Outcome::Taken);
    match key {
        "up" | "down" | "left" | "right" => {
            if !moved && !taken {
                (!repeat).then_some("edge")
            } else {
                Some(if repeat { "tick" } else { "move" })
            }
        }
        "select" => Some("select"),
        "back" | "home" => (moved || taken).then_some("back"),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use Outcome::*;

    #[test]
    fn arrows() {
        assert_eq!(for_key("left", false, Moved, true), Some("move"));
        assert_eq!(for_key("left", true, Moved, true), Some("tick"));
        // A stepper or slider that took the arrow, though focus stayed.
        assert_eq!(for_key("right", false, Taken, false), Some("move"));
        assert_eq!(for_key("up", false, Edge, false), Some("edge"));
        assert_eq!(for_key("up", false, No, false), Some("edge"));
        // A held arrow against the edge stays quiet after the first.
        assert_eq!(for_key("down", true, Edge, false), None);
        // An arrow that changed the screen (the app saw the stack change).
        assert_eq!(for_key("down", false, No, true), Some("move"));
    }

    #[test]
    fn select_back_home() {
        assert_eq!(for_key("select", false, Taken, false), Some("select"));
        assert_eq!(for_key("select", false, No, false), Some("select"));
        assert_eq!(for_key("back", false, No, true), Some("back"));
        assert_eq!(for_key("back", false, No, false), None); // Back on Home
        assert_eq!(for_key("back", false, Taken, false), Some("back")); // a screen closed a panel
        assert_eq!(for_key("home", false, No, false), None);
        assert_eq!(for_key("home", false, No, true), Some("back"));
        assert_eq!(for_key("play_pause", false, Taken, false), None);
    }

    #[test]
    fn setting_and_foreground() {
        assert!(enabled(Some("on"), "launcher", Some("bluetooth:aa")));
        assert!(enabled(None, "launcher", None)); // the default is on
        assert!(!enabled(Some("off"), "launcher", Some("tv")));
        assert!(enabled(Some("tv"), "launcher", Some("tv")));
        assert!(!enabled(Some("tv"), "launcher", Some("jack")));
        assert!(!enabled(Some("tv"), "launcher", None));
        assert!(!enabled(Some("on"), "youtube", Some("tv")));
        assert!(!enabled(Some("on"), "plex", Some("tv")));
    }

    #[test]
    fn ticks_are_spaced() {
        let s = Sounds::new();
        let t = Instant::now();
        assert!(s.repeat_due(t));
        assert!(!s.repeat_due(t + Duration::from_millis(40)));
        assert!(s.repeat_due(t + Duration::from_millis(70)));
        assert!(!s.repeat_due(t + Duration::from_millis(100)));
    }
}
