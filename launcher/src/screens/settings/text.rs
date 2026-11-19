//! What Settings says: values, hints and labels worked out from couchd's state. Pure, so
//! they're tested here.

use chrono::{DateTime, Local, TimeZone};
use serde_json::Value;

use crate::state;

/// The user settings' choices (couchd/settings.py's): value and label.
pub fn choices(key: &str) -> Vec<(Value, &'static str)> {
    let s = |v: &str| Value::from(v);
    match key {
        "display.evening" => vec![(s("sunset"), "After sunset"), (s("always"), "Always"), (s("off"), "Off")],
        "audio.ui_sounds" => vec![(s("on"), "On"), (s("tv"), "TV only"), (s("off"), "Off")],
        "display.dim_after" => [
            (60, "1 minute"),
            (120, "2 minutes"),
            (300, "5 minutes"),
            (600, "10 minutes"),
            (900, "15 minutes"),
            (1800, "30 minutes"),
            (3600, "1 hour"),
            (0, "Never"),
        ]
        .into_iter()
        .map(|(v, l)| (Value::from(v), l))
        .collect(),
        _ => Vec::new(),
    }
}

/// Two setting values are the same (couchd may send 300.0 for 300).
pub fn same(a: &Value, b: &Value) -> bool {
    match (a.as_f64(), b.as_f64()) {
        (Some(x), Some(y)) => x == y,
        _ => a == b,
    }
}

pub const VOLUME_MIN: i64 = 10;
pub const VOLUME_MAX: i64 = 100;
pub const VOLUME_STEP: i64 = 5;

pub fn volume_values() -> Vec<Value> {
    (VOLUME_MIN..=VOLUME_MAX).step_by(VOLUME_STEP as usize).map(Value::from).collect()
}

pub fn volume_label(v: &Value) -> String {
    match v.as_f64() {
        Some(n) if n >= VOLUME_MAX as f64 => "No limit".into(),
        Some(n) => format!("{n}"),
        None => "No limit".into(),
    }
}

pub fn dim_label(v: &Value) -> String {
    choices("display.dim_after")
        .into_iter()
        .find(|(c, _)| same(c, v))
        .map(|(_, l)| l.to_string())
        .unwrap_or_else(|| format!("{} s", v.as_f64().map(|n| n.to_string()).unwrap_or_default()))
}

/// Under Evening colors: when "After sunset" is today (live state `evening`).
pub fn sun_hint(s: &Value) -> String {
    let Some(dark) = state::bool_at(s, "evening.dark") else { return String::new() };
    if state::str_at(s, "evening.place").is_none() {
        return "After sunset is 18:00 to 07:00 until the timezone is set".into();
    }
    match (state::str_at(s, "evening.sunset"), state::str_at(s, "evening.sunrise")) {
        (Some(sunset), sunrise) => format!("Sunset today {sunset}, sunrise {}", sunrise.unwrap_or("")),
        (None, _) if dark => "The sun doesn't rise today".into(),
        (None, _) => "The sun doesn't set today".into(),
    }
}

/// Under the Wi-Fi toggle: what the radio is doing (live state `wifi`).
pub fn wifi_hint(s: &Value, on: bool) -> String {
    if !on {
        return "Off. Couch uses the network cable.".into();
    }
    if state::bool_at(s, "wifi.available") == Some(false) {
        return "Waiting for the Wi-Fi chip".into();
    }
    if state::bool_at(s, "wifi.enabled") != Some(true) {
        return "Turning on…".into();
    }
    let ssid = state::str_at(s, "wifi.ssid");
    match state::str_at(s, "wifi.connection") {
        Some("connected") => {
            let mut parts = vec![ssid.unwrap_or("Connected").to_string()];
            if let Some(w) = signal_words(state::num_at(s, "wifi.signal")) {
                parts.push(w.into());
            }
            parts.join(" · ")
        }
        Some("connecting") => format!("Connecting to {}…", ssid.unwrap_or("a network")),
        _ => "On. Not connected to a network.".into(),
    }
}

/// Settings › Network's Wi-Fi network row: the network it's on, or isn't.
pub fn wifi_network(s: &Value) -> String {
    match (state::str_at(s, "wifi.connection"), state::str_at(s, "wifi.ssid")) {
        (Some("connected"), Some(ssid)) => ssid.into(),
        (Some("connecting"), Some(ssid)) => format!("Connecting to {ssid}…"),
        _ => "Not connected".into(),
    }
}

