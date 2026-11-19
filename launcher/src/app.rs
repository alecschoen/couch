//! The launcher's brain, on the UI thread: couchd's live state, the screens and their back
//! stack, the remote's keys, toasts, and the theme.
//!
//! Keys, in order, until something takes them (as in app.js):
//!   0. the idle clock: a key that wakes it does nothing else
//!   1. the screen on top (a takeover if one is up): `Screen::key`
//!   2. Back goes back, Home goes to the first screen
//!
//! Screens don't hold the App: it's passed to each call. They change the stack through
//! `app.go()`, `app.back()` and `app.home()`, which are queued and done after the call.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;
use std::time::{Duration, Instant};

use serde_json::{Value, json};
use slint::{ComponentHandle, Model, Timer, TimerMode, VecModel};

use crate::couch::{Couch, Event};
use crate::screens;
use crate::sounds::Sounds;
use crate::state;
use crate::{Bump, Layer, MainWindow, Router, Theme, ToastItem};

pub const TOAST_MS: u64 = 2400;
const MAX_TOASTS: usize = 3;
const RESTART_GRACE: Duration = Duration::from_millis(1500);
const EVENING_STARTS: u32 = 18;
const EVENING_ENDS: u32 = 7;
/// How far the "nothing that way" nudge goes (focus.js _bump).
const BUMP: f32 = 9.0;

/// A remote key, as couchd sent it.
pub struct Key {
    pub key: String,
    pub repeat: bool,
    pub hold: bool,
}

