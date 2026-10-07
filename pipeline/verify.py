"""Verification gate (section 14, extended in fix pass 01) for a rendered run:

  * ffprobe: 1080x1920, 30 fps, 30-45 s, an audio stream
  * loudness of the final MP4 via FFmpeg ebur128: -14 +-1 LUFS integrated, true peak <= -1 dBFS
  * sync: note onsets detected in audio.wav (spectral flux) vs the timeline's year onsets,
    median error must be <= 40 ms
  * stems: pluck stem integrated loudness >= pad stem + 6 LU
  * data -> pitch: YIN pitch of every year's pluck on the pluck stem; Spearman(values, notes) >= 0.9
  * centring: the owner's method on MP4 frames - horizontal extent of non-background pixels per
    element band; every centred element at x = 539.5 +-2 px and margins equal within 4 px
  * contact sheet: one frame mid-way through each country plus the end card -> contact.png
Results go to out/<run_id>/verify.json.
"""
from __future__ import annotations

import io
import json
import logging
import re
import shutil
import subprocess

import numpy as np
from scipy.io import wavfile
from scipy.ndimage import maximum_filter1d, median_filter
from scipy.stats import spearmanr

from .config import country_by_iso3, get_config, run_dir
from .timeline import Timeline

log = logging.getLogger(__name__)


def ffprobe(video) -> dict:
    out = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-print_format", "json", "-show_streams",
                          "-show_format", str(video)], capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    a = [s for s in d["streams"] if s["codec_type"] == "audio"]
    num, den = (int(x) for x in v["r_frame_rate"].split("/"))
    return {"width": v["width"], "height": v["height"], "fps": num / den, "vcodec": v["codec_name"],
            "pix_fmt": v.get("pix_fmt"), "duration": float(d["format"]["duration"]), "audio_streams": len(a),
            "acodec": a[0]["codec_name"] if a else None, "sample_rate": int(a[0]["sample_rate"]) if a else None,
            "bit_rate": int(d["format"].get("bit_rate", 0))}


def loudness(video) -> dict:
    r = subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-nostats", "-i", str(video), "-map", "0:a:0",
                        "-af", "ebur128=peak=true", "-f", "null", "-"], capture_output=True, text=True)
    txt = r.stderr[r.stderr.rfind("Summary:"):]
    i = re.search(r"I:\s+(-?[\d.]+) LUFS", txt)
    tp = re.search(r"True peak:\s+Peak:\s+(-?[\d.]+|-inf) dBFS", txt, re.S)
    return {"integrated_lufs": float(i.group(1)) if i else None,
            "true_peak_dbfs": float(tp.group(1)) if tp and tp.group(1) != "-inf" else None}


def yin_f0(x: np.ndarray, sr: int, fmin: float = 110.0, fmax: float = 2000.0, thr: float = 0.15) -> float | None:
    """YIN fundamental-frequency estimate of a short mono window (Hz), or None."""
    x = np.asarray(x, dtype=np.float64)
    tmin, tmax = int(sr / fmax), int(sr / fmin)
    w = len(x) - tmax
    if w < tmax // 2:
        return None
    d = np.array([np.sum((x[:w] - x[tau:tau + w]) ** 2) for tau in range(tmax + 1)])
    cm = np.ones_like(d)
    cs = np.cumsum(d[1:])
    cm[1:] = d[1:] * np.arange(1, tmax + 1) / np.where(cs > 0, cs, 1)
    lo = max(tmin, 2)
    tau = None
    for t in range(lo, tmax):
        if cm[t] < thr:
            while t + 1 < tmax and cm[t + 1] < cm[t]:
                t += 1
            tau = t
            break
    if tau is None:
        t = int(np.argmin(cm[lo:tmax])) + lo
        if cm[t] > 0.4:
            return None
        tau = t
    a, b = cm[tau - 1], cm[tau]
    c = cm[tau + 1] if tau + 1 <= tmax else cm[tau]
    den = a - 2 * b + c
    frac = 0.5 * (a - c) / den if den else 0.0
    return sr / (tau + frac)


