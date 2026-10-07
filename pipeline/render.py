"""Frames -> FFmpeg (section 8, layout revised in fix pass 01).

Frames are drawn one at a time and piped as raw RGB into FFmpeg's stdin together with the WAV;
no frame is ever kept in memory beyond the current one (plus one cached static layer per country)
and nothing is written to disk per frame.

Layout: one centre axis (x = 540) with symmetric 130 px side margins, which also keeps everything
out of the right-most 12% (Shorts/Reels buttons). Top to bottom, vertically centred inside the
safe area (y 0-1536): metric header, progress dots, flag + country, chart, year, value.
No title pill, no source line, no channel name (the hook and the data credit live in the post).
"""
from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .assets import ensure_all, flag_png, font_files
from .canvas import make_canvas
from .config import country_by_iso3, get_config
from .timeline import Slot, Timeline
from .topics import format_value

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ layout constants (px)
W, H = 1080, 1920
CX = W / 2                     # the one centre axis
MARGIN = 130                   # symmetric side margins
SAFE_LEFT, SAFE_RIGHT = MARGIN, W - MARGIN       # 130 .. 950
SAFE_RIGHT_FRAC = 0.12         # Reels/Shorts action buttons: SAFE_RIGHT must stay <= 88% of W
SAFE_BOTTOM_FRAC = 0.20        # captions / channel name / description
SAFE_BOTTOM = int(H * (1 - SAFE_BOTTOM_FRAC))    # 1536
SAFE_TOP = 96
CONTENT_W = SAFE_RIGHT - SAFE_LEFT               # 820

PLOT_L, PLOT_R = SAFE_LEFT, SAFE_RIGHT           # gridlines span the full content width
DATA_INSET = 34                # data x-range is inset so the line's glow stays inside the gridlines
FLAG_W, FLAG_H = 88, 66
DOT_R, DOT_GAP = 9, 32
CHART_H = 620
YEAR_SIZE, VALUE_SIZE = 104, 128
CAP = 0.727                    # Inter cap height / em

# vertical rhythm (gaps between visual blocks)
GAP_HEADER_DOTS, GAP_DOTS_COUNTRY, GAP_COUNTRY_CHART = 44, 44, 60
XLAB_BELOW = 44                # x-axis label baseline below the chart bottom
GAP_CHART_YEAR, GAP_YEAR_VALUE = 64, 40


def ease(p: float) -> float:
    p = max(0.0, min(1.0, p))
    return p * p * (3 - 2 * p)


def nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=10 * mag)
    first = math.ceil(lo / step) * step
    ticks, v = [], first
    while v <= hi + 1e-9:
        ticks.append(round(v, 10))
        v += step
    return ticks


def header_fit(cv, text: str) -> tuple[int, list[str]]:
    """Largest header size that fits on one line (>= 40 px); otherwise the largest that fits on two."""
    for size in range(54, 39, -2):
        if cv.text_width(text, "bold", size) <= CONTENT_W:
            return size, [text]
    for size in range(54, 33, -2):
        lines = cv.wrap(text, "bold", size, CONTENT_W)
        if len(lines) <= 2 and all(cv.text_width(ln, "bold", size) <= CONTENT_W for ln in lines):
            return size, lines
    return 34, cv.wrap(text, "bold", 34, CONTENT_W)[:2]


