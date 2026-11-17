//! The UI sounds' synth, ported sample for sample from the web launcher's Web Audio graph.
//!
//! Each note is an oscillator (sine, or a band-limited triangle like Web Audio's) whose pitch
//! glides exponentially from `from` to `to` over its duration, through its own gain: 0.0001 up
//! to `gain` in 4 ms (no click), then an exponential decay back to 0.0001 at the end, and the
//! oscillator stops 20 ms later. All notes go through one low-pass biquad at 2400 Hz (Web
//! Audio's lowpass, Q 1 dB) and the master gain 0.32, as in the web's `_context()`.
//!
//! Everything here is pure: the audio thread (output.rs) pulls 10 ms chunks out of a Mixer,
//! and the dev WAVs and the tests render a whole sound at once.

use std::f64::consts::PI;

pub const RATE: u32 = 48_000;
const MASTER: f64 = 0.32;
const LOWPASS_HZ: f64 = 2400.0;
const LOWPASS_Q_DB: f64 = 1.0;
const ATTACK: f64 = 0.004;
// Exponential ramps can't reach 0, so the web starts and ends each note at this.
const FLOOR: f64 = 0.0001;
// The web stops each oscillator this long after its note's end.
const TAIL: f64 = 0.02;

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Wave {
    Sine,
    Triangle,
}

/// One note: the web's [type, start Hz, end Hz, duration s, gain, delay s].
#[derive(Clone, Copy, PartialEq, Debug)]
pub struct Note {
    pub wave: Wave,
    pub from: f64,
    pub to: f64,
    pub dur: f64,
    pub gain: f64,
    pub delay: f64,
}

impl Note {
    /// The oscillator's frequency `t` s into the note: the glide, then `to` until the stop.
    fn freq_at(&self, t: f64) -> f64 {
        if t >= self.dur || self.to == self.from { self.to } else { exp_ramp(self.from, self.to, t / self.dur) }
    }

    /// The note's gain `t` s in: up from the floor over the attack, then down to it at `dur`.
    fn gain_at(&self, t: f64) -> f64 {
        if t < ATTACK {
            exp_ramp(FLOOR, self.gain, t / ATTACK)
        } else if t < self.dur {
            exp_ramp(self.gain, FLOOR, (t - ATTACK) / (self.dur - ATTACK))
        } else {
            FLOOR
        }
    }
}

const fn note(wave: Wave, from: f64, to: f64, dur: f64, gain: f64, delay: f64) -> Note {
    Note { wave, from, to, dur, gain, delay }
}

use Wave::{Sine, Triangle};

/// The web's SOUNDS, one or two notes each.
pub const SOUNDS: &[(&str, &[Note])] = &[
    ("move", &[note(Sine, 740.0, 660.0, 0.045, 0.5, 0.0)]),
    ("tick", &[note(Sine, 700.0, 660.0, 0.03, 0.28, 0.0)]),
    ("edge", &[note(Sine, 190.0, 150.0, 0.07, 0.55, 0.0)]),
    ("select", &[note(Triangle, 523.0, 523.0, 0.06, 0.45, 0.0), note(Triangle, 784.0, 784.0, 0.09, 0.4, 0.05)]),
    ("back", &[note(Triangle, 659.0, 659.0, 0.06, 0.4, 0.0), note(Triangle, 440.0, 440.0, 0.09, 0.38, 0.05)]),
    ("toast", &[note(Sine, 659.0, 659.0, 0.08, 0.35, 0.0), note(Sine, 880.0, 880.0, 0.12, 0.3, 0.06)]),
    ("error", &[note(Triangle, 330.0, 330.0, 0.08, 0.45, 0.0), note(Triangle, 262.0, 262.0, 0.12, 0.45, 0.07)]),
];

/// A sound's name as a `&'static str` (so it can go to the audio thread), and its notes.
pub fn sound(name: &str) -> Option<(&'static str, &'static [Note])> {
    SOUNDS.iter().find(|(n, _)| *n == name).copied()
}