impl Key {
    pub fn is(&self, name: &str) -> bool {
        self.key == name
    }
    pub fn arrow(&self) -> Option<Dir> {
        match self.key.as_str() {
            "up" => Some(Dir::Up),
            "down" => Some(Dir::Down),
            "left" => Some(Dir::Left),
            "right" => Some(Dir::Right),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Dir {
    Up,
    Down,
    Left,
    Right,
}

/// What a screen did with a key. Picks the UI sound (sounds.rs) and whether the app goes on
/// to its own handling (Back, Home).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Outcome {
    /// Not taken: the app handles it (Back goes back, Home home).
    No,
    /// Focus moved (or a control changed: a stepper, a slider).
    Moved,
    /// An arrow with nowhere to go.
    Edge,
    /// Taken, nothing moved (OK on something, a key the screen used).
    Taken,
}

pub trait Screen {
    /// The router name: "home", "settings", "startup", …
    fn name(&self) -> &'static str;
    /// The flood colour while it's on top: a palette name from tokens.slint.
    fn palette(&self, app: &App) -> String;
    /// On top now (pushed, or uncovered): draw everything into its Slint global.
    fn show(&mut self, _app: &mut App) {}
    /// Covered, or closed.
    fn hide(&mut self, _app: &mut App) {}
    /// Closed for good: stop timers.
    fn destroy(&mut self, _app: &mut App) {}
    /// couchd's state changed (`patch` is None for a full snapshot). Every screen in the
    /// stack hears it, covered or not.
    fn state(&mut self, _app: &mut App, _patch: Option<&Value>) {}
    /// A remote key, while it's on top.
    fn key(&mut self, _key: &Key, _app: &mut App) -> Outcome {
        Outcome::No
    }
    /// Appear without the slide-in (Startup).
    fn still(&self) -> bool {
        false
    }
}

enum Action {
    Go(String, Value),
    Back,
    Home,
}

type Reply = Box<dyn FnOnce(&mut App, Result<Value, String>)>;

pub struct App {
    pub ui: MainWindow,
    couch: Couch,
    pub state: Value,
    pub connected: bool,
    heard: bool,
    next_id: u64,
    pending: HashMap<u64, Reply>,
    stack: Vec<Box<dyn Screen>>,
    takeover: Option<Box<dyn Screen>>,
    generation: i32,
    actions: Vec<Action>,
    repaint_pending: bool,
    toasts: Rc<VecModel<ToastItem>>,
    toast_ids: Vec<u64>,
    next_toast: u64,
    pub sounds: Sounds,
    // screens/system.js
    system_shown: Option<String>,
    system_expired: Vec<String>,
    system_timer: Timer,
    grace_until: Option<Instant>,
    grace_timer: Timer,
    evening_timer: Timer,
    bump_timer: Timer,
    first_frame: FirstFrame,
    // couchd's draw checks (back from Plex: is cage drawing us again?), answered after the
    // next frame.
    draw_checks: Vec<u64>,
    /// The speaker source Now Playing last came up for (screens/nowplaying.rs `follow`).
    pub music_shown: Option<String>,
}

#[derive(Default)]
struct FirstFrame {
    drawn_ms: Option<u64>,
    sent: bool,
}

thread_local! {
    static APP: RefCell<Option<App>> = const { RefCell::new(None) };
}

/// Run `f` with the app, from a timer or a callback on the UI thread. Does nothing before
/// the app exists, or if it's already in use (a nested call).
pub fn with_app<R>(f: impl FnOnce(&mut App) -> R) -> Option<R> {
    APP.with(|cell| match cell.try_borrow_mut() {
        Ok(mut slot) => slot.as_mut().map(f),
        Err(_) => {
            eprintln!("couch-launcher: nested app call skipped");
            None
        }
    })
}

pub fn start(ui: MainWindow, url: String) {
    let couch = Couch::spawn(url, |event| {
        let _ = slint::invoke_from_event_loop(move || {
            with_app(|app| app.on_couch(event));
        });
    });
    let toasts = Rc::new(VecModel::<ToastItem>::default());
    ui.global::<Layer>().set_toasts(toasts.clone().into());

    ui.on_keyboard(|key| {
        with_app(|app| {
            app.command("remote.press", json!({"key": key.as_str(), "keyboard": true}), None);
        });
    });

    let started = Instant::now();
    let _ = ui.window().set_rendering_notifier(move |state, _| {
        if let slint::RenderingState::AfterRendering = state {
            let ms = started.elapsed().as_millis() as u64;
            Timer::single_shot(Duration::ZERO, move || {
                with_app(|app| app.frame_drawn(ms));
            });
        }
    });

    let app = App {
        ui,
        couch,
        state: json!({}),
        connected: false,
        heard: false,
        next_id: 1,
        pending: HashMap::new(),
        stack: Vec::new(),
        takeover: None,
        generation: 0,
        actions: Vec::new(),
        repaint_pending: false,
        toasts,
        toast_ids: Vec::new(),
        next_toast: 1,
        sounds: Sounds::new(),
        system_shown: None,
        system_expired: Vec::new(),
        system_timer: Timer::default(),
        grace_until: None,
        grace_timer: Timer::default(),
        evening_timer: Timer::default(),
        bump_timer: Timer::default(),
        first_frame: FirstFrame::default(),
        draw_checks: Vec::new(),
        music_shown: None,
    };
    APP.with(|cell| *cell.borrow_mut() = Some(app));
    with_app(|app| {
        app.apply_theme();
        // Startup until couchd's first snapshot: the very first frame.
        app.update_system();
        app.go("home", json!({}));
        app.flush();
        app.evening_timer.start(TimerMode::Repeated, Duration::from_secs(60), || {
            with_app(|app| app.apply_theme());
        });
    });
}

impl App {
    // ---- couchd

    fn on_couch(&mut self, event: Event) {
        match event {
            Event::Connected(up) => {
                self.connected = up;
                if !up {
                    for (_, reply) in std::mem::take(&mut self.pending) {
                        reply(self, Err("Lost connection to couchd".into()));
                    }
                } else {
                    self.send_first_frame();
                }
            }
            Event::Message(msg) => self.on_message(msg),
        }
        self.flush();
    }

    fn on_message(&mut self, msg: Value) {
        let kind = msg.get("type").and_then(Value::as_str).unwrap_or("");
        let data = msg.get("data").cloned().unwrap_or(Value::Null);
        match kind {
            "result" => {
                let id = msg.get("id").and_then(Value::as_u64).unwrap_or(0);
                if let Some(reply) = self.pending.remove(&id) {
                    if msg.get("ok").and_then(Value::as_bool) == Some(true) {
                        reply(self, Ok(data));
                    } else {
                        let error = msg.get("error").and_then(Value::as_str).unwrap_or("Failed").to_string();
                        reply(self, Err(error));
                    }
                }
            }
            "state" => {
                let full = msg.get("full").and_then(Value::as_bool) == Some(true);
                state::apply(&mut self.state, &data, full);
                if full {
                    if self.heard {
                        self.grace_until = Some(Instant::now() + RESTART_GRACE);
                    }
                    self.heard = true;
                }
                let patch = if full { None } else { Some(&data) };
                self.apply_theme();
                self.each_screen(|s, app| s.state(app, patch));
                crate::overlay::state(self, patch);
                crate::idle::state(self, patch);
                crate::updates::state(self);
                screens::nowplaying::follow(self);
                self.update_system();
                #[cfg(feature = "window")]
                crate::stacking::state(self);
            }
            "key" => self.on_key(&data),
            "toast" => {
                let text = data.get("text").and_then(Value::as_str).unwrap_or("");
                let level = data.get("level").and_then(Value::as_str).unwrap_or("info");
                let icon = data.get("icon").and_then(Value::as_str);
                crate::overlay::hide_volume(self);
                self.toast(text, level, icon);
            }
            "volume_panel" => crate::overlay::volume(self, &data),
            "draw_check" => {
                // couchd (apps.py) wants to know we're on screen: answer after a frame. While
                // cage hasn't got the screen there are no frames, and so no answer.
                if let Some(id) = data.get("id").and_then(Value::as_u64) {
                    self.draw_checks.push(id);
                    self.ui.window().request_redraw();
                }
            }
            "open_screen" => {
                // The quick menu's Home or Bluetooth: couchd has put the launcher in front.
                self.home();
                if let Some(screen) = data.get("screen").and_then(Value::as_str)
                    && screen != "home"
                {
                    self.go(screen, json!({}));
                }
            }
            _ => {}
        }
    }

    /// Send a command; `reply` gets its result (or the error), on the UI thread.
    pub fn command(&mut self, name: &str, args: Value, reply: Option<Reply>) {
        if !self.connected {
            if let Some(reply) = reply {
                reply(self, Err("Not connected to couchd".into()));
            }
            return;
        }
        let id = self.next_id;
        self.next_id += 1;
        if let Some(reply) = reply {
            self.pending.insert(id, reply);
        }
        self.couch.send(json!({"type": "command", "id": id, "name": name, "args": args}).to_string());
    }

    /// A command whose failure is shown as an error toast.
    pub fn command_or_toast(&mut self, name: &str, args: Value) {
        self.command(
            name,
            args,
            Some(Box::new(|app, result| {
                if let Err(e) = result {
                    app.toast(&e, "error", None);
                }
            })),
        );
    }

    /// GET a file couchd makes (`path` from its state, "/api/file/…"); `done` gets the body,
    /// or why not, on the UI thread.
    pub fn fetch(&self, path: &str, done: impl FnOnce(&mut App, Result<Vec<u8>, String>) + Send + 'static) {
        self.couch.fetch(path, move |result| {
            let _ = slint::invoke_from_event_loop(move || {
                with_app(|app| {
                    done(app, result);
                    app.flush();
                });
            });
        });
    }

    pub fn setting(&self, key: &str) -> &Value {
        state::setting(&self.state, key)
    }

    /// The app in front, per couchd ("launcher" until it says otherwise).
    pub fn foreground(&self) -> &str {
        state::str_at(&self.state, "apps.foreground").unwrap_or("launcher")
    }

    // ---- Keys

    fn on_key(&mut self, d: &Value) {
        let key = Key {
            key: d.get("key").and_then(Value::as_str).unwrap_or("").to_string(),
            repeat: d.get("repeat").and_then(Value::as_bool).unwrap_or(false),
            hold: d.get("hold").and_then(Value::as_bool).unwrap_or(false),
        };
        if d.get("overlay").and_then(Value::as_bool) == Some(true) {
            return; // the quick menu took it (couchd runs the menu)
        }
        let target = d.get("app").and_then(Value::as_str);
        if target.is_some_and(|t| t != "launcher") && !key.is("home") {
            return;
        }
        let before = (self.depth(), self.current_name(), self.generation);
        let woke = crate::idle::wake(self, &key);
        let mut outcome = Outcome::No;
        if !woke {
            outcome = self.with_current(|s, app| s.key(&key, app)).unwrap_or(Outcome::No);
            if outcome == Outcome::No {
                if key.is("back") {
                    self.back();
                } else if key.is("home") {
                    self.home();
                }
            }
        }
        if outcome == Outcome::Edge
            && let Some(dir) = key.arrow()
        {
            self.bump(dir);
        }
        self.flush();
        let moved = outcome == Outcome::Moved || (self.depth(), self.current_name(), self.generation) != before;
        if !woke && target.is_none_or(|t| t == "launcher") {
            self.sounds.for_key(self, &key, outcome, moved);
        }
    }

    // ---- The router

    /// Push a screen. Queued: done after the current call.
    pub fn go(&mut self, name: &str, params: Value) {
        self.actions.push(Action::Go(name.to_string(), params));
    }

    pub fn back(&mut self) {
        self.actions.push(Action::Back);
    }

    pub fn home(&mut self) {
        self.actions.push(Action::Home);
    }

    pub fn depth(&self) -> usize {
        self.stack.len()
    }

    pub fn current_name(&self) -> &'static str {
        self.takeover.as_ref().or(self.stack.last()).map(|s| s.name()).unwrap_or("")
    }

    /// The screen on top of the stack, under any takeover.
    pub fn stack_top(&self) -> &'static str {
        self.stack.last().map(|s| s.name()).unwrap_or("")
    }

