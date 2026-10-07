"""Fix-pass verification artefacts.

crash_test()      out/crash_test.mp4 from a synthetic flat -> crash -> recover series (+ stems, analysis.json)
compare_runs()    out/<new>/compare.png (old vs new frame, same timestamp) and compare_spectrogram.png
                  (old vs new spectrogram over the first country's segment, 60 Hz - 3 kHz, log scale)
"""
from __future__ import annotations

import copy
import json
import logging
import shutil

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.io import wavfile

from .assets import font_files
from .config import get_config, path, run_dir
from .timeline import Timeline, build

log = logging.getLogger(__name__)

FCR_YEARS = list(range(1990, 2021))
FCR_VALUES = [100.0] * 13 + [30.0, 31.0, 29.0, 30.0, 32.0] + [30 + 70 * (k + 1) / 13 for k in range(13)]


def _font(size, w="semibold"):
    return ImageFont.truetype(str(font_files()[w]), size)


# ------------------------------------------------------------------ crash test clip

def crash_test() -> dict:
    from . import audio, render
    from .verify import loudness, run_detection
    cfg = copy.deepcopy(get_config())
    cfg["video"]["min_seconds"] = 0          # one country: keep the clip short
    d = path("out") / "crash_test"
    d.mkdir(parents=True, exist_ok=True)
    topic = {"id": "crash_test", "title": "Crash test", "subtitle": "Synthetic test series (not real data)",
             "unit_format": "{:.0f}", "source": "synthetic", "start_year": 1990, "category": "test", "code": "-"}
    tl = build([("TST", FCR_YEARS, FCR_VALUES)], 1990, 2020, cfg)
    tl.save(d / "timeline.json")
    mix, stems, info = audio.synthesize(tl, {"TST": FCR_VALUES}, seed=5, cfg=cfg)
    audio.write_wav(d / "audio.wav", mix, info["sample_rate"])
    for k, x in stems.items():
        audio.write_wav(d / f"{k}.wav", x, info["sample_rate"])
    views = [render.CountryView(iso3="TST", name="Test series", iso2=None, color=cfg["render"]["palette"][0],
                                years=FCR_YEARS, values=FCR_VALUES)]
    rinfo = render.render_video(topic, tl, views, d / "audio.wav", d / "video.mp4", cfg=cfg)
    shutil.copy2(d / "video.mp4", path("out") / "crash_test.mp4")

    sr = info["sample_rate"]
    s = tl.slots[0]
    e = next(e for e in s.events if e["kind"] == "fall")
    pad = stems["pad"].mean(axis=1)

    def win(y0, y1):
        return pad[int(s.onset_of(y0) * sr):int(s.onset_of(y1) * sr)]

    def rms(x):
        return float(np.sqrt(np.mean(x ** 2)))

    def centroid(x):
        sp = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        f = np.fft.rfftfreq(len(x), 1 / sr)
        return float((sp * f).sum() / sp.sum())

    flat, low = win(1992, 2001), win(2003, 2005)
    det = run_detection(stems["pluck"], sr, e["t_from"], e["t_to"])
    analysis = {
        "events": s.events,
        "slowmo_step_s": round(e["t_to"] - e["t_from"], 3),
        "normal_step_s": round(s.onset_of(1991) - s.onset_of(1990), 3),
        "pad_flat": {"rms": round(rms(flat), 5), "centroid_hz": round(centroid(flat))},
        "pad_crash": {"rms": round(rms(low), 5), "centroid_hz": round(centroid(low))},
        "event_run_intended_midi": next(x["run_midis"] for x in info["events"] if x["year"] == e["year"]),
        "event_run_detected_midi": det["midi"],
        "event_run_longest_descending": det["longest_descending"],
        "stem_lufs": info["stem_lufs"],
        "mp4_loudness": loudness(path("out") / "crash_test.mp4"),
        "duration_s": tl.duration,
        "render": rinfo,
    }
    (d / "analysis.json").write_text(json.dumps(analysis, indent=1), encoding="utf-8")
    return analysis


# ------------------------------------------------------------------ old vs new

def _frame(video, t):
    from .verify import _frame as f
    return f(video, t)


def compare_frames(old_run: str, new_run: str) -> str:
    od, nd = run_dir(old_run), run_dir(new_run)
    otl = Timeline.load(od / "timeline.json")
    s = otl.slots[0]
    t = (s.draw_start + s.draw_end) / 2          # mid first country (Germany) on the OLD timeline
    crop = 1600                                  # below this the old frames carried the removed brand text
    old = _frame(od / "video.mp4", t).crop((0, 0, 1080, crop))
    new = _frame(nd / "video.mp4", t).crop((0, 0, 1080, crop))
    bar = 90
    img = Image.new("RGB", (2 * 1080 + 40, crop + bar), (24, 26, 32))
    img.paste(old, (0, bar))
    img.paste(new, (1080 + 40, bar))
    dr = ImageDraw.Draw(img)
    f = _font(40)
    dr.text((20, 22), f"v0 (old)  {old_run}  t={t:.2f}s", font=f, fill=(230, 233, 240))
    dr.text((1080 + 60, 22), f"fix pass 01 (new)  {new_run}  t={t:.2f}s", font=f, fill=(230, 233, 240))
    for x in (540, 1080 + 40 + 540):              # the centre axis, for the eye
        dr.line([(x, bar), (x, bar + 18)], fill=(255, 80, 80), width=3)
        dr.line([(x, bar + crop - 18), (x, bar + crop)], fill=(255, 80, 80), width=3)
    out = nd / "compare.png"
    img.save(out)
    return str(out)