fn signal_words(signal: Option<f64>) -> Option<&'static str> {
    let s = signal?;
    Some(if s >= 70.0 {
        "Strong signal"
    } else if s >= 40.0 {
        "Good signal"
    } else {
        "Weak signal"
    })
}

/// system.info's `network`: None while it hasn't answered.
pub fn network_label(info: Option<&Value>) -> String {
    let Some(info) = info else { return "…".into() };
    let Some(n) = info.get("network") else { return "Unknown".into() };
    if n.is_null() {
        return "Not connected".into();
    }
    let kind = if n.get("kind").and_then(Value::as_str) == Some("wifi") { "Wi-Fi" } else { "Ethernet" };
    let mut parts = vec![kind.to_string()];
    for k in ["name", "address"] {
        if let Some(v) = n.get(k).and_then(Value::as_str).filter(|v| !v.is_empty()) {
            parts.push(v.into());
        }
    }
    parts.join(" · ")
}

pub fn tv_connection(s: &Value) -> String {
    let service = state::str_at(s, "services.tv");
    let power = state::str_at(s, "tv.power");
    match (service, power) {
        (Some("running"), Some("on")) => "HDMI-CEC · TV on".into(),
        (Some("running"), Some(p)) if p != "unknown" => "HDMI-CEC · TV in standby".into(),
        _ => "No HDMI-CEC".into(),
    }
}

// ---- Updates (couchd's live state `updates`)

pub fn running(u: &Value) -> bool {
    u.get("state").and_then(Value::as_str).is_some_and(|s| s != "idle")
}

pub fn running_label(state: &str) -> Option<&'static str> {
    Some(match state {
        "checking" => "Checking…",
        "downloading" => "Downloading…",
        "installing" => "Installing…",
        "rolling-back" => "Going back…",
        "system" => "Updating the system…",
        _ => return None,
    })
}

pub fn check_label(u: &Value) -> String {
    if running(u) {
        running_label(u["state"].as_str().unwrap_or("")).unwrap_or("Checking…").into()
    } else {
        "Check now".into()
    }
}

pub fn launcher_version(u: &Value) -> String {
    if !u.is_object() {
        return "…".into();
    }
    match u.get("version").and_then(Value::as_str) {
        Some(v) if !v.is_empty() => format!("Couch {v}"),
        _ => "Couch (not from a release)".into(),
    }
}

pub fn last_check(u: &Value, now: DateTime<Local>) -> String {
    let str_of = |k: &str| u.get(k).and_then(Value::as_str);
    if running(u) {
        return str_of("message")
            .map(str::to_string)
            .unwrap_or_else(|| running_label(str_of("state").unwrap_or("")).unwrap_or("Checking…").into());
    }
    let Some(at) = u.get("last_check").and_then(Value::as_f64).filter(|&t| t != 0.0) else {
        return "Not checked yet".into();
    };
    let when = ago(at, now);
    let what = match str_of("result") {
        Some("up-to-date") => Some("Up to date".to_string()),
        Some("installed") => Some(format!("Updated to {}", str_of("version").unwrap_or("undefined"))),
        Some("rolled-back") | Some("broken") => Some("Update didn't work".into()),
        Some("failed") => Some("Couldn't check".into()),
        Some("no-access") => Some("Not set up yet".into()),
        Some("waiting") => Some("Waiting".into()),
        _ => None,
    };
    match what {
        Some(w) => format!("{w} · {when}"),
        None => when,
    }
}

/// The sentence under Last check: why it waited, or what went wrong.
pub fn check_hint(u: &Value) -> String {
    if !u.is_object() || running(u) {
        return String::new();
    }
    let str_of = |k: &str| u.get(k).and_then(Value::as_str);
    let message = str_of("message");
    match str_of("result") {
        Some("waiting") if str_of("waiting").is_some() => format!("Waiting because {}", str_of("waiting").unwrap()),
        Some("no-access") => {
            let why = message.unwrap_or("Couch can't read its releases");
            if u.get("nightly").and_then(Value::as_bool) == Some(true) {
                format!("{why}. Security updates still run. See Release access in system/README.md.")
            } else {
                format!(
                    "{why}. Nightly and security updates are off until they're turned on. See Updates in system/README.md."
                )
            }
        }
        Some("rolled-back" | "broken" | "failed") => message.unwrap_or("").into(),
        Some("up-to-date") => message.unwrap_or("").into(),
        _ => String::new(),
    }
}

