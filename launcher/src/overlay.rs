//! couchd's overlays in the launcher (as ui/js/overlay-view.js draws them in YouTube): the quick menu and
//! the volume panel. couchd runs both (couchd/overlay.py): it opens the menu on Blue, moves
//! its focus and acts on OK; the keys meant for the menu come marked `overlay: true`, so
//! screens never see them. This only draws `state.overlay.menu` when the launcher is the
//! menu's host, and the `volume_panel` messages.
//!
//! The quick menu is a sheet that slides up in the app's colour (the launcher's own screen
//! colour when couchd names none). A menu with other cards is a new sheet. The volume panel
//! stays while the volume keeps changing and goes TOAST_MS after the last change; a toast
//! from couchd replaces it at once (one thing at a time up there).

use std::cell::RefCell;
use std::rc::Rc;
use std::time::Duration;

use serde_json::Value;
use slint::{ComponentHandle, Model, Timer, VecModel};

use crate::app::{App, TOAST_MS};
use crate::state;
use crate::{MainWindow, MenuCard, OverlayData, Theme};

// The CSS durations (tokens.css), for taking things away once they've animated out.
const SHEET_MS: u64 = 320;
const QUICK_MS: u64 = 200;

#[derive(Default)]
struct Overlay {
    /// The open sheet's card ids ("home sound volume bluetooth sleep"); None while there's none
    /// (or it's sliding out).
    menu_ids: Option<String>,
    menu_generation: i32,
    cards: Rc<VecModel<MenuCard>>,
    menu_gone: Timer,
    /// A volume panel is up and not fading.
    volume_up: bool,
    volume_generation: i32,
    volume_timer: Timer,
    volume_gone: Timer,
}

thread_local! {
    static OVERLAY: RefCell<Overlay> = RefCell::new(Overlay::default());
}

/// couchd's state changed (`patch` is None for a full snapshot).
pub fn state(app: &mut App, patch: Option<&Value>) {
    if patch.is_none_or(|p| p.get("overlay").is_some() || p.get("system").is_some()) {
        draw_menu(app);
    }
}

/// The `volume_panel` message: `{output, name, volume, muted}`.
pub fn volume(app: &mut App, data: &Value) {
    let (name, value, level) = volume_view(data);
    let ui = app.ui.as_weak();
    let ms = if app.reduced_motion() { 0 } else { QUICK_MS };
    let d = app.ui.global::<OverlayData>();
    OVERLAY.with_borrow_mut(|o| {
        if !o.volume_up {
            // A new panel (the last one, if it's still fading, goes at once).
            o.volume_up = true;
            o.volume_generation += 1;
            o.volume_gone.stop();
            d.set_volume_generation(o.volume_generation);
            d.set_volume_fading(false);
            d.set_volume_shown(true);
        }
        // Up for TOAST_MS after the last change, then it fades out and goes.
        o.volume_timer.start(slint::TimerMode::SingleShot, Duration::from_millis(TOAST_MS), move || {
            OVERLAY.with_borrow_mut(|o| {
                o.volume_up = false;
                if let Some(ui) = ui.upgrade() {
                    ui.global::<OverlayData>().set_volume_fading(true);
                }
                let ui = ui.clone();
                o.volume_gone.start(slint::TimerMode::SingleShot, Duration::from_millis(ms), move || {
                    if let Some(ui) = ui.upgrade() {
                        ui.global::<OverlayData>().set_volume_shown(false);
                    }
                });
            });
        });
    });
    d.set_volume_name(name.into());
    d.set_volume_value(value.into());
    d.set_volume_level(level);
}

/// A toast from couchd: it takes the panel's place, at once.
pub fn hide_volume(app: &mut App) {
    OVERLAY.with_borrow_mut(|o| {
        o.volume_up = false;
        o.volume_timer.stop();
        o.volume_gone.stop();
    });
    app.ui.global::<OverlayData>().set_volume_shown(false);
}

fn draw_menu(app: &mut App) {
    let menu = state::at(&app.state, "overlay.menu");
    let dev = state::str_at(&app.state, "system.mode") == Some("dev");
    let view = menu_view(menu, dev);
    let ui = &app.ui;
    let d = ui.global::<OverlayData>();
    let Some(view) = view else {
        let ms = if app.reduced_motion() { 0 } else { SHEET_MS };
        close_menu(ui, ms);
        return;
    };
    OVERLAY.with_borrow_mut(|o| {
        if o.menu_ids.as_deref() != Some(view.ids.as_str()) {
            // A new sheet: slides in from below.
            o.menu_ids = Some(view.ids.clone());
            o.menu_generation += 1;
            o.menu_gone.stop();
            o.cards.set_vec(view.cards.clone());
            d.set_menu_cards(o.cards.clone().into());
            d.set_menu_generation(o.menu_generation);
            d.set_menu_open(true);
            d.set_menu_shown(true);
        } else {
            // The same cards: change only the ones that changed, so the focus fill fades.
            for (i, card) in view.cards.iter().enumerate() {
                if o.cards.row_data(i).as_ref() != Some(card) {
                    o.cards.set_row_data(i, card.clone());
                }
            }
        }
    });
    let palette = match view.palette {
        Some(p) => p,
        None => {
            let p = ui.global::<Theme>().get_palette();
            if p.is_empty() { "system".into() } else { p.to_string() }
        }
    };
    d.set_menu_palette(palette.into());
    d.set_menu_title(view.title.into());
    d.set_menu_focus(view.focus);
}

