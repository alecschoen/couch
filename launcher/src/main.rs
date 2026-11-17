//! Couch's launcher, drawn natively with Slint: Home, Settings, Bluetooth and the system
//! screens, talking to couchd over its WebSocket (docs/protocol.md).
//!
//!   couch-launcher [ws://127.0.0.1:8765/ws]     (or COUCH_URL)
//!
//! On the Pi it's the client of the session's cage (system/couch-session-start), full screen;
//! on a desktop it opens a 1920×1080 window (scripts/dev.sh).

slint::include_modules!();

mod app;
mod couch;
mod idle;
mod overlay;
mod screens;
#[cfg(feature = "window")]
mod shot;
mod sounds;
#[cfg(feature = "window")]
mod stacking;
mod state;
mod updates;

fn main() -> Result<(), slint::PlatformError> {
    let url = std::env::args().nth(1).or_else(|| std::env::var("COUCH_URL").ok()).unwrap_or_else(|| {
        format!("ws://127.0.0.1:{}/ws", std::env::var("COUCH_PORT").unwrap_or_else(|_| "8765".into()))
    });
    let ui = MainWindow::new()?;
    app::start(ui.clone_strong(), url);
    #[cfg(feature = "window")]
    let _shots = shot::watch(&ui);
    #[cfg(feature = "window")]
    stacking::start(&ui);
    // Not ui.run(): that ends when the window hides, and in native mode it hides while a
    // Chromium app is in front (src/stacking.rs). Closing it (on a desktop) still ends it.
    ui.window().on_close_requested(|| {
        let _ = slint::quit_event_loop();
        slint::CloseRequestResponse::HideWindow
    });
    ui.show()?;
    slint::run_event_loop_until_quit()
}
