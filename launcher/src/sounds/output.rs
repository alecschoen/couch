//! Where the UI sounds go: one audio thread, fed sound names through a channel, so the UI
//! thread never waits on audio. It keeps one playback stream open once the first sound has
//! played, as Chromium's AudioContext did, and writes to it only while something is playing.
//!
//! The stream is PulseAudio's, which is PipeWire's pipewire-pulse on the Pi: the same API
//! Chromium (YouTube), Kodi and couchd (pactl) use. So the sounds play on PipeWire's
//! default sink, and when couchd switches the output (couchd/pipewire.py: set-default-sink,
//! then move-sink-input for every stream) this stream moves with everything else. The other
//! route, ALSA "default", only reaches PipeWire through pipewire-alsa, which install.sh
//! doesn't install; without it, ALSA would open the HDMI card directly and miss Bluetooth.
//!
//! libpulse-simple is loaded when the first sound plays, not linked: the dev containers have
//! no libpulse, and a missing library must only turn the sounds off (one log line), not stop
//! the launcher starting. It also means the cross-build needs no audio -dev packages. If the
//! stream breaks (PipeWire restarts, or kills it with the Bluetooth speaker it was on), the
//! write fails and a new stream is opened at once; if the server isn't there, sounds are off
//! (one log line) and each sound after a 2 s pause tries again.
//!
//! COUCH_SOUND_WAV=<dir> (dev): also write each sound played as <dir>/<n>-<name>.wav (mono
//! 32-bit float, 48 kHz), rendered on its own from a silent start.

use std::ffi::{CStr, CString, c_char, c_int, c_void};
use std::path::{Path, PathBuf};
use std::sync::mpsc::{Receiver, SyncSender, TryRecvError, sync_channel};
use std::time::{Duration, Instant};

use super::synth::{self, Mixer, RATE};

// Sounds are short; a few queued are plenty, and more are dropped rather than waited for.
const QUEUE: usize = 16;
const CHANNELS: usize = 2;
// The thread renders 10 ms at a time while something plays. A sound starting from silence
// goes out as 30 ms in one write, and the server starts playing once it has that (prebuf),
// so PipeWire's first read (a 21 ms quantum) doesn't find the stream short and click.
const CHUNK: usize = RATE as usize / 100;
const FIRST: usize = 3 * CHUNK;
// The most queued in the server: a sound that starts while another is playing waits this.
const TARGET: usize = 4 * CHUNK;
// After the server couldn't be reached, sounds in the next while don't try again.
const RETRY: Duration = Duration::from_secs(2);

/// The UI thread's end: a way to send sound names to the audio thread.
pub struct Output {
    tx: SyncSender<&'static str>,
}

impl Output {
    /// Start the audio thread. It opens the stream when the first sound arrives.
    pub fn start() -> Option<Output> {
        let wavs = std::env::var_os("COUCH_SOUND_WAV").map(PathBuf::from);
        let (tx, rx) = sync_channel(QUEUE);
        let spawned = std::thread::Builder::new().name("couch-sounds".into()).spawn(move || run(rx, wavs));
        match spawned {
            Ok(_) => Some(Output { tx }),
            Err(e) => {
                eprintln!("couch-launcher: UI sounds off: no audio thread: {e}");
                None
            }
        }
    }

    /// Queue a sound. Never blocks: if the thread is stuck, the sound is dropped.
    pub fn play(&self, name: &'static str) {
        let _ = self.tx.try_send(name);
    }
}

fn run(rx: Receiver<&'static str>, wavs: Option<PathBuf>) {
    let mut dev = Device::new();
    let mut mixer = Mixer::new();
    let mut wav_count = 0u32;
    let mut start = |name: &'static str, mixer: &mut Mixer, dev: &mut Device| {
        let Some((_, notes)) = synth::sound(name) else { return };
        if let Some(dir) = &wavs {
            wav_count += 1;
            let path = dir.join(format!("{wav_count:03}-{name}.wav"));
            if let Err(e) = write_wav(&path, &synth::render(notes)) {
                eprintln!("couch-launcher: couldn't write {}: {e}", path.display());
            }
        }
        if dev.ready() {
            mixer.add(notes);
        }
    };
    let mut mono = vec![0.0f32; FIRST];
    let mut frames = vec![0.0f32; FIRST * CHANNELS];
    // Idle: wait for a sound. Then write until nothing is playing (the stream underruns and
    // waits, silent, for the next one).
    while let Ok(name) = rx.recv() {
        start(name, &mut mixer, &mut dev);
        let mut len = FIRST;
        while !mixer.idle() {
            loop {
                match rx.try_recv() {
                    Ok(name) => start(name, &mut mixer, &mut dev),
                    Err(TryRecvError::Empty) => break,
                    Err(TryRecvError::Disconnected) => return,
                }
            }
            mixer.render(&mut mono[..len]);
            for (f, &s) in frames.chunks_mut(CHANNELS).zip(&mono[..len]) {
                f.fill(s);
            }
            if !dev.write(&frames[..len * CHANNELS]) {
                mixer.clear(); // the rest of this sound is lost with the stream
            }
            len = CHUNK;
        }
    }
}

