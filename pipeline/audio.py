"""Numpy synthesis (section 7). No samples, no drums.

Per country: a sustained pad in that country's key, one Karplus-Strong pluck per data year
whose pitch follows the value (snapped to a scale), and a noise whoosh at each transition.
The end card plays every pad root as one chord. All onsets come from the Timeline.
"""
from __future__ import annotations

import logging
import re

import numpy as np
import pyloudnorm as pyln
from scipy.io import wavfile
from scipy.ndimage import minimum_filter1d, uniform_filter1d
from scipy.signal import butter, lfilter, resample_poly, sosfilt

from .config import get_config
from .timeline import Timeline

log = logging.getLogger(__name__)

SCALES = {
    "minor_pentatonic": [0, 3, 5, 7, 10],
    "major_pentatonic": [0, 2, 4, 7, 9],
    "natural_minor": [0, 2, 3, 5, 7, 8, 10],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "major": [0, 2, 4, 5, 7, 9, 11],
}
_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def note_to_midi(name: str) -> int:
    m = re.fullmatch(r"([A-Ga-g])([#b♭♯]?)(-?\d)", name.strip())
    if not m:
        raise ValueError(f"bad note name {name!r}")
    pc = _PC[m.group(1).upper()] + {"#": 1, "♯": 1, "b": -1, "♭": -1, "": 0}[m.group(2)]
    return 12 * (int(m.group(3)) + 1) + pc


def midi_to_hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def scale_notes(root_midi: int, scale: str, octaves: int, register: tuple[int, int]) -> list[int]:
    """Scale degrees over `octaves` octaves (plus the top root), lowest note moved into `register`."""
    base = root_midi
    while base < register[0]:
        base += 12
    while base > register[1]:
        base -= 12
    steps = SCALES[scale]
    notes = [base + 12 * o + s for o in range(octaves) for s in steps]
    notes.append(base + 12 * octaves)
    return notes


def value_to_note(v: float, vmin: float, vmax: float, notes: list[int]) -> int:
    f = 0.5 if vmax <= vmin else (v - vmin) / (vmax - vmin)
    f = min(max(f, 0.0), 1.0)
    return notes[int(round(f * (len(notes) - 1)))]


# ------------------------------------------------------------------ voices

def karplus_strong(freq: float, dur: float, sr: int, t60: float, rng: np.random.Generator,
                   brightness: float = 0.6) -> np.ndarray:
    """Plucked string: delay line + averaging filter + first-order allpass for exact tuning,
    expressed as one IIR filter so scipy runs the loop in C."""
    n = int(dur * sr)
    period = sr / freq
    N = int(np.floor(period - 0.6))
    delta = period - N - 0.5               # fractional delay the allpass must add, in [0.1, 1.1)
    C = (1 - delta) / (1 + delta)
    d = 10 ** (-3.0 / (t60 * freq))        # per-period loss so the note decays 60 dB in t60 s
    a = np.zeros(N + 3)
    a[0] = 1.0
    a[1] += C
    a[N] -= d / 2 * C
    a[N + 1] -= d / 2 * (1 + C)
    a[N + 2] -= d / 2
    b = np.array([1.0, C])
    burst = rng.uniform(-1, 1, N)
    # soften the excitation (one-pole low-pass) for a rounder pluck
    burst = lfilter([brightness], [1, -(1 - brightness)], burst)
    burst -= burst.mean()
    x = np.zeros(n)
    x[: len(burst)] = burst
    y = lfilter(b, a, x)
    fade = min(int(0.05 * sr), n)
    y[-fade:] *= np.linspace(1, 0, fade)
    peak = np.max(np.abs(y)) or 1.0
    return y / peak


def adsr(n: int, sr: int, attack: float, release: float) -> np.ndarray:
    env = np.ones(n)
    a = min(int(attack * sr), n)
    r = min(int(release * sr), n - a)
    if a:
        env[:a] = np.sin(np.linspace(0, np.pi / 2, a)) ** 2
    if r:
        env[n - r:] *= np.cos(np.linspace(0, np.pi / 2, r)) ** 2
    return env


def saw(freq: float, n: int, sr: int, phase0: float = 0.0) -> np.ndarray:
    ph = (phase0 + freq * np.arange(n) / sr) % 1.0
    return 2 * ph - 1