def compute_layout(header_size: int, header_lines: int) -> dict:
    """Vertical positions of every block, with the whole stack centred in y 0..SAFE_BOTTOM."""
    line = round(header_size * 1.22)
    header_h = CAP * header_size + (header_lines - 1) * line
    stack_h = (header_h + GAP_HEADER_DOTS + 2 * DOT_R + GAP_DOTS_COUNTRY + FLAG_H + GAP_COUNTRY_CHART
               + CHART_H + XLAB_BELOW + GAP_CHART_YEAR + CAP * YEAR_SIZE + GAP_YEAR_VALUE + CAP * VALUE_SIZE)
    y = (SAFE_BOTTOM - stack_h) / 2
    L = {"stack_top": y, "header_line": line}
    L["header_baselines"] = [y + CAP * header_size + k * line for k in range(header_lines)]
    L["header_band"] = (y - 4, L["header_baselines"][-1] + 0.3 * header_size)
    y = L["header_baselines"][-1] + GAP_HEADER_DOTS
    L["dots_y"] = y + DOT_R
    L["dots_band"] = (y - 3, y + 2 * DOT_R + 3)
    y += 2 * DOT_R + GAP_DOTS_COUNTRY
    L["country_top"] = y
    L["country_band"] = (y - 4, y + FLAG_H + 4)
    y += FLAG_H + GAP_COUNTRY_CHART
    L["chart_t"], L["chart_b"] = y, y + CHART_H
    L["xlab_base"] = L["chart_b"] + XLAB_BELOW
    L["chart_band"] = (y - 6, L["xlab_base"] + 8)
    y = L["xlab_base"] + GAP_CHART_YEAR
    L["year_base"] = y + CAP * YEAR_SIZE
    L["year_band"] = (y - 4, L["year_base"] + 4)
    y = L["year_base"] + GAP_YEAR_VALUE
    L["value_base"] = y + CAP * VALUE_SIZE
    L["value_band"] = (y - 4, L["value_base"] + 0.3 * VALUE_SIZE)
    L["stack_bottom"] = L["value_base"]
    return L


@dataclass
class CountryView:
    iso3: str
    name: str
    iso2: str | None
    color: str
    years: list[int]
    values: list[float]
    ylo: float = 0.0
    yhi: float = 1.0
    ticks: list[float] = field(default_factory=list)
    label: dict | None = None

    def __post_init__(self):
        vmin, vmax = min(self.values), max(self.values)
        rng = (vmax - vmin) or abs(vmax) or 1.0
        self.ylo, self.yhi = vmin - 0.08 * rng, vmax + 0.14 * rng
        self.ticks = [t for t in nice_ticks(vmin, vmax, 4) if self.ylo <= t <= self.yhi]


