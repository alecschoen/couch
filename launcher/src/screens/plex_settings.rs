//! Settings › Network › Plex (#86): link Couch to a Plex account, for Continue watching on
//! Home. OK on the account asks couchd for a code (plex.link); the screen shows it, to enter
//! at plex.tv/link on a phone, until couchd hears it was (`plex.linked`). Linked, OK twice
//! within 4 s unlinks (plex.unlink): couchd deletes its token. Couchd holds the sign-in; this
//! never sees a token.
//!
//! The second row is the `home.plex_continue` switch. Up and Down move; Back cancels a code
//! that's waiting, and goes back to Settings.

use std::cell::RefCell;
use std::rc::{Rc, Weak};
use std::time::Duration;

use serde_json::{Value, json};
use slint::{ComponentHandle, Model, Timer, TimerMode, VecModel};

use crate::app::{App, Dir, Key, Outcome, Screen, with_app};
use crate::state;
use crate::{PlexData, PlexRow};

const SETTING: &str = "home.plex_continue";
const CONFIRM: Duration = Duration::from_secs(4);
const ROWS: usize = 2;

/// Where linking stands, from couchd's `plex` section.
#[derive(Debug, PartialEq)]
enum Account {
    Unlinked,
    /// A code to enter at plex.tv/link.
    Waiting(String),
    Linked {
        server: Option<String>,
    },
}

fn account(s: &Value) -> Account {
    if state::bool_at(s, "plex.linked") == Some(true) {
        return Account::Linked { server: state::str_at(s, "plex.server").map(str::to_string) };
    }
    match state::str_at(s, "plex.link.code") {
        Some(code) if !code.is_empty() => Account::Waiting(code.to_string()),
        _ => Account::Unlinked,
    }
}

/// The account row: its value and hint.
fn account_row(a: &Account, confirming: bool) -> (String, String) {
    match a {
        Account::Unlinked => ("Not linked".into(), "OK for a code to enter at plex.tv/link".into()),
        Account::Waiting(_) => ("Waiting for the code".into(), String::new()),
        Account::Linked { .. } if confirming => ("Press OK again to unlink".into(), String::new()),
        Account::Linked { server: Some(name) } => ("Linked".into(), format!("Server: {name} \u{b7} OK to unlink")),
        Account::Linked { server: None } => {
            ("Linked".into(), "Looking for your Plex server \u{b7} OK to unlink".into())
        }
    }
}

struct Inner {
    me: Weak<RefCell<Inner>>,
    focus: usize,
    confirming: Option<Timer>,
    rows: Rc<VecModel<PlexRow>>,
    shown: bool,
}

pub struct PlexSettings {
    inner: Rc<RefCell<Inner>>,
}

impl PlexSettings {
    pub fn new() -> Self {
        let inner = Rc::new_cyclic(|me| {
            RefCell::new(Inner {
                me: me.clone(),
                focus: 0,
                confirming: None,
                rows: Rc::new(VecModel::default()),
                shown: false,
            })
        });
        PlexSettings { inner }
    }
}

impl Inner {
    fn draw(&self, app: &App) {
        let s = &app.state;
        let a = account(s);
        let (value, hint) = account_row(&a, self.confirming.is_some());
        let on = state::setting(s, SETTING).as_bool().unwrap_or(true);
        let rows = vec![
            PlexRow {
                label: "Plex account".into(),
                hint: hint.into(),
                value: value.into(),
                focused: self.focus == 0,
                hairline: false,
                has_switch: false,
                switch_on: false,
            },
            PlexRow {
                label: "Continue watching on Home".into(),
                hint: "What you're partway through, on the Plex card".into(),
                value: "".into(),
                focused: self.focus == 1,
                hairline: false,
                has_switch: true,
                switch_on: on,
            },
        ];
        if !self.rows.iter().eq(rows.iter().cloned()) {
            if self.rows.row_count() == rows.len() {
                for (i, row) in rows.into_iter().enumerate() {
                    if self.rows.row_data(i).as_ref() != Some(&row) {
                        self.rows.set_row_data(i, row);
                    }
                }
            } else {
                self.rows.set_vec(rows);
            }
        }
        let data = app.ui.global::<PlexData>();
        data.set_rows(self.rows.clone().into());
        match &a {
            Account::Waiting(code) => {
                data.set_code(code.as_str().into());
                data.set_code_shown(true);
            }
            _ => data.set_code_shown(false),
        }
        let note = match &a {
            Account::Unlinked => state::str_at(s, "plex.link_error").unwrap_or(""),
            _ => "",
        };
        data.set_note(note.into());
    }