def hz_to_midi(f: float) -> float:
    return 69 + 12 * np.log2(f / 440.0)


def pitch_at(x: np.ndarray, sr: int, t0: float, t1: float) -> float | None:
    """MIDI pitch of the note starting at t0, using the window up to t1 (capped at 60 ms, min 20 ms)."""
    a = int((t0 + 0.004) * sr)
    b = int(min(t1 - 0.002, t0 + 0.064) * sr)
    if b - a < int(0.02 * sr):
        b = a + int(0.02 * sr)
    seg = x[a:b].mean(axis=1) if x.ndim > 1 else x[a:b]
    f = yin_f0(seg, sr)
    return float(hz_to_midi(f)) if f else None


def pluck_pitch_check(run_id: str) -> dict:
    """Spearman correlation per country between the data values and the pitch detected on the pluck stem."""
    d = run_dir(run_id)
    sr, x = wavfile.read(d / "pluck.wav")
    info = json.loads((d / "audio.json").read_text(encoding="utf-8"))
    return pitch_report(x, sr, info["notes"])


def pitch_report(x: np.ndarray, sr: int, notes: list[dict]) -> dict:
    times = sorted(n["t"] for n in notes)
    out = {}
    for iso in dict.fromkeys(n["iso3"] for n in notes if n["kind"] == "year"):
        vals, mids = [], []
        for n in (n for n in notes if n["kind"] == "year" and n["iso3"] == iso):
            j = int(np.searchsorted(times, n["t"] + 1e-6))
            nxt = times[j] if j < len(times) else n["t"] + 0.1
            m = pitch_at(x, sr, n["t"], nxt)
            if m is not None:
                vals.append(n["value"])
                mids.append(int(round(m)))   # detected NOTE number (notes are discrete)
        rho = float(spearmanr(vals, mids).statistic) if len(set(mids)) > 1 else float("nan")
        out[iso] = {"rho": round(rho, 3), "detected": len(mids),
                    "years": sum(1 for n in notes if n["kind"] == "year" and n["iso3"] == iso)}
    rhos = [v["rho"] for v in out.values() if np.isfinite(v["rho"])]
    return {"per_country": out, "min_rho": round(min(rhos), 3) if rhos else None}


def stem_balance(run_id: str) -> dict:
    import pyloudnorm as pyln
    d = run_dir(run_id)
    res = {}
    for name in ("pluck", "pad", "fx"):
        sr, x = wavfile.read(d / f"{name}.wav")
        res[name] = round(float(pyln.Meter(sr).integrated_loudness(x.astype(np.float64))), 2)
    res["pluck_minus_pad_lu"] = round(res["pluck"] - res["pad"], 2)
    return res


def measure_band(rgb: np.ndarray, y0: float, y1: float, bg=(11, 13, 18), thr: int = 16) -> dict | None:
    """Horizontal extent of the non-background pixels inside a horizontal band (the owner's method)."""
    band = rgb[max(0, int(y0)):int(np.ceil(y1))].astype(int)
    m = (np.abs(band - np.array(bg)).max(axis=2) > thr).any(axis=0)
    xs = np.nonzero(m)[0]
    if not len(xs):
        return None
    w = rgb.shape[1]
    return {"x0": int(xs[0]), "x1": int(xs[-1]), "centre": float(xs[0] + xs[-1]) / 2,
            "left": int(xs[0]), "right": int(w - 1 - xs[-1])}