    /// Set the flood to the top screen's palette (its colour follows focus). Done once the
    /// current call is over, when the screen is back in the stack.
    pub fn repaint(&mut self) {
        self.repaint_pending = true;
    }

    fn paint(&mut self) {
        self.repaint_pending = false;
        let palette = match self.takeover.as_ref().or(self.stack.last()) {
            Some(s) => s.palette(self),
            None => "system".into(),
        };
        self.ui.global::<Theme>().set_palette(palette.into());
    }

    /// Run the queued router actions.
    pub fn flush(&mut self) {
        if self.repaint_pending && self.actions.is_empty() {
            self.paint();
        }
        while !self.actions.is_empty() {
            for action in std::mem::take(&mut self.actions) {
                match action {
                    Action::Go(name, params) => self.do_go(&name, params),
                    Action::Back => {
                        self.do_back();
                    }
                    Action::Home => {
                        self.do_home();
                    }
                }
            }
            if self.repaint_pending {
                self.paint();
            }
        }
    }

    fn do_go(&mut self, name: &str, params: Value) {
        if self.stack.last().is_some_and(|s| s.name() == name) && params.as_object().is_none_or(|p| p.is_empty()) {
            return; // already there
        }
        let screen = screens::make(name, &params, self);
        if self.takeover.is_none()
            && let Some(mut covered) = self.stack.pop()
        {
            covered.hide(self);
            self.stack.push(covered);
        }
        self.stack.push(screen);
        if self.takeover.is_none() {
            self.show_top(true);
        }
    }

