//! Bluetooth: your devices on the left, devices nearby on the
//! right, a tip about pairing mode at the bottom.
//!
//! OK on a device does the obvious next thing:
//!   nearby                     pair it (pairing connects an audio device too)
//!   paired, not connected      connect it
//!   connected, sound elsewhere move the sound to it
//!   connected, sound here      Options: Disconnect, Forget
//! Holding OK on a paired device (a `select` key with `hold`) opens Options too. couchd's
//! "Sound now on X" toast says when sound has moved; this screen only toasts what couchd
//! doesn't (a non-audio device, Forget).
//!
//! The radio scans only while this screen is on top: couchd stops a scan after two minutes,
//! so it's asked again every minute, and told to stop when the screen is covered or closed.
//!
//! Focus moves as the web's focus.js does: the nearest row that way (scored the same, from
//! the same geometry), and coming back into a column returns to the row it had last. Focus
//! follows a device by its address, so it stays on it when it moves column (paired).

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::{Rc, Weak};
use std::time::{Duration, Instant};

use serde_json::{Value, json};
use slint::{ComponentHandle, Model, Timer, TimerMode, VecModel};

use crate::app::{App, Dir, Key, Outcome, Screen, with_app};
use crate::state;
use crate::{BluetoothData, BtRow};

const RESCAN: Duration = Duration::from_secs(60);
// After this long with nothing new nearby, say so rather than leave the column empty.
const NOTHING_NEARBY: Duration = Duration::from_secs(6);
// How long to keep a device "Connecting" while waiting for its sound to arrive. couchd waits
// 15 s for the output before it gives up.
const SOUND_WAIT: Duration = Duration::from_secs(16);

// The layout (bluetooth.slint BluetoothLayout), for focus moves and scrolling.
const COL_X: [f32; 2] = [108.0, 996.0];
const COL_W: f32 = 816.0;
const ROWS_TOP: f32 = 360.0;
const ROWS_H: f32 = 557.55;
const ROW_H: f32 = 120.65;
const ROW_STEP: f32 = 138.65;
// A column with more rows than fit: the browser shrinks each row to its smallest height, a
// whole 120px, so they sit 138 apart.
const TIGHT_ROW_H: f32 = 120.0;
const TIGHT_ROW_STEP: f32 = 138.0;
// focus.js scrolls a column so the focused row is this far inside it.
const SCROLL_MARGIN: f32 = 96.0;