def centring_check(run_id: str, tol_centre: float = 2.0, tol_margin: float = 4.0) -> dict:
    """Measure element bands on frames decoded from the MP4 (mid-country frames + end card)."""
    from . import db, render
    from .topics import get_topic
    d = run_dir(run_id)
    tl = Timeline.load(d / "timeline.json")
    topic = get_topic(db.get_run(run_id)["topic_id"])
    data = json.loads((d / "data.json").read_text(encoding="utf-8"))
    pick = json.loads((d / "pick.json").read_text(encoding="utf-8"))
    views = render.build_views(topic, pick, data, {}, tl)
    r = render.Renderer(topic, tl, views)
    r.end_layer()  # fills the end-card bands
    L = r.L
    bands = ["header_band", "dots_band", "country_band", "chart_band", "year_band", "value_band"]
    frames = [((s.draw_start + s.draw_end) / 2, s.iso3, bands) for s in tl.slots]
    frames.append((tl.end_start + min(2.0, tl.end_card - 0.5), "end",
                   ["end_title_band", "end_rows_band", "end_cta_band"]))
    worst_c, worst_m, rows = 0.0, 0.0, []
    for t, label, names in frames:
        rgb = np.asarray(_frame(d / "video.mp4", t))
        for b in names:
            m = measure_band(rgb, *L[b])
            if m is None:
                continue
            dc, dm = abs(m["centre"] - (rgb.shape[1] - 1) / 2), abs(m["left"] - m["right"])
            worst_c, worst_m = max(worst_c, dc), max(worst_m, dm)
            rows.append({"frame": label, "t": round(t, 2), "band": b.replace("_band", ""), **m})
    return {"max_centre_error_px": round(worst_c, 1), "max_margin_diff_px": round(worst_m, 1),
            "ok": worst_c <= tol_centre and worst_m <= tol_margin, "measurements": rows}


def run_detection(x: np.ndarray, sr: int, t_from: float, t_to: float) -> dict:
    """Detect note onsets on a pluck stem between t_from and t_to (an event run) and their pitches."""
    a, b = int((t_from + 0.01) * sr), int((t_to + 0.03) * sr)
    on = detect_onsets(x[a:b], sr, hop=64, n_fft=512, min_gap=0.012) + a / sr
    on = [float(o) for o in on if t_from + 0.01 <= o <= t_to + 0.03]
    pitches = []
    for i, o in enumerate(on):
        nxt = on[i + 1] if i + 1 < len(on) else o + 0.06
        pitches.append(pitch_at(x, sr, o, nxt))
    ps = [q for q in pitches if q is not None]
    best = cur = 1 if ps else 0
    for p0, p1 in zip(ps, ps[1:]):
        cur = cur + 1 if p1 < p0 - 0.5 else 1
        best = max(best, cur)
    return {"onsets": [round(o, 4) for o in on], "midi": [round(q, 2) if q else None for q in pitches],
            "longest_descending": best}


