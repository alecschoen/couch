//! Settings: categories on the left, the chosen one's settings on
//! the right. Values come from couchd (state section `settings`, written with settings.set)
//! and redraw when they change anywhere.
//!
//! Moving onto a category shows it; OK or Right goes into it, Back comes out again. Up and
//! Down in the panel stop at its first and last rows. Rows that pick from a range (Dim to
//! clock after, Volume limit) change with Left and Right, or OK to step forward.
//!
//!   Display > Apps on Home   the cards Home shows, a switch each (screens/home_apps.rs)
//!   Network > Plex    link Plex for Continue watching on Home (screens/plex_settings.rs)
//!   Sound > Output    a sheet of the outputs; OK on one moves the sound there
//!   Remote > Test     OK, then every key shows in the box (couchd's colour keys stand down
//!                     meanwhile: remote.raw_keys); Back twice, Home, or 10 s idle stops it
//!   Power             Restart and Turn off want a second OK within 4 s
//!
//! Focus works as focus.js does here: coming back into the panel (or a segmented control)
//! returns to where it was, unless the panel was redrawn while focus was elsewhere; the
//! first time in, it starts on the panel's first selected segment, else its first control.
//! Redraws (a setting changed, a state patch it shows) build the rows again, so fills and
//! switches don't animate then, as with the web's replace().

mod text;

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::{Rc, Weak};
use std::time::{Duration, Instant};

use serde_json::{Value, json};
use slint::{ComponentHandle, Model, ModelRc, SharedString, Timer, TimerMode, VecModel};

use crate::app::{App, Dir, Key, Outcome, Screen, with_app};
use crate::state;
use crate::{SettingsData, SettingsRow, SheetRow, Theme};

const CATEGORIES: [&str; 7] = ["display", "sound", "network", "remote", "updates", "about", "power"];

/// Live state sections each category shows; a patch to one of them redraws it. Settings and
/// the sun (`evening`) redraw whatever is shown, as settings.js's change events do.
fn uses(category: &str) -> &'static [&'static str] {
    match category {
        "display" => &["settings", "evening"],
        "sound" => &["settings", "evening", "audio"],
        "network" => &["settings", "evening", "wifi", "plex"],
        "remote" => &["settings", "evening", "remote"],
        "updates" => &["settings", "evening", "system", "updates"],
        "about" => &["settings", "evening", "tv", "services", "system"],
        _ => &["settings", "evening"],
    }
}

const TEST_IDLE: Duration = Duration::from_secs(10); // the remote test stops after this long idle
const DOUBLE_BACK: Duration = Duration::from_millis(1500);
const CONFIRM: Duration = Duration::from_secs(4); // Restart and Turn off want a second OK within this

#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
enum Kind {
    #[default]
    Readout,
    Link,
    Toggle,
    Segments,
    Stepper,
    Remote,
    Pill,
    Power,
}

impl Kind {
    fn name(self) -> &'static str {
        match self {
            Kind::Readout => "readout",
            Kind::Link => "link",
            Kind::Toggle => "toggle",
            Kind::Segments => "segments",
            Kind::Stepper => "stepper",
            Kind::Remote => "remote",
            Kind::Pill => "pill",
            Kind::Power => "power",
        }
    }
    /// A .setting line (they share hairlines).
    fn is_line(self) -> bool {
        matches!(self, Kind::Readout | Kind::Link | Kind::Toggle | Kind::Segments | Kind::Stepper)
    }
}