/// The playback stream, opened on demand.
struct Device {
    lib: Option<Pulse>,
    // libpulse-simple can't be loaded: the sounds are off for good.
    missing: bool,
    stream: Option<Stream>,
    failed_at: Option<Instant>,
    // Said why there's no sound since it last worked (one log line per outage).
    told: bool,
}

impl Device {
    fn new() -> Self {
        Device { lib: None, missing: false, stream: None, failed_at: None, told: false }
    }

    /// Is a stream open (opening one if it can)?
    fn ready(&mut self) -> bool {
        if self.stream.is_some() {
            return true;
        }
        if self.missing || self.failed_at.is_some_and(|t| t.elapsed() < RETRY) {
            return false;
        }
        if self.lib.is_none() {
            match Pulse::load() {
                Ok(lib) => self.lib = Some(lib),
                Err(e) => {
                    eprintln!("couch-launcher: UI sounds off: can't load libpulse-simple: {e}");
                    self.missing = true;
                    return false;
                }
            }
        }
        match Stream::open(self.lib.unwrap()) {
            Ok(s) => {
                if self.told {
                    eprintln!("couch-launcher: UI sounds back");
                }
                self.stream = Some(s);
                self.failed_at = None;
                self.told = false;
                true
            }
            Err(e) => {
                if !self.told {
                    eprintln!("couch-launcher: UI sounds off for now: no sound server: {e}");
                    self.told = true;
                }
                self.failed_at = Some(Instant::now());
                false
            }
        }
    }

    /// Write interleaved frames (blocks while the server has TARGET queued). If the stream
    /// broke (PipeWire restarted, or killed it with its output), open a new one and write
    /// there, so the sound still plays if the server is still up. False if that fails too.
    fn write(&mut self, frames: &[f32]) -> bool {
        let Some(stream) = &self.stream else { return false };
        let Err(e) = stream.write(frames) else { return true };
        eprintln!("couch-launcher: UI sounds: lost the stream ({e}), opening another");
        self.stream = None;
        if self.ready() && self.stream.as_ref().is_some_and(|s| s.write(frames).is_ok()) {
            return true;
        }
        self.stream = None;
        false
    }
}

// ---- libpulse-simple, loaded at runtime (pulse/simple.h, pulse/def.h, pulse/sample.h)

const PA_STREAM_PLAYBACK: c_int = 1;
const PA_SAMPLE_FLOAT32LE: c_int = 5;

#[repr(C)]
struct SampleSpec {
    format: c_int,
    rate: u32,
    channels: u8,
}

#[repr(C)]
struct BufferAttr {
    maxlength: u32,
    tlength: u32,
    prebuf: u32,
    minreq: u32,
    fragsize: u32,
}

type NewFn = unsafe extern "C" fn(
    server: *const c_char,
    name: *const c_char,
    dir: c_int,
    dev: *const c_char,
    stream_name: *const c_char,
    ss: *const SampleSpec,
    map: *const c_void,
    attr: *const BufferAttr,
    error: *mut c_int,
) -> *mut c_void;
type WriteFn = unsafe extern "C" fn(s: *mut c_void, data: *const c_void, bytes: usize, error: *mut c_int) -> c_int;
type FreeFn = unsafe extern "C" fn(s: *mut c_void);
type StrErrorFn = unsafe extern "C" fn(error: c_int) -> *const c_char;

/// libpulse-simple's functions. The library is loaded once and stays loaded for the life of
/// the process, so these can be copied freely.
#[derive(Clone, Copy)]
struct Pulse {
    new: NewFn,
    write: WriteFn,
    free: FreeFn,
    strerror: StrErrorFn,
}

impl Pulse {
    fn load() -> Result<Pulse, libloading::Error> {
        // SAFETY: libpulse-simple's initialisers have no requirements, and each symbol is
        // given its C signature from pulse/simple.h and pulse/error.h. pa_strerror is in
        // libpulse, which libpulse-simple links, so dlsym finds it through this handle.
        unsafe {
            let lib = libloading::Library::new("libpulse-simple.so.0")?;
            let pulse = Pulse {
                new: *lib.get::<NewFn>(b"pa_simple_new\0")?,
                write: *lib.get::<WriteFn>(b"pa_simple_write\0")?,
                free: *lib.get::<FreeFn>(b"pa_simple_free\0")?,
                strerror: *lib.get::<StrErrorFn>(b"pa_strerror\0")?,
            };
            // Never unloaded: the function pointers above stay valid.
            std::mem::forget(lib);
            Ok(pulse)
        }
    }

    fn error(&self, code: c_int) -> String {
        // SAFETY: pa_strerror returns a static string (or null for an unknown code).
        let s = unsafe { (self.strerror)(code) };
        if s.is_null() { format!("error {code}") } else { unsafe { CStr::from_ptr(s) }.to_string_lossy().into_owned() }
    }
}

