//! Wi-Fi (Settings › Network › Wi-Fi): the networks around, from couchd's `wifi.networks`,
//! connected first and then by signal, each with its bars and a lock if it needs a password.
//!
//! OK on a network does the obvious next thing:
//!   connected            Options: Forget
//!   saved                connect to it
//!   open                 connect to it
//!   needs a password     the keyboard (src/keyboard.rs), then connect with what was typed
//! Holding OK on a saved network opens Options too. A join shows "Connecting" and a spinner
//! on its row until couchd answers: "Connected to X", or its error as a toast. A wrong
//! password opens the keyboard again with what was typed, to fix it.
//!
//! The radio scans only while this screen is on top: once as it opens, then every 30 s.
//! While the keyboard is open the screen holds the remote's raw keys, so Red and Blue type
//! (delete, shift) instead of moving the sound and opening the quick menu.
//!
//! The password goes to couchd in `wifi.connect` and nowhere else: it's never logged, and the
//! keyboard forgets it once the join is over.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::{Rc, Weak};
use std::time::Duration;

use serde_json::{Value, json};
use slint::{ComponentHandle, Model, Timer, TimerMode, VecModel};

use super::bluetooth::{max_scroll, scroll_to_show};
use crate::app::{App, Key, Outcome, Screen, with_app};
use crate::keyboard::{self, Action, Keyboard, Purpose, RawKeys};
use crate::state;
use crate::{WifiData, WifiRow};

const RESCAN: Duration = Duration::from_secs(30);
/// The longest password WPA takes (63 characters, or the 64 hex digits of the key itself).
const MAX_PASSWORD: usize = 64;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Pending {
    Connect,
    Forget,
}

#[derive(Clone, Debug, PartialEq)]
struct Network {
    ssid: String,
    signal: i64,
    secure: bool,
    known: bool,
    active: bool,
}

impl Network {
    fn from(v: &Value) -> Self {
        let flag = |k: &str| v.get(k).and_then(Value::as_bool).unwrap_or(false);
        Network {
            ssid: v.get("ssid").and_then(Value::as_str).unwrap_or("").to_string(),
            signal: v.get("signal").and_then(Value::as_i64).unwrap_or(0),
            secure: flag("secure"),
            known: flag("known"),
            active: flag("active"),
        }
    }
}

/// Signal (0–100) as bars (1–4).
fn bars(signal: i64) -> i32 {
    match signal {
        75.. => 4,
        50..=74 => 3,
        25..=49 => 2,
        _ => 1,
    }
}

/// The networks as listed: the one it's on first, then by bars (not the exact signal, so
/// rows don't swap places each scan), saved ones first among equals, then by name.
fn networks(s: &Value) -> Vec<Network> {
    let mut all: Vec<Network> =
        state::list_at(s, "wifi.networks").iter().map(Network::from).filter(|n| !n.ssid.is_empty()).collect();
    all.sort_by_key(|n| (!n.active, -bars(n.signal), !n.known, n.ssid.to_lowercase()));
    all
}

fn find(s: &Value, ssid: &str) -> Option<Network> {
    networks(s).into_iter().find(|n| n.ssid == ssid)
}

fn enabled(s: &Value) -> bool {
    state::bool_at(s, "wifi.enabled") == Some(true)
}

/// Top right: what the Wi-Fi is doing.
fn status(s: &Value) -> String {
    let ssid = state::str_at(s, "wifi.ssid").unwrap_or("a network");
    if !enabled(s) {
        return "Wi-Fi is off".into();
    }
    match state::str_at(s, "wifi.connection") {
        Some("connected") => format!("Connected to {ssid}"),
        Some("connecting") => format!("Connecting to {ssid}…"),
        _ => "Not connected".into(),
    }
}

/// An error that a (new) password would put right: the keyboard opens again.
fn wants_password(error: &str) -> bool {
    error == "Wrong password"
        || error.ends_with("needs a password")
        || error.starts_with("Wi-Fi passwords are")
        || error.starts_with("That password has")
}

