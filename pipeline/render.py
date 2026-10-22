"""Frames -> FFmpeg (section 8, layout revised in fix pass 01 and for the opening on 2026-10-13).

Frames are drawn one at a time and piped as raw RGB into FFmpeg's stdin together with the WAV;
no frame is ever kept in memory beyond the current one (plus one cached static layer per country)
and nothing is written to disk per frame.

Layout: one centre axis (x = 540) with symmetric 130 px side margins, which also keeps everything
out of the right-most 12% (Shorts/Reels buttons). Top to bottom, from SAFE_TOP (below the Reels top
bar, inside the profile grid's 3:4 crop) to STACK_BOTTOM: the opening question, the metric with its
unit, progress dots, flag + country, chart, year, value.

The opening question (captions.opening: a question about a computed fact) is on screen from frame 0,
big for timing.hook_hold seconds, then shrinks into the header that stays for the whole video. The
first country's chart and line are there from frame 0 as well. The end card reveals the answer.
No title pill, no source line, no channel name (the data credit lives in the post).

A reel with a cold open (coldopen.py, timeline.cold_open_time) shows its most dramatic beat first, under the same
opening question on the same timing, then rewinds to the first country and the race starts.
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
SAFE_BOTTOM = int(H * (1 - SAFE_BOTTOM_FRAC))    # 1536: no text below this
SAFE_TOP = 250                 # below the Reels top bar; the profile grid's 3:4 crop keeps y 240..1680
STACK_BOTTOM = 1440            # the stack ends here, clear of the caption the Reels app lays over the bottom
CONTENT_W = SAFE_RIGHT - SAFE_LEFT               # 820

PLOT_L, PLOT_R = SAFE_LEFT, SAFE_RIGHT           # gridlines span the full content width
DATA_INSET = 34                # data x-range is inset so the line's glow stays inside the gridlines
FLAG_W, FLAG_H = 88, 66
DOT_R, DOT_GAP = 9, 32
CHART_H_MIN, CHART_H_MAX = 420, 560              # the chart takes the height the text above it leaves
YEAR_SIZE, VALUE_SIZE = 84, 108
CAP = 0.727                    # Inter cap height / em
ASCENT = 0.77                  # Inter Black ascender (h, k, l) / em
HOOK_BIG_1LINE = (88, 72)      # opening question on one line, if it fits this large ...
HOOK_BIG_2LINES = (88, 60)     # ... else on two balanced lines
HOOK_SMALL = (54, 38)          # the header it becomes: one line if it fits, else two
HOOK_LINE, METRIC_LINE = 1.08, 1.18

# vertical rhythm (gaps between visual blocks)
GAP_HOOK_METRIC, GAP_METRIC_DOTS, GAP_DOTS_COUNTRY, GAP_COUNTRY_CHART = 26, 30, 30, 40
XLAB_BELOW = 40                # x-axis label baseline below the chart bottom
GAP_CHART_YEAR, GAP_YEAR_VALUE = 44, 24


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


def balanced_lines(cv, text: str, font: str, size: float, max_w: float) -> list[str] | None:
    """`text` on one line, or split into the two lines whose longer one is shortest; None if neither fits."""
    if cv.text_width(text, font, size) <= max_w:
        return [text]
    words = text.split()
    best = None
    for k in range(1, len(words)):
        a, b = " ".join(words[:k]), " ".join(words[k:])
        w = max(cv.text_width(a, font, size), cv.text_width(b, font, size))
        if w <= max_w and (best is None or w < best[0]):
            best = (w, [a, b])
    return best[1] if best else None


def header_fit(cv, text: str) -> tuple[int, list[str]]:
    """The metric line: the largest size that fits on one line (>= 44 px); otherwise the largest on two."""
    for size in range(52, 43, -2):
        if cv.text_width(text, "bold", size) <= CONTENT_W:
            return size, [text]
    for size in range(48, 33, -2):
        lines = cv.wrap(text, "bold", size, CONTENT_W)
        if len(lines) <= 2 and all(cv.text_width(ln, "bold", size) <= CONTENT_W for ln in lines):
            return size, lines
    return 34, cv.wrap(text, "bold", 34, CONTENT_W)[:2]


def hook_fit(cv, text: str) -> tuple[tuple[int, list[str]], tuple[int, list[str]]]:
    """((big size, lines), (small size, lines)) for the opening question and the header it becomes."""
    big = None
    for size in range(HOOK_BIG_1LINE[0], HOOK_BIG_1LINE[1] - 1, -2):
        if cv.text_width(text, "black", size) <= CONTENT_W:
            big = (size, [text])
            break
    if big is None:
        for size in range(HOOK_BIG_2LINES[0], HOOK_BIG_2LINES[1] - 1, -2):
            lines = balanced_lines(cv, text, "black", size, CONTENT_W)
            if lines:
                big = (size, lines)
                break
    big = big or (HOOK_BIG_2LINES[1], cv.wrap(text, "black", HOOK_BIG_2LINES[1], CONTENT_W)[:2])
    small = None
    for size in range(HOOK_SMALL[0], HOOK_SMALL[1] - 1, -2):
        if cv.text_width(text, "black", size) <= CONTENT_W:
            small = (size, [text])
            break
    if small is None:
        for size in range(46, HOOK_SMALL[1] - 1, -2):
            lines = balanced_lines(cv, text, "black", size, CONTENT_W)
            if lines:
                small = (size, lines)
                break
    small = small or (HOOK_SMALL[1], cv.wrap(text, "black", HOOK_SMALL[1], CONTENT_W)[:2])
    return big, small


def compute_layout(hook_big: int, n_big: int, hook_small: int, n_small: int, metric_size: int,
                   n_metric: int) -> dict:
    """Vertical positions of every block, from SAFE_TOP down; the chart takes what is left above STACK_BOTTOM."""
    L = {"stack_top": SAFE_TOP}
    y = SAFE_TOP
    lb = round(hook_big * HOOK_LINE)
    L["hook_big_baselines"] = [y + ASCENT * hook_big + k * lb for k in range(n_big)]   # ascenders stay below SAFE_TOP
    L["hook_base"] = L["hook_big_baselines"][-1]          # the big question and the header share this baseline
    L["hook_band_big"] = (y - 4, L["hook_base"] + 0.3 * hook_big)
    ls = round(hook_small * HOOK_LINE)
    L["hook_small_baselines"] = [L["hook_base"] - (n_small - 1 - k) * ls for k in range(n_small)]
    L["hook_band"] = (L["hook_small_baselines"][0] - CAP * hook_small - 4, L["hook_base"] + 0.3 * hook_small)
    y = L["hook_band_big"][1] + GAP_HOOK_METRIC
    line = round(metric_size * METRIC_LINE)
    L["header_line"] = line
    L["header_baselines"] = [y + CAP * metric_size + k * line for k in range(n_metric)]
    L["header_band"] = (y - 4, L["header_baselines"][-1] + 0.3 * metric_size)
    y = L["header_band"][1] + GAP_METRIC_DOTS
    L["dots_y"] = y + DOT_R
    L["dots_band"] = (y - 3, y + 2 * DOT_R + 3)
    y += 2 * DOT_R + GAP_DOTS_COUNTRY
    L["country_top"] = y
    L["country_band"] = (y - 4, y + FLAG_H + 4)
    y += FLAG_H + GAP_COUNTRY_CHART
    below = XLAB_BELOW + GAP_CHART_YEAR + CAP * YEAR_SIZE + GAP_YEAR_VALUE + CAP * VALUE_SIZE
    ch = max(CHART_H_MIN, min(CHART_H_MAX, STACK_BOTTOM - below - y))
    L["chart_t"], L["chart_b"], L["chart_h"] = y, y + ch, ch
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


def opening_for(topic: dict, views: list[CountryView], x0: int, x1: int, seed: int = 0,
                history: list | None = None, run_id: str | None = None) -> dict:
    """The video's opening question and the answer its end card reveals, from the countries and years the
    video shows.

    opening.hook_template which_last (the default): a guess-the-loser phrasing from opening.phrasings whose
    truth check holds for this data and that fits on screen like the first one, picked by the rotation in
    hooks.choose() over `history` (default: hooks.phrase_history(), without run_id itself); the end card shows
    that phrasing's answer. When none is true, and for opening.hook_explore_pct % of the reels, the old choice
    among the other questions (caption_formats.json openers; "fallback" says why). Other hook_template values:
    the old choice (captions.opening)."""
    import random
    from . import hooks
    from .captions import check_ends_last, compute_facts, load_formats, opening
    rows = {v.iso3: list(zip(v.years, v.values)) for v in views}
    facts = compute_facts(topic, [(v.iso3, v.name) for v in views], rows, x0, x1)
    fmt = lambda val: format_value(topic, val)  # noqa: E731
    s = hooks.settings()
    if s["hook_template"] == hooks.WHICH_LAST:
        if s["hook_explore_pct"] > 0 and random.Random(seed).random() * 100 < s["hook_explore_pct"]:
            why = f"exploring: {s['hook_explore_pct']:g}% of reels open with another question"
        else:
            past = hooks.phrase_history(exclude=run_id) if history is None else list(history)
            o, why_not = hooks.which_last_opening(topic, rows, {v.iso3: v.name for v in views}, fmt, past,
                                                  phrasing_fits)
            if o:
                return o
            why = "no which_last phrasing is true of this data and fits: " + \
                "; ".join(f"{q} ({w})" for q, w in why_not.items())
        formats = load_formats()   # the old choice, without the loser questions (their truth is checked above)
        formats["openers"] = [x for x in formats["openers"] if hooks.classify(x["question"]) != hooks.WHICH_LAST]
        o = opening(facts, seed, formats=formats)
        if o:
            o["fallback"] = why
    else:
        verify = (lambda f: check_ends_last(f, rows, fmt)) if hooks.preferred_opener() == "ends_last" else None
        o = opening(facts, seed, prefer=hooks.preferred_opener(), explore_pct=s["hook_explore_pct"], verify=verify)
    o = o or {"id": None, "question": topic.get("name") or topic.get("title", ""), "answer": "", "highlight": []}
    o["hook_template"] = hooks.classify(o["question"])
    o["hook_phrase"] = o["question"] if o["hook_template"] == hooks.WHICH_LAST else None
    return o


_MEASURE: dict[str, object] = {}


def _measure_canvas(cfg: dict | None = None):
    """A canvas with the renderer's backend and fonts, only for measuring text."""
    from .assets import ensure_fonts
    backend = (cfg or get_config())["render"]["backend"]
    if backend not in _MEASURE:
        _MEASURE[backend] = make_canvas(W, H, ensure_fonts(), backend)
    return _MEASURE[backend]