def pad_voice(midi: int, dur: float, sr: int, ac: dict, rng: np.random.Generator) -> np.ndarray:
    """Three detuned saws through a gentle low-pass, slow attack/release; stereo (n, 2)."""
    n = int(dur * sr)
    f = midi_to_hz(midi)
    det = ac["pad_detune_cents"]
    left = np.zeros(n)
    right = np.zeros(n)
    for cents, pan in ((-det, 0.2), (0, 0.5), (det, 0.8)):
        v = saw(f * 2 ** (cents / 1200), n, sr, rng.uniform())
        left += v * (1 - pan)
        right += v * pan
    sos = butter(2, ac["pad_cutoff_hz"], "low", fs=sr, output="sos")
    out = np.stack([sosfilt(sos, left), sosfilt(sos, right)], axis=1) / 3
    if ac["sub_bass"]:
        sub = np.sin(2 * np.pi * (f / 2) * np.arange(n) / sr) * (ac["sub_gain"] / ac["pad_gain"])
        out += sub[:, None] * 0.5
    return out * adsr(n, sr, ac["pad_attack"], ac["pad_release"])[:, None]


def whoosh(dur: float, sr: int, rng: np.random.Generator) -> np.ndarray:
    """Band-passed white noise whose centre sweeps up then down, with a rise/fall envelope; stereo."""
    n = int(dur * sr)
    noise = rng.standard_normal((n, 2))
    blocks = 48
    edges = np.linspace(0, n, blocks + 1).astype(int)
    out = np.zeros_like(noise)
    zi = None
    for k in range(blocks):
        p = (k + 0.5) / blocks
        centre = 250 * (5500 / 250) ** (np.sin(np.pi * p) ** 1.5)  # 250 Hz -> 5.5 kHz -> back
        lo, hi = centre / 1.6, min(centre * 1.6, sr / 2 - 100)
        sos = butter(2, [lo, hi], "band", fs=sr, output="sos")
        if zi is None:
            zi = np.zeros((sos.shape[0], 2, 2))
        seg = noise[edges[k]:edges[k + 1]]
        for ch in range(2):
            out[edges[k]:edges[k + 1], ch], zi[:, :, ch] = sosfilt(sos, seg[:, ch], zi=zi[:, :, ch])
    t = np.linspace(0, 1, n)
    env = np.where(t < 0.6, (t / 0.6) ** 2, ((1 - t) / 0.4) ** 1.5)
    out *= env[:, None]
    return out / (np.max(np.abs(out)) or 1.0)


# ------------------------------------------------------------------ mastering

def true_peak_db(x: np.ndarray, block: int = 2400) -> float:
    """4x-oversampled peak (BS.1770 style). Only blocks whose sample peak is within 3 dB of the
    loudest sample are oversampled, since inter-sample overs happen next to existing peaks."""
    n = len(x)
    nb = (n + block - 1) // block
    pad = np.zeros((nb * block - n, x.shape[1]))
    bp = np.max(np.abs(np.concatenate([x, pad]).reshape(nb, block, -1)), axis=(1, 2))
    best = 0.0
    for b in np.nonzero(bp >= bp.max() * 10 ** (-3 / 20))[0]:
        i0, i1 = max(0, b * block - 64), min(n, (b + 1) * block + 64)
        best = max(best, float(np.max(np.abs(resample_poly(x[i0:i1], 4, 1, axis=0)))))
    return 20 * np.log10(max(best, float(bp.max())) + 1e-12)


def limit(x: np.ndarray, ceiling: float, sr: int, window_ms: float = 6.0) -> np.ndarray:
    """Look-ahead brickwall: per-sample required gain, min-filtered then box-smoothed with the same
    centred window. Each smoothed gain is an average of values <= the required gain at that
    sample, so the output never exceeds the ceiling (sample peak)."""
    w = max(3, int(window_ms / 1000 * sr) | 1)
    peak = np.max(np.abs(x), axis=1)
    g = np.minimum(1.0, ceiling / np.maximum(peak, 1e-12))
    g = minimum_filter1d(g, size=w, mode="nearest")
    g = uniform_filter1d(g, size=w, mode="nearest")
    return x * g[:, None]