class Renderer:
    TAG_SIZE, TAG_H = 28, 48

    def __init__(self, topic: dict, tl: Timeline, views: list[CountryView], cfg: dict | None = None):
        self.cfg = cfg or get_config()
        self.rc = self.cfg["render"]
        self.topic = topic
        self.tl = tl
        self.views = views
        fonts = ensure_fonts_and_flags()
        self.cv = make_canvas(W, H, fonts, self.rc["backend"])
        self.backend = self.cv.backend
        for v in views:
            if v.iso2:
                self.cv.load_image(v.iso2, flag_png(v.iso2))
        self.x0, self.x1 = tl.slots[0].x_start, tl.slots[0].x_end
        self.header_size, self.header_lines = header_fit(self.cv, topic["subtitle"])
        self.L = compute_layout(self.header_size, len(self.header_lines))
        self.brand = (self.cfg.get("brand") or {}).get("name") or ""
        self._static: dict[int, object] = {}
        self._final: dict[int, object] = {}
        self._tags: dict[int, tuple] = {}
        self._intro = None
        self._chrome = None
        self._end = None

    # ------------------------------------------------------------ helpers
    def X(self, year: float) -> float:
        a, b = PLOT_L + DATA_INSET, PLOT_R - DATA_INSET
        return a + (year - self.x0) / (self.x1 - self.x0) * (b - a)

    def Y(self, v: CountryView, val: float) -> float:
        t, b = self.L["chart_t"], self.L["chart_b"]
        return b - (val - v.ylo) / (v.yhi - v.ylo) * (b - t)

    def fmt(self, v: float) -> str:
        return format_value(self.topic, v)

    def _bg(self):
        self.cv.clear(self.rc["background"])

    def _header(self):
        for ln, base in zip(self.header_lines, self.L["header_baselines"]):
            self.cv.text(CX, base, ln, "bold", self.header_size, self.rc["text"], anchor="m", tag="header")

    def _dots(self, active: int):
        n = len(self.views)
        x = CX - DOT_GAP * (n - 1) / 2
        y = self.L["dots_y"]
        for i, v in enumerate(self.views):
            cx = x + i * DOT_GAP
            if i == active:
                self.cv.circle(cx, y, DOT_R, v.color)
            elif i < active:
                self.cv.circle(cx, y, DOT_R, self.rc["muted"])
            else:
                self.cv.circle(cx, y, DOT_R - 1, self.rc["muted"], alpha=0.9, stroke=2)

    def _brand(self):
        if self.brand:  # off by default
            self.cv.text(CX, 1650, self.brand, "semibold", 30, "#ffffff", anchor="m", alpha=0.28, tag="watermark")

    # ------------------------------------------------------------ layers
    def intro_layer(self):
        if self._intro is None:
            self._bg()
            self._header()
            self._dots(-1)
            self._brand()
            self._intro = self.cv.snapshot()
        return self._intro

    def chrome_layer(self):
        """The parts every country frame shares (background + header): transitions dip through it."""
        if self._chrome is None:
            self._bg()
            self._header()
            self._brand()
            self._chrome = self.cv.snapshot()
        return self._chrome

    def static_layer(self, i: int):
        if i in self._static:
            return self._static[i]
        cv, v, rc, L = self.cv, self.views[i], self.rc, self.L
        self._bg()
        self._header()
        self._dots(i)
        # flag + name measured as ONE group and centred on CX by ink
        name = v.name.upper()
        flag_w = FLAG_W + 24 if v.iso2 else 0
        size = cv.fit_size(name, "black", CONTENT_W - flag_w, 84, 40)
        il, ir, _, _ = cv.ink(name, "black", size)
        group = flag_w + (ir - il)
        x0 = CX - group / 2
        top = L["country_top"]
        if v.iso2:
            cv.image(v.iso2, x0, top, FLAG_W, FLAG_H, radius=8)
        cv.text(x0 + flag_w, top + FLAG_H / 2 + CAP * size / 2, name, "black", size, v.color, tag="country")
        # chart: gridlines across the full content width (tick labels are drawn per frame, ABOVE the line)
        for tval in v.ticks:
            y = self.Y(v, tval)
            cv.line(PLOT_L, y, PLOT_R, y, rc["grid"], width=2)
        cv.line(PLOT_L, L["chart_b"], PLOT_R, L["chart_b"], rc["grid"], width=2)
        mid = int((self.x0 + self.x1) / 2 + 0.5)  # half-up (round() would give 2006 for 2006.5)
        for yr in (self.x0, mid, self.x1):
            cv.text(self.X(yr), L["xlab_base"], str(yr), "semibold", 26, rc["muted"], anchor="m", tag="xlabel")
        self._brand()
        self._static[i] = cv.snapshot()
        return self._static[i]

    def end_layer(self):
        if self._end is not None:
            return self._end
        cv, rc = self.cv, self.rc
        self._bg()
        q = rc["end_card_question"]
        qs = cv.fit_size(q, "black", CONTENT_W, 64, 44)
        qlines = cv.wrap(q, "black", qs, CONTENT_W)[:2]
        cta = rc["end_card_cta"]
        cs = cv.fit_size(cta, "bold", CONTENT_W, 46, 30)
        n = len(self.views)
        row_h = min(128.0, 880.0 / n)
        qline = qs * 1.15
        stack = CAP * qs + (len(qlines) - 1) * qline + 64 + n * row_h + 56 + CAP * cs
        y = (SAFE_BOTTOM - stack) / 2
        self.L["end_title_band"] = (y - 4, y + CAP * qs + (len(qlines) - 1) * qline + 0.3 * qs)
        for k, ln in enumerate(qlines):
            cv.text(CX, y + CAP * qs + k * qline, ln, "black", qs, rc["text"], anchor="m", tag="end_question")
        y += CAP * qs + (len(qlines) - 1) * qline + 64
        rows_top = y
        spark_l, spark_r = SAFE_LEFT + 430, SAFE_RIGHT - 6   # end dot (r=6) touches the right margin
        for i, v in enumerate(self.views):
            ym = rows_top + row_h * (i + 0.5)
            fh = min(44.0, row_h * 0.4)
            x = SAFE_LEFT
            if v.iso2:
                cv.image(v.iso2, x, ym - fh / 2 - 12, fh * 4 / 3, fh, radius=5)
                x += fh * 4 / 3 + 18
            ns = cv.fit_size(v.name, "bold", spark_l - x - 24, 34, 22)
            cv.text(x, ym - 6, v.name, "bold", ns, v.color, tag="end_name")
            cv.text(x, ym + 26, f"{self.fmt(v.values[-1])} in {v.years[-1]}", "regular", 24, rc["muted"],
                    tag="end_value")
            band = row_h * 0.30
            vmin, vmax = min(v.values), max(v.values)
            span = (vmax - vmin) or 1.0
            pts = [(spark_l + (yy - self.x0) / (self.x1 - self.x0) * (spark_r - spark_l),
                    ym + band - (val - vmin) / span * 2 * band) for yy, val in zip(v.years, v.values)]
            cv.polyline(pts, v.color, 4)
            cv.circle(pts[-1][0], pts[-1][1], 6, v.color)
            if i < n - 1:
                cv.line(SAFE_LEFT, ym + row_h / 2, SAFE_RIGHT, ym + row_h / 2, rc["grid"], width=1.5)
        self.L["end_rows_band"] = (rows_top - 2, rows_top + n * row_h + 2)
        y = rows_top + n * row_h + 56
        cv.text(CX, y + CAP * cs, cta, "bold", cs, rc["text"], anchor="m", tag="end_cta")
        self.L["end_cta_band"] = (y - 4, y + CAP * cs + 0.3 * cs)
        self._brand()
        self._end = cv.snapshot()
        return self._end

    # ------------------------------------------------------------ dynamic
    def draw_country(self, slot: Slot, t: float):
        cv, v, L = self.cv, self.views[slot.index], self.L
        cv.draw_snapshot(self.static_layer(slot.index))
        yp = slot.year_pos(t)
        k = slot.reached_index(t)
        pts = [(self.X(y), self.Y(v, val)) for y, val in zip(v.years, v.values) if y <= yp]
        if pts and k >= 0 and k + 1 < len(v.years) and yp > v.years[k]:
            y0, y1 = v.years[k], v.years[k + 1]
            f = (yp - y0) / (y1 - y0)
            pts.append((self.X(yp), self.Y(v, v.values[k] + f * (v.values[k + 1] - v.values[k]))))
        if len(pts) >= 2:
            cv.area(pts, L["chart_b"], v.color, 0.30, 0.0, L["chart_t"])
            cv.polyline(pts, v.color, 18, alpha=0.55, blur=11)
            cv.polyline(pts, v.color, 7)
            cv.polyline(pts, "#ffffff", 2.2, alpha=0.45)
        self._draw_events(slot, v, t)
        # tick labels sit inside the plot, just above their gridline; drawn over the line with a
        # background halo so a line passing through them goes visibly behind and they stay readable
        for tval in v.ticks:
            cv.text(PLOT_L, self.Y(v, tval) - 9, self.fmt(tval), "semibold", 24, self.rc["muted"], tag="tick",
                    halo=5, halo_color=self.rc["background"])
        if pts:
            hx, hy = pts[-1]
            pulse = 1.0
            if k >= 0:
                big = slot.event_for(v.years[k]) is not None
                pulse += (1.4 if big else 0.6) * math.exp(-(t - slot.onsets[k]) / (0.12 if big else 0.07))
            cv.circle(hx, hy, 26 * pulse, v.color, alpha=0.55, blur=14)
            cv.circle(hx, hy, 11 * pulse, v.color)
            cv.circle(hx, hy, 4.5, "#ffffff")
        self._draw_label(slot, v, t, yp)
        if k >= 0:
            rc = self.rc
            cv.text(CX, L["year_base"], str(v.years[k]), "black", YEAR_SIZE, rc["text"], anchor="m", tag="year")
            val = self.fmt(v.values[k])
            vs = cv.fit_size(val, "black", CONTENT_W, VALUE_SIZE, 60)
            cv.text(CX, L["value_base"], val, "black", vs, v.color, anchor="m", tag="value")

    def _draw_events(self, slot: Slot, v: CountryView, t: float):
        """One expanding ring where an event lands (the head dot also pulses harder there)."""
        for e in slot.events:
            p = (t - e["t_to"]) / 0.5
            if 0 <= p < 1:
                ex = self.X(e["year"])
                ey = self.Y(v, v.values[e["index"]])
                rmax = min(74.0, ex - PLOT_L - 4, PLOT_R - ex - 4)
                self.cv.circle(ex, ey, 14 + (rmax - 14) * ease(p), v.color, alpha=0.9 * (1 - p),
                               stroke=1 + 4 * (1 - p))

    def tag_rect(self, i: int) -> tuple | None:
        """Place country i's label tag where it overlaps the finished line least (computed once)."""
        if i in self._tags:
            return self._tags[i]
        v, L = self.views[i], self.L
        lab = v.label
        if not lab or not lab.get("label") or lab.get("year") not in v.years:
            self._tags[i] = None
            return None
        lx = self.X(lab["year"])
        ly = self.Y(v, v.values[v.years.index(lab["year"])])
        tw = self.cv.text_width(lab["label"], "semibold", self.TAG_SIZE) + 30
        th = self.TAG_H
        line = [(self.X(y), self.Y(v, val)) for y, val in zip(v.years, v.values)]
        samples = [(a[0] + (b[0] - a[0]) * k / 10, a[1] + (b[1] - a[1]) * k / 10)
                   for a, b in zip(line, line[1:]) for k in range(10)] + line[-1:]
        gap = 70
        cands = [(0, -gap - th / 2, 0), (0, gap + th / 2, 3), (-tw / 2 - 30, -gap - th / 2, 1),
                 (tw / 2 + 30, -gap - th / 2, 1), (-tw / 2 - 30, gap + th / 2, 4), (tw / 2 + 30, gap + th / 2, 4),
                 (tw / 2 + 40, 0, 2), (-tw / 2 - 40, 0, 2)]
        best = None
        for dx, dy, pref in cands:
            tx = max(PLOT_L, min(lx + dx - tw / 2, PLOT_R - tw))
            ty = max(L["chart_t"] - 10, min(ly + dy - th / 2, L["chart_b"] - th - 6))
            hits = sum(1 for (px, py) in samples if tx - 10 <= px <= tx + tw + 10 and ty - 10 <= py <= ty + th + 10)
            near = abs(tx + tw / 2 - lx) + abs(ty + th / 2 - ly)
            cost = hits * 100 + near * 0.2 + pref * 5
            if best is None or cost < best[0]:
                best = (cost, tx, ty)
        _, tx, ty = best
        self._tags[i] = (lx, ly, tx, ty, tw, th)
        return self._tags[i]

    def _draw_label(self, slot: Slot, v: CountryView, t: float, yp: float):
        r = self.tag_rect(slot.index)
        if r is None or yp < v.label["year"]:
            return
        cv, rc = self.cv, self.rc
        lx, ly, tx, ty, tw, th = r
        a = ease((t - slot.onset_of(v.label["year"])) / 0.25)
        ex, ey = min(max(lx, tx), tx + tw), min(max(ly, ty), ty + th)
        cv.line(lx, ly, ex, ey, "#ffffff", width=2, alpha=0.6 * a)
        cv.circle(lx, ly, 9, rc["background"], alpha=a)
        cv.circle(lx, ly, 9, "#ffffff", alpha=a, stroke=3)
        cv.rrect(tx, ty, tw, th, 12, fill=rc["background"], stroke=v.color, stroke_w=2.5, alpha=0.92 * a)
        cv.text(tx + 15, ty + th / 2 + self.TAG_SIZE * 0.36, v.label["label"], "semibold", self.TAG_SIZE,
                "#ffffff", alpha=a, tag="label")

    def final_layer(self, i: int):
        if i not in self._final:
            s = self.tl.slots[i]
            self.draw_country(s, s.end - 1e-6)
            self._final[i] = self.cv.snapshot()
        return self._final[i]

    def draw_frame(self, t: float):
        tl = self.tl
        if t < tl.intro:
            self.cv.draw_snapshot(self.intro_layer())
            return
        # transitions dip through the shared chrome: old content fades out, then new content fades in
        if t < tl.end_start:
            slot = tl.slot_at(t) or tl.slots[-1]
            p = (t - slot.start) / tl.transition
            if p < 0.5:
                prev = self.intro_layer() if slot.index == 0 else self.final_layer(slot.index - 1)
                chrome = self.chrome_layer()
                self.cv.draw_snapshot(prev)
                self.cv.draw_snapshot(chrome, alpha=ease(p * 2))
                return
            chrome = self.chrome_layer()  # (layers draw on the canvas when first built: build before drawing)
            self.draw_country(slot, t)
            if p < 1:
                self.cv.draw_snapshot(chrome, alpha=1 - ease((p - 0.5) * 2))
            return
        p = (t - tl.end_start) / tl.transition
        if p < 0.5:
            prev = self.final_layer(len(tl.slots) - 1)
            chrome = self.chrome_layer()
            self.cv.draw_snapshot(prev)
            self.cv.draw_snapshot(chrome, alpha=ease(p * 2))
            return
        chrome, end = self.chrome_layer(), self.end_layer()
        self.cv.draw_snapshot(end)
        if p < 1:
            self.cv.draw_snapshot(chrome, alpha=1 - ease((p - 0.5) * 2))

    def frame_array(self, t: float) -> np.ndarray:
        self.draw_frame(t)
        return np.frombuffer(self.cv.rgb_bytes(), dtype=np.uint8).reshape(H, W, 3)


