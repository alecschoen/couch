//! The connection to couchd (docs/protocol.md): one WebSocket on its own thread, reconnecting
//! with backoff. Messages come out through `on_event`, which runs on that
//! thread: hand them to the UI thread (`slint::invoke_from_event_loop`). Text sent while the
//! socket is down is dropped; the app fails its pending commands on `Connected(false)`.
//!
//! Files couchd makes (`GET /api/file/<name>`: the speaker's cover) come over plain HTTP from
//! the same host, each on a thread of its own (`fetch`).

use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use serde_json::Value;
use tungstenite::stream::MaybeTlsStream;
use tungstenite::{Error, Message, WebSocket};

const MIN_RETRY: Duration = Duration::from_millis(500);
const MAX_RETRY: Duration = Duration::from_millis(5000);
const POLL: Duration = Duration::from_millis(15);
const FETCH_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_FILE: u64 = 16 << 20;

pub enum Event {
    Connected(bool),
    Message(Value),
}

#[derive(Clone)]
pub struct Couch {
    out: mpsc::Sender<String>,
    /// couchd's host and port, for HTTP ("127.0.0.1:8765").
    host: String,
}

impl Couch {
    pub fn spawn(url: String, on_event: impl Fn(Event) + Send + 'static) -> Couch {
        let (out, rx) = mpsc::channel::<String>();
        let host = host_of(&url).to_string();
        thread::Builder::new()
            .name("couchd".into())
            .spawn(move || run(&url, &rx, &on_event))
            .expect("couldn't start the couchd thread");
        Couch { out, host }
    }

    pub fn send(&self, text: String) {
        let _ = self.out.send(text);
    }

    /// GET `path` ("/api/file/speaker-cover?v=3") from couchd, on a thread of its own; `done`
    /// gets the body, or why not, on that thread.
    pub fn fetch(&self, path: &str, done: impl FnOnce(Result<Vec<u8>, String>) + Send + 'static) {
        let (host, path) = (self.host.clone(), path.to_string());
        let spawned = thread::Builder::new().name("couchd-get".into()).spawn(move || done(get(&host, &path)));
        if let Err(e) = spawned {
            eprintln!("couch-launcher: couldn't fetch from couchd: {e}");
        }
    }
}

/// "ws://127.0.0.1:8765/ws" → "127.0.0.1:8765".
fn host_of(url: &str) -> &str {
    let rest = url.split_once("://").map_or(url, |(_, rest)| rest);
    rest.split('/').next().unwrap_or(rest)
}

/// HTTP/1.0, so the body is the rest of the stream and never chunked.
fn get(host: &str, path: &str) -> Result<Vec<u8>, String> {
    let mut stream = TcpStream::connect(host).map_err(|e| e.to_string())?;
    let _ = stream.set_read_timeout(Some(FETCH_TIMEOUT));
    let _ = stream.set_write_timeout(Some(FETCH_TIMEOUT));
    write!(stream, "GET {path} HTTP/1.0\r\nHost: {host}\r\n\r\n").map_err(|e| e.to_string())?;
    let mut response = Vec::new();
    stream.take(MAX_FILE).read_to_end(&mut response).map_err(|e| e.to_string())?;
    body_of(response)
}

fn body_of(mut response: Vec<u8>) -> Result<Vec<u8>, String> {
    let end = response.windows(4).position(|w| w == b"\r\n\r\n").ok_or("no HTTP response")?;
    let head = String::from_utf8_lossy(&response[..end]).into_owned();
    let status = head.split_whitespace().nth(1).unwrap_or("");
    if status != "200" {
        return Err(format!("HTTP {status}"));
    }
    Ok(response.split_off(end + 4))
}

fn run(url: &str, rx: &mpsc::Receiver<String>, on_event: &dyn Fn(Event)) {
    let mut retry = MIN_RETRY;
    loop {
        match tungstenite::connect(url) {
            Ok((mut ws, _)) => {
                if let MaybeTlsStream::Plain(s) = ws.get_mut() {
                    let _ = s.set_read_timeout(Some(POLL));
                    let _ = s.set_nodelay(true);
                }
                retry = MIN_RETRY;
                // Anything queued while we were away is stale.
                while rx.try_recv().is_ok() {}
                on_event(Event::Connected(true));
                session(&mut ws, rx, on_event);
                on_event(Event::Connected(false));
            }
            Err(e) => eprintln!("couch-launcher: can't reach couchd at {url}: {e}"),
        }
        thread::sleep(retry);
        retry = (retry * 2).min(MAX_RETRY);
        while rx.try_recv().is_ok() {}
    }
}

fn session(ws: &mut WebSocket<MaybeTlsStream<TcpStream>>, rx: &mpsc::Receiver<String>, on_event: &dyn Fn(Event)) {
    loop {
        while let Ok(text) = rx.try_recv() {
            if ws.send(Message::text(text)).is_err() {
                return;
            }
        }
        match ws.read() {
            Ok(Message::Text(text)) => {
                if text.as_str() == "pong" {
                    continue;
                }
                match serde_json::from_str::<Value>(text.as_str()) {
                    Ok(v) => on_event(Event::Message(v)),
                    Err(e) => eprintln!("couch-launcher: bad message from couchd: {e}"),
                }
            }
            Ok(Message::Close(_)) => return,
            Ok(_) => {}
            // Interrupted: a signal landed mid-read (on the Pi, while the window starts); just read again.
            Err(Error::Io(e))
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut | std::io::ErrorKind::Interrupted
                ) => {}
            Err(e) => {
                eprintln!("couch-launcher: lost couchd: {e}");
                return;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn http_from_the_socket_url() {
        assert_eq!(host_of("ws://127.0.0.1:8765/ws"), "127.0.0.1:8765");
        assert_eq!(host_of("ws://couch.local:9000"), "couch.local:9000");
        let ok = b"HTTP/1.0 200 OK\r\nContent-Type: image/png\r\n\r\nPNG\r\n\r\n".to_vec();
        assert_eq!(body_of(ok).unwrap(), b"PNG\r\n\r\n");
        assert_eq!(body_of(b"HTTP/1.1 404 Not Found\r\n\r\nno such file".to_vec()), Err("HTTP 404".into()));
        assert!(body_of(b"garbage".to_vec()).is_err());
    }
}
