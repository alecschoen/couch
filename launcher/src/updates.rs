//! Updates (#24): the "Updated to vX" toast, the next time the launcher is
//! on the TV.
//!
//! couchd keeps live state `updates.announce` ({id, kind, version, kept}) until the UI says it
//! was shown (`updates.announced {id}`), so a toast for an update installed at 3 am, with the
//! TV off, waits for the morning.

use std::cell::RefCell;
use std::time::Duration;

use serde_json::{Value, json};
use slint::{Timer, TimerMode};

use crate::app::{App, with_app};
use crate::state;

/// Give the screen a moment to settle (Startup → Home) before the toast comes in.
const DELAY: Duration = Duration::from_millis(2000);

#[derive(Default)]
struct Updates {
    timer: Timer,
    waiting: bool,
    /// The id last shown, so a slow couchd doesn't get a second toast.
    shown: Option<String>,
}

thread_local! {
    static UPDATES: RefCell<Updates> = RefCell::new(Updates::default());
}

/// couchd's state changed: toast a pending announcement once someone can see it.
pub fn state(app: &mut App) {
    UPDATES.with_borrow_mut(|u| {
        if u.waiting || pending(&app.state, u.shown.as_deref()).is_none() || !visible(&app.state) {
            return;
        }
        u.waiting = true;
        u.timer.start(TimerMode::SingleShot, DELAY, || {
            with_app(announce);
        });
    });
}

fn announce(app: &mut App) {
    let a = UPDATES.with_borrow_mut(|u| {
        u.waiting = false;
        let a = pending(&app.state, u.shown.as_deref())?.clone();
        if !visible(&app.state) {
            return None;
        }
        u.shown = a.get("id").and_then(Value::as_str).map(str::to_string);
        Some(a)
    });
    let Some(a) = a else { return };
    let (text, level) = message(&a);
    app.toast(&text, level, None);
    let id = a.get("id").cloned().unwrap_or(Value::Null);
    app.command("updates.announced", json!({"id": id}), None);
}

/// Someone can see the launcher: the TV is on, the launcher is in front and done starting,
/// and it isn't dimmed to the idle clock.
fn visible(s: &Value) -> bool {
    state::str_at(s, "tv.power") != Some("standby")
        && state::str_at(s, "apps.foreground").unwrap_or("launcher") == "launcher"
        && state::bool_at(s, "startup.done") != Some(false)
        && state::bool_at(s, "idle.idle") != Some(true)
}

fn pending<'a>(s: &'a Value, shown: Option<&str>) -> Option<&'a Value> {
    let a = state::at(s, "updates.announce");
    let id = a.get("id")?.as_str();
    (a.is_object() && id != shown).then_some(a)
}

/// The toast's text and level.
fn message(a: &Value) -> (String, &'static str) {
    let field = |k: &str| match a.get(k) {
        Some(Value::String(s)) => s.clone(),
        Some(v) => v.to_string(),
        None => "undefined".into(),
    };
    if a.get("kind").and_then(Value::as_str) == Some("rolled-back") {
        let kept = match a.get("kept") {
            Some(Value::String(k)) if !k.is_empty() => format!(" Couch stayed on {k}."),
            _ => String::new(),
        };
        return (format!("Couldn't update to {}.{kept}", field("version")), "warning");
    }
    (format!("Updated to {}", field("version")), "info")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn messages() {
        let a = json!({"id": "x", "kind": "installed", "version": "v0.2.0", "kept": null});
        assert_eq!(message(&a), ("Updated to v0.2.0".into(), "info"));
        let a = json!({"id": "x", "kind": "rolled-back", "version": "v0.3.0", "kept": "v0.2.0"});
        assert_eq!(message(&a), ("Couldn't update to v0.3.0. Couch stayed on v0.2.0.".into(), "warning"));
        let a = json!({"id": "x", "kind": "rolled-back", "version": "v0.3.0", "kept": null});
        assert_eq!(message(&a), ("Couldn't update to v0.3.0.".into(), "warning"));
    }

    #[test]
    fn waits_until_someone_can_see_it() {
        let a = json!({"id": "installed:v0.2.0:1", "kind": "installed", "version": "v0.2.0"});
        let s = json!({"updates": {"announce": a}, "tv": {"power": "on"}, "startup": {"done": true}});
        assert!(visible(&s));
        assert!(pending(&s, None).is_some());
        assert!(pending(&s, Some("installed:v0.2.0:1")).is_none());
        assert!(pending(&json!({"updates": {"announce": null}}), None).is_none());
        let mut off = s.clone();
        off["tv"]["power"] = json!("standby");
        assert!(!visible(&off));
        let mut idle = s.clone();
        idle["idle"] = json!({"idle": true});
        assert!(!visible(&idle));
        let mut away = s.clone();
        away["apps"] = json!({"foreground": "youtube"});
        assert!(!visible(&away));
    }
}