def master(x: np.ndarray, sr: int, target_lufs: float, tp_db: float) -> tuple[np.ndarray, dict]:
    sos = butter(2, 30, "high", fs=sr, output="sos")
    x = sosfilt(sos, x, axis=0)
    meter = pyln.Meter(sr)
    ceiling_db = tp_db - 0.4
    for _ in range(10):
        lufs = meter.integrated_loudness(x)
        x = x * 10 ** ((target_lufs - lufs) / 20)
        if true_peak_db(x) <= tp_db - 0.05:
            break
        x = limit(x, 10 ** (ceiling_db / 20), sr)
    tp = true_peak_db(x)
    while tp > tp_db - 0.05:          # final guarantee; may leave loudness a fraction below target
        ceiling_db -= 0.2
        x = limit(x, 10 ** (ceiling_db / 20), sr)
        tp = true_peak_db(x)
    lufs = meter.integrated_loudness(x)
    return x, {"lufs": round(float(lufs), 2), "true_peak_db": round(float(tp), 2), "ceiling_db": round(ceiling_db, 2)}


# ------------------------------------------------------------------ the whole track

def synthesize(tl: Timeline, values: dict[str, list[float]], seed: int = 0, cfg: dict | None = None):
    """values[iso3] lists the data values aligned with tl.slots[i].years.
    Returns (stereo float array (n, 2), info) with every note onset used."""
    cfg = cfg or get_config()
    ac = cfg["audio"]
    sr = ac["sample_rate"]
    rng = np.random.default_rng(seed)
    n_total = int(round(tl.n_frames / tl.fps * sr))
    mix = np.zeros((n_total + sr, 2))
    keys = [note_to_midi(k) for k in ac["keys"]]
    notes_log = []

    def add(sig: np.ndarray, t0: float, gain: float, pan: float | None = None):
        i0 = int(round(t0 * sr))
        if sig.ndim == 1:
            p = 0.5 if pan is None else pan
            sig = np.stack([sig * np.sqrt(1 - p), sig * np.sqrt(p)], axis=1)
        i1 = min(i0 + len(sig), len(mix))
        mix[i0:i1] += sig[: i1 - i0] * gain

    roots = []
    for slot in tl.slots:
        root = keys[slot.index % len(keys)]
        roots.append(root)
        vals = values[slot.iso3]
        vmin, vmax = min(vals), max(vals)
        notes = scale_notes(root, ac["scale"], ac["scale_octaves"], tuple(ac["pluck_register"]))
        # pad: from the transition through the hold, releasing into the next country
        pad_len = (slot.end - slot.start) + ac["pad_release"]
        add(pad_voice(root, pad_len, sr, ac, rng), slot.start, ac["pad_gain"])
        add(whoosh(tl.transition, sr, rng), slot.start, ac["whoosh_gain"])
        for year, onset, v in zip(slot.years, slot.onsets, vals):
            m = value_to_note(v, vmin, vmax, notes)
            sig = karplus_strong(midi_to_hz(m), ac["pluck_length"], sr, ac["pluck_t60"], rng)
            add(sig, onset, ac["pluck_gain"], pan=0.5 + rng.uniform(-0.15, 0.15))
            notes_log.append({"t": onset, "iso3": slot.iso3, "year": year, "value": v, "midi": m})

    # end card: every pad root as one chord, slow fade, plus a low root pluck to resolve
    chord_notes = sorted(set(roots))
    chord_len = tl.duration - tl.end_start
    chord = np.zeros((int(chord_len * sr), 2))
    for i, m in enumerate(chord_notes):
        v = pad_voice(m + 12, chord_len, sr, {**ac, "pad_attack": 0.35, "pad_release": chord_len * 0.7,
                                               "sub_bass": False}, rng)
        chord += v[: len(chord)]
    chord /= max(1, len(chord_notes)) ** 0.5
    add(chord, tl.end_start, ac["end_chord_gain"])
    add(karplus_strong(midi_to_hz(min(chord_notes) + 12), 2.5, sr, 2.5, rng), tl.end_start, ac["pluck_gain"] * 0.8)

    mix = mix[:n_total]
    out, stats = master(mix, sr, ac["target_lufs"], ac["true_peak_db"] - ac.get("aac_headroom_db", 0.0))
    info = {"sample_rate": sr, "notes": notes_log, "roots": roots, "chord": chord_notes, **stats}
    return out.astype(np.float32), info


def write_wav(path, x: np.ndarray, sr: int) -> None:
    wavfile.write(str(path), sr, x.astype(np.float32))
