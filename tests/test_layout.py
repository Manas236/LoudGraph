"""Layout: one centre axis measured in PIXELS, symmetric margins, safe zones, no pill/source/brand."""
import copy

import numpy as np
import pytest

from pipeline import render as R
from pipeline.assets import flag_png, font_files
from pipeline.canvas import SkiaCanvas, hex_rgb
from pipeline.config import countries, get_config
from pipeline.timeline import build
from pipeline.topics import load_topics
from pipeline.verify import measure_band

YEARS = list(range(1990, 2024))
T = np.arange(len(YEARS))
CENTRED = ["header_band", "dots_band", "country_band", "chart_band", "year_band", "value_band"]
END = ["end_title_band", "end_rows_band", "end_cta_band"]


def test_safe_zone_constants():
    assert R.SAFE_LEFT == R.MARGIN and R.SAFE_RIGHT == R.W - R.MARGIN       # symmetric
    assert R.MARGIN >= 130
    assert R.SAFE_RIGHT <= int(R.W * (1 - R.SAFE_RIGHT_FRAC))               # also clears the right 12%
    assert R.SAFE_BOTTOM == int(R.H * 0.80) == 1536
    assert R.CX == R.W / 2
    assert (R.PLOT_L, R.PLOT_R) == (R.SAFE_LEFT, R.SAFE_RIGHT)


def _video(names=("GBR", "SAU", "ZAF", "USA", "KOR", "NZL", "IND", "BRA"), events=True):
    pal = get_config()["render"]["palette"]
    by = {c["iso3"]: c for c in countries()}
    rows = []
    views = []
    for i, c in enumerate(names):
        vals = list(1000 + 900 * np.sin(T / (2 + i)) + 40 * T)
        if events and i % 2 == 0:
            vals[20] -= 1600     # a crash -> slow-mo event
        rows.append((c, YEARS, vals))
        views.append(R.CountryView(iso3=c, name=by[c]["name"], iso2=by[c]["iso2"], color=pal[i], years=YEARS,
                                   values=vals, label={"year": 2008, "label": "Global financial crisis"[:24]}))
    tl = build(rows, 1990, 2023)
    topic = {"id": "t", "title": "What your carbon footprint sounds like",
             "subtitle": "CO2 from fossil fuels & industry, tonnes per person", "unit_format": "${:,.0f}",
             "source": "owid", "start_year": 1990}
    return topic, tl, views


def _frames(tl):
    s3, s4 = tl.slots[3], tl.slots[4]
    return {"mid": (s3.draw_start + s3.draw_end) / 2,
            "transition": s4.start + 0.75 * tl.transition,
            "end": tl.end_start + 2.0}


@pytest.mark.parametrize("backend", ["skia", "pillow"])
def test_every_centred_element_is_on_x540_in_pixels(backend):
    """A1: render real frames and measure each element's bbox from the IMAGE (non-background pixels),
    the same way the owner measured the MP4. Centre must be 539.5 +-2 px (pixel centre of 0..1079)
    and the left/right margins must match within 4 px."""
    cfg = copy.deepcopy(get_config())
    cfg["render"]["backend"] = backend
    topic, tl, views = _video()
    r = R.Renderer(topic, tl, views, cfg)
    for name, t in _frames(tl).items():
        rgb = r.frame_array(t)
        bands = END if name == "end" else CENTRED
        for b in bands:
            m = measure_band(rgb, *r.L[b])
            if m is None and name == "transition" and b in ("year_band", "value_band"):
                continue   # the incoming country's year/value appear with its first note
            assert m is not None, (name, b)
            assert abs(m["centre"] - 539.5) <= 2, (backend, name, b, m)
            assert abs(m["left"] - m["right"]) <= 4, (backend, name, b, m)


def test_stack_is_vertically_centred_in_safe_area():
    topic, tl, views = _video()
    r = R.Renderer(topic, tl, views)
    rgb = r.frame_array(_frames(tl)["mid"])
    rows = np.nonzero((np.abs(rgb.astype(int) - np.array([11, 13, 18])).max(axis=2) > 16).any(axis=1))[0]
    top, bottom = rows[0], rows[-1]
    assert bottom < R.SAFE_BOTTOM
    assert abs(top - (R.SAFE_BOTTOM - 1 - bottom)) <= 40, (top, bottom)