fn samples(seconds: f64) -> i64 {
    (seconds * RATE as f64).round() as i64
}

/// A note playing: where it is, and its oscillator's phase (in cycles).
struct Voice {
    note: Note,
    // Samples since the note started; negative while it waits for its delay.
    pos: i64,
    end: i64,
    phase: f64,
}

impl Voice {
    fn new(note: Note) -> Self {
        Voice { note, pos: -samples(note.delay), end: samples(note.dur + TAIL), phase: 0.0 }
    }

    /// The next sample, before the filter. The oscillator, then the note's gain.
    fn next(&mut self) -> f64 {
        let pos = self.pos;
        self.pos += 1;
        if pos < 0 {
            return 0.0;
        }
        let n = &self.note;
        let t = pos as f64 / RATE as f64;
        let (freq, gain) = (n.freq_at(t), n.gain_at(t));
        // Web Audio outputs the wave at the current phase, then advances it by this
        // sample's frequency.
        let out = match n.wave {
            Sine => (2.0 * PI * self.phase).sin(),
            Triangle => triangle(self.phase, freq),
        };
        self.phase = (self.phase + freq / RATE as f64).fract();
        out * gain
    }

    fn done(&self) -> bool {
        self.pos >= self.end
    }
}

/// AudioParam's exponentialRampToValueAtTime: `a` at x = 0 to `b` at x = 1.
fn exp_ramp(a: f64, b: f64, x: f64) -> f64 {
    a * (b / a).powf(x)
}

/// Web Audio's triangle: its Fourier series (8/π²n² on the odd sines, alternating signs) with
/// only the partials below Nyquist, as Chromium's wavetables do, so nothing aliases.
fn triangle(phase: f64, freq: f64) -> f64 {
    let theta = 2.0 * PI * phase;
    let top = ((RATE as f64 / 2.0) / freq.max(1.0)) as u32;
    // sin(nθ) for odd n by the recurrence sin((n+2)θ) = 2cos(2θ)·sin(nθ) − sin((n−2)θ).
    let c2 = 2.0 * (2.0 * theta).cos();
    let (mut prev, mut cur) = (-theta.sin(), theta.sin());
    let mut sum = 0.0;
    let mut n = 1;
    while n <= top {
        let b = 8.0 / (PI * PI * (n * n) as f64);
        sum += if n % 4 == 1 { b * cur } else { -b * cur };
        (prev, cur) = (cur, c2 * cur - prev);
        n += 2;
    }
    sum
}

/// Web Audio's BiquadFilterNode "lowpass" (the Audio EQ Cookbook, Q in dB), direct form I.
#[derive(Clone)]
pub struct Lowpass {
    b: [f64; 3],
    a: [f64; 2],
    x: [f64; 2],
    y: [f64; 2],
}

impl Lowpass {
    pub fn new(freq: f64, q_db: f64) -> Self {
        let w0 = 2.0 * PI * freq / RATE as f64;
        let alpha = w0.sin() / (2.0 * 10f64.powf(q_db / 20.0));
        let cos = w0.cos();
        let a0 = 1.0 + alpha;
        let b0 = (1.0 - cos) / 2.0 / a0;
        Lowpass { b: [b0, 2.0 * b0, b0], a: [-2.0 * cos / a0, (1.0 - alpha) / a0], x: [0.0; 2], y: [0.0; 2] }
    }

    pub fn next(&mut self, x: f64) -> f64 {
        let y = self.b[0] * x + self.b[1] * self.x[0] + self.b[2] * self.x[1]
            - self.a[0] * self.y[0]
            - self.a[1] * self.y[1];
        self.x = [x, self.x[0]];
        self.y = [y, self.y[0]];
        y
    }

    pub fn reset(&mut self) {
        self.x = [0.0; 2];
        self.y = [0.0; 2];
    }