def hook_layout(question: str, cfg: dict | None = None) -> tuple[int, int, int, int]:
    """(big size, big lines, header size, header lines) the opening question is drawn with."""
    (bs, bl), (ss, sl) = hook_fit(_measure_canvas(cfg), question)
    return bs, len(bl), ss, len(sl)


def answer_fit(cv, answer: str) -> tuple[int, list[str]] | None:
    """The end card answer: the largest size from 64 px down to 44 px at which it fits on 1 or 2 balanced lines."""
    for s in range(64, 43, -2):
        fit = balanced_lines(cv, answer, "black", s, CONTENT_W)
        if fit:
            return s, fit
    return None


def phrasing_fits(question: str, answer: str, cfg: dict | None = None) -> str | None:
    """None when a which_last phrasing is drawn exactly like the first phrasing (same sizes and lines, so the
    metric line and the chart stay where they are) and its end card answer fits; else why not."""
    from .hooks import DEFAULT_PHRASINGS, phrasings
    ref = (phrasings() or DEFAULT_PHRASINGS)[0]["text"]
    want, got = hook_layout(ref, cfg), hook_layout(question, cfg)
    if got != want:
        return (f"it is drawn at {got[0]} px on {got[1]} line(s) (header {got[2]} px, {got[3]} line(s)) instead of "
                f"{want[0]} px on {want[1]} (header {want[2]} px, {want[3]}): the chart would move")
    if answer_fit(_measure_canvas(cfg), answer) is None:
        return f"its end card answer {answer!r} does not fit on 2 lines at 44 px"
    return None