def ensure_fonts_and_flags():
    ensure_all()
    return font_files()


def build_views(topic: dict, pick: dict, data: dict, labels: dict, tl: Timeline, cfg: dict | None = None) -> list[CountryView]:
    cfg = cfg or get_config()
    names = country_by_iso3()
    pal = cfg["render"]["palette"]
    views = []
    for slot in tl.slots:
        rows = dict((y, v) for y, v in data["series"][slot.iso3])
        meta = names.get(slot.iso3, {"name": slot.iso3, "iso2": None})
        views.append(CountryView(iso3=slot.iso3, name=meta["name"], iso2=meta.get("iso2"),
                                 color=pal[slot.index % len(pal)], years=list(slot.years),
                                 values=[rows[y] for y in slot.years], label=labels.get(slot.iso3)))
    return views


def render_video(topic: dict, tl: Timeline, views: list[CountryView], wav: Path, out_mp4: Path,
                 thumb: Path | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg or get_config()
    vc = cfg["video"]
    r = Renderer(topic, tl, views, cfg)
    log.info("render backend: %s (%s)", r.backend, r.cv.backend_note)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not on PATH")
    tmp = out_mp4.with_suffix(".part.mp4")
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(vc["fps"]), "-i", "-",
        "-i", str(wav),
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
        "-c:v", "libx264", "-preset", vc["preset"], "-crf", str(vc["crf"]), "-pix_fmt", "yuv420p",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
        # -aac_pns 0: the native encoder's Perceptual Noise Substitution re-synthesises the plucks'
        # noise-burst attacks and created peaks up to +6 dB over the WAV; without it TP matches the WAV.
        "-c:a", "aac", "-aac_pns", "0", "-b:a", vc["audio_bitrate"], "-ar", str(cfg["audio"]["sample_rate"]),
        "-movflags", "+faststart", str(tmp),
    ]
    errlog = out_mp4.parent / "ffmpeg.log"
    t0 = time.time()
    with open(errlog, "wb") as ef:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=ef)
        try:
            for f in range(tl.n_frames):
                r.draw_frame(f / tl.fps)
                proc.stdin.write(r.cv.rgb_bytes())
                if f and f % 300 == 0:
                    el = time.time() - t0
                    log.info("render %d/%d frames (%.1f fps)", f, tl.n_frames, f / el)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"ffmpeg exited {rc}: {errlog.read_text(errors='replace')[-800:]}")
    guard = _true_peak_guard(tmp, wav, cfg)
    tmp.replace(out_mp4)
    if thumb is not None:
        r.cv.draw_snapshot(r.end_layer())
        jpg_from_canvas(r.cv, thumb)
    el = time.time() - t0
    return {"backend": r.backend, "backend_note": r.cv.backend_note, "frames": tl.n_frames,
            "render_seconds": round(el, 1), "fps": round(tl.n_frames / el, 1), "true_peak_guard": guard}


