"""Numpy synthesis (section 7, revised in fix pass 01). No samples, no drums.

Three stems, mixed at the end and also written out for verification:
  pluck  the lead: one Karplus-Strong pluck per data year, pitch = the value on a 3-octave scale;
         an EVENT year (move >= 25% of range) is approached by a fast scale run from the previous
         note to the new one (falls are heard falling, jumps rising)
  pad    one sustained note per country whose low-pass cutoff and gain follow the current value
         (smoothed ~150 ms), so a drop darkens and thins the sound; ducks to near silence after a fall
  fx     whoosh between countries, a short low sub hit on falls, a short bright accent on jumps
The pad and fx stems are balanced by measured loudness relative to the pluck stem, so the plucks
always lead. All onsets come from the Timeline.
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
from .timeline import Slot, Timeline

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


def note_indices(values: list[float], n_notes: int, move_threshold: float = 0.15, min_steps: int = 2) -> list[int]:
    """Scale index per value (own min-max range). Guarantees that any move of >= move_threshold of
    the range changes the note by at least min_steps scale steps (never the same/adjacent note)."""
    vmin, vmax = min(values), max(values)
    rng = vmax - vmin
    idx = []
    for i, v in enumerate(values):
        f = 0.5 if rng <= 0 else (v - vmin) / rng
        k = int(round(min(max(f, 0.0), 1.0) * (n_notes - 1)))
        if i and rng > 0 and abs(v - values[i - 1]) >= move_threshold * rng and abs(k - idx[-1]) < min_steps:
            k = int(np.clip(idx[-1] + min_steps * np.sign(v - values[i - 1]), 0, n_notes - 1))
        idx.append(k)
    return idx


def run_indices(p: int, q: int, max_notes: int) -> list[int]:
    """Scale indices strictly between p and q (in order from p towards q), at most max_notes."""
    if abs(q - p) <= 1:
        return []
    inner = list(range(p + 1, q)) if q > p else list(range(p - 1, q, -1))
    if len(inner) <= max_notes:
        return inner
    pos = np.linspace(0, len(inner) - 1, max_notes)
    return [inner[int(round(x))] for x in pos]


# ------------------------------------------------------------------ voices

def karplus_strong(freq: float, dur: float, sr: int, t60: float, rng: np.random.Generator,
                   brightness: float = 0.9) -> np.ndarray:
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
    burst = lfilter([brightness], [1, -(1 - brightness)], burst)
    burst -= burst.mean()
    x = np.zeros(n)
    x[: len(burst)] = burst
    y = lfilter(b, a, x)
    fade = min(int(0.03 * sr), n)
    y[-fade:] *= np.linspace(1, 0, fade)
    peak = np.max(np.abs(y)) or 1.0
    return y / peak


def pluck(freq: float, dur: float, sr: int, t60: float, rng, brightness: float, click: float) -> np.ndarray:
    y = karplus_strong(freq, dur, sr, t60, rng, brightness)
    if click > 0:  # bright pick transient
        k = int(0.002 * sr)
        c = rng.uniform(-1, 1, k) * np.linspace(1, 0, k)
        c = sosfilt(butter(2, 3000, "high", fs=sr, output="sos"), c)
        y[:k] += click * c / (np.max(np.abs(c)) or 1.0)
    return y


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


def pad_voice(midi: int, n: int, sr: int, cutoff_hz: np.ndarray, gain: np.ndarray, ac: dict, rng,
              block: int = 240) -> np.ndarray:
    """Three saws detuned by at most pad_detune_cents in total, through a 2-pole low-pass whose cutoff
    follows `cutoff_hz` (one value per block), times a per-sample `gain`. Stereo (n, 2)."""
    f = midi_to_hz(midi)
    det = ac["pad_detune_cents"] / 2
    left, right = np.zeros(n), np.zeros(n)
    for cents, pan in ((-det, 0.25), (0.0, 0.5), (det, 0.75)):
        v = saw(f * 2 ** (cents / 1200), n, sr, rng.uniform())
        left += v * (1 - pan)
        right += v * pan
    out = np.zeros((n, 2))
    zi = np.zeros((1, 2, 2))
    for bi, i0 in enumerate(range(0, n, block)):
        i1 = min(i0 + block, n)
        fc = float(np.clip(cutoff_hz[min(bi, len(cutoff_hz) - 1)], 40, sr / 2 - 500))
        sos = butter(2, fc, "low", fs=sr, output="sos")
        out[i0:i1, 0], zi[:, :, 0] = sosfilt(sos, left[i0:i1], zi=zi[:, :, 0])
        out[i0:i1, 1], zi[:, :, 1] = sosfilt(sos, right[i0:i1], zi=zi[:, :, 1])
    return out / 3 * gain[:, None]


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
        centre = 250 * (5500 / 250) ** (np.sin(np.pi * p) ** 1.5)
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


def sub_hit(sr: int, dur: float = 0.45) -> np.ndarray:
    """Short low thump for a fall: sine sweeping 75 -> 42 Hz with a fast decay."""
    n = int(dur * sr)
    t = np.arange(n) / sr
    freq = 42 + 33 * np.exp(-t / 0.06)
    y = np.sin(2 * np.pi * np.cumsum(freq) / sr) * np.exp(-t / 0.13)
    y[: int(0.004 * sr)] *= np.linspace(0, 1, int(0.004 * sr))
    return y / (np.max(np.abs(y)) or 1.0)


def accent(freq: float, sr: int, rng, dur: float = 0.35) -> np.ndarray:
    """Short bright accent for a jump: bell-like partials above the arrival note plus a click."""
    n = int(dur * sr)
    t = np.arange(n) / sr
    y = sum(a * np.sin(2 * np.pi * freq * m * t) * np.exp(-t / tau)
            for m, a, tau in ((2.0, 1.0, 0.12), (3.01, 0.6, 0.08), (4.2, 0.4, 0.05)))
    k = int(0.003 * sr)
    y[:k] += sosfilt(butter(2, 4000, "high", fs=sr, output="sos"), rng.uniform(-1, 1, k)) * 0.8
    return y / (np.max(np.abs(y)) or 1.0)


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


def master_filters(x: np.ndarray, sr: int, lowpass_hz: float | None) -> np.ndarray:
    x = sosfilt(butter(2, 30, "high", fs=sr, output="sos"), x, axis=0)
    if lowpass_hz:
        # AAC drops content above ~16 kHz; removing it first means the limiter shapes what AAC keeps
        x = sosfilt(butter(8, lowpass_hz, "low", fs=sr, output="sos"), x, axis=0)
    return x


def master(x: np.ndarray, sr: int, target_lufs: float, tp_db: float, lowpass_hz: float | None = None):
    """Returns (mastered, stats). stats["gain"] is the linear normalisation gain applied (the
    limiter only shaves peaks on top of it), so stems can be scaled the same way."""
    x = master_filters(x, sr, lowpass_hz)
    meter = pyln.Meter(sr)
    ceiling_db = tp_db - 0.4
    gain = 1.0
    # normalise -> limit, repeated: each pass recovers the loudness the previous limiting took,
    # until loudness is within 0.15 LU of the target AND the true peak is under the ceiling
    for _ in range(16):
        g = 10 ** ((target_lufs - meter.integrated_loudness(x)) / 20)
        x, gain = x * g, gain * g
        x = limit(x, 10 ** (ceiling_db / 20), sr)
        tp = true_peak_db(x)
        if tp > tp_db - 0.05:
            ceiling_db -= 0.2          # inter-sample overs: aim the sample-peak ceiling lower
            continue
        if abs(meter.integrated_loudness(x) - target_lufs) <= 0.15:
            break
    tp = true_peak_db(x)
    while tp > tp_db - 0.05:          # final guarantee; may leave loudness a fraction below target
        ceiling_db -= 0.2
        x = limit(x, 10 ** (ceiling_db / 20), sr)
        tp = true_peak_db(x)
    lufs = meter.integrated_loudness(x)
    return x, {"lufs": round(float(lufs), 2), "true_peak_db": round(float(tp), 2),
               "ceiling_db": round(ceiling_db, 2), "gain": gain}


def lufs(x: np.ndarray, sr: int) -> float:
    v = pyln.Meter(sr).integrated_loudness(x)
    return float(v) if np.isfinite(v) else -120.0


# ------------------------------------------------------------------ the whole track

def _control(slot: Slot, years: list[int], values: list[float], t: np.ndarray) -> np.ndarray:
    """Normalised value (0..1 of the country's range) the line head shows at each time t."""
    vmin, vmax = min(values), max(values)
    rng = (vmax - vmin) or 1.0
    yp = np.array([slot.year_pos(x) for x in t])
    return np.clip((np.interp(yp, years, values) - vmin) / rng, 0, 1)


def synthesize(tl: Timeline, values: dict[str, list[float]], seed: int = 0, cfg: dict | None = None):
    """values[iso3] lists the data values aligned with tl.slots[i].years.
    Returns (mix (n, 2) float32, stems {"pluck","pad","fx"} (n, 2) float32, info)."""
    cfg = cfg or get_config()
    ac = cfg["audio"]
    sr = ac["sample_rate"]
    rng = np.random.default_rng(seed)
    n_total = int(round(tl.n_frames / tl.fps * sr))
    stems = {k: np.zeros((n_total + 2 * sr, 2)) for k in ("pluck", "pad", "fx")}
    keys = [note_to_midi(k) for k in ac["keys"]]
    notes_log, events_log = [], []

    def add(stem: str, sig: np.ndarray, t0: float, gain: float = 1.0, pan: float = 0.5):
        i0 = int(round(t0 * sr))
        if sig.ndim == 1:
            sig = np.stack([sig * np.sqrt(1 - pan), sig * np.sqrt(pan)], axis=1)
        buf = stems[stem]
        i1 = min(i0 + len(sig), len(buf))
        buf[i0:i1] += sig[: i1 - i0] * gain

    def pl(m, t0, gain=1.0, t60=None, length=None):
        sig = pluck(midi_to_hz(m), length or ac["pluck_length"], sr, t60 or ac["pluck_t60"], rng,
                    ac["pluck_brightness"], ac["pluck_click"])
        add("pluck", sig, t0, gain, pan=0.5 + rng.uniform(-0.12, 0.12))

    roots = []
    for slot in tl.slots:
        root = keys[slot.index % len(keys)]
        roots.append(root)
        vals = values[slot.iso3]
        notes = scale_notes(root, ac["scale"], ac["scale_octaves"], tuple(ac["pluck_register"]))
        idx = note_indices(vals, len(notes), ac["move_threshold"], ac["move_min_steps"])
        events = {e["year"]: e for e in slot.events}

        # --- pluck lead (+ event runs)
        for i, (year, onset, v) in enumerate(zip(slot.years, slot.onsets, vals)):
            e = events.get(year)
            if e and i:
                run = run_indices(idx[i - 1], idx[i], ac["run_max_notes"])
                span = e["t_to"] - e["t_from"]
                for k, ri in enumerate(run):
                    tr = e["t_from"] + span * (k + 1) / (len(run) + 1)
                    pl(notes[ri], tr, ac["run_gain"], t60=ac["run_t60"], length=ac["run_t60"] + 0.05)
                    notes_log.append({"t": tr, "iso3": slot.iso3, "year": year, "midi": notes[ri], "kind": "run"})
                events_log.append({**e, "iso3": slot.iso3, "from_midi": notes[idx[i - 1]], "to_midi": notes[idx[i]],
                                   "run_midis": [notes[r] for r in run]})
            pl(notes[idx[i]], onset, ac["event_note_gain"] if e else 1.0)
            notes_log.append({"t": onset, "iso3": slot.iso3, "year": year, "value": v, "midi": notes[idx[i]],
                              "kind": "year"})
            # --- fx on events
            if e:
                if e["kind"] == "fall":
                    add("fx", sub_hit(sr), onset, ac["sub_hit_gain"])
                else:
                    add("fx", accent(midi_to_hz(notes[idx[i]]), sr, rng), onset, ac["accent_gain"])

        # --- pad following the data
        t0 = slot.start
        n = int(round((slot.end - slot.start + ac["pad_release"]) * sr))
        block = 240
        tb = t0 + (np.arange((n + block - 1) // block) + 0.5) * block / sr
        c = _control(slot, slot.years, vals, tb)
        c = uniform_filter1d(c, size=max(1, int(ac["pad_smooth_s"] * sr / block)), mode="nearest")
        cutoff = ac["pad_cutoff_min_hz"] * (ac["pad_cutoff_max_hz"] / ac["pad_cutoff_min_hz"]) ** c
        gain_b = ac["pad_gain_min"] + (1 - ac["pad_gain_min"]) * c
        for ev in slot.events:  # riser-less silence after a fall
            if ev["kind"] == "fall":
                inside = (tb >= ev["t_to"]) & (tb < ev["t_to"] + ac["duck_seconds"])
                gain_b[inside] *= ac["duck_level"]
        ts = t0 + np.arange(n) / sr
        gain = np.interp(ts, tb, gain_b) * adsr(n, sr, ac["pad_attack"], ac["pad_release"])
        add("pad", pad_voice(root, n, sr, cutoff, gain, ac, rng, block), t0)
        add("fx", whoosh(tl.transition, sr, rng), slot.start, ac["whoosh_gain"])

    # --- end card: every pad root as one chord with a slow fade, plus a low resolving pluck
    chord_notes = sorted(set(roots))
    n = int((tl.duration - tl.end_start) * sr)
    chord = np.zeros((n, 2))
    for m in chord_notes:
        g = adsr(n, sr, 0.35, n / sr * 0.7)
        chord += pad_voice(m + 12, n, sr, np.full(n // 240 + 1, ac["pad_cutoff_max_hz"] * 0.6), g, ac, rng)
    chord /= max(1, len(chord_notes)) ** 0.5
    add("pad", chord, tl.end_start, ac["end_chord_gain"])
    first_notes = scale_notes(min(chord_notes), ac["scale"], ac["scale_octaves"], tuple(ac["pluck_register"]))
    pl(first_notes[0], tl.end_start, 0.9, t60=1.6, length=2.0)
    notes_log.append({"t": tl.end_start, "iso3": None, "year": None, "midi": first_notes[0], "kind": "resolve"})

    # --- balance stems by loudness (plucks lead), mix, master
    for k in stems:
        stems[k] = stems[k][:n_total]
    l_pluck = lufs(stems["pluck"], sr)
    for k, below in (("pad", ac["pad_below_pluck_lu"]), ("fx", ac["fx_below_pluck_lu"])):
        lk = lufs(stems[k], sr)
        if lk > -120:
            stems[k] *= 10 ** ((l_pluck - below - lk) / 20)
    mix = stems["pluck"] + stems["pad"] + stems["fx"]
    out, stats = master(mix, sr, ac["target_lufs"], ac["true_peak_db"] - ac.get("aac_headroom_db", 0.0),
                        ac.get("master_lowpass_hz"))
    for k in stems:
        stems[k] = (master_filters(stems[k], sr, ac.get("master_lowpass_hz")) * stats["gain"]).astype(np.float32)
    stem_lufs = {k: round(lufs(v, sr), 2) for k, v in stems.items()}
    info = {"sample_rate": sr, "notes": notes_log, "events": events_log, "roots": roots, "chord": chord_notes,
            "stem_lufs": stem_lufs, "pluck_minus_pad_lu": round(stem_lufs["pluck"] - stem_lufs["pad"], 2),
            **{k: v for k, v in stats.items() if k != "gain"}}
    return out.astype(np.float32), stems, info


def write_wav(path, x: np.ndarray, sr: int) -> None:
    wavfile.write(str(path), sr, x.astype(np.float32))