#[derive(Default)]
struct Inner {
    this: Weak<RefCell<Inner>>,
    pending: HashMap<String, Pending>,
    visible: bool,
    rescan: Timer,
    /// The focused row's network, and where it was (for the nearest row if it goes).
    focus: Option<String>,
    last_index: usize,
    scroll: f32,
    /// The networks as drawn.
    keys: Vec<String>,
    /// Options for this network.
    sheet: Option<String>,
    /// The keyboard, typing a password for this network.
    keyboard: Option<(String, Keyboard)>,
    raw: RawKeys,
    model: Rc<VecModel<WifiRow>>,
}

pub struct Wifi {
    inner: Rc<RefCell<Inner>>,
}

impl Wifi {
    pub fn new(_params: &Value, _app: &mut App) -> Self {
        let inner = Rc::new_cyclic(|this| RefCell::new(Inner { this: this.clone(), ..Default::default() }));
        Wifi { inner }
    }
}

/// Run `f` on the screen's state, now if it's free, else once the call that has it is over
/// (a command's reply comes straight back when couchd isn't connected).
fn soon(weak: &Weak<RefCell<Inner>>, app: &mut App, f: impl FnOnce(&mut Inner, &mut App) + 'static) {
    let Some(inner) = weak.upgrade() else { return };
    match inner.try_borrow_mut() {
        Ok(mut i) => f(&mut i, app),
        Err(_) => {
            let weak = weak.clone();
            Timer::single_shot(Duration::ZERO, move || {
                with_app(|app| {
                    if let Some(inner) = weak.upgrade() {
                        f(&mut inner.borrow_mut(), app);
                    }
                });
            });
        }
    }
}

impl Inner {
    // ---- Drawing