def detect_onsets(x: np.ndarray, sr: int, hop: int = 128, n_fft: int = 1024, fmin: float = 1500.0,
                  min_gap: float = 0.05) -> np.ndarray:
    """Spectral-flux onset times (s). Plucks have a broadband attack; the pad is low-passed, so
    flux above `fmin` isolates the plucks."""
    if x.ndim > 1:
        x = x.mean(axis=1)
    x = np.pad(x.astype(np.float64), n_fft // 2)
    n = 1 + (len(x) - n_fft) // hop
    frames = np.lib.stride_tricks.as_strided(x, shape=(n, n_fft), strides=(x.strides[0] * hop, x.strides[0]))
    spec = np.abs(np.fft.rfft(frames * np.hanning(n_fft), axis=1))
    k0 = int(fmin / (sr / n_fft))
    s = np.log1p(1000 * spec[:, k0:])
    flux = np.maximum(0, np.diff(s, axis=0)).sum(axis=1)
    flux = np.concatenate([[0], flux])
    thr = median_filter(flux, size=31) + 0.1 * flux.max()
    peaks = (flux == maximum_filter1d(flux, size=int(min_gap * sr / hop) | 1)) & (flux > thr)
    return np.nonzero(peaks)[0] * hop / sr


def sync_check(run_id: str) -> dict:
    d = run_dir(run_id)
    tl = Timeline.load(d / "timeline.json")
    sr, x = wavfile.read(d / "audio.wav")
    det = detect_onsets(x, sr)
    exp = np.array([o for o, _, _ in tl.all_onsets()])
    errs = []
    for e in exp:
        j = np.searchsorted(det, e)
        cands = [det[k] for k in (j - 1, j) if 0 <= k < len(det)]
        if cands:
            near = min(cands, key=lambda c: abs(c - e))
            if abs(near - e) <= 0.1:
                errs.append(abs(near - e))
    errs = np.array(errs)
    return {"expected_onsets": int(len(exp)), "detected_onsets": int(len(det)), "matched": int(len(errs)),
            "recall": round(len(errs) / len(exp), 3) if len(exp) else 0,
            "median_error_ms": round(float(np.median(errs)) * 1000, 2) if len(errs) else None,
            "p95_error_ms": round(float(np.percentile(errs, 95)) * 1000, 2) if len(errs) else None}


def _frame(video, t: float):
    from PIL import Image
    png = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                          "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, check=True).stdout
    return Image.open(io.BytesIO(png)).convert("RGB")


def contact_sheet(run_id: str, cols: int = 3, tile_w: int = 360) -> dict:
    from PIL import Image, ImageDraw, ImageFont
    from .assets import font_files
    d = run_dir(run_id)
    tl = Timeline.load(d / "timeline.json")
    names = country_by_iso3()
    shots = [((s.start + s.end) / 2, names[s.iso3]["name"]) for s in tl.slots]
    shots.append((tl.end_start + min(2.0, tl.end_card - 0.5), "end card"))
    tile_h = tile_w * 16 // 9
    cap = 34
    rows = (len(shots) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tile_w, rows * (tile_h + cap)), (20, 22, 28))
    font = ImageFont.truetype(str(font_files()["semibold"]), 20)
    dr = ImageDraw.Draw(sheet)
    for i, (t, label) in enumerate(shots):
        im = _frame(d / "video.mp4", t).resize((tile_w, tile_h), Image.LANCZOS)
        x, y = (i % cols) * tile_w, (i // cols) * (tile_h + cap)
        sheet.paste(im, (x, y + cap))
        dr.text((x + 8, y + 6), f"{label}  @ {t:.1f}s", font=font, fill=(230, 233, 240))
    sheet.save(d / "contact.png")
    from .config import ROOT
    rel = (d / "contact.png").relative_to(ROOT).as_posix()   # repo-relative: no machine paths in outputs
    return {"contact": rel, "frames": [{"t": round(t, 2), "label": lbl} for t, lbl in shots]}


def verify_run(run_id: str) -> dict:
    cfg = get_config()
    d = run_dir(run_id)
    probe = ffprobe(d / "video.mp4")
    loud = loudness(d / "video.mp4")
    sync = sync_check(run_id)
    stems = stem_balance(run_id)
    pitch = pluck_pitch_check(run_id)
    centring = centring_check(run_id)
    sheet = contact_sheet(run_id)
    v = cfg["video"]
    checks = {
        "resolution_1080x1920": probe["width"] == 1080 and probe["height"] == 1920,
        "fps_30": abs(probe["fps"] - 30) < 1e-6,
        "duration_30_45": v["min_seconds"] - 0.05 <= probe["duration"] <= v["max_seconds"] + 0.05,
        "audio_present": probe["audio_streams"] >= 1,
        "loudness_-14_pm1": loud["integrated_lufs"] is not None and abs(loud["integrated_lufs"] + 14) <= 1,
        "true_peak_le_-1": loud["true_peak_dbfs"] is not None and loud["true_peak_dbfs"] <= -1.0 + 0.05,
        "sync_median_le_40ms": sync["median_error_ms"] is not None and sync["median_error_ms"] <= 40,
        "pluck_ge_pad_plus_6LU": stems["pluck_minus_pad_lu"] >= 6,
        "pitch_spearman_ge_0.9": pitch["min_rho"] is not None and pitch["min_rho"] >= 0.9,
        "centred_540": centring["ok"],
    }
    res = {"run_id": run_id, "passed": all(checks.values()), "checks": checks, "ffprobe": probe,
           "loudness": loud, "sync": sync, "stems": stems, "pitch": pitch, "centring": centring, **sheet}
    (d / "verify.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res
