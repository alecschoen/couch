//! Plex's pictures for Home's Continue watching (#86), from couchd: `GET /api/plex/thumb/<name>`
//! on the address the launcher reaches couchd at. couchd fetched them from the Plex server
//! and keeps them; the launcher never talks to Plex.
//!
//! Each picture is fetched once, on a thread of its own, and decoded on the UI thread (Slint's
//! images stay there), then kept while Home lists it. When one arrives the screens hear it
//! (`Screen::images`) and draw it. One that fails shows the flat placeholder, and is tried
//! again after `RETRY`.

use std::cell::RefCell;
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::OnceLock;
use std::thread;
use std::time::{Duration, Instant};

use slint::Image;

use crate::app::with_app;

/// The only paths fetched: couchd's thumbnails, by the names it made (24 hex digits).
const PREFIX: &str = "/api/plex/thumb/";
const NAME_LEN: usize = 24;
const TIMEOUT: Duration = Duration::from_secs(5);
const RETRY: Duration = Duration::from_secs(30);
/// More than couchd ever serves (it caps a picture at 2 MB): anything bigger is refused.
const MAX: usize = 4 * 1024 * 1024;

/// couchd's "host:port", from the WebSocket URL.
static COUCHD: OnceLock<String> = OnceLock::new();

enum Entry {
    Loading,
    Ready(Image),
    Failed(Instant),
}

thread_local! {
    static CACHE: RefCell<HashMap<String, Entry>> = RefCell::new(HashMap::new());
}

/// Where couchd is: the host and port of its WebSocket URL ("ws://127.0.0.1:8765/ws").
pub fn init(ws_url: &str) {
    if let Some(at) = authority(ws_url) {
        let _ = COUCHD.set(at);
    }
}

fn authority(url: &str) -> Option<String> {
    let rest = url.strip_prefix("ws://")?;
    let at = rest.split(['/', '?', '#']).next()?;
    if at.is_empty() || at.contains('@') {
        return None;
    }
    Some(if at.starts_with('[') || at.rsplit_once(':').is_some_and(|(_, p)| p.parse::<u16>().is_ok()) {
        at.to_string()
    } else {
        format!("{at}:80")
    })
}

/// One of couchd's thumbnail paths, and nothing else.
pub fn valid(path: &str) -> bool {
    path.strip_prefix(PREFIX)
        .is_some_and(|name| name.len() == NAME_LEN && name.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')))
}

/// The picture at `path`, if it's here; otherwise None, and it's on its way (unless it
/// failed lately).
pub fn get(path: &str) -> Option<Image> {
    if !valid(path) {
        return None;
    }
    let start = CACHE.with(|c| {
        let mut c = c.borrow_mut();
        match c.get(path) {
            Some(Entry::Ready(image)) => return Err(image.clone()),
            Some(Entry::Loading) => return Ok(false),
            Some(Entry::Failed(at)) if at.elapsed() < RETRY => return Ok(false),
            _ => {}
        }
        c.insert(path.to_string(), Entry::Loading);
        Ok(true)
    });
    match start {
        Err(image) => Some(image),
        Ok(true) => {
            fetch(path.to_string());
            None
        }
        Ok(false) => None,
    }
}

/// Forget the pictures not in `paths` (Home's list moved on).
pub fn keep<'a>(paths: impl IntoIterator<Item = &'a str>) {
    let paths: Vec<&str> = paths.into_iter().collect();
    CACHE.with(|c| c.borrow_mut().retain(|k, _| paths.contains(&k.as_str())));
}

fn fetch(path: String) {
    let Some(at) = COUCHD.get().cloned() else {
        CACHE.with(|c| c.borrow_mut().insert(path, Entry::Failed(Instant::now())));
        return;
    };
    let spawned = thread::Builder::new().name("thumb".into()).spawn(move || {
        let body = http_get(&at, &path);
        let _ = slint::invoke_from_event_loop(move || arrived(path, body));
    });
    if spawned.is_err() {
        eprintln!("couch-launcher: couldn't start a thumbnail thread");
    }
}

/// On the UI thread: decode it, keep it, and have the screens draw it.
fn arrived(path: String, body: Result<Vec<u8>, String>) {
    let entry = match body.and_then(|b| Image::load_from_data(&b, None).map_err(|_| "not a picture".to_string())) {
        Ok(image) => Entry::Ready(image),
        Err(e) => {
            eprintln!("couch-launcher: no thumbnail {path}: {e}");
            Entry::Failed(Instant::now())
        }
    };
    let wanted = CACHE.with(|c| {
        let mut c = c.borrow_mut();
        // Dropped meanwhile (the list moved on): not kept.
        let wanted = c.contains_key(&path);
        if wanted {
            c.insert(path, entry);
        }
        wanted
    });
    if wanted {
        with_app(|app| app.images_changed());
    }
}