    fn sub_for(&self, s: &Value, n: &Network) -> &'static str {
        let joining = state::str_at(s, "wifi.connection") == Some("connecting")
            && state::str_at(s, "wifi.ssid") == Some(n.ssid.as_str());
        match self.pending.get(&n.ssid) {
            Some(Pending::Connect) => "Connecting",
            Some(Pending::Forget) => "Forgetting",
            None if n.active => "Connected · OK for options",
            None if joining => "Connecting",
            None if n.known => "Saved · OK to connect",
            None if !n.secure => "Open · OK to connect",
            None => "OK to type the password",
        }
    }

    fn end_for(&self, s: &Value, n: &Network) -> &'static str {
        let joining = state::str_at(s, "wifi.connection") == Some("connecting")
            && state::str_at(s, "wifi.ssid") == Some(n.ssid.as_str());
        if self.pending.contains_key(&n.ssid) || (joining && !n.active) {
            "spinner"
        } else if n.active {
            "check"
        } else {
            ""
        }
    }

    fn render(&mut self, app: &App) {
        let s = &app.state;
        let data = app.ui.global::<WifiData>();
        data.set_status(status(s).into());
        let on = enabled(s);
        let scanning = state::bool_at(s, "wifi.scanning") == Some(true);
        data.set_looking(self.visible && on && scanning);

        let list = if on { networks(s) } else { Vec::new() };
        let note = if !on {
            "Wi-Fi is off. Turn it on in Settings › Network."
        } else if list.is_empty() && !scanning {
            "No networks found"
        } else {
            ""
        };
        self.keys = list.iter().map(|n| n.ssid.clone()).collect();

        // Focus stays on its network; if that's gone, the row where it was.
        if self.focus.as_ref().is_some_and(|f| !self.keys.contains(f)) {
            self.focus = self.keys.get(self.last_index.min(self.keys.len().saturating_sub(1))).cloned();
        }
        if self.focus.is_none() {
            self.focus = self.keys.first().cloned();
        }
        self.note_focus();

        let rows: Vec<WifiRow> = list
            .iter()
            .map(|n| WifiRow {
                key: n.ssid.as_str().into(),
                title: n.ssid.as_str().into(),
                sub: self.sub_for(s, n).into(),
                bars: bars(n.signal),
                secure: n.secure,
                end: self.end_for(s, n).into(),
                focused: self.is_focused(&n.ssid),
            })
            .collect();
        let same = self.model.row_count() == rows.len() && self.model.iter().zip(&rows).all(|(a, b)| a.key == b.key);
        if same {
            for (i, row) in rows.into_iter().enumerate() {
                if self.model.row_data(i).as_ref() != Some(&row) {
                    self.model.set_row_data(i, row);
                }
            }
        } else {
            self.model.set_vec(rows);
        }
        data.set_rows(self.model.clone().into());
        data.set_note(note.into());
        data.set_scroll(self.scroll);
    }

    fn is_focused(&self, ssid: &str) -> bool {
        self.sheet.is_none() && self.keyboard.is_none() && self.focus.as_deref() == Some(ssid)
    }

    fn draw_focus(&self, app: &App) {
        for i in 0..self.model.row_count() {
            let mut row = self.model.row_data(i).unwrap();
            let focused = self.is_focused(row.key.as_str());
            if row.focused != focused {
                row.focused = focused;
                self.model.set_row_data(i, row);
            }
        }
        app.ui.global::<WifiData>().set_scroll(self.scroll);
    }

    /// Focus is on a row: scroll it into view.
    fn note_focus(&mut self) {
        let rows = self.keys.len();
        match self.focus.as_ref().and_then(|f| self.keys.iter().position(|k| k == f)) {
            Some(i) => {
                self.last_index = i;
                self.scroll = scroll_to_show(self.scroll, i, rows);
            }
            None => self.scroll = self.scroll.min(max_scroll(rows)),
        }
    }

    fn move_focus(&mut self, app: &App, down: bool) -> Outcome {
        let Some(i) = self.focus.as_ref().and_then(|f| self.keys.iter().position(|k| k == f)) else {
            return Outcome::Edge;
        };
        let next = if down { i + 1 } else { i.wrapping_sub(1) };
        let Some(key) = self.keys.get(next) else { return Outcome::Edge };
        self.focus = Some(key.clone());
        self.note_focus();
        self.draw_focus(app);
        Outcome::Moved
    }

    // ---- Joining and forgetting

    fn select(&mut self, app: &mut App, ssid: &str) {
        let Some(n) = find(&app.state, ssid) else { return };
        if self.pending.contains_key(ssid) {
            return;
        }
        if n.active {
            self.open_options(app, &n);
        } else if n.known || !n.secure {
            self.connect(app, ssid, None);
        } else {
            self.open_keyboard(app, ssid, "");
        }
    }

    fn connect(&mut self, app: &mut App, ssid: &str, password: Option<String>) {
        self.pending.insert(ssid.to_string(), Pending::Connect);
        self.render(app);
        let mut args = json!({"ssid": ssid});
        if let Some(p) = &password {
            args["password"] = json!(p);
        }
        let weak = self.this.clone();
        let ssid = ssid.to_string();
        app.command(
            "wifi.connect",
            args,
            Some(Box::new(move |app, result| {
                soon(&weak, app, move |i, app| {
                    i.pending.remove(&ssid);
                    match result {
                        Ok(_) => app.toast(&format!("Connected to {ssid}"), "info", None),
                        Err(e) => {
                            app.toast(&e, "error", None);
                            if i.visible && wants_password(&e) && find(&app.state, &ssid).is_some_and(|n| n.secure) {
                                // What was typed comes back to be fixed; nothing typed yet (a
                                // saved password that's out of date): an empty box.
                                i.open_keyboard(app, &ssid, password.as_deref().unwrap_or(""));
                            }
                        }
                    }
                    i.render(app);
                });
            })),
        );
    }

    fn forget(&mut self, app: &mut App, ssid: &str) {
        self.pending.insert(ssid.to_string(), Pending::Forget);
        self.render(app);
        let weak = self.this.clone();
        let ssid = ssid.to_string();
        app.command(
            "wifi.forget",
            json!({"ssid": ssid}),
            Some(Box::new(move |app, result| {
                soon(&weak, app, move |i, app| {
                    i.pending.remove(&ssid);
                    match result {
                        Ok(_) => app.toast(&format!("Forgot {ssid}"), "info", None),
                        Err(e) => app.toast(&e, "error", None),
                    }
                    i.render(app);
                });
            })),
        );
    }

    // ---- Options: Forget

    fn open_options(&mut self, app: &App, n: &Network) {
        if self.sheet.is_some() || !n.known {
            return;
        }
        self.sheet = Some(n.ssid.clone());
        let data = app.ui.global::<WifiData>();
        data.set_sheet_title(n.ssid.as_str().into());
        data.set_sheet_open(true);
        self.draw_focus(app);
    }

    fn close_options(&mut self, app: &App) -> Option<String> {
        let ssid = self.sheet.take()?;
        app.ui.global::<WifiData>().set_sheet_open(false);
        self.draw_focus(app);
        Some(ssid)
    }

    fn sheet_key(&mut self, key: &Key, app: &mut App) -> Outcome {
        if key.arrow().is_some() {
            return Outcome::Edge;
        }
        match key.key.as_str() {
            "back" => {
                self.close_options(app);
                Outcome::Taken
            }
            "select" => {
                if let Some(ssid) = self.close_options(app) {
                    self.forget(app, &ssid);
                }
                Outcome::Taken
            }
            _ => Outcome::No,
        }
    }

    // ---- The keyboard

    fn open_keyboard(&mut self, app: &mut App, ssid: &str, text: &str) {
        let purpose = Purpose {
            kicker: format!("Wi-Fi · {ssid}"),
            title: "Password".into(),
            placeholder: "Password".into(),
            done: "Join".into(),
            secret: true,
            max_len: MAX_PASSWORD,
        };
        let kb = Keyboard::new(purpose, text);
        kb.draw(app);
        self.keyboard = Some((ssid.to_string(), kb));
        if !self.raw.held() {
            self.raw.claim(app);
        }
        self.draw_focus(app);
    }

    fn close_keyboard(&mut self, app: &mut App) {
        if self.keyboard.take().is_some() {
            keyboard::close(app);
        }
        self.raw.release(app);
        self.draw_focus(app);
    }

    fn keyboard_key(&mut self, key: &Key, app: &mut App) -> Outcome {
        let Some((ssid, kb)) = self.keyboard.as_mut() else { return Outcome::No };
        match kb.key(key) {
            Action::Moved => {
                kb.draw(app);
                Outcome::Moved
            }
            Action::Edge => Outcome::Edge,
            Action::Taken => Outcome::Taken,
            // Home still goes home (hide() closes the keyboard); the rest do nothing here.
            Action::No if key.is("home") => Outcome::No,
            Action::No => Outcome::Taken,
            Action::Done(text) if text.is_empty() => Outcome::Edge,
            Action::Done(text) => {
                let ssid = ssid.clone();
                self.close_keyboard(app);
                self.connect(app, &ssid, Some(text));
                Outcome::Taken
            }
            Action::Close => {
                self.close_keyboard(app);
                Outcome::Taken
            }
        }
    }

    // ---- Scanning, only while on top

    fn start_scan(&mut self, app: &mut App) {
        if !self.visible || !app.connected || !enabled(&app.state) {
            return;
        }
        scan(app);
        self.rescan.start(TimerMode::Repeated, RESCAN, || {
            with_app(scan);
        });
    }

    fn stop_scan(&mut self) {
        self.rescan.stop();
    }

    fn key(&mut self, key: &Key, app: &mut App) -> Outcome {
        if self.keyboard.is_some() {
            return self.keyboard_key(key, app);
        }
        if self.sheet.is_some() {
            return self.sheet_key(key, app);
        }
        match key.arrow() {
            Some(crate::app::Dir::Up) => return self.move_focus(app, false),
            Some(crate::app::Dir::Down) => return self.move_focus(app, true),
            Some(_) => return Outcome::Edge,
            None => {}
        }
        if !key.is("select") {
            return Outcome::No;
        }
        let Some(ssid) = self.focus.clone() else { return Outcome::No };
        if key.hold
            && let Some(n) = find(&app.state, &ssid).filter(|n| n.known)
        {
            self.open_options(app, &n);
            return Outcome::Taken;
        }
        self.select(app, &ssid);
        Outcome::Taken
    }
}

