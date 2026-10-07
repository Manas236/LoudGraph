"""Frames -> FFmpeg (section 8).

Frames are drawn one at a time and piped as raw RGB into FFmpeg's stdin together with the WAV;
no frame is ever kept in memory beyond the current one (plus one cached static layer per country)
and nothing is written to disk per frame.
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

from .assets import ensure_all, flag_png, font_files
from .canvas import make_canvas
from .config import country_by_iso3, get_config
from .timeline import Slot, Timeline
from .topics import format_value, source_label

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ layout constants (px)
W, H = 1080, 1920
SAFE_RIGHT_FRAC = 0.12      # Reels/Shorts action buttons
SAFE_BOTTOM_FRAC = 0.20     # captions / channel name / description
SAFE_LEFT = 64
SAFE_TOP = 96
SAFE_RIGHT = int(W * (1 - SAFE_RIGHT_FRAC))     # 950
SAFE_BOTTOM = int(H * (1 - SAFE_BOTTOM_FRAC))   # 1536
CONTENT_W = SAFE_RIGHT - SAFE_LEFT
CX = (SAFE_LEFT + SAFE_RIGHT) / 2

PILL_Y, PILL_H = 116, 92
DOTS_Y = 258
NAME_BASE = 372
FLAG_W, FLAG_H = 88, 66
SUB_BASE = 436
SUB_LINE = 40
CHART_L, CHART_R = SAFE_LEFT, SAFE_RIGHT - 28
CHART_T, CHART_B = 548, 1136
XLAB_BASE = CHART_B + 44
YEAR_BASE = 1296
VALUE_BASE = 1428
SOURCE_BASE = 1500
WATERMARK_BASE = 1650

END_Q_BASE = 250
END_ROWS_T, END_ROWS_B = 380, 1360
END_CTA_BASE = 1440
END_SOURCE_BASE = 1500


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


@dataclass
class CountryView:
    iso3: str
    name: str
    iso2: str
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
        self.ylo, self.yhi = vmin - 0.08 * rng, vmax + 0.12 * rng
        self.ticks = [t for t in nice_ticks(vmin, vmax, 4) if self.ylo <= t <= self.yhi]

    def Y(self, v: float) -> float:
        return CHART_B - (v - self.ylo) / (self.yhi - self.ylo) * (CHART_B - CHART_T)


class Renderer:
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
            self.cv.load_image(v.iso2, flag_png(v.iso2))
        self.x0, self.x1 = tl.slots[0].x_start, tl.slots[0].x_end
        # y tick labels live in a left gutter shared by all countries, so the line never crosses them
        widest = max((self.cv.text_width(self.fmt(t), "semibold", 24) for v in views for t in v.ticks), default=40)
        self.plot_l = CHART_L + widest + 18
        self._static: dict[int, object] = {}
        self._final: dict[int, object] = {}
        self._tags: dict[int, tuple] = {}
        self._intro = None
        self._chrome = None
        self._end = None

    # ------------------------------------------------------------ helpers
    def X(self, year: float) -> float:
        return self.plot_l + (year - self.x0) / (self.x1 - self.x0) * (CHART_R - self.plot_l)

    def fmt(self, v: float) -> str:
        return format_value(self.topic, v)

    def _bg(self):
        self.cv.clear(self.rc["background"])

    def _pill(self):
        cv, title = self.cv, self.topic["title"]
        size = cv.fit_size(title, "bold", CONTENT_W - 72, 46, 30)
        lines = [title] if cv.text_width(title, "bold", size) <= CONTENT_W - 72 else cv.wrap(title, "bold", size, CONTENT_W - 72)
        tw = max(cv.text_width(ln, "bold", size) for ln in lines)
        ph = PILL_H + (len(lines) - 1) * size * 1.15
        cv.rrect(CX - tw / 2 - 36, PILL_Y, tw + 72, ph, PILL_H / 2, fill="#ffffff")
        for i, ln in enumerate(lines):
            cv.text(CX, PILL_Y + PILL_H / 2 + size * 0.36 + i * size * 1.15, ln, "bold", size,
                    self.rc["background"], anchor="m", tag="title")

    def _dots(self, active: int):
        n = len(self.views)
        gap = 30
        x = CX - gap * (n - 1) / 2
        for i, v in enumerate(self.views):
            cx = x + i * gap
            if i == active:
                self.cv.circle(cx, DOTS_Y, 16, v.color, alpha=0.35, blur=6)
                self.cv.circle(cx, DOTS_Y, 10, v.color)
            elif i < active:
                self.cv.circle(cx, DOTS_Y, 7, "#ffffff", alpha=0.75)
            else:
                self.cv.circle(cx, DOTS_Y, 7, self.rc["muted"], alpha=0.8, stroke=2)

    def _footer(self, source_base=SOURCE_BASE):
        self.cv.text(CX, source_base, f"Source: {source_label(self.topic)}", "regular", 26, self.rc["muted"],
                     anchor="m", tag="source")
        self.cv.text(W / 2, WATERMARK_BASE, self.cfg["brand"]["name"], "semibold", 30, "#ffffff", anchor="m",
                     alpha=0.28, tag="watermark")

    # ------------------------------------------------------------ layers
    def intro_layer(self):
        if self._intro is None:
            self._bg()
            self._pill()
            self._dots(-1)
            self._footer()
            self._intro = self.cv.snapshot()
        return self._intro

    def chrome_layer(self):
        """The parts every country frame shares (background, title, footer): transitions dip through it."""
        if self._chrome is None:
            self._bg()
            self._pill()
            self._footer()
            self._chrome = self.cv.snapshot()
        return self._chrome

    def static_layer(self, i: int):
        if i in self._static:
            return self._static[i]
        cv, v, rc = self.cv, self.views[i], self.rc
        self._bg()
        self._pill()
        self._dots(i)
        name = v.name.upper()
        size = cv.fit_size(name, "black", CONTENT_W - FLAG_W - 24, 80, 40)
        tw = cv.text_width(name, "black", size)
        x0 = CX - (FLAG_W + 24 + tw) / 2
        cap_mid = NAME_BASE - 0.727 * size / 2
        cv.image(v.iso2, x0, cap_mid - FLAG_H / 2, FLAG_W, FLAG_H, radius=8)
        cv.text(x0 + FLAG_W + 24, NAME_BASE, name, "black", size, v.color, tag="country")
        sub_lines = cv.wrap(self.topic["subtitle"], "regular", 32, CONTENT_W)[:2]
        for k, ln in enumerate(sub_lines):
            cv.text(CX, SUB_BASE + k * SUB_LINE, ln, "regular", 32, rc["muted"], anchor="m", tag="subtitle")
        for tval in v.ticks:
            y = v.Y(tval)
            cv.line(self.plot_l, y, CHART_R, y, rc["grid"], width=2)
            cv.text(self.plot_l - 12, y + 8.5, self.fmt(tval), "semibold", 24, rc["muted"], anchor="r", tag="tick")
        cv.line(self.plot_l, CHART_B, CHART_R, CHART_B, rc["grid"], width=2)
        mid = round((self.x0 + self.x1) / 2)
        cv.text(self.plot_l, XLAB_BASE, str(self.x0), "semibold", 26, rc["muted"], tag="xlabel")
        cv.text(self.X(mid), XLAB_BASE, str(mid), "semibold", 26, rc["muted"], anchor="m", tag="xlabel")
        cv.text(CHART_R, XLAB_BASE, str(self.x1), "semibold", 26, rc["muted"], anchor="r", tag="xlabel")
        self._footer()
        self._static[i] = cv.snapshot()
        return self._static[i]

    def end_layer(self):
        if self._end is not None:
            return self._end
        cv, rc = self.cv, self.rc
        self._bg()
        q = rc["end_card_question"]
        qs = cv.fit_size(q, "black", CONTENT_W, 64, 44)
        for k, ln in enumerate(cv.wrap(q, "black", qs, CONTENT_W)[:2]):
            cv.text(CX, END_Q_BASE + k * qs * 1.15, ln, "black", qs, rc["text"], anchor="m", tag="end_question")
        n = len(self.views)
        row_h = min(150.0, (END_ROWS_B - END_ROWS_T) / n)
        name_w = 300
        spark_l, spark_r = SAFE_LEFT + 72 + name_w + 24, CHART_R
        for i, v in enumerate(self.views):
            ym = END_ROWS_T + row_h * (i + 0.5)
            fh = min(42.0, row_h * 0.42)
            cv.image(v.iso2, SAFE_LEFT, ym - fh / 2 - 12, fh * 4 / 3, fh, radius=5)
            ns = cv.fit_size(v.name, "bold", name_w, 34, 22)
            cv.text(SAFE_LEFT + 72, ym - 6, v.name, "bold", ns, v.color, tag="end_name")
            cv.text(SAFE_LEFT + 72, ym + 26, f"{self.fmt(v.values[-1])} in {v.years[-1]}", "regular", 24,
                    rc["muted"], tag="end_value")
            band = row_h * 0.30
            vmin, vmax = min(v.values), max(v.values)
            span = (vmax - vmin) or 1.0
            pts = [(spark_l + (y - self.x0) / (self.x1 - self.x0) * (spark_r - spark_l),
                    ym + band - (val - vmin) / span * 2 * band) for y, val in zip(v.years, v.values)]
            cv.polyline(pts, v.color, 10, alpha=0.45, blur=6)
            cv.polyline(pts, v.color, 4)
            cv.circle(pts[-1][0], pts[-1][1], 6, v.color)
            if i < n - 1:
                cv.line(SAFE_LEFT, ym + row_h / 2, CHART_R, ym + row_h / 2, rc["grid"], width=1.5)
        cta = rc["end_card_cta"]
        cs = cv.fit_size(cta, "bold", CONTENT_W, 46, 30)
        cv.text(CX, END_CTA_BASE, cta, "bold", cs, rc["text"], anchor="m", tag="end_cta")
        self._footer(END_SOURCE_BASE)
        self._end = cv.snapshot()
        return self._end

    # ------------------------------------------------------------ dynamic
    def draw_country(self, slot: Slot, t: float):
        cv, v = self.cv, self.views[slot.index]
        cv.draw_snapshot(self.static_layer(slot.index))
        yp = slot.year_pos(t)
        k = slot.reached_index(t)
        pts = [(self.X(y), v.Y(val)) for y, val in zip(v.years, v.values) if y <= yp]
        if pts and k + 1 < len(v.years) and yp > v.years[max(k, 0)] and k >= 0:
            y0, y1 = v.years[k], v.years[k + 1]
            f = (yp - y0) / (y1 - y0)
            pts.append((self.X(yp), v.Y(v.values[k] + f * (v.values[k + 1] - v.values[k]))))
        if len(pts) >= 2:
            cv.area(pts, CHART_B, v.color, 0.30, 0.0, CHART_T)
            cv.polyline(pts, v.color, 18, alpha=0.55, blur=11)
            cv.polyline(pts, v.color, 7)
            cv.polyline(pts, "#ffffff", 2.2, alpha=0.45)
        if pts:
            hx, hy = pts[-1]
            pulse = 1.0
            if k >= 0:
                pulse += 0.6 * math.exp(-(t - slot.onsets[k]) / 0.07)
            cv.circle(hx, hy, 26 * pulse, v.color, alpha=0.55, blur=14)
            cv.circle(hx, hy, 11 * pulse, v.color)
            cv.circle(hx, hy, 4.5, "#ffffff")
        self._draw_label(slot, v, t, yp)
        if k >= 0:
            rc = self.rc
            cv.text(CX, YEAR_BASE, str(v.years[k]), "black", 104, rc["text"], anchor="m", tag="year")
            val = self.fmt(v.values[k])
            vs = cv.fit_size(val, "black", CONTENT_W, 128, 60)
            cv.text(CX, VALUE_BASE, val, "black", vs, v.color, anchor="m", tag="value")

    TAG_SIZE, TAG_H, TAG_TOP = 28, 48, 476

    def tag_rect(self, i: int) -> tuple | None:
        """Place country i's label tag where it overlaps the finished line least (computed once)."""
        if i in self._tags:
            return self._tags[i]
        v = self.views[i]
        lab = v.label
        if not lab or not lab.get("label") or lab.get("year") not in v.years:
            self._tags[i] = None
            return None
        lx = self.X(lab["year"])
        ly = v.Y(v.values[v.years.index(lab["year"])])
        tw = self.cv.text_width(lab["label"], "semibold", self.TAG_SIZE) + 30
        th = self.TAG_H
        line = [(self.X(y), v.Y(val)) for y, val in zip(v.years, v.values)]
        samples = [(a[0] + (b[0] - a[0]) * k / 10, a[1] + (b[1] - a[1]) * k / 10)
                   for a, b in zip(line, line[1:]) for k in range(10)] + line[-1:]
        gap = 70
        cands = [(0, -gap - th / 2, 0), (0, gap + th / 2, 3), (-tw / 2 - 30, -gap - th / 2, 1),
                 (tw / 2 + 30, -gap - th / 2, 1), (-tw / 2 - 30, gap + th / 2, 4), (tw / 2 + 30, gap + th / 2, 4),
                 (tw / 2 + 40, 0, 2), (-tw / 2 - 40, 0, 2)]
        best = None
        right = min(CHART_R, SAFE_RIGHT)
        for dx, dy, pref in cands:
            tx = max(self.plot_l, min(lx + dx - tw / 2, right - tw))
            ty = max(self.TAG_TOP, min(ly + dy - th / 2, CHART_B - th - 6))
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
        # leader from the point to the nearest point on the tag's border
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
        views.append(CountryView(iso3=slot.iso3, name=names[slot.iso3]["name"], iso2=names[slot.iso3]["iso2"],
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
        "-c:a", "aac", "-b:a", vc["audio_bitrate"], "-ar", str(cfg["audio"]["sample_rate"]),
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
    tmp.replace(out_mp4)
    if thumb is not None:
        r.cv.draw_snapshot(r.end_layer())
        jpg_from_canvas(r.cv, thumb)
    el = time.time() - t0
    return {"backend": r.backend, "backend_note": r.cv.backend_note, "frames": tl.n_frames,
            "render_seconds": round(el, 1), "fps": round(tl.n_frames / el, 1)}


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
    tags = ["data visualization", "sonification", "data", "statistics", "charts", topic["category"] if "category" in topic else "data",
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
        "sources": {"source": topic["source"], "code": topic["code"], "indicator_name": indicator,
                    "citation": data.get("citation"), "license": "CC BY 4.0",
                    "fetched_at": data.get("fetched_at")},
        "data_years": {"x_start": tl.slots[0].x_start, "x_end": tl.slots[0].x_end},
        "duration_seconds": round(tl.n_frames / tl.fps, 3),
        "audio": {k: audio_info[k] for k in ("lufs", "true_peak_db") if k in audio_info},
    }


def save_json(p: Path, obj) -> None:
    p.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