/// Is this audio output that device? Same rule as couchd's _output_matches: compare the hex
/// digits only, so `bluetooth:AA:BB:…` and `bluez_output.AA_BB_….1` both match.
pub fn output_matches(output_id: &str, address: &str) -> bool {
    let hex = |s: &str| -> String { s.to_uppercase().chars().filter(|c| c.is_ascii_hexdigit()).collect() };
    let wanted = hex(address);
    !wanted.is_empty() && hex(output_id).contains(&wanted)
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Pending {
    Pair,
    Connect,
    /// Connected, waiting for its sound to move there.
    Sound,
}

#[derive(Clone, Debug)]
struct Device {
    address: String,
    name: String,
    audio: bool,
    paired: bool,
    connected: bool,
}

impl Device {
    fn from(v: &Value) -> Self {
        let flag = |k: &str| v.get(k).and_then(Value::as_bool).unwrap_or(false);
        let text = |k: &str| v.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        Device {
            address: text("address"),
            name: text("name"),
            audio: flag("audio"),
            paired: flag("paired"),
            connected: flag("connected"),
        }
    }
}

fn devices(s: &Value) -> Vec<Device> {
    state::list_at(s, "bluetooth.devices").iter().map(Device::from).collect()
}

fn find(s: &Value, address: &str) -> Option<Device> {
    devices(s).into_iter().find(|d| d.address == address)
}

fn sound_here(s: &Value, d: &Device) -> bool {
    state::str_at(s, "audio.current").is_some_and(|c| output_matches(c, &d.address))
}

/// The device's audio output, if it's up.
fn output_for(s: &Value, d: &Device) -> Option<String> {
    state::list_at(s, "audio.outputs")
        .iter()
        .find(|o| {
            o.get("kind").and_then(Value::as_str) == Some("bluetooth")
                && o.get("available").and_then(Value::as_bool) != Some(false)
                && o.get("id").and_then(Value::as_str).is_some_and(|id| output_matches(id, &d.address))
        })
        .and_then(|o| o.get("id").and_then(Value::as_str))
        .map(str::to_string)
}

fn sound_name(s: &Value) -> String {
    let current = state::at(s, "audio.current");
    state::list_at(s, "audio.outputs")
        .iter()
        .find(|o| o.get("id") == Some(current))
        .and_then(|o| o.get("name").and_then(Value::as_str))
        .unwrap_or("None")
        .to_string()
}

/// The two columns: paired devices, then the rest (audio first, otherwise as couchd sends
/// them, by name), and the row focus starts on.
fn columns(s: &Value) -> ([Vec<Device>; 2], Option<String>) {
    let all = devices(s);
    let paired: Vec<Device> = all.iter().filter(|d| d.paired).cloned().collect();
    let mut fresh: Vec<Device> = all.iter().filter(|d| !d.paired).cloned().collect();
    fresh.sort_by_key(|d| !d.audio); // stable: keeps couchd's order otherwise
    let preferred = paired
        .iter()
        .find(|d| d.connected && sound_here(s, d))
        .or_else(|| paired.iter().find(|d| d.connected))
        .or(paired.first())
        .map(|d| d.address.clone());
    let default = preferred.or_else(|| fresh.first().map(|d| d.address.clone()));
    ([paired, fresh], default)
}

// ---- focus.js geometry

#[derive(Clone, Copy, Debug, PartialEq)]
struct Rect {
    left: f32,
    top: f32,
    right: f32,
    bottom: f32,
}

/// A column's row height and spacing, with this many rows.
pub(crate) fn row_size(rows: usize) -> (f32, f32) {
    let natural = rows as f32 * ROW_STEP - (ROW_STEP - ROW_H);
    if natural > ROWS_H { (TIGHT_ROW_H, TIGHT_ROW_STEP) } else { (ROW_H, ROW_STEP) }
}

fn row_rect(col: usize, index: usize, scroll: f32, rows: usize) -> Rect {
    let (h, step) = row_size(rows);
    let top = ROWS_TOP + index as f32 * step - scroll;
    Rect { left: COL_X[col], top, right: COL_X[col] + COL_W, bottom: top + h }
}

/// focus.js scoreCandidate: lower is better; None if it isn't that way at all.
fn score(o: Rect, c: Rect, dir: Dir) -> Option<f32> {
    let horizontal = matches!(dir, Dir::Left | Dir::Right);
    let ahead = matches!(dir, Dir::Right | Dir::Down);
    let sign = if ahead { 1.0 } else { -1.0 };
    let (o_start, o_end, c_start, c_end) =
        if horizontal { (o.left, o.right, c.left, c.right) } else { (o.top, o.bottom, c.top, c.bottom) };
    let c_center = (c_start + c_end) / 2.0;
    if (c_center - if ahead { o_end } else { o_start }) * sign <= 0.0 {
        return None;
    }
    if if ahead { c_start < o_start } else { c_end > o_end } {
        return None;
    }
    let gap = (if ahead { c_start - o_end } else { o_start - c_end }).max(0.0);
    let (oc_start, oc_end, cc_start, cc_end) =
        if horizontal { (o.top, o.bottom, c.top, c.bottom) } else { (o.left, o.right, c.left, c.right) };
    let overlap = oc_end.min(cc_end) - oc_start.max(cc_start);
    let cross_gap = (-overlap).max(0.0);
    let cross_offset = ((cc_start + cc_end) / 2.0 - (oc_start + oc_end) / 2.0).abs();
    if overlap > 0.0 {
        return Some(gap + cross_offset * 0.1);
    }
    // Up and down only within a 45° cone, so Down at the end of a column doesn't jump across.
    if !horizontal && cross_gap > gap {
        return None;
    }
    Some(100_000.0 + gap + cross_gap * 2.0 + cross_offset * 0.1)
}

/// focus.js _scrollIntoView, for a column: the new scroll offset.
pub(crate) fn scroll_to_show(scroll: f32, index: usize, rows: usize) -> f32 {
    let r = row_rect(0, index, scroll, rows);
    let (top, bottom) = (ROWS_TOP, ROWS_TOP + ROWS_H);
    let mut s = scroll;
    if r.top < top + SCROLL_MARGIN {
        s -= top + SCROLL_MARGIN - r.top;
    } else if r.bottom > bottom - SCROLL_MARGIN {
        s += r.bottom - (bottom - SCROLL_MARGIN);
    }
    s.clamp(0.0, max_scroll(rows))
}

pub(crate) fn max_scroll(rows: usize) -> f32 {
    if rows == 0 {
        return 0.0;
    }
    let (h, step) = row_size(rows);
    let content = rows as f32 * step - (step - h);
    // The browser scrolls in whole px.
    (content - ROWS_H).round().max(0.0)
}

// ---- The screen

struct Sheet {
    address: String,
    name: String,
    connected: bool,
    /// 0 Disconnect (only if connected), 1 Forget
    focus: usize,
}

#[derive(Default)]
struct Inner {
    this: Weak<RefCell<Inner>>,
    /// address → what's being done to it
    pending: HashMap<String, Pending>,
    sound_timers: HashMap<String, Timer>,
    visible: bool,
    rescan: Timer,
    scan_since: Option<Instant>,
    nothing_timer: Timer,
    /// The focused row's address.
    focus: Option<String>,
    /// Where focus was last, to find the nearest row if its device goes.
    last_rect: Option<Rect>,
    /// Each column's last focused row (data-focus-group).
    group_last: [Option<String>; 2],
    scroll: [f32; 2],
    /// The addresses in each column, as drawn.
    cols: [Vec<String>; 2],
    /// Where focus starts (data-focus-default).
    default: Option<String>,
    sheet: Option<Sheet>,
    models: [Rc<VecModel<BtRow>>; 2],
}

pub struct Bluetooth {
    inner: Rc<RefCell<Inner>>,
}

impl Bluetooth {
    pub fn new(_params: &Value, _app: &mut App) -> Self {
        let inner = Rc::new_cyclic(|this| RefCell::new(Inner { this: this.clone(), ..Default::default() }));
        Bluetooth { inner }
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

/// Run `f` on the screen's state from a timer.
fn from_timer(weak: Weak<RefCell<Inner>>, f: impl Fn(&mut Inner, &mut App) + 'static) -> impl FnMut() + 'static {
    move || {
        with_app(|app| {
            if let Some(inner) = weak.upgrade() {
                f(&mut inner.borrow_mut(), app);
            }
        });
    }
}

impl Inner {
    // ---- Drawing

    fn sub_for(&self, s: &Value, d: &Device) -> &'static str {
        match self.pending.get(&d.address) {
            Some(Pending::Pair) => "Pairing",
            Some(Pending::Connect | Pending::Sound) => "Connecting",
            None if !d.paired => "OK to pair",
            None if !d.connected => "Not connected · OK to connect",
            None if !d.audio => "Connected",
            None if sound_here(s, d) => "Connected · sound plays here",
            None => "Connected · OK to play sound here",
        }
    }

    fn end_for(&self, s: &Value, d: &Device) -> &'static str {
        if self.pending.contains_key(&d.address) {
            "spinner"
        } else if d.connected && (sound_here(s, d) || !d.audio) {
            "check"
        } else {
            ""
        }
    }

    fn render(&mut self, app: &App) {
        let s = &app.state;
        let data = app.ui.global::<BluetoothData>();
        data.set_sound(format!("Sound: {}", sound_name(s)).into());
        let scanning = state::bool_at(s, "bluetooth.scanning") == Some(true);
        data.set_looking(self.visible && scanning);

        let available = state::str_at(s, "services.bluetooth") == Some("running");
        let ([paired, fresh], default) = columns(s);
        let (cols, notes) = if available {
            let waited = self.scan_since.is_some_and(|t| t.elapsed() >= NOTHING_NEARBY);
            let yours = if paired.is_empty() { "Nothing paired yet" } else { "" };
            let nearby = if fresh.is_empty() && waited { "Nothing new nearby" } else { "" };
            ([paired, fresh], [yours, nearby])
        } else {
            ([Vec::new(), Vec::new()], ["Bluetooth isn't available right now", ""])
        };
        self.default = if available { default } else { None };
        self.cols = [0, 1].map(|c| cols[c].iter().map(|d| d.address.clone()).collect());

        // The web redraws every row, so what a column remembered (an element) is gone: coming
        // back into it then goes to the nearest row, not where it was. Only the column focus
        // is in remembers afresh, below.
        self.group_last = [None, None];
        // Focus stays on its device. If that's gone, the nearest row (focus.js ensure()).
        if self.focus.as_ref().is_some_and(|f| self.position(f).is_none()) {
            self.focus = self.nearest_to_last();
        }
        if self.focus.is_none() && self.sheet.is_none() {
            self.focus = self.initial();
        }
        // The web redraws the rows and focus comes back to the new one, noted afresh (its
        // column remembers it, and it's scrolled into view).
        if self.sheet.is_none() {
            self.note_focus();
        }
        for c in 0..2 {
            self.scroll[c] = self.scroll[c].min(max_scroll(self.cols[c].len()));
        }

        for (c, devices) in cols.iter().enumerate() {
            let rows: Vec<BtRow> = devices
                .iter()
                .map(|d| BtRow {
                    key: d.address.as_str().into(),
                    title: d.name.as_str().into(),
                    sub: self.sub_for(s, d).into(),
                    end: self.end_for(s, d).into(),
                    focused: self.is_focused(&d.address),
                })
                .collect();
            let model = &self.models[c];
            let same_devices = model.row_count() == rows.len() && model.iter().zip(&rows).all(|(a, b)| a.key == b.key);
            if same_devices {
                // Only what changed: a focus fill moving fades as it does on the web.
                for (i, row) in rows.into_iter().enumerate() {
                    if model.row_data(i).as_ref() != Some(&row) {
                        model.set_row_data(i, row);
                    }
                }
            } else {
                // Rows replaced: drawn at once (components.js replace() skips transitions).
                model.set_vec(rows);
            }
        }
        data.set_yours(self.models[0].clone().into());
        data.set_nearby(self.models[1].clone().into());
        data.set_yours_note(notes[0].into());
        data.set_nearby_note(notes[1].into());
        data.set_yours_scroll(self.scroll[0]);
        data.set_nearby_scroll(self.scroll[1]);
    }

    fn is_focused(&self, address: &str) -> bool {
        self.sheet.is_none() && self.focus.as_deref() == Some(address)
    }

    /// Redraw just the focus fills.
    fn draw_focus(&self, app: &App) {
        for model in &self.models {
            for i in 0..model.row_count() {
                let mut row = model.row_data(i).unwrap();
                let focused = self.is_focused(row.key.as_str());
                if row.focused != focused {
                    row.focused = focused;
                    model.set_row_data(i, row);
                }
            }
        }
        let data = app.ui.global::<BluetoothData>();
        data.set_yours_scroll(self.scroll[0]);
        data.set_nearby_scroll(self.scroll[1]);
    }

    // ---- Focus

    /// (column, index) of a device's row.
    fn position(&self, address: &str) -> Option<(usize, usize)> {
        (0..2).find_map(|c| self.cols[c].iter().position(|a| a == address).map(|i| (c, i)))
    }

    fn rect(&self, col: usize, index: usize) -> Rect {
        row_rect(col, index, self.scroll[col], self.cols[col].len())
    }

    fn initial(&self) -> Option<String> {
        self.default
            .clone()
            .filter(|d| self.position(d).is_some())
            .or_else(|| self.cols.iter().flatten().next().cloned())
    }

    fn nearest_to_last(&self) -> Option<String> {
        let last = self.last_rect?;
        let centre = |r: Rect| ((r.left + r.right) / 2.0, (r.top + r.bottom) / 2.0);
        let (x, y) = centre(last);
        let mut best: Option<(f32, &String)> = None;
        for c in 0..2 {
            for (i, a) in self.cols[c].iter().enumerate() {
                let (cx, cy) = centre(self.rect(c, i));
                let d = (cx - x).hypot(cy - y);
                if best.is_none_or(|(bd, _)| d < bd) {
                    best = Some((d, a));
                }
            }
        }
        best.map(|(_, a)| a.clone())
    }

    /// Focus arrived on a row: scroll it into view, and remember it for its column.
    fn note_focus(&mut self) {
        let Some((c, i)) = self.focus.as_ref().and_then(|f| self.position(f)) else { return };
        self.scroll[c] = scroll_to_show(self.scroll[c], i, self.cols[c].len());
        self.group_last[c] = self.focus.clone();
        self.last_rect = Some(self.rect(c, i));
    }

    fn move_focus(&mut self, app: &App, dir: Dir) -> Outcome {
        let Some((col, idx)) = self.focus.as_ref().and_then(|f| self.position(f)) else {
            self.focus = self.initial();
            self.note_focus();
            self.draw_focus(app);
            return if self.focus.is_some() { Outcome::Moved } else { Outcome::Edge };
        };
        let from = self.rect(col, idx);
        let mut best: Option<(f32, usize, usize)> = None;
        for c in 0..2 {
            for i in 0..self.cols[c].len() {
                if (c, i) == (col, idx) {
                    continue;
                }
                if let Some(score) = score(from, self.rect(c, i), dir)
                    && best.is_none_or(|(b, ..)| score < b)
                {
                    best = Some((score, c, i));
                }
            }
        }
        let Some((_, c, i)) = best else { return Outcome::Edge };
        let mut target = self.cols[c][i].clone();
        // Coming back into a column: where its focus was.
        if c != col
            && let Some(last) = &self.group_last[c]
            && self.cols[c].contains(last)
        {
            target = last.clone();
        }
        self.focus = Some(target);
        self.note_focus();
        self.draw_focus(app);
        Outcome::Moved
    }

    // ---- Doing

    /// A command whose failure is toasted; `then` gets whether it worked.
    fn run(&self, app: &mut App, name: &str, args: Value, then: impl FnOnce(&mut Inner, &mut App, bool) + 'static) {
        let weak = self.this.clone();
        app.command(
            name,
            args,
            Some(Box::new(move |app, result| {
                let ok = match result {
                    Ok(_) => true,
                    Err(e) => {
                        app.toast(&e, "error", None);
                        false
                    }
                };
                soon(&weak, app, move |i, app| then(i, app, ok));
            })),
        );
    }

    fn wait_for_sound(&mut self, address: &str) {
        self.pending.insert(address.to_string(), Pending::Sound);
        let a = address.to_string();
        let timer = Timer::default();
        timer.start(
            TimerMode::SingleShot,
            SOUND_WAIT,
            from_timer(self.this.clone(), move |i, app| {
                if i.pending.get(&a) == Some(&Pending::Sound) {
                    i.pending.remove(&a);
                }
                i.render(app);
            }),
        );
        self.sound_timers.insert(address.to_string(), timer);
    }

    /// Connected, waiting for sound: done once the sound is there (couchd toasts it).
    fn settle_sound(&mut self, s: &Value) {
        let waiting: Vec<String> =
            self.pending.iter().filter(|(_, p)| **p == Pending::Sound).map(|(a, _)| a.clone()).collect();
        for address in waiting {
            if find(s, &address).is_none_or(|d| !d.connected || sound_here(s, &d)) {
                self.pending.remove(&address);
                self.sound_timers.remove(&address);
            }
        }
    }

    fn connect_like(&mut self, app: &mut App, d: Device, what: Pending) {
        self.pending.insert(d.address.clone(), what);
        self.render(app);
        let name = if what == Pending::Pair { "bluetooth.pair" } else { "bluetooth.connect" };
        let address = d.address.clone();
        self.run(app, name, json!({"address": d.address}), move |i, app, ok| {
            i.pending.remove(&address);
            let now = find(&app.state, &address);
            match now {
                Some(now) if ok && now.connected && now.audio && !sound_here(&app.state, &now) => {
                    i.wait_for_sound(&address);
                }
                Some(now) if ok && !now.audio => {
                    let text = if what == Pending::Pair {
                        format!("Paired with {}", now.name)
                    } else {
                        format!("{} connected", now.name)
                    };
                    app.toast(&text, "info", None);
                }
                _ => {}
            }
            i.render(app);
        });
    }

    fn select(&mut self, app: &mut App, address: &str) {
        let Some(d) = find(&app.state, address) else { return };
        if self.pending.contains_key(address) {
            return;
        }
        if !d.paired {
            return self.connect_like(app, d, Pending::Pair);
        }
        if !d.connected {
            return self.connect_like(app, d, Pending::Connect);
        }
        if d.audio && !sound_here(&app.state, &d) {
            match output_for(&app.state, &d) {
                Some(output) => self.run(app, "audio.set_output", json!({"output": output}), |_, _, _| {}),
                // Connected but its sound isn't up yet: connecting again brings the audio
                // profile up.
                None => self.connect_like(app, d, Pending::Connect),
            }
            return;
        }
        self.open_options(app, &d);
    }

    // ---- Options: Disconnect, Forget

    fn open_options(&mut self, app: &App, d: &Device) {
        if self.sheet.is_some() || !d.paired {
            return;
        }
        let focus = if d.connected { 0 } else { 1 };
        self.sheet = Some(Sheet { address: d.address.clone(), name: d.name.clone(), connected: d.connected, focus });
        let data = app.ui.global::<BluetoothData>();
        data.set_sheet_title(d.name.as_str().into());
        data.set_sheet_disconnect(d.connected);
        data.set_sheet_focus(focus as i32);
        data.set_sheet_open(true);
        self.draw_focus(app);
    }

    /// Close the sheet; focus goes back to the row.
    fn close_options(&mut self, app: &App) -> Option<Sheet> {
        let sheet = self.sheet.take()?;
        app.ui.global::<BluetoothData>().set_sheet_open(false);
        self.draw_focus(app);
        Some(sheet)
    }

    fn sheet_key(&mut self, key: &Key, app: &mut App) -> Outcome {
        let Some(sheet) = self.sheet.as_mut() else { return Outcome::No };
        let first = if sheet.connected { 0 } else { 1 };
        let moved = match (key.arrow(), key.key.as_str()) {
            (Some(Dir::Left), _) if sheet.focus > first => Some(0),
            (Some(Dir::Right), _) if sheet.focus == 0 => Some(1),
            (Some(_), _) => return Outcome::Edge,
            (None, "back") => {
                self.close_options(app);
                return Outcome::Taken;
            }
            (None, "select") => {
                self.sheet_select(app);
                return Outcome::Taken;
            }
            _ => None,
        };
        match moved {
            Some(f) => {
                sheet.focus = f;
                app.ui.global::<BluetoothData>().set_sheet_focus(f as i32);
                Outcome::Moved
            }
            None => Outcome::No,
        }
    }

    fn sheet_select(&mut self, app: &mut App) {
        let Some(sheet) = self.close_options(app) else { return };
        let address = sheet.address.clone();
        if sheet.focus == 0 {
            self.run(app, "bluetooth.disconnect", json!({"address": address}), |i, app, _| i.render(app));
        } else {
            // A connected device gets couchd's "X disconnected" (or "Sound now on …") toast.
            let was_connected = find(&app.state, &address).is_some_and(|d| d.connected);
            let name = sheet.name;
            self.run(app, "bluetooth.forget", json!({"address": address}), move |i, app, ok| {
                if ok && !was_connected {
                    app.toast(&format!("Forgot {name}"), "info", None);
                }
                i.render(app);
            });
        }
    }

    // ---- Scanning, only while on top

    fn start_scan(&mut self, app: &mut App) {
        if !self.visible || !app.connected {
            return;
        }
        if self.scan_since.is_none() {
            self.scan_since = Some(Instant::now());
        }
        app.command("bluetooth.scan", json!({"on": true}), None);
        self.rescan.start(TimerMode::Repeated, RESCAN, || {
            with_app(|app| app.command("bluetooth.scan", json!({"on": true}), None));
        });
        self.nothing_timer.start(
            TimerMode::SingleShot,
            NOTHING_NEARBY,
            from_timer(self.this.clone(), |i, app| i.render(app)),
        );
    }

    fn stop_scan(&mut self, app: &mut App) {
        self.rescan.stop();
        self.nothing_timer.stop();
        self.scan_since = None;
        app.command("bluetooth.scan", json!({"on": false}), None);
    }

    fn key(&mut self, key: &Key, app: &mut App) -> Outcome {
        if self.sheet.is_some() {
            return self.sheet_key(key, app);
        }
        if let Some(dir) = key.arrow() {
            return self.move_focus(app, dir);
        }
        if !key.is("select") {
            return Outcome::No;
        }
        let Some(address) = self.focus.clone() else { return Outcome::No };
        // A held OK (sent once, instead of the press) on a paired device opens Options. Held
        // anywhere else, it's an ordinary OK.
        if key.hold
            && let Some(d) = find(&app.state, &address).filter(|d| d.paired)
        {
            self.open_options(app, &d);
            return Outcome::Taken;
        }
        self.select(app, &address);
        Outcome::Taken
    }
}

impl Screen for Bluetooth {
    fn name(&self) -> &'static str {
        "bluetooth"
    }

    fn palette(&self, _app: &App) -> String {
        "bt".into()
    }

    fn show(&mut self, app: &mut App) {
        let mut i = self.inner.borrow_mut();
        i.visible = true;
        // Focus starts where enter() would: the device you'd want (covering re-renders the
        // rows on the web, so it doesn't remember one).
        i.focus = None;
        i.render(app);
        i.start_scan(app);
    }

    fn hide(&mut self, app: &mut App) {
        let mut i = self.inner.borrow_mut();
        i.visible = false;
        i.close_options(app);
        i.stop_scan(app);
        i.render(app);
    }

    fn destroy(&mut self, app: &mut App) {
        let mut i = self.inner.borrow_mut();
        i.sound_timers.clear();
        if i.visible {
            i.stop_scan(app);
        }
    }

    fn state(&mut self, app: &mut App, patch: Option<&Value>) {
        let mut i = self.inner.borrow_mut();
        match patch {
            Some(p) if !["bluetooth", "audio", "services"].iter().any(|k| p.get(k).is_some()) => return,
            // A full snapshot: couchd is (back) up, so scan again.
            None => i.start_scan(app),
            _ => {}
        }
        let s = app.state.clone();
        i.settle_sound(&s);
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
    fn outputs_match_by_hex_digits() {
        assert!(output_matches("bluetooth:AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF"));
        assert!(output_matches("bluez_output.AA_BB_CC_DD_EE_FF.1", "aa:bb:cc:dd:ee:ff"));
        assert!(!output_matches("bluetooth:AA:BB:CC:DD:EE:00", "AA:BB:CC:DD:EE:FF"));
        assert!(!output_matches("tv", ""));
    }

    #[test]
    fn columns_put_audio_first_and_prefer_the_one_playing() {
        let s = json!({
            "audio": {"current": "bluetooth:FA:KE:00:00:00:03", "outputs": []},
            "bluetooth": {"devices": [
                {"address": "FA:KE:00:00:00:01", "name": "Kitchen", "audio": true, "paired": true, "connected": true},
                {"address": "FA:KE:00:00:00:03", "name": "Phones", "audio": true, "paired": true, "connected": true},
                {"address": "FA:KE:00:00:00:04", "name": "Phone", "audio": false, "paired": false},
                {"address": "FA:KE:00:00:00:05", "name": "Speaker", "audio": true, "paired": false},
            ]},
        });
        let ([paired, fresh], default) = columns(&s);
        assert_eq!(paired.len(), 2);
        assert_eq!(fresh.iter().map(|d| d.name.as_str()).collect::<Vec<_>>(), ["Speaker", "Phone"]);
        assert_eq!(default.as_deref(), Some("FA:KE:00:00:00:03"));
        assert_eq!(sound_name(&s), "None");
    }

    #[test]
    fn focus_moves_like_focus_js() {
        let from = row_rect(0, 1, 0.0, 4);
        // Right: the row level with it in the other column, not the one above.
        let level = score(from, row_rect(1, 1, 0.0, 4), Dir::Right).unwrap();
        let above = score(from, row_rect(1, 0, 0.0, 4), Dir::Right).unwrap();
        assert!(level < above);
        // Right from below the other column's last row still reaches it.
        assert!(score(row_rect(0, 3, 0.0, 4), row_rect(1, 0, 0.0, 4), Dir::Right).is_some());
        // Down stays in its column.
        assert!(score(from, row_rect(1, 2, 0.0, 4), Dir::Down).is_none());
        assert!(score(from, row_rect(0, 2, 0.0, 4), Dir::Down).is_some());
        assert!(score(from, row_rect(0, 0, 0.0, 4), Dir::Down).is_none());
        assert!(score(from, row_rect(1, 1, 0.0, 4), Dir::Left).is_none());
    }

    #[test]
    fn scrolling_keeps_focus_clear_of_the_edges() {
        // Four rows fit: no scrolling.
        assert_eq!(scroll_to_show(0.0, 3, 4), 0.0);
        // Five don't: the last one comes up to 96px above the bottom, as far as it can.
        let s = scroll_to_show(0.0, 4, 5);
        assert!((s - max_scroll(5)).abs() < 0.01 && s > 0.0);
        // Then the rows are 120 tall, 138 apart: 672 of them in 558, as Chrome measures.
        assert_eq!(max_scroll(5), 114.0);
        // Back to the first: all the way up.
        assert_eq!(scroll_to_show(s, 0, 5), 0.0);
    }
}