/// Ask the radio to look; its answer only matters as `wifi.scanning`.
fn scan(app: &mut App) {
    if enabled(&app.state) {
        app.command("wifi.scan", json!({}), Some(Box::new(|_, _| {})));
    }
}

impl Screen for Wifi {
    fn name(&self) -> &'static str {
        "wifi"
    }

    fn palette(&self, _app: &App) -> String {
        "settings".into()
    }

    fn show(&mut self, app: &mut App) {
        let mut i = self.inner.borrow_mut();
        i.visible = true;
        i.focus = None;
        i.render(app);
        i.start_scan(app);
    }

    fn hide(&mut self, app: &mut App) {
        let mut i = self.inner.borrow_mut();
        i.visible = false;
        i.close_keyboard(app);
        i.close_options(app);
        i.stop_scan();
        i.render(app);
    }

    fn destroy(&mut self, app: &mut App) {
        let mut i = self.inner.borrow_mut();
        i.stop_scan();
        i.close_keyboard(app);
    }

    fn state(&mut self, app: &mut App, patch: Option<&Value>) {
        let mut i = self.inner.borrow_mut();
        if patch.is_some_and(|p| p.get("wifi").is_none()) {
            return;
        }
        let on = enabled(&app.state);
        if !on && i.keyboard.is_some() {
            i.close_keyboard(app);
        }
        if !on {
            i.close_options(app);
        }
        // couchd is (back) up, or the radio came on: look again.
        if i.visible && on && (patch.is_none() || !i.rescan.running()) {
            i.start_scan(app);
        }
        i.render(app);
    }

    fn key(&mut self, key: &Key, app: &mut App) -> Outcome {
        self.inner.borrow_mut().key(key, app)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signal_as_bars() {
        assert_eq!([0, 24, 25, 49, 50, 74, 75, 100].map(bars), [1, 1, 2, 2, 3, 3, 4, 4]);
    }

    #[test]
    fn the_connected_network_first_then_by_bars() {
        let s = json!({"wifi": {"networks": [
            {"ssid": "Upstairs", "signal": 80, "secure": true, "known": false, "active": false},
            {"ssid": "Neighbours", "signal": 78, "secure": true, "known": false, "active": false},
            {"ssid": "Living room", "signal": 40, "secure": true, "known": true, "active": true},
            {"ssid": "Café", "signal": 55, "secure": false, "known": false, "active": false},
            {"ssid": "Garden", "signal": 60, "secure": true, "known": true, "active": false},
            {"ssid": "", "signal": 90, "secure": false, "known": false, "active": false},
        ]}});
        let order: Vec<String> = networks(&s).into_iter().map(|n| n.ssid).collect();
        // Same bars: saved first, then by name (Neighbours before Upstairs, whatever the
        // exact signal says this scan).
        assert_eq!(order, ["Living room", "Neighbours", "Upstairs", "Garden", "Café"]);
    }

    #[test]
    fn status_says_what_its_doing() {
        assert_eq!(status(&json!({"wifi": {"enabled": false}})), "Wi-Fi is off");
        assert_eq!(status(&json!({"wifi": {"enabled": true, "connection": "disconnected"}})), "Not connected");
        let s = json!({"wifi": {"enabled": true, "connection": "connected", "ssid": "Upstairs"}});
        assert_eq!(status(&s), "Connected to Upstairs");
        let s = json!({"wifi": {"enabled": true, "connection": "connecting", "ssid": "Upstairs"}});
        assert_eq!(status(&s), "Connecting to Upstairs…");
    }

    #[test]
    fn errors_a_password_would_fix() {
        assert!(wants_password("Wrong password"));
        assert!(wants_password("Upstairs needs a password"));
        assert!(wants_password("Wi-Fi passwords are 8 to 63 characters"));
        assert!(wants_password("That password has a character Wi-Fi doesn't allow"));
        assert!(!wants_password("Can't find Upstairs. Is it in range?"));
        assert!(!wants_password("Couldn't connect to Upstairs"));
    }
}
