"""Vendored assets: Inter (OFL, from google/fonts) and flags (MIT, from lipis/flag-icons).

The files are committed under assets/, so this only downloads when something is missing.
Flags are kept as the original SVG plus a PNG raster (the Pillow fallback cannot read SVG).
"""
from __future__ import annotations

import logging
from pathlib import Path

import requests

from .config import countries, path

log = logging.getLogger(__name__)

FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/inter/Inter%5Bopsz%2Cwght%5D.ttf"
FONT_LICENSE_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/inter/OFL.txt"
FLAG_URL = "https://raw.githubusercontent.com/lipis/flag-icons/main/flags/4x3/{iso2}.svg"
FLAG_LICENSE_URL = "https://raw.githubusercontent.com/lipis/flag-icons/main/LICENSE"
WEIGHTS = {"regular": 400, "semibold": 600, "bold": 700, "black": 800}
FLAG_PX = (320, 240)


def font_files() -> dict[str, Path]:
    d = path("fonts")
    return {k: d / f"Inter-{k.capitalize()}.ttf" for k in WEIGHTS}


def flag_png(iso2: str) -> Path:
    return path("flags") / f"{iso2}.png"


def _get(url: str) -> bytes:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def ensure_fonts() -> dict[str, Path]:
    files = font_files()
    if all(p.exists() for p in files.values()):
        return files
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    d = path("fonts")
    var = d / "Inter-Variable.ttf"
    if not var.exists():
        var.write_bytes(_get(FONT_URL))
    lic = d / "OFL.txt"
    if not lic.exists():
        lic.write_bytes(_get(FONT_LICENSE_URL))
    for k, w in WEIGHTS.items():
        if files[k].exists():
            continue
        f = TTFont(var)
        axes = {a.axisTag for a in f["fvar"].axes}
        loc = {"wght": w}
        if "opsz" in axes:
            loc["opsz"] = 32
        instantiateVariableFont(f, loc, inplace=True)
        f.save(files[k])
        log.info("font instance %s (wght %d)", files[k].name, w)
    return files


def ensure_flags() -> None:
    d = path("flags")
    lic = d / "LICENSE"
    if not lic.exists():
        lic.write_bytes(_get(FLAG_LICENSE_URL))
    for c in countries():
        svg = d / f"{c['iso2']}.svg"
        png = flag_png(c["iso2"])
        if not svg.exists():
            svg.write_bytes(_get(FLAG_URL.format(iso2=c["iso2"])))
        if not png.exists():
            rasterize_svg(svg, png, *FLAG_PX)
            log.info("flag %s", png.name)


def rasterize_svg(svg: Path, png: Path, w: int, h: int) -> None:
    import skia

    stream = skia.Stream.MakeFromFile(str(svg))
    dom = skia.SVGDOM.MakeFromStream(stream)
    dom.setContainerSize(skia.Size(w, h))
    surface = skia.Surface(w, h)
    canvas = surface.getCanvas()
    canvas.clear(skia.ColorTRANSPARENT)
    dom.render(canvas)
    surface.makeImageSnapshot().save(str(png), skia.kPNG)


def ensure_all() -> None:
    ensure_fonts()
    ensure_flags()
