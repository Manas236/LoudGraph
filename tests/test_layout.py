"""Safe zones: no text in the bottom 20% or the right-most 12% (except the watermark)."""
import numpy as np
import pytest

from pipeline import render as R
from pipeline.assets import flag_png, font_files
from pipeline.canvas import SkiaCanvas, hex_rgb
from pipeline.config import countries, get_config
from pipeline.timeline import build
from pipeline.topics import load_topics


def test_safe_zone_constants():
    assert R.SAFE_RIGHT == int(R.W * 0.88) == 950
    assert R.SAFE_BOTTOM == int(R.H * 0.80) == 1536
    assert R.CHART_R <= R.SAFE_RIGHT
    # every text baseline plus a generous descent stays above the bottom safe line
    for base, size in [(R.YEAR_BASE, 104), (R.VALUE_BASE, 128), (R.SOURCE_BASE, 26), (R.XLAB_BASE, 26),
                       (R.END_CTA_BASE, 46), (R.END_SOURCE_BASE, 26)]:
        assert base + 0.3 * size <= R.SAFE_BOTTOM, base
    assert R.END_ROWS_B <= R.END_CTA_BASE - 46
    assert R.PILL_Y >= R.SAFE_TOP


def _views_and_timeline(labelled=True):
    years = list(range(1990, 2024))
    t = np.arange(len(years))
    names = ["GBR", "SAU", "ZAF", "USA", "KOR", "NZL", "IND", "BRA"]  # long names on purpose
    tl = build([(c, years) for c in names], 1990, 2023)
    pal = get_config()["render"]["palette"]
    by = {c["iso3"]: c for c in countries()}
    views = []
    for i, c in enumerate(names):
        vals = list(1000 + 900 * np.sin(t / (2 + i)) + 40 * t)  # wide values -> wide tick labels
        lab = {"year": 2008, "label": "Global financial crisis"[:24]} if labelled else None
        views.append(R.CountryView(iso3=c, name=by[c]["name"], iso2=by[c]["iso2"], color=pal[i], years=years,
                                   values=vals, label=lab))
    topic = {"id": "t", "title": "What your carbon footprint sounds like",
             "subtitle": "CO2 from fossil fuels & industry, tonnes per person, every year", "unit_format": "${:,.0f}",
             "source": "owid", "start_year": 1990}
    return topic, tl, views


@pytest.mark.parametrize("which", ["mid0", "end0", "mid7", "end7", "endcard", "intro"])
def test_rendered_text_stays_in_safe_zone(tmp_path, which):
    topic, tl, views = _views_and_timeline()
    s0, s7 = tl.slots[0], tl.slots[-1]
    t = {"mid0": (s0.draw_start + s0.draw_end) / 2, "end0": s0.end - 0.01, "mid7": (s7.draw_start + s7.draw_end) / 2,
         "end7": s7.end - 0.01, "endcard": tl.end_start + 2, "intro": 0.1}[which]
    boxes = R.render_still(topic, tl, views, t, tmp_path / "f.png")
    assert boxes
    for s, x0, y0, x1, y1, tag in boxes:
        if tag == "watermark":
            continue
        assert x0 >= R.SAFE_LEFT - 1 and x1 <= R.SAFE_RIGHT + 1, (tag, s, x0, x1)
        assert y0 >= R.SAFE_TOP - 1 and y1 <= R.SAFE_BOTTOM + 1, (tag, s, y0, y1)


def test_pillow_fallback_renders_in_safe_zone(tmp_path):
    import copy
    cfg = copy.deepcopy(get_config())
    cfg["render"]["backend"] = "pillow"
    topic, tl, views = _views_and_timeline()
    s = tl.slots[3]
    boxes = R.render_still(topic, tl, views, s.end - 0.01, tmp_path / "p.png", cfg=cfg)
    assert (tmp_path / "p.png").stat().st_size > 10000
    for txt, x0, y0, x1, y1, tag in boxes:
        if tag != "watermark":
            assert R.SAFE_LEFT - 1 <= x0 and x1 <= R.SAFE_RIGHT + 1 and y1 <= R.SAFE_BOTTOM + 1, (tag, txt)


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


def test_every_topic_title_and_subtitle_fit():
    cv = SkiaCanvas(R.W, R.H, font_files())
    for t in load_topics():
        assert cv.text_width(t["title"], "bold", 30) <= R.CONTENT_W - 72, t["id"]
        assert len(cv.wrap(t["subtitle"], "regular", 32, R.CONTENT_W)) <= 2, t["id"]


def test_flags_present_for_pool():
    for c in countries():
        assert flag_png(c["iso2"]).exists(), c