    /// The gain at `freq` (for the tests).
    #[cfg(test)]
    fn response(&self, freq: f64) -> f64 {
        use std::f64::consts::TAU;
        let w = TAU * freq / RATE as f64;
        let z = |k: f64| (-(k * w)).sin_cos();
        let (s1, c1) = z(1.0);
        let (s2, c2) = z(2.0);
        let num = (self.b[0] + self.b[1] * c1 + self.b[2] * c2, self.b[1] * s1 + self.b[2] * s2);
        let den = (1.0 + self.a[0] * c1 + self.a[1] * c2, self.a[0] * s1 + self.a[1] * s2);
        (num.0.hypot(num.1)) / (den.0.hypot(den.1))
    }
}

/// Every note playing, summed through the shared filter and the master gain: the web's graph.
pub struct Mixer {
    voices: Vec<Voice>,
    filter: Lowpass,
}

impl Mixer {
    pub fn new() -> Self {
        Mixer { voices: Vec::new(), filter: Lowpass::new(LOWPASS_HZ, LOWPASS_Q_DB) }
    }

    /// Start a sound now (its notes from the next sample on).
    pub fn add(&mut self, notes: &[Note]) {
        if self.voices.is_empty() {
            self.filter.reset(); // what's left in it from the last sound is long silent
        }
        self.voices.extend(notes.iter().map(|&n| Voice::new(n)));
    }

    pub fn idle(&self) -> bool {
        self.voices.is_empty()
    }

    pub fn clear(&mut self) {
        self.voices.clear();
    }

    /// Fill `out` (mono, -1..1) with what's playing; silence once nothing is.
    pub fn render(&mut self, out: &mut [f32]) {
        for s in out.iter_mut() {
            let dry: f64 = self.voices.iter_mut().map(Voice::next).sum();
            *s = (self.filter.next(dry) * MASTER) as f32;
            self.voices.retain(|v| !v.done());
        }
    }
}

