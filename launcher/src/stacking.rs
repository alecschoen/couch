//! The launcher and Chromium in one cage.
//!
//! couchd starts Chromium (couch-chromium.service) only for YouTube, as a second window in
//! the launcher's cage, and stops it after. cage shows the newest window on top and gives it
//! the keyboard, so Chromium starting would cover the Opening screen with its own grey while
//! YouTube loads. So:
//!   - while the launcher should be seen, losing the keyboard means something mapped over
//!     it: unmap and map again, and the launcher is the newest window, on top, once more;
//!   - once a Chromium app is in front (apps.foreground), unmap: Chromium shows;
//!   - when the launcher is back in front, map again: on top of Chromium at once, before
//!     couchd has even stopped it.
//!
//! couch-splash (system/couch-splash) is cage's first window, holding the boot picture; ours
//! maps over it and it goes once it's covered (or couchd ends it after our first frame).
//! While it's up, losing the keyboard is the splash's doing: let it be.
//!
//! cage has no other way to raise a window (no xdg-activation or foreign-toplevel in
//! the Pi's cage). Without a keyboard there are no focus events; on the Pi couchd's
//! "Couch remote" (couchd/uinput.py) is one. Elsewhere (a desktop, the X11 lab) this does
//! nothing.

use std::cell::RefCell;
use std::collections::VecDeque;
use std::time::{Duration, Instant};

use slint::winit_030::winit::event::WindowEvent;
use slint::winit_030::{EventResult, WinitWindowAccessor};
use slint::{ComponentHandle, Timer};

use crate::app::{App, with_app};
use crate::state;

/// More remaps than this in REMAP_WINDOW, and something else wants the top too: stop
/// fighting it until the launcher is next mapped by `state`.
const REMAP_LIMIT: usize = 4;
const REMAP_WINDOW: Duration = Duration::from_secs(2);

#[derive(Default)]
struct Stacking {
    /// Unmapped, so a Chromium app shows.
    hidden: bool,
    remaps: VecDeque<Instant>,
    gave_up: bool,
}

thread_local! {
    static STACKING: RefCell<Stacking> = RefCell::new(Stacking::default());
}

/// Only in a Wayland session (the Pi's cage): not in the X11 lab.
fn enabled() -> bool {
    std::env::var_os("WAYLAND_DISPLAY").is_some()
}

/// Should the launcher be on top? Not while a Chromium app is in front.
fn wanted(app: &App) -> bool {
    let fg = app.foreground();
    let kind = state::list_at(&app.state, "apps.list")
        .iter()
        .find(|a| a.get("id").and_then(serde_json::Value::as_str) == Some(fg))
        .and_then(|a| a.get("kind").and_then(serde_json::Value::as_str));
    kind != Some("chromium")
}

pub fn start(ui: &crate::MainWindow) {
    ui.window().on_winit_window_event(|_, event| {
        if let WindowEvent::Focused(false) = event {
            // Not from inside winit's callback: hiding the window there would pull it out
            // from under the event that's being delivered.
            Timer::single_shot(Duration::ZERO, || {
                with_app(lost_focus);
            });
        }
        EventResult::Propagate
    });
}

/// couch-splash still up? couch-session-start writes its pid, and couchd removes the file
/// when it ends the splash (couchd/splash.py). The pid must still be the splash's.
fn splash_up() -> bool {
    let Some(dir) = std::env::var_os("XDG_RUNTIME_DIR") else {
        return false;
    };
    let path = std::path::Path::new(&dir).join("couch-splash.pid");
    let Some(pid) = std::fs::read_to_string(path).ok().and_then(|s| s.trim().parse::<u32>().ok()) else {
        return false;
    };
    std::fs::read_to_string(format!("/proc/{pid}/comm")).is_ok_and(|c| c.trim() == "couch-splash")
}

fn lost_focus(app: &mut App) {
    if !enabled() || !wanted(app) || splash_up() {
        return;
    }
    STACKING.with_borrow_mut(|s| {
        if s.hidden || s.gave_up {
            return;
        }
        let now = Instant::now();
        s.remaps.retain(|t| now - *t < REMAP_WINDOW);
        if s.remaps.len() >= REMAP_LIMIT {
            eprintln!("couch-launcher: another window keeps taking the top; leaving it");
            s.gave_up = true;
            return;
        }
        s.remaps.push_back(now);
        let _ = app.ui.hide();
        let _ = app.ui.show();
        eprintln!("couch-launcher: back on top");
    });
}

/// After every state change: step aside for a Chromium app, or come back.
pub fn state(app: &mut App) {
    if !enabled() {
        return;
    }
    let want = wanted(app);
    STACKING.with_borrow_mut(|s| {
        if want == !s.hidden {
            return;
        }
        s.hidden = !want;
        s.gave_up = false;
        s.remaps.clear();
        let _ = if want { app.ui.show() } else { app.ui.hide() };
        eprintln!("couch-launcher: {}", if want { "in front" } else { "behind the app" });
    });
}