_MAGMA = np.array([[0, 0, 4], [40, 11, 84], [101, 21, 110], [159, 42, 99], [212, 72, 66], [245, 125, 21],
                   [250, 193, 39], [252, 253, 191]], dtype=float)


def _cmap(v):
    """v in 0..1 -> RGB using a magma-like ramp."""
    x = np.clip(v, 0, 1) * (len(_MAGMA) - 1)
    i = np.minimum(x.astype(int), len(_MAGMA) - 2)
    f = (x - i)[..., None]
    return (_MAGMA[i] * (1 - f) + _MAGMA[i + 1] * f).astype(np.uint8)


def _spectrogram(x, sr, t0, t1, fmin=60, fmax=3000, rows=360, cols=1400, n_fft=8192, hop=256):
    seg = x[int(t0 * sr):int(t1 * sr)]
    if seg.ndim > 1:
        seg = seg.mean(axis=1)
    seg = np.pad(seg, n_fft // 2)
    n = 1 + (len(seg) - n_fft) // hop
    frames = np.lib.stride_tricks.as_strided(seg, (n, n_fft), (seg.strides[0] * hop, seg.strides[0]))
    spec = np.abs(np.fft.rfft(frames * np.hanning(n_fft), axis=1))
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    logf = np.geomspace(fmax, fmin, rows)            # top row = fmax
    cols_idx = np.linspace(0, n - 1, cols)
    db = 20 * np.log10(spec + 1e-9)
    grid = np.array([np.interp(logf, freqs, db[int(round(c))]) for c in cols_idx]).T
    return grid, logf


def compare_spectrogram(old_run: str, new_run: str, year: int = 2020) -> str:
    od, nd = run_dir(old_run), run_dir(new_run)
    panels = []
    for label, d in (("v0 (old)", od), ("fix pass 01 (new)", nd)):
        tl = Timeline.load(d / "timeline.json")
        s = tl.slots[0]
        sr, x = wavfile.read(d / "audio.wav")
        g, logf = _spectrogram(x, sr, s.start, s.end)
        crash_t = s.onset_of(year)
        panels.append((label, g, s, crash_t))
    vmax = max(p[1].max() for p in panels)
    rows, cols = panels[0][1].shape
    left, top, gap = 110, 70, 90
    img = Image.new("RGB", (left + cols + 40, top + 2 * rows + gap + 70), (18, 19, 24))
    dr = ImageDraw.Draw(img)
    f, fs = _font(28), _font(20, "regular")
    for k, (label, g, s, crash_t) in enumerate(panels):
        y0 = top + k * (rows + gap)
        rgb = _cmap((g - (vmax - 70)) / 70)       # 70 dB range below the loudest bin of both
        img.paste(Image.fromarray(rgb), (left, y0))
        dur = s.end - s.start
        dr.text((left, y0 - 40), f"{label} - {s.iso3} segment {s.start:.2f}-{s.end:.2f}s ({dur:.1f}s)",
                font=f, fill=(230, 233, 240))
        for hz in (60, 100, 200, 500, 1000, 2000, 3000):
            yy = y0 + (np.log(3000) - np.log(hz)) / (np.log(3000) - np.log(60)) * (rows - 1)
            dr.line([(left - 8, yy), (left, yy)], fill=(200, 200, 200))
            dr.text((left - 14, yy - 11), f"{hz if hz < 1000 else str(hz // 1000) + 'k'}", font=fs,
                    fill=(200, 200, 200), anchor="ra")
        for sec in range(0, int(dur) + 1):
            xx = left + sec / dur * (cols - 1)
            dr.line([(xx, y0 + rows), (xx, y0 + rows + 6)], fill=(200, 200, 200))
            dr.text((xx, y0 + rows + 8), f"{sec}s", font=fs, fill=(200, 200, 200), anchor="ma")
        cx = left + (crash_t - s.start) / dur * (cols - 1)
        for yy in range(y0, y0 + rows, 14):
            dr.line([(cx, yy), (cx, yy + 7)], fill=(255, 255, 255), width=2)
        dr.text((cx + 8, y0 + 6), f"{year} crash", font=f, fill=(255, 255, 255))
    dr.text((left, top + 2 * rows + gap + 30), "Spectrogram 60 Hz - 3 kHz, log frequency, 70 dB range",
            font=fs, fill=(170, 170, 170))
    out = nd / "compare_spectrogram.png"
    img.save(out)
    return str(out)


def compare_runs(old_run: str, new_run: str) -> dict:
    return {"compare": compare_frames(old_run, new_run), "spectrogram": compare_spectrogram(old_run, new_run)}