/// A whole sound on its own, as the web plays it from a silent start: for the dev WAVs and
/// the tests. As long as its longest note (to that oscillator's stop).
pub fn render(notes: &[Note]) -> Vec<f32> {
    let len = notes.iter().map(|n| samples(n.delay) + samples(n.dur + TAIL)).max().unwrap_or(0);
    let mut out = vec![0.0; len as usize];
    let mut mixer = Mixer::new();
    mixer.add(notes);
    mixer.render(&mut out);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn notes(name: &str) -> &'static [Note] {
        sound(name).unwrap().1
    }

    fn peak(s: &[f32]) -> f32 {
        s.iter().fold(0.0, |m, x| m.max(x.abs()))
    }

    /// Frequency from upward zero crossings over a stretch.
    fn freq(s: &[f32]) -> f64 {
        let ups: Vec<usize> = (1..s.len()).filter(|&i| s[i - 1] < 0.0 && s[i] >= 0.0).collect();
        (ups.len() - 1) as f64 * RATE as f64 / (ups[ups.len() - 1] - ups[0]) as f64
    }

    #[test]
    fn every_web_sound_is_here() {
        for name in ["move", "tick", "edge", "select", "back", "toast", "error"] {
            assert!(sound(name).is_some(), "{name}");
        }
        assert!(sound("nope").is_none());
        assert_eq!(notes("select")[1], note(Triangle, 784.0, 784.0, 0.09, 0.4, 0.05));
    }

    #[test]
    fn lengths_run_to_the_last_oscillator_stop() {
        assert_eq!(render(notes("move")).len(), 3120); // 45 + 20 ms
        assert_eq!(render(notes("tick")).len(), 2400);
        assert_eq!(render(notes("select")).len(), 7680); // 50 + 90 + 20 ms
        assert_eq!(render(notes("error")).len(), 10_080); // 70 + 120 + 20 ms
    }

    #[test]
    fn envelope_is_the_webs_ramps() {
        let n = notes("move")[0]; // gain 0.5 over 45 ms
        assert!((n.gain_at(0.0) - FLOOR).abs() < 1e-12);
        assert!((n.gain_at(0.002) - (FLOOR * 0.5f64).sqrt()).abs() < 1e-9); // halfway, geometrically
        assert!((n.gain_at(ATTACK) - 0.5).abs() < 1e-12);
        assert!((n.gain_at(0.045) - FLOOR).abs() < 1e-12);
        assert_eq!(n.gain_at(0.06), FLOOR); // held until the stop
        // And the rendered voice follows it: 0 at the start, never above the gain.
        let mut v = Voice::new(n);
        let s: Vec<f64> = (0..samples(0.065)).map(|_| v.next()).collect();
        assert_eq!(s[0], 0.0); // a sine from phase 0
        assert!(s.iter().all(|x| x.abs() <= 0.5));
        assert!(s[samples(0.045) as usize..].iter().all(|x| x.abs() <= FLOOR));
        assert!(v.done());
    }

    #[test]
    fn pitch_glides_from_start_to_end() {
        let n = notes("edge")[0]; // 190 → 150 Hz over 70 ms
        assert_eq!(n.freq_at(0.0), 190.0);
        assert!((n.freq_at(0.035) - (190.0f64 * 150.0).sqrt()).abs() < 1e-9);
        assert_eq!(n.freq_at(0.08), 150.0);
        assert_eq!(notes("select")[0].freq_at(0.03), 523.0);
        // The rendered pitch: about the glide's average in its first half.
        let s = render(notes("edge"));
        let early = freq(&s[0..samples(0.035) as usize]);
        assert!(early > 175.0 && early < 190.0, "{early}");
    }

    #[test]
    fn second_note_starts_after_its_delay() {
        let s = render(notes("select"));
        let second = freq(&s[samples(0.07) as usize..samples(0.12) as usize]);
        assert!((second - 784.0).abs() < 5.0, "{second}");
        // Peaks: the louder note through the filter and master gain.
        let p = peak(&s);
        assert!(p > 0.12 && p < 0.16, "{p}");
    }

    #[test]
    fn triangle_is_web_audios() {
        // Starts at 0 rising, peaks at 1 a quarter in, and falls through 0 halfway.
        assert!(triangle(0.0, 100.0).abs() < 1e-12);
        assert!((triangle(0.25, 100.0) - 1.0).abs() < 0.005);
        assert!(triangle(0.5, 100.0).abs() < 1e-9);
        assert!((triangle(0.75, 100.0) + 1.0).abs() < 0.005);
        assert!((triangle(0.125, 100.0) - 0.5).abs() < 0.01);
        // No partials at or above Nyquist.
        let high = triangle(0.1, 9000.0); // 9 and 27 kHz: only the first
        assert!((high - 8.0 / (PI * PI) * (2.0 * PI * 0.1).sin()).abs() < 1e-12);
    }

    #[test]
    fn lowpass_is_web_audios() {
        let f = Lowpass::new(2400.0, 1.0);
        assert!((f.response(1.0) - 1.0).abs() < 1e-6);
        // At the cutoff, the resonance: Q in dB.
        assert!((f.response(2400.0) - 10f64.powf(1.0 / 20.0)).abs() < 1e-3);
        assert!(f.response(9600.0) < 0.08); // about -12 dB an octave above
    }

    #[test]
    fn chunks_add_up_to_the_whole() {
        let whole = render(notes("toast"));
        let mut m = Mixer::new();
        m.add(notes("toast"));
        let mut parts = vec![0.0; whole.len()];
        for c in parts.chunks_mut(480) {
            m.render(c);
        }
        assert_eq!(whole, parts);
        assert!(m.idle());
    }

    #[test]
    fn overlapping_sounds_mix() {
        let mut m = Mixer::new();
        m.add(notes("tick"));
        m.add(notes("tick"));
        let mut two = vec![0.0; 2400];
        m.render(&mut two);
        let one = render(notes("tick"));
        for (a, b) in two.iter().zip(&one) {
            assert!((a - 2.0 * b).abs() < 1e-6);
        }
    }
}
