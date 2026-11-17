//! couchd's live state (docs/protocol.md): a JSON object of sections, patched section by
//! section. Small helpers to read it by dotted path.

use serde_json::{Map, Value};

/// Merge a message's `data` into `state`: a full snapshot replaces everything, a patch merges
/// each section's keys into that section.
pub fn apply(state: &mut Value, data: &Value, full: bool) {
    if full || !state.is_object() {
        *state = if data.is_object() { data.clone() } else { Value::Object(Map::new()) };
        return;
    }
    let Some(sections) = data.as_object() else { return };
    let root = state.as_object_mut().expect("state is an object");
    for (name, values) in sections {
        match (root.get_mut(name), values.as_object()) {
            (Some(Value::Object(section)), Some(values)) => {
                for (k, v) in values {
                    section.insert(k.clone(), v.clone());
                }
            }
            _ => {
                root.insert(name.clone(), values.clone());
            }
        }
    }
}

/// The value at a dotted path ("audio.current"), or Null. Setting keys contain dots too
/// ("settings" → "display.evening"): use `setting()` for those.
pub fn at<'a>(state: &'a Value, path: &str) -> &'a Value {
    let mut v = state;
    for part in path.split('.') {
        v = match v.get(part) {
            Some(next) => next,
            None => return &Value::Null,
        };
    }
    v
}

pub fn str_at<'a>(state: &'a Value, path: &str) -> Option<&'a str> {
    at(state, path).as_str()
}

pub fn bool_at(state: &Value, path: &str) -> Option<bool> {
    at(state, path).as_bool()
}

pub fn num_at(state: &Value, path: &str) -> Option<f64> {
    at(state, path).as_f64()
}

pub fn list_at<'a>(state: &'a Value, path: &str) -> &'a [Value] {
    at(state, path).as_array().map(Vec::as_slice).unwrap_or(&[])
}

/// A user setting (state section `settings`), or its default (couchd/settings.py's).
pub fn setting<'a>(state: &'a Value, key: &str) -> &'a Value {
    match state.get("settings").and_then(|s| s.get(key)) {
        Some(v) => v,
        None => default_setting(key),
    }
}

fn default_setting(key: &str) -> &'static Value {
    use std::sync::OnceLock;
    static DEFAULTS: OnceLock<Value> = OnceLock::new();
    let d = DEFAULTS.get_or_init(|| {
        serde_json::json!({
            "display.evening": "sunset",
            "display.motion": "auto",
            "display.dim_after": 300,
            "audio.volume_limit": 100,
            "audio.tv_remote_volume": true,
            "audio.ui_sounds": "on",
            "bluetooth.auto_reconnect": true,
            "network.wifi": false,
            "tv.off_when_idle": false,
            "tv.standby_on_poweroff": false,
            "updates.overnight": true,
        })
    });
    d.get(key).unwrap_or(&Value::Null)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn patches_merge_by_section() {
        let mut s = json!({});
        apply(&mut s, &json!({"audio": {"current": "tv", "volume": 40}, "system": {"mode": "dev"}}), true);
        apply(&mut s, &json!({"audio": {"current": "jack"}}), false);
        assert_eq!(str_at(&s, "audio.current"), Some("jack"));
        assert_eq!(num_at(&s, "audio.volume"), Some(40.0));
        assert_eq!(str_at(&s, "system.mode"), Some("dev"));
        apply(&mut s, &json!({"tv": {"power": "on"}}), true);
        assert_eq!(at(&s, "audio"), &Value::Null);
    }

    #[test]
    fn settings_fall_back_to_defaults() {
        let s = json!({"settings": {"display.evening": "off"}});
        assert_eq!(setting(&s, "display.evening"), "off");
        assert_eq!(setting(&s, "display.dim_after"), 300);
    }
}