def _true_peak_guard(mp4: Path, wav: Path, cfg: dict) -> dict:
    """Measure the encoded file. If AAC pushed the true peak above the target, re-encode the audio
    from the ORIGINAL WAV turned down by the excess (video stream copied). Re-encoding the decoded
    AAC instead would add a second generation of overshoot."""
    from .verify import loudness
    target = cfg["audio"]["true_peak_db"]
    m = loudness(mp4)
    tp = m["true_peak_dbfs"]
    if tp is None or tp <= target:
        return {"measured_true_peak": tp, "adjusted_db": 0.0}
    cut = round(tp - target + 0.2, 2)
    fixed = mp4.with_suffix(".tp.mp4")
    subprocess.run([shutil.which("ffmpeg"), "-y", "-v", "error", "-i", str(mp4), "-i", str(wav),
                    "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-af", f"volume=-{cut}dB", "-c:a", "aac", "-aac_pns", "0", "-b:a",
                    cfg["video"]["audio_bitrate"], "-ar", str(cfg["audio"]["sample_rate"]), "-movflags", "+faststart",
                    str(fixed)], check=True)
    fixed.replace(mp4)
    after = loudness(mp4)
    log.info("true-peak guard: MP4 peak %.1f dBFS > %.1f, audio turned down %.2f dB -> %s", tp, target, cut, after)
    return {"measured_true_peak": tp, "adjusted_db": -cut, "after": after}


