import copy

import numpy as np
import pyloudnorm as pyln
import pytest

from pipeline.audio import (limit, master, note_indices, note_to_midi, run_indices, scale_notes, synthesize,
                            true_peak_db, value_to_note)
from pipeline.config import get_config
from pipeline.timeline import build
from pipeline.verify import pitch_report, run_detection

SR = 48000


def test_note_names():
    assert note_to_midi("A4") == 69
    assert note_to_midi("A2") == 45
    assert note_to_midi("C#3") == 49 and note_to_midi("Bb2") == 46


def test_three_octave_scale_and_monotone_mapping():
    notes = scale_notes(45, "minor_pentatonic", 3, (48, 55))
    assert len(notes) == 16 and notes[-1] - notes[0] == 36
    assert value_to_note(0, 0, 10, notes) == notes[0] and value_to_note(10, 0, 10, notes) == notes[-1]
    seq = [value_to_note(v, 0, 10, notes) for v in np.linspace(0, 10, 80)]
    assert seq == sorted(seq)


def test_big_moves_always_change_the_note_by_two_steps():
    rng = np.random.default_rng(0)
    for _ in range(300):
        vals = list(np.cumsum(rng.normal(0, 1, 30)))
        idx = note_indices(vals, 16, 0.15, 2)
        rngv = max(vals) - min(vals)
        for i in range(1, len(vals)):
            if abs(vals[i] - vals[i - 1]) >= 0.15 * rngv:
                assert abs(idx[i] - idx[i - 1]) >= 2, (vals[i - 1], vals[i], idx[i - 1], idx[i])


def test_run_indices():
    r = run_indices(15, 0, 9)          # 14 inner steps thinned to 9, falling
    assert len(r) == 9 and r == sorted(r, reverse=True) and r[0] == 14 and r[-1] == 1
    assert run_indices(3, 7, 9) == [4, 5, 6]
    assert run_indices(5, 6, 9) == [] and run_indices(5, 5, 9) == []


def test_limiter_never_exceeds_ceiling():
    rng = np.random.default_rng(0)
    x = rng.standard_normal((48000, 2)) * 0.3
    x[1000:1010] = 3.0
    y = limit(x, 0.5, 48000)
    assert np.max(np.abs(y)) <= 0.5 + 1e-9


def test_master_hits_targets():
    rng = np.random.default_rng(1)
    t = np.arange(SR * 10) / SR
    x = np.stack([np.sin(2 * np.pi * 220 * t), np.sin(2 * np.pi * 330 * t)], axis=1) * 0.05
    x[::4800] += rng.uniform(-1, 1, (len(x[::4800]), 2))  # sharp transients
    y, st = master(x, SR, -14.0, -1.0)
    assert abs(pyln.Meter(SR).integrated_loudness(y) + 14.0) < 0.3
    assert true_peak_db(y) <= -1.0


# ------------------------------------------------------------------ synthesized track (shared fixture)

YEARS = list(range(1990, 2021))
FCR = [100.0] * 13 + [30.0, 31.0, 29.0, 30.0, 32.0] + [30 + 70 * (k + 1) / 13 for k in range(13)]   # flat-crash-recover
SERIES = {
    "UPP": list(10 + 0.8 * np.arange(31) + 2.5 * np.sin(np.arange(31) / 2.3)),    # rising, wobbly
    "DWN": list(60 - 1.1 * np.arange(31) + 3 * np.sin(np.arange(31) / 1.7)),      # falling, wobbly
    "FCR": FCR,
}


@pytest.fixture(scope="module")
def track():
    cfg = copy.deepcopy(get_config())
    cfg["video"]["min_seconds"] = 0      # keep the test track short (4.5 s per country + slow-mo)
    tl = build([(k, YEARS, v) for k, v in SERIES.items()], 1990, 2020, cfg)
    vals = {s.iso3: list(SERIES[s.iso3]) for s in tl.slots}
    mix, stems, info = synthesize(tl, vals, seed=11, cfg=cfg)
    return tl, mix, stems, info


def test_pitch_follows_the_data_on_the_pluck_stem(track):
    tl, mix, stems, info = track
    rep = pitch_report(stems["pluck"], SR, info["notes"])
    print("Spearman per country:", {k: v["rho"] for k, v in rep["per_country"].items()})
    for iso, v in rep["per_country"].items():
        assert v["detected"] >= v["years"] - 2, (iso, v)
        assert v["rho"] >= 0.9, (iso, v)


def test_plucks_lead_the_pad_by_6_lu(track):
    tl, mix, stems, info = track
    lp = pyln.Meter(SR).integrated_loudness(stems["pluck"].astype(np.float64))
    lpad = pyln.Meter(SR).integrated_loudness(stems["pad"].astype(np.float64))
    print(f"pluck {lp:.2f} LUFS, pad {lpad:.2f} LUFS, diff {lp - lpad:.2f} LU")
    assert lp >= lpad + 6


def _rms(x):
    return float(np.sqrt(np.mean(x ** 2)))


def _centroid(x):
    sp = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    f = np.fft.rfftfreq(len(x), 1 / SR)
    return float((sp * f).sum() / sp.sum())


def test_crash_darkens_and_thins_the_pad_and_plays_a_falling_run(track):
    tl, mix, stems, info = track
    s = next(s for s in tl.slots if s.iso3 == "FCR")
    crash = next(e for e in s.events if e["kind"] == "fall")
    assert crash["year"] == 2003
    pad = stems["pad"].mean(axis=1)

    def win(y0, y1):
        return pad[int(s.onset_of(y0) * SR):int(s.onset_of(y1) * SR)]
    flat, low = win(1992, 2001), win(2003, 2005)      # flat years vs the crash year and the one after
    r_flat, r_low = _rms(flat), _rms(low)
    c_flat, c_low = _centroid(flat), _centroid(low)
    print(f"pad stem: flat RMS {r_flat:.4f} centroid {c_flat:.0f} Hz | crash RMS {r_low:.4f} centroid {c_low:.0f} Hz")
    assert r_low < 0.4 * r_flat
    assert c_low < 0.5 * c_flat
    det = run_detection(stems["pluck"], SR, crash["t_from"], crash["t_to"])
    print("event run detected (MIDI):", det["midi"], "longest descending:", det["longest_descending"])
    assert det["longest_descending"] >= 4


def test_sub_bass_only_on_falls(track):
    tl, mix, stems, info = track
    fx = stems["fx"].mean(axis=1)
    from scipy.signal import butter, sosfilt
    low = sosfilt(butter(4, 90, "low", fs=SR, output="sos"), fx)
    fall_times = [e["t_to"] for s in tl.slots for e in s.events if e["kind"] == "fall"]
    mask = np.zeros(len(low), bool)
    for t in fall_times:
        mask[int(t * SR):int((t + 0.5) * SR)] = True
    assert _rms(low[mask]) > 10 * _rms(low[~mask])
