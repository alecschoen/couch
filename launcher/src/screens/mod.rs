//! The launcher's screens, one module each, and the factory the router builds them with.

use serde_json::Value;

use crate::app::{App, Screen};

pub mod bluetooth;
pub mod home;
pub mod settings;
pub mod system;
pub mod wifi;

/// Build a screen by its router name.
pub fn make(name: &str, params: &Value, app: &mut App) -> Box<dyn Screen> {
    match name {
        "home" => Box::new(home::Home::new()),
        "settings" => Box::new(settings::Settings::new(params, app)),
        "bluetooth" => Box::new(bluetooth::Bluetooth::new(params, app)),
        "wifi" => Box::new(wifi::Wifi::new(params, app)),
        "startup" => Box::new(system::System::new("startup", params, app)),
        "opening" => Box::new(system::System::new("opening", params, app)),
        "error" => Box::new(system::System::new("error", params, app)),
        _ => Box::new(Stub),
    }
}

/// Stands in for a screen while the router has it out of the stack for a moment.
pub struct Placeholder;

impl Screen for Placeholder {
    fn name(&self) -> &'static str {
        ""
    }
    fn palette(&self, _app: &App) -> String {
        "system".into()
    }
}

/// A screen name the launcher doesn't know: empty, in the system
/// colour; Back leaves it.
pub struct Stub;

impl Screen for Stub {
    fn name(&self) -> &'static str {
        "stub"
    }
    fn palette(&self, _app: &App) -> String {
        "system".into()
    }
}