    fn do_back(&mut self) -> bool {
        if self.stack.len() < 2 {
            return false;
        }
        let mut top = self.stack.pop().unwrap();
        top.hide(self);
        top.destroy(self);
        if self.takeover.is_none() {
            self.show_top(false);
        }
        true
    }

    fn do_home(&mut self) -> bool {
        if self.stack.len() < 2 {
            return false;
        }
        while self.stack.len() > 1 {
            let mut s = self.stack.pop().unwrap();
            s.hide(self);
            s.destroy(self);
        }
        if self.takeover.is_none() {
            self.show_top(false);
        }
        true
    }

    /// Put a screen up above the stack (Startup, Opening, an error), replacing any takeover.
    fn takeover(&mut self, name: &str, params: Value) {
        let screen = screens::make(name, &params, self);
        let previous = self.takeover.take();
        match previous {
            Some(mut p) => {
                p.hide(self);
                p.destroy(self);
            }
            None => {
                if let Some(mut covered) = self.stack.pop() {
                    covered.hide(self);
                    self.stack.push(covered);
                }
            }
        }
        let entering = !screen.still();
        self.takeover = Some(screen);
        self.show_top(entering);
    }

    fn release(&mut self) {
        let Some(mut screen) = self.takeover.take() else { return };
        screen.hide(self);
        screen.destroy(self);
        if !self.stack.is_empty() {
            self.show_top(false);
        }
    }

    fn show_top(&mut self, entering: bool) {
        let name = self.current_name();
        self.with_current(|s, app| s.show(app));
        self.generation += 1;
        let router = self.ui.global::<Router>();
        router.set_top(name.into());
        router.set_entering(entering);
        router.set_generation(self.generation);
        self.paint();
        if name == "error" {
            self.sounds.play(self, "error");
        }
    }

    /// Call `f` on the screen on top, with the app.
    fn with_current<R>(&mut self, f: impl FnOnce(&mut dyn Screen, &mut App) -> R) -> Option<R> {
        if let Some(mut s) = self.takeover.take() {
            let r = f(s.as_mut(), self);
            if self.takeover.is_none() {
                self.takeover = Some(s);
            }
            return Some(r);
        }
        let mut s = self.stack.pop()?;
        let r = f(s.as_mut(), self);
        self.stack.push(s);
        Some(r)
    }

