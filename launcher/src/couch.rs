//! The connection to couchd (docs/protocol.md): one WebSocket on its own thread, reconnecting
//! with backoff. Messages come out through `on_event`, which runs on that
//! thread: hand them to the UI thread (`slint::invoke_from_event_loop`). Text sent while the
//! socket is down is dropped; the app fails its pending commands on `Connected(false)`.

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

pub enum Event {
    Connected(bool),
    Message(Value),
}

#[derive(Clone)]
pub struct Couch {
    out: mpsc::Sender<String>,
}

impl Couch {
    pub fn spawn(url: String, on_event: impl Fn(Event) + Send + 'static) -> Couch {
        let (out, rx) = mpsc::channel::<String>();
        thread::Builder::new()
            .name("couchd".into())
            .spawn(move || run(&url, &rx, &on_event))
            .expect("couldn't start the couchd thread");
        Couch { out }
    }

    pub fn send(&self, text: String) {
        let _ = self.out.send(text);
    }
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
