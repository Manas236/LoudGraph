"""Verification gate (section 14) for a rendered run:

  * ffprobe: 1080x1920, 30 fps, 30-45 s, an audio stream
  * loudness of the final MP4 via FFmpeg ebur128: -14 ±1 LUFS integrated, true peak
  * sync: note onsets detected in audio.wav (spectral flux) vs the timeline's year onsets,
    median error must be <= 40 ms
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


def detect_onsets(x: np.ndarray, sr: int, hop: int = 128, n_fft: int = 1024, fmin: float = 1500.0) -> np.ndarray:
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
    peaks = (flux == maximum_filter1d(flux, size=int(0.05 * sr / hop) | 1)) & (flux > thr)
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
    return {"contact": str(d / "contact.png"), "frames": [{"t": round(t, 2), "label": lbl} for t, lbl in shots]}


def verify_run(run_id: str) -> dict:
    cfg = get_config()
    d = run_dir(run_id)
    probe = ffprobe(d / "video.mp4")
    loud = loudness(d / "video.mp4")
    sync = sync_check(run_id)
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
    }
    res = {"run_id": run_id, "passed": all(checks.values()), "checks": checks, "ffprobe": probe,
           "loudness": loud, "sync": sync, **sheet}
    (d / "verify.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res