    fn each_screen(&mut self, mut f: impl FnMut(&mut dyn Screen, &mut App)) {
        for i in 0..self.stack.len() {
            let mut s = std::mem::replace(&mut self.stack[i], Box::new(screens::Placeholder));
            f(s.as_mut(), self);
            self.stack[i] = s;
        }
        if let Some(mut s) = self.takeover.take() {
            f(s.as_mut(), self);
            if self.takeover.is_none() {
                self.takeover = Some(s);
            }
        }
    }

    // ---- Startup, Opening and errors: couchd decides

    fn wanted(&self) -> Option<(&'static str, Value, String)> {
        let s = &self.state;
        if !self.heard || state::bool_at(s, "startup.done") == Some(false) {
            return Some(("startup", json!({}), "startup".into()));
        }
        if let Some(app) = state::str_at(s, "apps.opening") {
            return Some(("opening", json!({"app": app}), format!("opening:{app}")));
        }
        let error = state::at(s, "apps.error");
        if !error.is_null() {
            return Some(("error", json!({"error": error}), format!("error:{error}")));
        }
        None
    }

    fn update_system(&mut self) {
        let mut want = self.wanted();
        if let (Some(("startup", ..)), Some(until)) = (&want, self.grace_until) {
            let now = Instant::now();
            if self.system_shown.as_deref() != Some("startup") && until > now {
                self.grace_timer.start(TimerMode::SingleShot, until - now, || {
                    with_app(|app| {
                        app.update_system();
                        app.flush();
                    });
                });
                want = None;
            }
        }
        let want_key = want.as_ref().map(|w| w.2.clone());
        self.system_expired.retain(|k| Some(k) == want_key.as_ref());
        let want = want.filter(|w| !self.system_expired.contains(&w.2));
        let key = want.as_ref().map(|w| w.2.clone());
        if key == self.system_shown {
            return;
        }
        self.system_timer.stop();
        self.system_shown = key.clone();
        let Some((name, params, key)) = want else {
            self.release();
            return;
        };
        self.takeover(name, params);
        // couchd times these out itself; in case it doesn't, the screen under shows again.
        let limit = match name {
            "startup" => 45,
            "opening" => 40,
            _ => 150,
        };
        self.system_timer.start(TimerMode::SingleShot, Duration::from_secs(limit), move || {
            with_app(|app| {
                eprintln!("couch-launcher: {name} screen timed out");
                app.system_expired.push(key.clone());
                app.update_system();
                app.flush();
            });
        });
    }

    // ---- Toasts (components.js Toasts, with system.js's error rule)

    /// Show a toast, top right, for TOAST_MS. couchd's own come here too.
    pub fn toast(&mut self, text: &str, level: &str, icon: Option<&str>) {
        if level == "error" {
            // The error screen says what went wrong, so a failed launch needn't say it again.
            // A command's reply can beat the state patch that sets the error: wait a moment.
            let (text, icon) = (text.to_string(), icon.map(str::to_string));
            Timer::single_shot(Duration::from_millis(250), move || {
                with_app(|app| {
                    if state::at(&app.state, "apps.error").is_null() {
                        app.show_toast(&text, "error", icon.as_deref());
                    }
                });
            });
            return;
        }
        self.show_toast(text, level, icon);
    }

    fn show_toast(&mut self, text: &str, level: &str, icon: Option<&str>) {
        self.sounds.play(self, if level == "error" { "error" } else { "toast" });
        let icon = toast_icon(text, level, icon);
        // couchd's "Sound now on X" gets a bold X, as in the design.
        let (text, em) = match text.strip_prefix("Sound now on ") {
            Some(rest) if level == "info" => ("Sound now on".to_string(), rest.to_string()),
            _ => (text.to_string(), String::new()),
        };
        let id = self.next_toast;
        self.next_toast += 1;
        self.toasts.insert(
            0,
            ToastItem { text: text.into(), em: em.into(), icon: icon.into(), error: level == "error", gone: false },
        );
        self.toast_ids.insert(0, id);
        while self.toasts.row_count() > MAX_TOASTS {
            self.toasts.remove(self.toasts.row_count() - 1);
            self.toast_ids.pop();
        }
        Timer::single_shot(Duration::from_millis(TOAST_MS), move || {
            with_app(|app| app.drop_toast(id));
        });
    }