/// One row of the panel, as built for the current category.
#[derive(Default, Clone)]
struct Item {
    /// Focus key (focus.js data-focus-key): focus stays on it across redraws.
    key: &'static str,
    kind: Kind,
    label: String,
    hint: String,
    value: String,
    on: bool,
    /// The user setting it changes.
    setting: &'static str,
    current: Value,
    choices: Vec<(Value, &'static str)>,
    /// Stepper: its values, and whether OK wraps from the last to the first.
    values: Vec<Value>,
    wrap: bool,
    top: f32,
    em: String,
    sub: String,
}

impl Item {
    fn slots(&self) -> usize {
        match self.kind {
            Kind::Readout => 0,
            Kind::Segments => self.choices.len(),
            _ => 1,
        }
    }
    fn selected(&self) -> Option<usize> {
        self.choices.iter().position(|(v, _)| text::same(v, &self.current))
    }
    fn index(&self) -> Option<usize> {
        self.values.iter().position(|v| text::same(v, &self.current))
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Focus {
    Tab,
    Panel { key: &'static str, slot: usize },
}

struct Testing {
    timer: Timer,
    last_back: Option<Instant>,
    last_key: Option<(String, String)>, // name, hint
}

struct Sheet {
    outputs: Vec<(String, bool)>, // id, available
    focus: Option<usize>,
}

struct Inner {
    me: Weak<RefCell<Inner>>,
    current: usize,
    focus: Focus,
    items: Vec<Item>,
    rows: Rc<VecModel<SettingsRow>>,
    /// Where focus was in the panel, and in each segmented control (focus.js groups).
    panel_memory: Option<Focus>,
    segment_memory: HashMap<&'static str, usize>,
    info: Option<Value>,
    testing: Option<Testing>,
    confirming: Option<(&'static str, Timer)>,
    sheet: Option<Sheet>,
    sheet_rows: Rc<VecModel<SheetRow>>,
    hz: Option<u32>,
    frames: Vec<f64>,
    measure_start: Instant,
    /// On top (between show and hide): replies and timers redraw only then. (During
    /// state() the screen is out of the stack, so App::current_name can't tell.)
    shown: bool,
}

pub struct Settings {
    inner: Rc<RefCell<Inner>>,
}

impl Settings {
    pub fn new(params: &Value, app: &mut App) -> Self {
        let category = params.get("category").and_then(Value::as_str);
        let current = CATEGORIES.iter().position(|&c| Some(c) == category).unwrap_or(0);
        let inner = Rc::new_cyclic(|me| {
            RefCell::new(Inner {
                me: me.clone(),
                current,
                focus: Focus::Tab,
                items: Vec::new(),
                rows: Rc::new(VecModel::default()),
                panel_memory: None,
                segment_memory: HashMap::new(),
                info: None,
                testing: None,
                confirming: None,
                sheet: None,
                sheet_rows: Rc::new(VecModel::default()),
                hz: None,
                frames: Vec::new(),
                measure_start: Instant::now(),
                shown: false,
            })
        });
        {
            let mut s = inner.borrow_mut();
            s.fetch_info_if_needed(app);
            s.start_measuring(app);
        }
        Settings { inner }
    }
}

impl Screen for Settings {
    fn name(&self) -> &'static str {
        "settings"
    }

    fn palette(&self, _app: &App) -> String {
        "settings".into()
    }

    fn show(&mut self, app: &mut App) {
        let mut s = self.inner.borrow_mut();
        let data = app.ui.global::<SettingsData>();
        data.set_rows(s.rows.clone().into());
        data.set_sheet_rows(s.sheet_rows.clone().into());
        data.set_sheet_open(false);
        s.shown = true;
        s.draw(app);
    }

    fn hide(&mut self, app: &mut App) {
        let mut s = self.inner.borrow_mut();
        s.shown = false;
        s.stop_testing(app);
        s.cancel_confirm(app);
        if s.sheet.take().is_some() {
            app.ui.global::<SettingsData>().set_sheet_open(false);
        }
    }

    fn destroy(&mut self, app: &mut App) {
        self.hide(app);
        app.ui.global::<SettingsData>().set_measuring(false);
    }

    fn state(&mut self, app: &mut App, patch: Option<&Value>) {
        let mut s = self.inner.borrow_mut();
        let category = CATEGORIES[s.current];
        let touched = match patch.and_then(Value::as_object) {
            Some(p) => p.keys().any(|k| uses(category).contains(&k.as_str())),
            None => true,
        };
        if touched && s.shown {
            s.draw(app);
        }
    }

    fn key(&mut self, key: &Key, app: &mut App) -> Outcome {
        let mut s = self.inner.borrow_mut();
        let outcome = s.key(key, app);
        s.after_focus(app);
        s.render(app);
        outcome
    }
}

impl Inner {
    fn category(&self) -> &'static str {
        CATEGORIES[self.current]
    }

    fn ui(app: &App) -> SettingsData<'_> {
        app.ui.global::<SettingsData>()
    }

    // ---- Keys

    fn key(&mut self, key: &Key, app: &mut App) -> Outcome {
        if self.sheet.is_some() {
            return self.sheet_key(key, app);
        }
        if self.testing.is_some() {
            if key.is("home") {
                self.stop_testing(app);
                return Outcome::No;
            }
            self.test_key(key, app);
            return Outcome::Taken;
        }
        match self.focus {
            Focus::Tab => self.tab_key(key, app),
            Focus::Panel { key: k, slot } => self.panel_key(key, k, slot, app),
        }
    }

    fn tab_key(&mut self, key: &Key, app: &mut App) -> Outcome {
        match (key.arrow(), key.key.as_str()) {
            (Some(Dir::Up), _) if self.current > 0 => {
                self.show_category(self.current - 1, app);
                Outcome::Moved
            }
            (Some(Dir::Down), _) if self.current + 1 < CATEGORIES.len() => {
                self.show_category(self.current + 1, app);
                Outcome::Moved
            }
            (Some(Dir::Right), _) => match self.panel_entry(true) {
                Some(f) => {
                    self.focus_panel(f);
                    Outcome::Moved
                }
                None => Outcome::Edge,
            },
            (Some(_), _) => Outcome::Edge,
            (None, "select") => {
                if let Some(f) = self.panel_entry(false) {
                    self.focus_panel(f);
                }
                Outcome::Taken
            }
            _ => Outcome::No,
        }
    }

    fn panel_key(&mut self, key: &Key, k: &'static str, slot: usize, app: &mut App) -> Outcome {
        let Some(i) = self.items.iter().position(|it| it.key == k) else { return Outcome::No };
        let item = self.items[i].clone();
        match (key.arrow(), key.key.as_str()) {
            (Some(d @ (Dir::Left | Dir::Right)), _) if item.kind == Kind::Stepper => {
                self.step(&item, if d == Dir::Right { 1 } else { -1 }, app);
                Outcome::Moved
            }
            (Some(d @ (Dir::Up | Dir::Down)), _) => {
                let next = if d == Dir::Down {
                    self.items.iter().skip(i + 1).find(|it| it.slots() > 0)
                } else {
                    self.items[..i].iter().rev().find(|it| it.slots() > 0)
                };
                match next {
                    Some(n) => {
                        let slot = if n.kind == Kind::Segments {
                            self.segment_memory.get(n.key).copied().unwrap_or(0)
                        } else {
                            0
                        };
                        self.focus_panel(Focus::Panel { key: n.key, slot });
                        Outcome::Moved
                    }
                    None => Outcome::Edge,
                }
            }
            (Some(Dir::Left), _) => {
                if item.kind == Kind::Segments && slot > 0 {
                    self.focus_panel(Focus::Panel { key: k, slot: slot - 1 });
                } else {
                    self.focus = Focus::Tab;
                }
                Outcome::Moved
            }
            (Some(Dir::Right), _) => {
                if item.kind == Kind::Segments && slot + 1 < item.slots() {
                    self.focus_panel(Focus::Panel { key: k, slot: slot + 1 });
                    Outcome::Moved
                } else {
                    Outcome::Edge
                }
            }
            (None, "select") => {
                self.activate(&item, slot, app);
                Outcome::Taken
            }
            (None, "back") => {
                self.focus = Focus::Tab;
                Outcome::Moved
            }
            _ => Outcome::No,
        }
    }

    /// Where focus goes into the panel: back where it was (Right, from the categories), or
    /// the panel's first selected segment, else its first control.
    fn panel_entry(&self, remembered: bool) -> Option<Focus> {
        if remembered
            && let Some(Focus::Panel { key, slot }) = self.panel_memory
            && self.items.iter().any(|it| it.key == key && slot < it.slots())
        {
            return self.panel_memory;
        }
        if let Some((it, sel)) = self
            .items
            .iter()
            .find_map(|it| (it.kind == Kind::Segments).then(|| it.selected().map(|s| (it, s))).flatten())
        {
            return Some(Focus::Panel { key: it.key, slot: sel });
        }
        self.items.iter().find(|it| it.slots() > 0).map(|it| Focus::Panel { key: it.key, slot: 0 })
    }

    fn focus_panel(&mut self, f: Focus) {
        self.focus = f;
        self.note_focus();
    }

    fn note_focus(&mut self) {
        if let Focus::Panel { key, slot } = self.focus {
            self.panel_memory = Some(self.focus);
            if self.items.iter().any(|it| it.key == key && it.kind == Kind::Segments) {
                self.segment_memory.insert(key, slot);
            }
        }
    }

    /// Leaving the power buttons mustn't leave a confirmation armed.
    fn after_focus(&mut self, app: &mut App) {
        let on_power = matches!(self.focus, Focus::Panel { key, .. } if key.starts_with("power:"));
        if self.confirming.is_some() && !on_power {
            self.cancel_confirm(app);
        }
    }

    fn activate(&mut self, item: &Item, slot: usize, app: &mut App) {
        match item.kind {
            Kind::Toggle => {
                let on = !item.on;
                self.change(item.setting, Value::Bool(on), app);
            }
            Kind::Segments => {
                if let Some((v, _)) = item.choices.get(slot)
                    && !text::same(v, &item.current)
                {
                    self.change(item.setting, v.clone(), app);
                }
            }
            Kind::Stepper => {
                if item.wrap {
                    let i = item.index().map(|i| i as isize).unwrap_or(-1);
                    let next = item.values[((i + 1) as usize) % item.values.len()].clone();
                    self.change(item.setting, next, app);
                } else {
                    self.step(item, 1, app);
                }
            }
            Kind::Link if item.key == "home-apps" => app.go("home-apps", json!({})),
            Kind::Link if item.key == "plex" => app.go("plex-settings", json!({})),
            Kind::Link => self.open_outputs(app),
            Kind::Remote => {
                if self.testing.is_none() {
                    self.start_testing(app);
                }
            }
            Kind::Pill => {
                if !text::running(state::at(&app.state, "updates")) {
                    app.command_or_toast("updates.check", json!({}));
                }
            }
            Kind::Power => self.power(item.key, app),
            Kind::Readout => {}
        }
    }

    fn step(&mut self, item: &Item, d: isize, app: &mut App) {
        let i = item.index().unwrap_or(0) as isize;
        let next = &item.values[(i + d).clamp(0, item.values.len() as isize - 1) as usize];
        if !text::same(next, &item.current) {
            self.change(item.setting, next.clone(), app);
        }
    }

    /// Change a setting: shown at once; if couchd refuses, it goes back, with an error toast.
    fn change(&mut self, key: &'static str, value: Value, app: &mut App) {
        let before = state::at(&app.state, "settings").get(key).cloned();
        write_setting(app, key, Some(value.clone()));
        self.draw(app);
        let me = self.me.clone();
        app.command(
            "settings.set",
            json!({"key": key, "value": value}),
            Some(Box::new(move |app, result| {
                if let Err(e) = result {
                    write_setting(app, key, before);
                    app.toast(&e, "error", None);
                    if let Some(s) = me.upgrade() {
                        let mut s = s.borrow_mut();
                        if s.shown {
                            s.draw(app);
                        }
                    }
                }
            })),
        );
    }

    // ---- Categories

    fn show_category(&mut self, i: usize, app: &mut App) {
        if i == self.current {
            return;
        }
        self.stop_testing(app);
        self.cancel_confirm(app);
        self.current = i;
        self.focus = Focus::Tab;
        self.fetch_info_if_needed(app);
        self.draw(app);
    }

    fn fetch_info_if_needed(&mut self, app: &mut App) {
        if !matches!(self.category(), "about" | "network") {
            return;
        }
        let me = self.me.clone();
        app.command(
            "system.info",
            json!({}),
            Some(Box::new(move |app, result| {
                let Some(s) = me.upgrade() else { return };
                let mut s = s.borrow_mut();
                match result {
                    Ok(info) => s.info = Some(info),
                    Err(_) => {
                        if s.info.is_none() {
                            s.info = Some(json!({"name": "Couch", "device": "Unknown"}));
                        }
                    }
                }
                if matches!(s.category(), "about" | "network") && s.shown {
                    s.draw(app);
                }
            })),
        );
    }

    // ---- Drawing

    /// Build the rows again (the web's draw(): its panel is replaced, so focus memory in it
    /// lasts only where focus is).
    fn draw(&mut self, app: &App) {
        match self.focus {
            Focus::Panel { key, .. } => {
                self.panel_memory = Some(self.focus);
                self.segment_memory.retain(|k, _| *k == key);
            }
            Focus::Tab => {
                self.panel_memory = None;
                self.segment_memory.clear();
            }
        }
        let old_index = match self.focus {
            Focus::Panel { key, .. } => self.items.iter().position(|it| it.key == key),
            Focus::Tab => None,
        };
        self.items = self.build(app);
        // Focus follows its key; if that row went, the nearest one with a control.
        if let Focus::Panel { key, slot } = self.focus {
            match self.items.iter().find(|it| it.key == key && it.slots() > 0) {
                Some(it) => self.focus = Focus::Panel { key, slot: slot.min(it.slots() - 1) },
                None => {
                    let near = old_index.unwrap_or(0);
                    let pick = self
                        .items
                        .iter()
                        .enumerate()
                        .filter(|(_, it)| it.slots() > 0)
                        .min_by_key(|(i, _)| i.abs_diff(near))
                        .map(|(_, it)| it.key);
                    self.focus = pick.map(|key| Focus::Panel { key, slot: 0 }).unwrap_or(Focus::Tab);
                }
            }
            self.note_focus();
        }
        let rows: Vec<SettingsRow> = (0..self.items.len()).map(|i| self.row(i)).collect();
        self.rows.set_vec(rows);
        self.render(app);
    }

    /// Focus only: the rows in place, so their fills fade.
    fn render(&self, app: &App) {
        for i in 0..self.items.len() {
            let Some(mut row) = self.rows.row_data(i) else { continue };
            let (focus, hairline) = (self.focus_of(i), self.hairline(i));
            if row.focus != focus || row.hairline != hairline {
                row.focus = focus;
                row.hairline = hairline;
                self.rows.set_row_data(i, row);
            }
        }
        let data = Self::ui(app);
        data.set_tab(self.current as i32);
        data.set_tab_focus(if self.focus == Focus::Tab && self.sheet.is_none() { self.current as i32 } else { -1 });
        data.set_sheet_focus(self.sheet.as_ref().and_then(|s| s.focus).map(|f| f as i32).unwrap_or(-1));
    }

    fn focus_of(&self, i: usize) -> i32 {
        match self.focus {
            Focus::Panel { key, slot } if self.items[i].key == key && self.sheet.is_none() => slot as i32,
            _ => -1,
        }
    }

    /// The divider between two lines, hidden next to a focused line.
    fn hairline(&self, i: usize) -> bool {
        if i == 0 || !self.items[i].kind.is_line() || !self.items[i - 1].kind.is_line() || self.items[i].top > 0.0 {
            return false;
        }
        let filled = |j: usize| self.items[j].kind != Kind::Segments && self.focus_of(j) >= 0;
        !filled(i) && !filled(i - 1)
    }

    fn row(&self, i: usize) -> SettingsRow {
        let it = &self.items[i];
        let choices: Vec<SharedString> = it.choices.iter().map(|(_, l)| SharedString::from(*l)).collect();
        let index = it.index();
        SettingsRow {
            kind: it.kind.name().into(),
            label: it.label.as_str().into(),
            hint: it.hint.as_str().into(),
            value: it.value.as_str().into(),
            on: it.on,
            choices: ModelRc::new(VecModel::from(choices)),
            selected: it.selected().map(|s| s as i32).unwrap_or(-1),
            focus: self.focus_of(i),
            at_start: index.is_none_or(|i| i == 0),
            at_end: index.is_some_and(|i| i + 1 >= it.values.len()),
            hairline: self.hairline(i),
            top: it.top,
            em: it.em.as_str().into(),
            sub: it.sub.as_str().into(),
        }
    }

    fn build(&self, app: &App) -> Vec<Item> {
        let s = &app.state;
        let setting = |key: &str| -> Value { app.setting(key).clone() };
        let readout =
            |label: &str, value: String, hint: String| Item { label: label.into(), value, hint, ..Item::default() };
        let toggle = |key: &'static str, label: &str, hint: &str| Item {
            key,
            kind: Kind::Toggle,
            label: label.into(),
            hint: hint.into(),
            on: setting(key).as_bool().unwrap_or(false),
            setting: key,
            ..Item::default()
        };
        let segments = |key: &'static str, setting_key: &'static str, label: &str, hint: String| Item {
            key,
            kind: Kind::Segments,
            label: label.into(),
            hint,
            setting: setting_key,
            current: setting(setting_key),
            choices: text::choices(setting_key),
            ..Item::default()
        };
        let stepper = |key: &'static str, label: &str, values: Vec<Value>, wrap: bool, format: fn(&Value) -> String| {
            let current = setting(key);
            Item {
                key,
                kind: Kind::Stepper,
                label: label.into(),
                value: format(&current),
                setting: key,
                current,
                values,
                wrap,
                ..Item::default()
            }
        };
        let now = chrono::Local::now();
        let updates = state::at(s, "updates");
        match self.category() {
            "display" => vec![
                readout("Resolution", self.resolution(app), String::new()),
                segments("evening", "display.evening", "Evening colors", text::sun_hint(s)),
                stepper(
                    "display.dim_after",
                    "Dim to clock after",
                    text::choices("display.dim_after").into_iter().map(|(v, _)| v).collect(),
                    true,
                    text::dim_label,
                ),
                toggle(
                    "tv.off_when_idle",
                    "Turn the TV off when idle",
                    "After 2 hours on Couch with no buttons pressed. Never while something plays.",
                ),
                // On Home and the idle clock (#82). No hint: the panel has no room for one.
                toggle("display.weather", "Weather", ""),
                // The cards on Home (#89): a screen of its own (screens/home_apps.rs).
                Item {
                    key: "home-apps",
                    kind: Kind::Link,
                    label: "Apps on Home".into(),
                    value: crate::screens::home_apps::summary(s),
                    ..Item::default()
                },
            ],
            "sound" => {
                let current = state::str_at(s, "audio.current");
                let name = state::list_at(s, "audio.outputs")
                    .iter()
                    .find(|o| current.is_some() && o.get("id").and_then(Value::as_str) == current)
                    .and_then(|o| o.get("name").and_then(Value::as_str))
                    .unwrap_or("None");
                vec![
                    Item {
                        key: "output",
                        kind: Kind::Link,
                        label: "Output".into(),
                        value: format!("{name} ›"),
                        ..Item::default()
                    },
                    toggle("bluetooth.auto_reconnect", "Reconnect Bluetooth at start", ""),
                    toggle(
                        "audio.tv_remote_volume",
                        "TV remote controls volume",
                        "For the headphone jack and Bluetooth. Works on most TVs.",
                    ),
                    segments(
                        "ui_sounds",
                        "audio.ui_sounds",
                        "UI sounds",
                        "Quiet sounds when you press a button. TV only: not on the jack or Bluetooth.".into(),
                    ),
                    // No wrap: from No limit straight to 10 would turn the sound right down.
                    stepper("audio.volume_limit", "Volume limit", text::volume_values(), false, text::volume_label),
                ]
            }
            "network" => {
                let wifi_on = setting("network.wifi").as_bool().unwrap_or(false);
                let mut rows = vec![readout("Connection", text::network_label(self.info.as_ref()), String::new())];
                if state::bool_at(s, "wifi.available") == Some(false) && !wifi_on {
                    rows.push(readout("Wi-Fi", "Not available".into(), String::new()));
                } else {
                    rows.push(toggle("network.wifi", "Wi-Fi", &text::wifi_hint(s, wifi_on)));
                }
                // Plex's sign-in for Continue watching on Home (#86): a screen of its own.
                rows.push(Item {
                    key: "plex",
                    kind: Kind::Link,
                    label: "Plex".into(),
                    value: crate::plex::summary(s),
                    ..Item::default()
                });
                rows
            }
            "remote" => {
                let mut rows: Vec<Item> =
                    text::keymap(s).into_iter().map(|(b, a)| readout(&b, a, String::new())).collect();
                let (value, em, sub) = match &self.testing {
                    None => {
                        ("Press OK, then any button to see what the TV sends".to_string(), String::new(), String::new())
                    }
                    Some(Testing { last_key: None, .. }) => {
                        ("Listening… press any button. Back twice to stop".to_string(), String::new(), String::new())
                    }
                    Some(Testing { last_key: Some((name, hint)), .. }) => (String::new(), name.clone(), hint.clone()),
                };
                rows.push(Item {
                    key: "remote-test",
                    kind: Kind::Remote,
                    label: "Test your remote".into(),
                    value,
                    em,
                    sub,
                    on: self.testing.is_some(),
                    ..Item::default()
                });
                rows
            }
            "updates" => {
                let mut rows = vec![
                    readout("Launcher", text::launcher_version(updates), String::new()),
                    readout("Last check", text::last_check(updates, now), text::check_hint(updates)),
                    readout("System", text::system_updates(updates, now), String::new()),
                ];
                if updates.get("nightly").and_then(Value::as_bool) == Some(false) {
                    rows.push(readout(
                        "Overnight",
                        "Not turned on yet".into(),
                        "Check now still works. See Updates in system/README.md.".into(),
                    ));
                } else {
                    rows.push(toggle(
                        "updates.overnight",
                        "Install updates overnight",
                        "Only while nothing is playing",
                    ));
                }
                rows.push(Item {
                    key: "check",
                    kind: Kind::Pill,
                    label: text::check_label(updates),
                    on: true,
                    top: 15.0,
                    ..Item::default()
                });
                rows
            }
            "about" => {
                let info = |k: &str| {
                    self.info.as_ref().and_then(|i| i.get(k)).and_then(Value::as_str).unwrap_or("…").to_string()
                };
                vec![
                    readout("Name on the TV", info("name"), String::new()),
                    readout("Device", info("device"), String::new()),
                    readout("Network", text::network_label(self.info.as_ref()), String::new()),
                    readout("TV connection", text::tv_connection(s), String::new()),
                ]
            }
            _ => {
                let power = |key: &'static str, label: &str, primary: bool, top: f32| {
                    let pending = self.confirming.as_ref().is_some_and(|(a, _)| *a == key);
                    Item {
                        key,
                        kind: Kind::Power,
                        label: if pending {
                            format!("Press OK again to {}", label.to_lowercase())
                        } else {
                            label.into()
                        },
                        on: primary,
                        top,
                        ..Item::default()
                    }
                };
                let mut tv =
                    toggle("tv.standby_on_poweroff", "Turn the TV off too", "When Couch turns off. Not on a restart.");
                tv.top = 24.0;
                vec![
                    power("power:restart", "Restart", true, 12.0),
                    power("power:power_off", "Turn off", false, 18.0),
                    tv,
                ]
            }
        }
    }

    // ---- Display: the resolution, and the refresh rate from a second of frames

    fn resolution(&self, app: &App) -> String {
        let size = app.ui.window().size();
        match self.hz {
            Some(hz) => format!("{} × {} · {hz} Hz", size.width, size.height),
            None => format!("{} × {}", size.width, size.height),
        }
    }

    fn start_measuring(&mut self, app: &App) {
        self.frames.clear();
        self.measure_start = Instant::now();
        let me = self.me.clone();
        let data = Self::ui(app);
        data.on_frame(move || {
            let Some(s) = me.upgrade() else { return };
            let Ok(mut s) = s.try_borrow_mut() else { return };
            let t = s.measure_start.elapsed().as_secs_f64() * 1000.0;
            s.frames.push(t);
            if s.frames.len() > 60 {
                s.hz = text::refresh_rate(&s.frames);
                let me = s.me.clone();
                Timer::single_shot(Duration::ZERO, move || {
                    with_app(|app| {
                        Self::ui(app).set_measuring(false);
                        if let Some(s) = me.upgrade() {
                            let mut s = s.borrow_mut();
                            if s.category() == "display" && s.shown {
                                s.draw(app);
                            }
                        }
                    });
                });
            }
        });
        data.set_measuring(true);
    }

    // ---- Sound: pick an output

    fn open_outputs(&mut self, app: &mut App) {
        let current = state::str_at(&app.state, "audio.current").map(str::to_string);
        let list = state::list_at(&app.state, "audio.outputs");
        let mut outputs = Vec::new();
        let mut rows = Vec::new();
        for o in list {
            let id = o.get("id").and_then(Value::as_str).unwrap_or("").to_string();
            let available = o.get("available").and_then(Value::as_bool).unwrap_or(true);
            let here = Some(&id) == current.as_ref();
            rows.push(SheetRow {
                title: o.get("name").and_then(Value::as_str).unwrap_or("").into(),
                sub: if !available {
                    "Not available".into()
                } else if here {
                    "Sound plays here".into()
                } else {
                    SharedString::new()
                },
                selected: here,
                disabled: !available,
            });
            outputs.push((id, available));
        }
        let focus = outputs
            .iter()
            .position(|(id, ok)| *ok && Some(id) == current.as_ref())
            .or_else(|| outputs.iter().position(|(_, ok)| *ok));
        self.sheet_rows.set_vec(rows);
        self.sheet = Some(Sheet { outputs, focus });
        Self::ui(app).set_sheet_open(true);
    }

    fn close_sheet(&mut self, app: &App) {
        self.sheet = None;
        Self::ui(app).set_sheet_open(false);
    }

    fn sheet_key(&mut self, key: &Key, app: &mut App) -> Outcome {
        let Some(sheet) = self.sheet.as_mut() else { return Outcome::No };
        match (key.arrow(), key.key.as_str()) {
            (Some(d @ (Dir::Up | Dir::Down)), _) => {
                let Some(f) = sheet.focus else { return Outcome::Edge };
                let next = if d == Dir::Down {
                    (f + 1..sheet.outputs.len()).find(|&i| sheet.outputs[i].1)
                } else {
                    (0..f).rev().find(|&i| sheet.outputs[i].1)
                };
                match next {
                    Some(n) => {
                        sheet.focus = Some(n);
                        Outcome::Moved
                    }
                    None => Outcome::Edge,
                }
            }
            (Some(_), _) => Outcome::Edge,
            (None, "back") => {
                self.close_sheet(app);
                Outcome::Taken
            }
            (None, "select") => {
                let chosen = sheet.focus.map(|f| sheet.outputs[f].0.clone());
                if let Some(id) = chosen {
                    self.close_sheet(app);
                    if Some(id.as_str()) != state::str_at(&app.state, "audio.current") {
                        app.command_or_toast("audio.set_output", json!({"output": id}));
                    }
                }
                Outcome::Taken
            }
            _ => Outcome::No,
        }
    }

    // ---- Remote: the test box

    // While the test listens, couchd's colour keys stand down (remote.raw_keys), so Red shows up
    // here instead of moving the sound.
    fn claim_keys(app: &mut App, on: bool) {
        app.command("remote.raw_keys", json!({"on": on}), Some(Box::new(|_, _| {})));
    }

    fn start_testing(&mut self, app: &mut App) {
        self.testing = Some(Testing { timer: Timer::default(), last_back: None, last_key: None });
        Self::claim_keys(app, true);
        self.bump_test_timer();
        self.draw(app);
    }

    fn stop_testing(&mut self, app: &mut App) {
        if self.testing.take().is_none() {
            return;
        }
        Self::claim_keys(app, false);
        if self.category() == "remote" {
            self.draw(app);
        }
    }

    fn bump_test_timer(&mut self) {
        let me = self.me.clone();
        if let Some(t) = &self.testing {
            t.timer.start(TimerMode::SingleShot, TEST_IDLE, move || {
                with_app(|app| {
                    if let Some(s) = me.upgrade() {
                        s.borrow_mut().stop_testing(app);
                    }
                });
            });
        }
    }

    fn test_key(&mut self, key: &Key, app: &mut App) {
        let now = Instant::now();
        let Some(t) = self.testing.as_mut() else { return };
        if key.is("back") && t.last_back.is_some_and(|b| now - b < DOUBLE_BACK) {
            self.stop_testing(app);
            return;
        }
        t.last_back = key.is("back").then_some(now);
        let how = if key.hold {
            " (held)"
        } else if key.repeat {
            " (repeat)"
        } else {
            ""
        };
        let name = format!("{}{how}", text::key_name(&key.key));
        let hint = if name.starts_with("Back") { "Back again to stop" } else { "Back twice to stop" };
        t.last_key = Some((name, hint.into()));
        self.bump_test_timer();
        self.draw(app);
    }

    // ---- Power

    fn power(&mut self, action: &'static str, app: &mut App) {
        let pending = self.confirming.as_ref().is_some_and(|(a, _)| *a == action);
        self.cancel_confirm(app);
        if pending {
            let command = if action == "power:restart" { "system.restart" } else { "system.power_off" };
            app.command_or_toast(command, json!({}));
            return;
        }
        let me = self.me.clone();
        let timer = Timer::default();
        timer.start(TimerMode::SingleShot, CONFIRM, move || {
            with_app(|app| {
                if let Some(s) = me.upgrade() {
                    s.borrow_mut().cancel_confirm(app);
                }
            });
        });
        self.confirming = Some((action, timer));
        self.draw(app);
    }

    fn cancel_confirm(&mut self, app: &App) {
        if self.confirming.take().is_none() {
            return;
        }
        if self.category() == "power" {
            self.draw(app);
        }
    }
}