    fn account_ok(&mut self, app: &mut App) {
        match account(&app.state) {
            Account::Unlinked => {
                app.command_or_toast("plex.link", json!({}));
            }
            Account::Waiting(_) => {}
            Account::Linked { .. } => {
                if self.confirming.take().is_some() {
                    app.command_or_toast("plex.unlink", json!({}));
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
                self.confirming = Some(timer);
            }
        }
    }

    fn cancel_confirm(&mut self, app: &App) {
        if self.confirming.take().is_some() && self.shown {
            self.draw(app);
        }
    }

    /// The switch: shown at once; if couchd refuses, it goes back, with an error toast.
    fn flip(&mut self, app: &mut App) {
        let before = state::setting(&app.state, SETTING).as_bool().unwrap_or(true);
        write_setting(app, !before);
        let me = self.me.clone();
        app.command(
            "settings.set",
            json!({"key": SETTING, "value": !before}),
            Some(Box::new(move |app, result| {
                if let Err(e) = result {
                    write_setting(app, before);
                    if let Some(s) = me.upgrade() {
                        s.borrow().draw(app);
                    }
                    app.toast(&e, "error", None);
                }
            })),
        );
    }
}

/// Write the setting into the local state, as Settings does, until couchd's patch comes.
fn write_setting(app: &mut App, on: bool) {
    if let Some(settings) = app.state.get_mut("settings").and_then(Value::as_object_mut) {
        settings.insert(SETTING.to_string(), json!(on));
    }
}

impl Screen for PlexSettings {
    fn name(&self) -> &'static str {
        "plex-settings"
    }

    fn palette(&self, _app: &App) -> String {
        "settings".into()
    }

    fn show(&mut self, app: &mut App) {
        let mut s = self.inner.borrow_mut();
        s.shown = true;
        s.draw(app);
    }

    fn hide(&mut self, app: &mut App) {
        let mut s = self.inner.borrow_mut();
        s.shown = false;
        s.confirming = None;
        // Gone from the screen, so is the code: nobody's reading it now.
        if matches!(account(&app.state), Account::Waiting(_)) {
            app.command("plex.cancel_link", json!({}), None);
        }
    }

    fn state(&mut self, app: &mut App, _patch: Option<&Value>) {
        let s = self.inner.borrow();
        if s.shown {
            s.draw(app);
        }
    }

    fn key(&mut self, key: &Key, app: &mut App) -> Outcome {
        let mut s = self.inner.borrow_mut();
        let outcome = match (key.arrow(), key.key.as_str()) {
            (Some(Dir::Up), _) if s.focus > 0 => {
                s.focus -= 1;
                s.confirming = None;
                Outcome::Moved
            }
            (Some(Dir::Down), _) if s.focus + 1 < ROWS => {
                s.focus += 1;
                s.confirming = None;
                Outcome::Moved
            }
            (Some(_), _) => Outcome::Edge,
            (None, "select") if s.focus == 0 => {
                s.account_ok(app);
                Outcome::Taken
            }
            (None, "select") => {
                s.flip(app);
                Outcome::Taken
            }
            _ => Outcome::No,
        };
        s.draw(app);
        outcome
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn where_linking_stands() {
        assert_eq!(account(&json!({})), Account::Unlinked);
        assert_eq!(account(&json!({"plex": {"linked": false, "link": null}})), Account::Unlinked);
        let waiting = json!({"plex": {"linked": false, "link": {"code": "K7QX", "expires_at": 1}}});
        assert_eq!(account(&waiting), Account::Waiting("K7QX".into()));
        let linked = json!({"plex": {"linked": true, "server": "Living room"}});
        assert_eq!(account(&linked), Account::Linked { server: Some("Living room".into()) });
    }

    #[test]
    fn the_account_row_in_words() {
        assert_eq!(account_row(&Account::Unlinked, false).0, "Not linked");
        assert_eq!(account_row(&Account::Waiting("K7QX".into()), false).0, "Waiting for the code");
        let linked = Account::Linked { server: Some("Living room".into()) };
        assert_eq!(account_row(&linked, false), ("Linked".into(), "Server: Living room \u{b7} OK to unlink".into()));
        assert_eq!(account_row(&linked, true).0, "Press OK again to unlink");
    }
}