fn close_menu(ui: &MainWindow, ms: u64) {
    OVERLAY.with_borrow_mut(|o| {
        if o.menu_ids.take().is_none() {
            return;
        }
        ui.global::<OverlayData>().set_menu_open(false);
        let ui = ui.as_weak();
        o.menu_gone.start(slint::TimerMode::SingleShot, Duration::from_millis(ms), move || {
            if let Some(ui) = ui.upgrade() {
                ui.global::<OverlayData>().set_menu_shown(false);
            }
        });
    });
}

/// The quick menu as the launcher draws it.
#[derive(Debug, PartialEq)]
struct MenuView {
    ids: String,
    title: String,
    palette: Option<String>,
    focus: i32,
    cards: Vec<MenuCard>,
}

/// couchd's `overlay.menu`, if the launcher should draw it: it's open, and the launcher is its
/// host. In dev mode also the menu couchd hosts in Kodi, since the pretend Kodi leaves the
/// launcher on screen.
fn menu_view(menu: &Value, dev: bool) -> Option<MenuView> {
    let host = menu.get("host").and_then(Value::as_str);
    let here = host == Some("launcher") || (dev && host == Some("kodi"));
    if !here || menu.get("open").and_then(Value::as_bool) != Some(true) {
        return None;
    }
    let text = |v: &Value, k: &str| v.get(k).and_then(Value::as_str).unwrap_or("").to_string();
    let items = state::list_at(menu, "items");
    let focus = menu.get("focus").and_then(Value::as_str);
    Some(MenuView {
        ids: items.iter().map(|i| text(i, "id")).collect::<Vec<_>>().join(" "),
        title: text(menu, "title"),
        palette: menu.get("palette").and_then(Value::as_str).filter(|p| !p.is_empty()).map(str::to_string),
        focus: items
            .iter()
            .position(|i| focus.is_some() && i.get("id").and_then(Value::as_str) == focus)
            .map_or(-1, |i| i as i32),
        cards: items
            .iter()
            .map(|i| MenuCard {
                label: text(i, "label").into(),
                value: text(i, "value").into(),
                detail: text(i, "detail").into(),
                hint: text(i, "hint").into(),
            })
            .collect(),
    })
}

/// The volume panel's name, number ("Muted" when muted) and bar (0–1).
fn volume_view(data: &Value) -> (String, String, f32) {
    let name = data.get("name").and_then(Value::as_str).unwrap_or("").to_string();
    let volume = data.get("volume").and_then(Value::as_f64).unwrap_or(0.0);
    let muted = data.get("muted").and_then(Value::as_bool).unwrap_or(false);
    let level = volume.round().clamp(0.0, 100.0);
    if muted { (name, "Muted".into(), 0.0) } else { (name, format!("{level}"), level as f32 / 100.0) }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn menu(host: &str) -> Value {
        json!({"open": true, "app": "launcher", "host": host, "title": "Couch", "palette": null,
            "focus": "volume", "items": [
                {"id": "home", "label": "Go to", "value": "Home"},
                {"id": "volume", "label": "Volume", "value": "\u{2212} 60 +"},
                {"id": "sleep", "label": "Sleep", "value": "30 min", "detail": "TV off at 21:20",
                    "hint": "OK: Off \u{b7} 15 \u{b7} 30 \u{b7} 60 \u{b7} 90"}]})
    }

    #[test]
    fn menu_drawn_only_where_it_is_hosted() {
        let view = menu_view(&menu("launcher"), false).unwrap();
        assert_eq!(view.ids, "home volume sleep");
        assert_eq!(view.focus, 1);
        assert_eq!(view.palette, None);
        assert_eq!(view.cards[1].value, "\u{2212} 60 +");
        assert_eq!((view.cards[1].detail.as_str(), view.cards[1].hint.as_str()), ("", ""));
        assert_eq!(view.cards[2].detail, "TV off at 21:20");
        assert_eq!(view.cards[2].hint, "OK: Off \u{b7} 15 \u{b7} 30 \u{b7} 60 \u{b7} 90");
        assert!(menu_view(&menu("chromium"), true).is_none());
        assert!(menu_view(&menu("kodi"), false).is_none());
        assert!(menu_view(&menu("kodi"), true).is_some());
        assert!(menu_view(&Value::Null, true).is_none());
        let mut closed = menu("launcher");
        closed["open"] = json!(false);
        assert!(menu_view(&closed, false).is_none());
    }

    #[test]
    fn volume_panel_values() {
        assert_eq!(
            volume_view(&json!({"name": "TV speakers", "volume": 45, "muted": false})),
            ("TV speakers".into(), "45".into(), 0.45)
        );
        assert_eq!(volume_view(&json!({"name": "JBL", "volume": 140})).1, "100");
        assert_eq!(
            volume_view(&json!({"name": "JBL", "volume": 35, "muted": true})),
            ("JBL".into(), "Muted".into(), 0.0)
        );
    }
}
