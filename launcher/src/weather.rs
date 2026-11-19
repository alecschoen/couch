//! The weather (#82), as Home and the idle clock show it: couchd's live state `weather`
//! (couchd/weather.py) put into words. Home has now and today ("12°", "Cloudy · 15° / 7°");
//! the idle clock a strip of now and the hours to come.
//!
//! None of it shows with the `display.weather` setting off or while couchd has no forecast:
//! no placeholders, the screens are as they were without it.

use serde_json::Value;

use crate::state;

/// How far ahead the strip's first hour is at least, then the steps between its hours.
const FIRST_AFTER: i64 = 3600;
const STEP: i64 = 2 * 3600;
/// The strip's hours after "Now".
const LATER: usize = 3;

/// Now, for Home: the icon (one of couchd's `ICONS`), "12°", and "Cloudy · 15° / 7°".
#[derive(Debug, PartialEq)]
pub struct Now {
    pub icon: String,
    pub temp: String,
    pub line: String,
}

/// One of the strip's hours: "Now" or "22:00", its icon and "11°".
#[derive(Debug, PartialEq)]
pub struct Hour {
    pub label: String,
    pub icon: String,
    pub temp: String,
}

/// The weather to show, or None: the setting is off, or there's no forecast.
pub fn now(s: &Value) -> Option<Now> {
    if state::setting(s, "display.weather").as_bool() == Some(false) {
        return None;
    }
    let temp = state::num_at(s, "weather.temp")?;
    let condition = state::str_at(s, "weather.condition").unwrap_or("");
    let range = match (state::num_at(s, "weather.high"), state::num_at(s, "weather.low")) {
        (Some(high), Some(low)) => format!("{} / {}", degrees(high), degrees(low)),
        _ => String::new(),
    };
    let line = [condition, &range].into_iter().filter(|p| !p.is_empty()).collect::<Vec<_>>();
    Some(Now { icon: icon(state::at(s, "weather.icon")), temp: degrees(temp), line: line.join(" \u{b7} ") })
}

/// The idle clock's strip at `at` (Unix seconds): "Now", then the first hour at least an hour
/// away and two more two hours apart (20:51 → 22:00, 00:00, 02:00). Empty with no weather.
pub fn strip(s: &Value, at: i64) -> Vec<Hour> {
    let Some(now) = now(s) else { return Vec::new() };
    let mut hours = vec![Hour { label: "Now".into(), icon: now.icon, temp: now.temp }];
    let mut next = at + FIRST_AFTER;
    for h in state::list_at(s, "weather.hourly") {
        if hours.len() > LATER {
            break;
        }
        let (Some(when), Some(time), Some(temp)) = (
            h.get("at").and_then(Value::as_i64),
            h.get("time").and_then(Value::as_str),
            h.get("temp").and_then(Value::as_f64),
        ) else {
            continue;
        };
        if when < next {
            continue;
        }
        hours.push(Hour { label: time.into(), icon: icon(h.get("icon").unwrap_or(&Value::Null)), temp: degrees(temp) });
        next = when + STEP;
    }
    hours
}

/// "12°", "-3°".
fn degrees(t: f64) -> String {
    format!("{}\u{b0}", t.round() as i64)
}

/// couchd's icon name, or the plain cloud for one it might add later.
fn icon(v: &Value) -> String {
    const KNOWN: [&str; 8] = ["clear", "clear-night", "partly-cloudy", "cloudy", "fog", "rain", "snow", "thunder"];
    v.as_str().filter(|i| KNOWN.contains(i)).unwrap_or("cloudy").into()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// 20:51, as in the design; hourly from 21:00.
    const H21: i64 = 1_795_377_600;
    const AT: i64 = H21 - 9 * 60;

    fn weather() -> Value {
        let hourly: Vec<Value> = (0..12)
            .map(|i| {
                json!({"at": H21 + i * 3600, "time": format!("{:02}:00", (21 + i) % 24),
                    "temp": 11 - i, "icon": if i < 3 { "cloudy" } else { "rain" }})
            })
            .collect();
        json!({"weather": {"temp": 12, "condition": "Cloudy", "icon": "cloudy", "high": 15, "low": 7,
            "hourly": hourly, "updated_at": AT}})
    }

    #[test]
    fn now_and_today_in_words() {
        let n = now(&weather()).unwrap();
        assert_eq!(
            n,
            Now { icon: "cloudy".into(), temp: "12\u{b0}".into(), line: "Cloudy \u{b7} 15\u{b0} / 7\u{b0}".into() }
        );
        let s = json!({"weather": {"temp": -0.4, "condition": "", "icon": "frogs", "high": null, "low": null}});
        let n = now(&s).unwrap();
        assert_eq!((n.temp.as_str(), n.line.as_str(), n.icon.as_str()), ("0\u{b0}", "", "cloudy"));
        let s = json!({"weather": {"temp": -3, "condition": "", "icon": "snow", "high": 1, "low": -5}});
        assert_eq!(now(&s).unwrap().line, "1\u{b0} / -5\u{b0}");
    }

    #[test]
    fn nothing_without_a_forecast_or_with_the_setting_off() {
        assert_eq!(now(&json!({})), None);
        assert_eq!(now(&json!({"weather": {"temp": null, "hourly": []}})), None);
        let mut s = weather();
        s["settings"] = json!({"display.weather": false});
        assert_eq!(now(&s), None);
        assert!(strip(&s, AT).is_empty());
        s["settings"] = json!({"display.weather": true});
        assert!(now(&s).is_some());
    }

    #[test]
    fn the_strip_steps_two_hours_from_an_hour_away() {
        let labels = |at| strip(&weather(), at).into_iter().map(|h| h.label).collect::<Vec<_>>();
        assert_eq!(labels(AT), ["Now", "22:00", "00:00", "02:00"]);
        // On the hour: the next one is an hour away.
        assert_eq!(labels(H21 - 3600), ["Now", "21:00", "23:00", "01:00"]);
        // Late in the forecast: as many as there are.
        assert_eq!(labels(H21 + 8 * 3600), ["Now", "06:00", "08:00"]);
        let h = &strip(&weather(), AT)[2];
        assert_eq!((h.icon.as_str(), h.temp.as_str()), ("rain", "8\u{b0}"));
    }

    #[test]
    fn the_strip_skips_a_gap() {
        let mut s = weather();
        s["weather"]["hourly"].as_array_mut().unwrap().remove(3); // no 00:00
        let labels: Vec<_> = strip(&s, AT).into_iter().map(|h| h.label).collect();
        assert_eq!(labels, ["Now", "22:00", "01:00", "03:00"]);
    }
}