pub fn system_updates(u: &Value, now: DateTime<Local>) -> String {
    if let Some(at) = u.get("system").and_then(|s| s.get("at")).and_then(Value::as_f64).filter(|&t| t != 0.0) {
        return format!("Security updates · {}", ago(at, now));
    }
    if u.get("nightly").and_then(Value::as_bool) == Some(false) {
        "Off until overnight updates are turned on".into()
    } else {
        "Security updates overnight".into()
    }
}

/// "just now", "5 minutes ago", "today 20:17", "Monday 08:05" (en-GB, as on the Pi).
pub fn ago(unix: f64, now: DateTime<Local>) -> String {
    let s = (now.timestamp_millis() as f64 / 1000.0 - unix).max(0.0);
    if s < 90.0 {
        return "just now".into();
    }
    if s < 3600.0 {
        return format!("{} minutes ago", (s / 60.0).round());
    }
    let Some(d) = Local.timestamp_opt(unix as i64, 0).single() else { return String::new() };
    let time = d.format("%H:%M");
    if d.date_naive() == now.date_naive() { format!("today {time}") } else { format!("{} {time}", d.format("%A")) }
}

// ---- Remote: the test box

pub fn key_name(key: &str) -> String {
    match key {
        "up" => "Up",
        "down" => "Down",
        "left" => "Left",
        "right" => "Right",
        "select" => "OK",
        "back" => "Back",
        "home" => "Home",
        "play" => "Play",
        "pause" => "Pause",
        "play_pause" => "Play/Pause",
        "stop" => "Stop",
        "rewind" => "Rewind",
        "fast_forward" => "Fast forward",
        "red" => "Red",
        "green" => "Green",
        "yellow" => "Yellow",
        "blue" => "Blue",
        other => other,
    }
    .into()
}

/// What the TV remote does, until couchd publishes its own (live state `remote.keymap`).
pub fn keymap(s: &Value) -> Vec<(String, String)> {
    if let Some(list) = state::at(s, "remote.keymap").as_array() {
        return list
            .iter()
            .map(|k| {
                let get = |f: &str| k.get(f).and_then(Value::as_str).unwrap_or("").to_string();
                (get("button"), get("action"))
            })
            .collect();
    }
    [("Hold Back", "Go home"), ("Blue button", "Quick menu"), ("Red button", "Switch audio output")]
        .into_iter()
        .map(|(b, a)| (b.into(), a.into()))
        .collect()
}