/// Write a setting into the local state (None: back to its default) and apply the theme,
/// as settings.js shows a change at once.
fn write_setting(app: &mut App, key: &str, value: Option<Value>) {
    if let Some(settings) = app.state.get_mut("settings").and_then(Value::as_object_mut) {
        match value {
            Some(v) => {
                settings.insert(key.to_string(), v);
            }
            None => {
                settings.remove(key);
            }
        }
    }
    let theme = app.ui.global::<Theme>();
    theme.set_evening(app.evening());
    theme.set_reduced(app.reduced_motion());
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seg(key: &'static str, current: &str) -> Item {
        Item {
            key,
            kind: Kind::Segments,
            current: current.into(),
            choices: text::choices("display.evening"),
            ..Item::default()
        }
    }

    fn inner(items: Vec<Item>) -> Inner {
        Inner {
            me: Weak::new(),
            current: 0,
            focus: Focus::Tab,
            items,
            rows: Rc::new(VecModel::default()),
            panel_memory: None,
            segment_memory: HashMap::new(),
            info: None,
            testing: None,
            confirming: None,
            sheet: None,
            sheet_rows: Rc::new(VecModel::default()),
            hz: None,
            frames: Vec::new(),
            measure_start: Instant::now(),
            shown: false,
        }
    }

    #[test]
    fn entry_prefers_selected_segment_then_memory() {
        let toggle = Item { key: "t", kind: Kind::Toggle, ..Item::default() };
        let mut s = inner(vec![Item::default(), toggle.clone(), seg("e", "always")]);
        assert_eq!(s.panel_entry(true), Some(Focus::Panel { key: "e", slot: 1 }));
        s.focus_panel(Focus::Panel { key: "t", slot: 0 });
        assert_eq!(s.panel_entry(true), Some(Focus::Panel { key: "t", slot: 0 }));
        assert_eq!(s.panel_entry(false), Some(Focus::Panel { key: "e", slot: 1 }));
        let s = inner(vec![Item::default(), toggle]);
        assert_eq!(s.panel_entry(false), Some(Focus::Panel { key: "t", slot: 0 }));
        let s = inner(vec![Item::default()]);
        assert_eq!(s.panel_entry(true), None);
    }

    #[test]
    fn hairlines_hide_next_to_focus() {
        let toggle = |key| Item { key, kind: Kind::Toggle, ..Item::default() };
        let mut s = inner(vec![Item::default(), toggle("a"), toggle("b"), Item::default()]);
        assert!(!s.hairline(0));
        assert!(s.hairline(1) && s.hairline(2) && s.hairline(3));
        s.focus = Focus::Panel { key: "a", slot: 0 };
        assert!(!s.hairline(1) && !s.hairline(2) && s.hairline(3));
    }
}
