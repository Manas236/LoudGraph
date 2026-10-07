import numpy as np
import pyloudnorm as pyln

from pipeline.audio import limit, master, note_to_midi, scale_notes, true_peak_db, value_to_note


def test_note_names():
    assert note_to_midi("A4") == 69
    assert note_to_midi("A2") == 45
    assert note_to_midi("C#3") == 49 and note_to_midi("Bb2") == 46


def test_scale_and_mapping():
    notes = scale_notes(45, "minor_pentatonic", 2, (52, 63))
    assert notes[0] == 57 and notes[-1] == 81 and len(notes) == 11
    assert value_to_note(0, 0, 10, notes) == 57
    assert value_to_note(10, 0, 10, notes) == 81
    assert value_to_note(5, 0, 10, notes) == notes[5]
    # pitch is monotone in value
    seq = [value_to_note(v, 0, 10, notes) for v in np.linspace(0, 10, 50)]
    assert seq == sorted(seq)


def test_limiter_never_exceeds_ceiling():
    rng = np.random.default_rng(0)
    x = rng.standard_normal((48000, 2)) * 0.3
    x[1000:1010] = 3.0
    y = limit(x, 0.5, 48000)
    assert np.max(np.abs(y)) <= 0.5 + 1e-9


def test_master_hits_targets():
    sr = 48000
    rng = np.random.default_rng(1)
    t = np.arange(sr * 10) / sr
    x = np.stack([np.sin(2 * np.pi * 220 * t), np.sin(2 * np.pi * 330 * t)], axis=1) * 0.05
    x[::4800] += rng.uniform(-1, 1, (len(x[::4800]), 2))  # sharp transients
    y, st = master(x, sr, -14.0, -1.0)
    assert abs(pyln.Meter(sr).integrated_loudness(y) + 14.0) < 0.5
    assert true_peak_db(y) <= -1.0