/// An open playback stream.
struct Stream {
    pulse: Pulse,
    s: *mut c_void,
}

impl Stream {
    fn open(pulse: Pulse) -> Result<Stream, String> {
        // couchd and Chromium find the server through XDG_RUNTIME_DIR; a service started
        // without it still reaches couch's session at /run/user/<uid>, as couchd does.
        let server = match std::env::var_os("XDG_RUNTIME_DIR") {
            Some(_) => None,
            None => uid().map(|u| CString::new(format!("unix:/run/user/{u}/pulse/native")).unwrap()),
        };
        let bytes = |frames: usize| (frames * CHANNELS * size_of::<f32>()) as u32;
        let spec = SampleSpec { format: PA_SAMPLE_FLOAT32LE, rate: RATE, channels: CHANNELS as u8 };
        let attr = BufferAttr {
            maxlength: u32::MAX, // the server's default
            tlength: bytes(TARGET),
            prebuf: bytes(FIRST),
            minreq: bytes(CHUNK),
            fragsize: u32::MAX,
        };
        let mut err: c_int = 0;
        // SAFETY: every pointer is valid for the call (null server, device and channel map
        // mean the defaults), and the spec and attr structs have pulse's layout.
        let s = unsafe {
            (pulse.new)(
                server.as_ref().map_or(std::ptr::null(), |s| s.as_ptr()),
                c"Couch launcher".as_ptr(),
                PA_STREAM_PLAYBACK,
                std::ptr::null(),
                c"UI sounds".as_ptr(),
                &spec,
                std::ptr::null(),
                &attr,
                &mut err,
            )
        };
        if s.is_null() { Err(pulse.error(err)) } else { Ok(Stream { pulse, s }) }
    }

    fn write(&self, frames: &[f32]) -> Result<(), String> {
        let mut err: c_int = 0;
        // SAFETY: `s` is an open stream, and the data is valid for its length in bytes.
        let r = unsafe { (self.pulse.write)(self.s, frames.as_ptr().cast(), size_of_val(frames), &mut err) };
        if r < 0 { Err(self.pulse.error(err)) } else { Ok(()) }
    }
}

impl Drop for Stream {
    fn drop(&mut self) {
        // SAFETY: `s` came from pa_simple_new and is freed once.
        unsafe { (self.pulse.free)(self.s) };
    }
}

/// This process's uid, without libc: the owner of /proc/self.
fn uid() -> Option<u32> {
    use std::os::unix::fs::MetadataExt;
    std::fs::metadata("/proc/self").ok().map(|m| m.uid())
}

// ---- WAV (dev)

/// A mono 32-bit float WAV.
fn wav_bytes(samples: &[f32]) -> Vec<u8> {
    let data = (samples.len() * 4) as u32;
    let mut b = Vec::with_capacity(44 + data as usize);
    b.extend_from_slice(b"RIFF");
    b.extend_from_slice(&(36 + data).to_le_bytes());
    b.extend_from_slice(b"WAVEfmt ");
    b.extend_from_slice(&16u32.to_le_bytes());
    b.extend_from_slice(&3u16.to_le_bytes()); // IEEE float
    b.extend_from_slice(&1u16.to_le_bytes()); // mono
    b.extend_from_slice(&RATE.to_le_bytes());
    b.extend_from_slice(&(RATE * 4).to_le_bytes()); // bytes a second
    b.extend_from_slice(&4u16.to_le_bytes()); // bytes a frame
    b.extend_from_slice(&32u16.to_le_bytes());
    b.extend_from_slice(b"data");
    b.extend_from_slice(&data.to_le_bytes());
    for s in samples {
        b.extend_from_slice(&s.to_le_bytes());
    }
    b
}

fn write_wav(path: &Path, samples: &[f32]) -> std::io::Result<()> {
    std::fs::write(path, wav_bytes(samples))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wav_header() {
        let b = wav_bytes(&[0.5, -0.25]);
        assert_eq!(b.len(), 44 + 8);
        assert_eq!(&b[0..4], b"RIFF");
        assert_eq!(u32::from_le_bytes(b[4..8].try_into().unwrap()), 44);
        assert_eq!(u16::from_le_bytes(b[20..22].try_into().unwrap()), 3);
        assert_eq!(u32::from_le_bytes(b[24..28].try_into().unwrap()), 48_000);
        assert_eq!(u32::from_le_bytes(b[40..44].try_into().unwrap()), 8);
        assert_eq!(f32::from_le_bytes(b[44..48].try_into().unwrap()), 0.5);
    }

    #[test]
    fn layouts_match_pulse() {
        // pa_sample_spec is {enum, uint32_t, uint8_t}; pa_buffer_attr five uint32_t.
        assert_eq!(size_of::<SampleSpec>(), 12);
        assert_eq!(size_of::<BufferAttr>(), 20);
    }

    #[test]
    fn a_dead_channel_is_fine() {
        // The thread may have gone; playing must not block or panic.
        let (tx, rx) = sync_channel(1);
        drop(rx);
        let out = Output { tx };
        out.play("move");
    }
}