@pytest.mark.parametrize("which", ["mid0", "end7", "endcard", "intro", "transition"])
def test_no_pill_no_source_no_brand_and_text_in_safe_zone(tmp_path, which):
    topic, tl, views = _video()
    s0, s7 = tl.slots[0], tl.slots[-1]
    t = {"mid0": (s0.draw_start + s0.draw_end) / 2, "end7": s7.end - 0.01, "endcard": tl.end_start + 2,
         "intro": 0.1, "transition": tl.slots[2].start + 0.3}[which]
    boxes = R.render_still(topic, tl, views, t, tmp_path / "f.png")
    texts = [b[0] for b in boxes]
    assert topic["title"] not in texts                       # A2: no title pill
    assert not any(s.startswith("Source") for s in texts)    # A3: no source line
    assert not any(b[5] == "watermark" for b in boxes)       # A3: no brand by default
    for s, x0, y0, x1, y1, tag in boxes:
        assert R.SAFE_LEFT - 1 <= x0 and x1 <= R.SAFE_RIGHT + 1, (tag, s, x0, x1)
        assert y1 <= R.SAFE_BOTTOM, (tag, s, y1)


def test_header_is_the_metric_and_tick_labels_are_inside_the_chart(tmp_path):
    topic, tl, views = _video()
    r = R.Renderer(topic, tl, views)
    r.draw_frame(_frames(tl)["mid"])
    boxes = r.cv.text_boxes
    header = [b for b in boxes if b[5] == "header"]
    assert " ".join(b[0] for b in header) == topic["subtitle"] and len(header) <= 2
    ticks = [b for b in boxes if b[5] == "tick"]
    assert ticks
    for s, x0, y0, x1, y1, tag in ticks:
        assert R.PLOT_L <= x0 + 1 and x1 <= R.PLOT_R
        assert r.L["chart_t"] - 40 <= y0 and y1 <= r.L["chart_b"]


def test_brand_draws_only_when_configured(tmp_path):
    cfg = copy.deepcopy(get_config())
    assert cfg["brand"]["name"] == ""
    cfg["brand"]["name"] = "Example Channel"
    topic, tl, views = _video()
    boxes = R.render_still(topic, tl, views, 0.1, tmp_path / "b.png", cfg=cfg)
    assert any(b[5] == "watermark" and b[0] == "Example Channel" for b in boxes)


def _luminance(rgb):
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def test_palette_contrast():
    rc = get_config()["render"]
    bg = _luminance(hex_rgb(rc["background"]))
    pal = rc["palette"]
    assert len(set(pal)) == len(pal) >= get_config()["picker"]["max_countries"]
    for c in pal + [rc["muted"]]:
        ratio = (_luminance(hex_rgb(c)) + 0.05) / (bg + 0.05)
        assert ratio >= 4.5, (c, ratio)


def test_every_topic_header_fits_two_lines():
    cv = SkiaCanvas(R.W, R.H, font_files())
    for t in load_topics():
        size, lines = R.header_fit(cv, t["subtitle"])
        assert size >= 34 and 1 <= len(lines) <= 2, t["id"]
        assert " ".join(lines) == t["subtitle"], t["id"]
        assert all(cv.text_width(ln, "bold", size) <= R.CONTENT_W for ln in lines), t["id"]


def test_flags_present_for_pool():
    for c in countries():
        assert flag_png(c["iso2"]).exists(), c


@pytest.mark.parametrize("backend", ["skia", "pillow"])
def test_line_never_shows_through_a_tick_label(backend):
    """The line may pass a tick label at the left edge; it must go BEHIND it (halo), never over it."""
    cfg = copy.deepcopy(get_config())
    cfg["render"]["backend"] = backend
    pal = cfg["render"]["palette"]
    topic = {"id": "t", "title": "x", "subtitle": "Nuclear, % of electricity generated", "unit_format": "{:.1f}%",
             "source": "owid", "start_year": 1990}
    vals = [20.0 + 0.05 * k for k in range(len(YEARS))]
    vals[1], vals[2], vals[-1] = 20.0, 18.0, 25.0          # ticks 18/20/22/24
    probe = R.CountryView(iso3="USA", name="United States", iso2="us", color=pal[0], years=YEARS, values=vals)
    px_per_unit = (R.CHART_H) / (probe.yhi - probe.ylo)
    vals[0] = 20.0 + 18 / px_per_unit                       # first point lands INSIDE the "20.0%" label box
    views = [R.CountryView(iso3="USA", name="United States", iso2="us", color=pal[0], years=YEARS, values=vals)]
    tl = build([("USA", YEARS, vals)], 1990, 2023, cfg)
    r = R.Renderer(topic, tl, views, cfg)
    s = tl.slots[0]
    rgb = r.frame_array(s.end - 0.01).astype(int)
    accent = np.array(hex_rgb(pal[0]))
    ticks = [b for b in r.cv.text_boxes if b[5] == "tick"]
    assert ticks
    crossed = r.Y(views[0], vals[0])
    assert any(y0 <= crossed <= y1 for _, x0, y0, x1, y1, _ in ticks)   # the line really passes a label
    for txt, x0, y0, x1, y1, _ in ticks:
        box = rgb[int(y0) + 1:int(y1), int(x0) + 1:int(x1)]
        line_px = (np.abs(box - accent).max(axis=2) < 40).sum()
        assert line_px == 0, (backend, txt, line_px)