    /// Fade it out, then remove it.
    fn drop_toast(&mut self, id: u64) {
        let Some(i) = self.toast_ids.iter().position(|&t| t == id) else { return };
        if let Some(mut row) = self.toasts.row_data(i) {
            row.gone = true;
            self.toasts.set_row_data(i, row);
        }
        let fade = Duration::from_millis(if self.reduced_motion() { 0 } else { 200 }); // dur-quick
        Timer::single_shot(fade, move || {
            with_app(|app| {
                if let Some(i) = app.toast_ids.iter().position(|&t| t == id) {
                    app.toast_ids.remove(i);
                    app.toasts.remove(i);
                }
            });
        });
    }

    // ---- Theme: evening colours and reduced motion (settings.js, theme.js)

    pub fn evening(&self) -> bool {
        match self.setting("display.evening").as_str() {
            Some("always") => true,
            Some("off") => false,
            _ => match state::bool_at(&self.state, "evening.dark") {
                Some(dark) => dark,
                None => {
                    use chrono::Timelike;
                    let hour = chrono::Local::now().hour();
                    !(EVENING_ENDS..EVENING_STARTS).contains(&hour)
                }
            },
        }
    }

    /// Nothing that way: a small nudge of what's focused, so the press doesn't feel lost
    /// (ui/tokens.slint Bump). Out for half of dur-quick, back for the other half.
    fn bump(&mut self, dir: Dir) {
        if self.reduced_motion() {
            return;
        }
        let (x, y) = match dir {
            Dir::Up => (0.0, -BUMP),
            Dir::Down => (0.0, BUMP),
            Dir::Left => (-BUMP, 0.0),
            Dir::Right => (BUMP, 0.0),
        };
        let bump = self.ui.global::<Bump>();
        bump.set_x(x);
        bump.set_y(y);
        let ui = self.ui.as_weak();
        self.bump_timer.start(TimerMode::SingleShot, Duration::from_millis(100), move || {
            if let Some(ui) = ui.upgrade() {
                ui.global::<Bump>().set_x(0.0);
                ui.global::<Bump>().set_y(0.0);
            }
        });
    }

    pub fn reduced_motion(&self) -> bool {
        self.setting("display.motion").as_str() == Some("reduced")
    }

    fn apply_theme(&mut self) {
        let theme = self.ui.global::<Theme>();
        let evening = self.evening();
        if theme.get_evening() != evening {
            theme.set_evening(evening);
        }
        let reduced = self.reduced_motion();
        if theme.get_reduced() != reduced {
            theme.set_reduced(reduced);
        }
    }

    // ---- First frame: couchd ends the boot splash after it.

    fn frame_drawn(&mut self, ms: u64) {
        for id in std::mem::take(&mut self.draw_checks) {
            self.command("ui.drawn", json!({"id": id}), None);
        }
        if self.first_frame.drawn_ms.is_none() {
            self.first_frame.drawn_ms = Some(ms);
            self.send_first_frame();
        }
    }

    fn send_first_frame(&mut self) {
        let Some(at) = self.first_frame.drawn_ms else { return };
        if self.first_frame.sent || !self.connected {
            return;
        }
        self.first_frame.sent = true;
        self.command(
            "ui.first_frame",
            json!({"at": at}),
            Some(Box::new(|app, result| {
                if result.is_err() {
                    app.first_frame.sent = false; // couchd went away mid-send: again when it's back
                }
            })),
        );
    }
}

/// Which icon a toast gets (as ui/js/overlay-view.js's toastIcon).
pub fn toast_icon(text: &str, level: &str, icon: Option<&str>) -> &'static str {
    const ICONS: [&str; 6] = ["speaker", "bluetooth", "check", "info", "update", "muted"];
    if let Some(i) = icon.and_then(|i| ICONS.iter().find(|&&n| n == i)) {
        return i;
    }
    if level != "info" {
        return "info";
    }
    if text.starts_with("Sound now on ") || text.starts_with("Sound is on ") { "speaker" } else { "check" }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn toast_icons() {
        assert_eq!(toast_icon("Sound now on TV speakers", "info", None), "speaker");
        assert_eq!(toast_icon("Paired", "info", None), "check");
        assert_eq!(toast_icon("Oops", "error", None), "info");
        assert_eq!(toast_icon("Speaker connected", "info", Some("bluetooth")), "bluetooth");
        assert_eq!(toast_icon("x", "info", Some("nonsense")), "check");
    }
}