def jpg_from_canvas(cv, p: Path):
    from PIL import Image
    Image.frombytes("RGB", (W, H), cv.rgb_bytes()).save(p, "JPEG", quality=92)


def render_still(topic: dict, tl: Timeline, views: list[CountryView], t: float, out_png: Path, cfg=None) -> list:
    """Draw one frame to PNG; returns the recorded text boxes (used by tests and contact sheets)."""
    r = Renderer(topic, tl, views, cfg)
    r.draw_frame(t)
    from PIL import Image
    Image.frombytes("RGB", (W, H), r.cv.rgb_bytes()).save(out_png)
    return list(r.cv.text_boxes)


# ------------------------------------------------------------------ meta.json

def build_meta(run_id: str, topic: dict, data: dict, pick: dict, labels: dict, views: list[CountryView],
               tl: Timeline, audio_info: dict) -> dict:
    names = [v.name for v in views]
    src = "World Bank WDI" if topic["source"] == "worldbank" else "Our World in Data"
    indicator = data.get("indicator_name") or topic["code"]
    hook = f"{topic['title']}. Each note is one year of real data: the higher the pitch, the higher the value."
    lines = [
        hook,
        f"What the number is: {topic['subtitle']}.",
        f"Countries: {', '.join(names)}.",
    ]
    lab_lines = [f"{v.name} {lab['year']}: {lab['label']}" for v in views
                 if (lab := labels.get(v.iso3)) and lab.get("label")]
    if lab_lines:
        lines.append("Turning points: " + "; ".join(lab_lines) + ".")
    data_line = f"Data: {src} (CC BY 4.0), \"{indicator}\""
    if topic["source"] == "worldbank":
        data_line += f" ({topic['code']})"
    lines.append(data_line + ".")
    if topic["source"] == "owid" and data.get("citation"):
        lines.append(f"Original sources: {data['citation']}.")
    if topic.get("modelled_data_warning"):
        lines.append("Note: these figures are modelled estimates (e.g. ILO/UN), not direct measurements for every year.")
    base = "\n".join(lines)
    tags = ["data visualization", "sonification", "data", "statistics", "charts", topic.get("category", "data"),
            src] + names
    title = topic["title"][:100]
    return {
        "run_id": run_id,
        "topic_id": topic["id"],
        "title": title,
        "description": base,
        "description_youtube": base + "\n\n#Shorts #dataviz #sonification",
        "description_instagram": base + "\n\n#dataviz #sonification #data #reels",
        "tags": tags[:30],
        "countries": [{"iso3": v.iso3, "name": v.name, "color": v.color,
                       "score": pick["scores"][v.iso3]["score"],
                       "first_year": v.years[0], "last_year": v.years[-1]} for v in views],
        "labels": {v.iso3: (labels.get(v.iso3) or {}).get("label") for v in views},
        "events": {s.iso3: [{"year": e["year"], "kind": e["kind"], "move": e["move"]} for e in s.events]
                   for s in tl.slots},
        "sources": {"source": topic["source"], "code": topic["code"], "indicator_name": indicator,
                    "citation": data.get("citation"), "license": "CC BY 4.0",
                    "fetched_at": data.get("fetched_at")},
        "data_years": {"x_start": tl.slots[0].x_start, "x_end": tl.slots[0].x_end},
        "duration_seconds": round(tl.n_frames / tl.fps, 3),
        "audio": {k: audio_info[k] for k in ("lufs", "true_peak_db", "stem_lufs", "pluck_minus_pad_lu")
                  if k in audio_info},
    }


def save_json(p: Path, obj) -> None:
    p.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