def end_order(topic: dict, views: list[CountryView]) -> list[int]:
    """The end card's rows: where each country ends, first place first (the highest value, unless topics.yaml
    says rank: lower_first), so the which_last answer's country sits where the question says."""
    from .hooks import rank_sign
    sign = rank_sign(topic)
    return sorted(range(len(views)), key=lambda i: -sign * views[i].values[-1])


def cover_seconds(tl: Timeline, cfg: dict | None = None) -> float:
    """The cover frame: the opening question still big over a well-drawn first line (just before it shrinks)."""
    cfg = cfg or get_config()
    t = tl.hook_hold - float(cfg["render"].get("cover_before_shrink", 0.15))
    return round(max(0.0, min(t, tl.slots[0].end - 0.05)), 3)


class Renderer:
    TAG_SIZE, TAG_H = 28, 48

    def __init__(self, topic: dict, tl: Timeline, views: list[CountryView], cfg: dict | None = None,
                 opening: dict | None = None):
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
        self.opening = opening or opening_for(topic, views, self.x0, self.x1)
        (self.hook_big, self.hook_big_lines), (self.hook_small, self.hook_small_lines) = \
            hook_fit(self.cv, self.opening["question"])
        self.header_size, self.header_lines = header_fit(self.cv, topic["subtitle"])
        self.L = compute_layout(self.hook_big, len(self.hook_big_lines), self.hook_small, len(self.hook_small_lines),
                                self.header_size, len(self.header_lines))
        # brand.name is the product name; it reaches the frames only when render.watermark is true
        self.brand = ((self.cfg.get("brand") or {}).get("name") or "") if self.rc.get("watermark") else ""
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
        """The metric and its unit, always on screen."""
        for ln, base in zip(self.header_lines, self.L["header_baselines"]):
            self.cv.text(CX, base, ln, "bold", self.header_size, self.rc.get("metric", self.rc["text"]), anchor="m",
                         tag="header")

    def hook_progress(self, t: float) -> float:
        """0 while the opening question is big, 1 once it is the header."""
        hold, shrink = self.tl.hook_hold, self.tl.hook_shrink
        if t <= hold:
            return 0.0 if hold > 0 or shrink > 0 else 1.0
        return ease((t - hold) / shrink) if shrink > 0 else 1.0

    def _hook_lines(self, lines: list[str], size: float, alpha: float):
        if alpha <= 0.01:
            return
        step = size * HOOK_LINE
        for k, ln in enumerate(lines):
            y = self.L["hook_base"] - (len(lines) - 1 - k) * step
            self.cv.text(CX, y, ln, "black", size, self.rc["text"], anchor="m", alpha=min(1.0, alpha), tag="hook")

    def _hook(self, t: float):
        """The opening question: big, then shrinking into the header as the header fades in (same baseline)."""
        p = self.hook_progress(t)
        if p < 1:
            self._hook_lines(self.hook_big_lines, self.hook_big + (self.hook_small - self.hook_big) * p,
                             1.0 if p == 0 else 1 - p * 1.8)
        if p > 0:
            self._hook_lines(self.hook_small_lines, self.hook_small * (0.9 + 0.1 * p), (p - 0.35) / 0.65)

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
        if self.brand:  # render.watermark is off by default
            self.cv.text(CX, 1650, self.brand, "semibold", 30, "#ffffff", anchor="m", alpha=0.28, tag="watermark")

    # ------------------------------------------------------------ layers
    def intro_layer(self):
        """Only used when timing.intro > 0: the metric and empty dots before the first country."""
        if self._intro is None:
            self._bg()
            self._header()
            self._dots(-1)
            self._brand()
            self._intro = self.cv.snapshot()
        return self._intro

    def chrome_layer(self):
        """The parts every country frame shares (background + metric): transitions dip through it."""
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
        """The answer to the opening question, then every country ranked by where it ends (the
        countries in the answer highlighted), then a short call to comment."""
        if self._end is not None:
            return self._end
        cv, rc, L = self.cv, self.rc, self.L
        self._bg()
        self._header()
        y = L["dots_band"][0]
        answer = self.opening.get("answer") or ""
        if answer:
            size, lines = answer_fit(cv, answer) or (44, cv.wrap(answer, "black", 44, CONTENT_W)[:2])
            step = round(size * 1.12)
            for k, ln in enumerate(lines):
                cv.text(CX, y + CAP * size + k * step, ln, "black", size, rc["text"], anchor="m", tag="end_answer")
            L["end_title_band"] = (y - 4, y + CAP * size + (len(lines) - 1) * step + 0.3 * size)
            y = L["end_title_band"][1] + 40
        cta = rc.get("end_card_cta") or ""
        cs = cv.fit_size(cta, "bold", CONTENT_W, 42, 30) if cta else 0
        cta_h = CAP * cs + 44 if cta else 0
        n = len(self.views)
        row_h = min(150.0, (STACK_BOTTOM - cta_h - y) / n)
        rows_top = y
        hl = set(self.opening.get("highlight") or [])
        order = end_order(self.topic, self.views)
        pad = 22
        spark_l, spark_r = SAFE_LEFT + 450, SAFE_RIGHT - pad - 6
        for r, i in enumerate(order):
            v = self.views[i]
            ym = rows_top + row_h * (r + 0.5)
            if v.name in hl:
                cv.rrect(SAFE_LEFT, ym - row_h / 2 + 5, CONTENT_W, row_h - 10, 18, fill=v.color, alpha=0.14)
                cv.rrect(SAFE_LEFT, ym - row_h / 2 + 5, CONTENT_W, row_h - 10, 18, stroke=v.color, stroke_w=3)
            fh = min(48.0, row_h * 0.36)
            x = SAFE_LEFT + pad
            if v.iso2:
                cv.image(v.iso2, x, ym - fh / 2 - 14, fh * 4 / 3, fh, radius=5)
                x += fh * 4 / 3 + 18
            ns = cv.fit_size(v.name, "bold", spark_l - x - 20, 40, 24)
            cv.text(x, ym - 4, v.name, "bold", ns, v.color, tag="end_name")
            cv.text(x, ym + 32, f"{self.fmt(v.values[-1])} in {v.years[-1]}", "semibold", 28, rc["muted"],
                    tag="end_value")
            band = row_h * 0.28
            vmin, vmax = min(v.values), max(v.values)
            span = (vmax - vmin) or 1.0
            pts = [(spark_l + (yy - self.x0) / (self.x1 - self.x0) * (spark_r - spark_l),
                    ym + band - (val - vmin) / span * 2 * band) for yy, val in zip(v.years, v.values)]
            cv.polyline(pts, v.color, 5)
            cv.circle(pts[-1][0], pts[-1][1], 7, v.color)
            if r < n - 1:
                cv.line(SAFE_LEFT, ym + row_h / 2, SAFE_RIGHT, ym + row_h / 2, rc["grid"], width=1.5)
        L["end_rows_band"] = (rows_top - 2, rows_top + n * row_h + 2)
        if cta:
            y = rows_top + n * row_h + 44
            cv.text(CX, y + CAP * cs, cta, "bold", cs, rc["text"], anchor="m", tag="end_cta")
            L["end_cta_band"] = (y - 4, y + CAP * cs + 0.3 * cs)
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
        race = self.tl.cold_open_time(t)
        if race is None:
            self._draw_body(t)
        else:
            self._draw_cold_open(t, race)
        self._hook(t)   # the opening question keeps its own timing from frame 0, over the cold open too

    def _draw_cold_open(self, t: float, race: float):
        """The cold open (coldopen.py): the race at its most dramatic beat, already moving at frame 0; then the
        rewind cut, which runs that line back to its start while dipping through the shared chrome (like a change
        of country), and the race starts on the next frame."""
        tl = self.tl
        chrome = self.chrome_layer()  # (layers draw on the canvas when first built: build before drawing)
        self.draw_country(tl.slot_at(race), race)
        if t >= tl.cold_open:
            p = min(1.0, (t - tl.cold_open + 1 / tl.fps) / tl.snap)
            self.cv.draw_snapshot(chrome, alpha=0.85 * ease(p))

    def _draw_body(self, t: float):
        tl = self.tl
        if t < tl.lead + tl.intro:
            self.cv.draw_snapshot(self.intro_layer())
            return
        # transitions dip through the shared chrome: old content fades out, then new content fades in
        if t < tl.end_start:
            slot = tl.slot_at(t) or tl.slots[-1]
            trans = slot.trans_end - slot.start
            p = (t - slot.start) / trans if trans > 0 else 1.0   # the first country has no transition
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
                 thumb: Path | None = None, cfg: dict | None = None, on_progress=None,
                 opening: dict | None = None, audio_from: Path | None = None) -> dict:
    """on_progress(frame, n_frames) is called about once a second while frames are written.
    thumb gets the cover frame (the opening question over the first line, see cover_seconds).
    audio_from: an earlier MP4 of this same timeline whose audio stream is copied unchanged (a re-render
    with a new opening question); the WAV is then not encoded again and the true-peak guard is skipped."""
    cfg = cfg or get_config()
    vc = cfg["video"]
    r = Renderer(topic, tl, views, cfg, opening)
    log.info("render backend: %s (%s)", r.backend, r.cv.backend_note)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not on PATH")
    tmp = out_mp4.with_suffix(".part.mp4")
    if audio_from:
        audio = ["-i", str(audio_from), "-map", "0:v:0", "-map", "1:a:0"]
        audio_codec = ["-c:a", "copy"]
    else:
        audio = ["-i", str(wav), "-map", "0:v:0", "-map", "1:a:0"]
        # -aac_pns 0: the native encoder's Perceptual Noise Substitution re-synthesises the plucks'
        # noise-burst attacks and created peaks up to +6 dB over the WAV; without it TP matches the WAV.
        audio_codec = ["-c:a", "aac", "-aac_pns", "0", "-b:a", vc["audio_bitrate"], "-ar", str(cfg["audio"]["sample_rate"])]
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(vc["fps"]), "-i", "-",
        *audio,
        "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
        "-c:v", "libx264", "-preset", vc["preset"], "-crf", str(vc["crf"]), "-pix_fmt", "yuv420p",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
        *audio_codec,
        "-movflags", "+faststart", str(tmp),
    ]
    errlog = out_mp4.parent / "ffmpeg.log"
    t0 = time.time()
    last_progress = 0.0
    with open(errlog, "wb") as ef:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=ef)
        try:
            for f in range(tl.n_frames):
                r.draw_frame(f / tl.fps)
                proc.stdin.write(r.cv.rgb_bytes())
                if f and f % 300 == 0:
                    el = time.time() - t0
                    log.info("render %d/%d frames (%.1f fps)", f, tl.n_frames, f / el)
                if on_progress and time.time() - last_progress >= 1.0:
                    last_progress = time.time()
                    on_progress(f, tl.n_frames)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"ffmpeg exited {rc}: {errlog.read_text(errors='replace')[-800:]}")
    if on_progress:
        on_progress(tl.n_frames, tl.n_frames)
    guard = {"audio_copied_from": audio_from.name} if audio_from else _true_peak_guard(tmp, wav, cfg)
    tmp.replace(out_mp4)
    cover = cover_seconds(tl, cfg)
    if thumb is not None:
        r.draw_frame(cover)
        jpg_from_canvas(r.cv, thumb)
    el = time.time() - t0
    return {"backend": r.backend, "backend_note": r.cv.backend_note, "frames": tl.n_frames,
            "render_seconds": round(el, 1), "fps": round(tl.n_frames / el, 1), "true_peak_guard": guard,
            "cover_seconds": cover, "opening": {k: r.opening.get(k) for k in ("id", "question", "answer", "highlight",
                                                                            "hook_template", "fallback")}}


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


def render_still(topic: dict, tl: Timeline, views: list[CountryView], t: float, out_png: Path, cfg=None,
                 opening: dict | None = None) -> list:
    """Draw one frame to PNG; returns the recorded text boxes (used by tests and contact sheets)."""
    r = Renderer(topic, tl, views, cfg, opening)
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