/// The refresh rate from frame times: the median gap, rounded to a usual rate when near one.
pub fn refresh_rate(times_ms: &[f64]) -> Option<u32> {
    let mut gaps: Vec<f64> = times_ms.windows(2).map(|w| w[1] - w[0]).filter(|g| *g > 0.0).collect();
    if gaps.is_empty() {
        return None;
    }
    gaps.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let rate = 1000.0 / gaps[gaps.len() / 2];
    const USUAL: [f64; 10] = [24.0, 25.0, 30.0, 50.0, 60.0, 75.0, 90.0, 100.0, 120.0, 144.0];
    let near = USUAL.iter().copied().fold(USUAL[0], |a, b| if (b - rate).abs() < (a - rate).abs() { b } else { a });
    Some(if (near - rate).abs() < 3.0 { near as u32 } else { rate.round() as u32 })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn at(h: u32, m: u32) -> DateTime<Local> {
        Local.with_ymd_and_hms(2026, 11, 16, h, m, 0).unwrap()
    }

    #[test]
    fn labels() {
        assert_eq!(dim_label(&json!(300)), "5 minutes");
        assert_eq!(dim_label(&json!(300.0)), "5 minutes");
        assert_eq!(dim_label(&json!(45)), "45 s");
        assert_eq!(volume_label(&json!(100)), "No limit");
        assert_eq!(volume_label(&json!(35)), "35");
        assert_eq!(volume_values().len(), 19);
        assert_eq!(key_name("fast_forward"), "Fast forward");
        assert_eq!(key_name("7"), "7");
    }

    #[test]
    fn sun() {
        assert_eq!(sun_hint(&json!({})), "");
        let s = json!({"evening": {"dark": true, "sunset": "16:54", "sunrise": "07:59", "place": "Europe/Brussels"}});
        assert_eq!(sun_hint(&s), "Sunset today 16:54, sunrise 07:59");
        let s = json!({"evening": {"dark": true, "sunset": null, "sunrise": null, "place": "Arctic"}});
        assert_eq!(sun_hint(&s), "The sun doesn't rise today");
        let s = json!({"evening": {"dark": false, "place": null}});
        assert_eq!(sun_hint(&s), "After sunset is 18:00 to 07:00 until the timezone is set");
    }

    #[test]
    fn network() {
        assert_eq!(network_label(None), "…");
        assert_eq!(network_label(Some(&json!({}))), "Unknown");
        assert_eq!(network_label(Some(&json!({"network": null}))), "Not connected");
        let i = json!({"network": {"kind": "wifi", "name": "Home", "address": "10.0.0.2"}});
        assert_eq!(network_label(Some(&i)), "Wi-Fi · Home · 10.0.0.2");
        let i = json!({"network": {"kind": "ethernet", "name": null, "address": "192.168.1.42"}});
        assert_eq!(network_label(Some(&i)), "Ethernet · 192.168.1.42");
        let w = json!({"wifi": {"available": true, "enabled": true, "connection": "connected", "ssid": "Home", "signal": 55}});
        assert_eq!(wifi_hint(&w, true), "Home · Good signal");
        assert_eq!(wifi_hint(&w, false), "Off. Couch uses the network cable.");
        let w = json!({"wifi": {"available": true, "enabled": true, "connection": "connecting", "ssid": null}});
        assert_eq!(wifi_hint(&w, true), "Connecting to a network…");
        assert_eq!(wifi_network(&w), "Not connected");
        let w = json!({"wifi": {"enabled": true, "connection": "connected", "ssid": "Home"}});
        assert_eq!(wifi_network(&w), "Home");
        let w = json!({"wifi": {"enabled": true, "connection": "connecting", "ssid": "Café"}});
        assert_eq!(wifi_network(&w), "Connecting to Café…");
        let s = json!({"services": {"tv": "running"}, "tv": {"power": "standby"}});
        assert_eq!(tv_connection(&s), "HDMI-CEC · TV in standby");
        assert_eq!(tv_connection(&json!({})), "No HDMI-CEC");
    }

    #[test]
    fn updates() {
        let now = at(20, 17);
        let t = now.timestamp() as f64;
        assert_eq!(launcher_version(&Value::Null), "…");
        assert_eq!(launcher_version(&json!({"version": null})), "Couch (not from a release)");
        assert_eq!(last_check(&json!({"state": "idle"}), now), "Not checked yet");
        assert_eq!(last_check(&json!({"state": "checking"}), now), "Checking…");
        assert_eq!(
            last_check(&json!({"state": "installing", "message": "Installing v0.2.0"}), now),
            "Installing v0.2.0"
        );
        let u = json!({"state": "idle", "result": "up-to-date", "last_check": t - 30.0});
        assert_eq!(last_check(&u, now), "Up to date · just now");
        let u = json!({"state": "idle", "result": "installed", "version": "v0.2.0", "last_check": t - 600.0});
        assert_eq!(last_check(&u, now), "Updated to v0.2.0 · 10 minutes ago");
        assert_eq!(ago(t - 3.0 * 3600.0, now), "today 17:17");
        assert_eq!(ago(t - 24.0 * 3600.0, now), "Sunday 20:17");
        let u = json!({"state": "idle", "result": "waiting", "waiting": "YouTube is playing"});
        assert_eq!(check_hint(&u), "Waiting because YouTube is playing");
        assert_eq!(
            check_hint(&json!({"state": "idle", "result": "no-access", "nightly": true})),
            "Couch can't read its releases. Security updates still run. See Release access in system/README.md."
        );
        assert_eq!(system_updates(&json!({"nightly": false}), now), "Off until overnight updates are turned on");
        assert_eq!(system_updates(&json!({}), now), "Security updates overnight");
        assert_eq!(check_label(&json!({"state": "checking"})), "Checking…");
        assert_eq!(check_label(&json!({"state": "idle"})), "Check now");
    }

    #[test]
    fn refresh() {
        let times: Vec<f64> = (0..61).map(|i| i as f64 * 16.7).collect();
        assert_eq!(refresh_rate(&times), Some(60));
        let times: Vec<f64> = (0..61).map(|i| i as f64 * 25.0).collect();
        assert_eq!(refresh_rate(&times), Some(40));
        assert_eq!(refresh_rate(&[1.0]), None);
    }
}
