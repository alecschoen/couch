//! Dev only: screenshots on request (docs/images is made this way). With COUCH_SHOTS set
//! to a directory, writing a name into `<dir>/request` saves the window as `<dir>/<name>.png`
//! (then the request file is removed).

use std::path::PathBuf;
use std::time::Duration;

use slint::{ComponentHandle, Timer, TimerMode};

use crate::MainWindow;

pub fn watch(ui: &MainWindow) -> Option<Timer> {
    let dir = PathBuf::from(std::env::var_os("COUCH_SHOTS")?);
    let _ = std::fs::create_dir_all(&dir);
    let weak = ui.as_weak();
    let timer = Timer::default();
    timer.start(TimerMode::Repeated, Duration::from_millis(100), move || {
        let request = dir.join("request");
        let Ok(name) = std::fs::read_to_string(&request) else { return };
        let _ = std::fs::remove_file(&request);
        let name = name.trim();
        let Some(ui) = weak.upgrade() else { return };
        match ui.window().take_snapshot() {
            Ok(buf) => {
                let path = dir.join(format!("{name}.png"));
                match save(&path, buf.width(), buf.height(), buf.as_bytes()) {
                    Ok(()) => eprintln!("couch-launcher: saved {}", path.display()),
                    Err(e) => eprintln!("couch-launcher: couldn't save {}: {e}", path.display()),
                }
            }
            Err(e) => eprintln!("couch-launcher: no snapshot: {e}"),
        }
    });
    Some(timer)
}

/// Written beside it and renamed, so a watcher never sees half a file.
fn save(path: &std::path::Path, w: u32, h: u32, rgba: &[u8]) -> Result<(), Box<dyn std::error::Error>> {
    let part = path.with_extension("png.part");
    {
        let file = std::io::BufWriter::new(std::fs::File::create(&part)?);
        let mut enc = png::Encoder::new(file, w, h);
        enc.set_color(png::ColorType::Rgba);
        enc.set_depth(png::BitDepth::Eight);
        enc.write_header()?.write_image_data(rgba)?;
    }
    std::fs::rename(&part, path)?;
    Ok(())
}