/// A plain HTTP/1.0 GET to couchd, on localhost: the body of a 200, or why not.
fn http_get(at: &str, path: &str) -> Result<Vec<u8>, String> {
    let addr = at.to_socket_addrs().map_err(|e| e.to_string())?.next().ok_or("no address")?;
    let mut stream = TcpStream::connect_timeout(&addr, TIMEOUT).map_err(|e| e.to_string())?;
    stream.set_read_timeout(Some(TIMEOUT)).map_err(|e| e.to_string())?;
    stream.set_write_timeout(Some(TIMEOUT)).map_err(|e| e.to_string())?;
    let request = format!("GET {path} HTTP/1.0\r\nHost: {at}\r\nConnection: close\r\n\r\n");
    stream.write_all(request.as_bytes()).map_err(|e| e.to_string())?;
    let mut response = Vec::new();
    stream.take((MAX + 16 * 1024) as u64).read_to_end(&mut response).map_err(|e| e.to_string())?;
    body(&response)
}

/// The body of an HTTP response, if it's a 200 of no more than MAX.
fn body(response: &[u8]) -> Result<Vec<u8>, String> {
    let end = response.windows(4).position(|w| w == b"\r\n\r\n").ok_or("no headers")?;
    let head = std::str::from_utf8(&response[..end]).map_err(|_| "bad headers")?;
    let status = head.lines().next().and_then(|l| l.split_whitespace().nth(1)).ok_or("no status")?;
    if status != "200" {
        return Err(format!("HTTP {status}"));
    }
    let body = &response[end + 4..];
    if body.len() > MAX {
        return Err("too big".into());
    }
    Ok(body.to_vec())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn only_couchds_thumbnails() {
        let name = "0123456789abcdef01234567";
        assert!(valid(&format!("/api/plex/thumb/{name}")));
        for bad in [
            "/api/plex/thumb/0123456789ABCDEF01234567",
            "/api/plex/thumb/../../api/state",
            "/api/plex/thumb/0123456789abcdef0123456",
            "/api/plex/thumb/0123456789abcdef01234567/x",
            "/api/state",
            "http://elsewhere/api/plex/thumb/0123456789abcdef01234567",
            "",
        ] {
            assert!(!valid(bad), "{bad}");
        }
    }

    #[test]
    fn couchd_where_the_socket_is() {
        assert_eq!(authority("ws://127.0.0.1:8765/ws").as_deref(), Some("127.0.0.1:8765"));
        assert_eq!(authority("ws://localhost/ws").as_deref(), Some("localhost:80"));
        assert_eq!(authority("ws://[::1]:8765/ws").as_deref(), Some("[::1]:8765"));
        assert_eq!(authority("wss://example.org/ws"), None);
        assert_eq!(authority("ws://user@127.0.0.1:1/ws"), None);
    }

    #[test]
    fn a_200s_body_and_nothing_else() {
        assert_eq!(body(b"HTTP/1.0 200 OK\r\nContent-Type: image/png\r\n\r\n\x89PNG").unwrap(), b"\x89PNG");
        assert_eq!(body(b"HTTP/1.1 404 Not Found\r\n\r\nnope").unwrap_err(), "HTTP 404");
        assert!(body(b"HTTP/1.0 200 OK\r\n").is_err());
    }

    #[test]
    fn fetched_over_http() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let at = listener.local_addr().unwrap().to_string();
        let server = thread::spawn(move || {
            let (mut s, _) = listener.accept().unwrap();
            let mut request = Vec::new();
            let mut buf = [0u8; 1024];
            while !request.ends_with(b"\r\n\r\n") {
                let n = s.read(&mut buf).unwrap();
                request.extend_from_slice(&buf[..n]);
            }
            s.write_all(b"HTTP/1.0 200 OK\r\n\r\npicture").unwrap();
            String::from_utf8_lossy(&request).into_owned()
        });
        let path = "/api/plex/thumb/0123456789abcdef01234567";
        assert_eq!(http_get(&at, path).unwrap(), b"picture");
        let request = server.join().unwrap();
        assert!(request.starts_with(&format!("GET {path} HTTP/1.0\r\nHost: {at}\r\n")));
    }
}
