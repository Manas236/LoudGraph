"""branding/profile_pic.png: 1080x1080 Graphony profile picture in the video's style (dark background,
glowing accent line with a bright head dot, the palette's cyan and pink). Everything sits inside the
circle Instagram and Facebook crop to. Also writes profile_pic_mark.png (the line only, no text).

    python scripts/make_profile_pic.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import skia

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.assets import font_files  # noqa: E402

S = 1080
BG, GRID, CYAN, PINK, TEXT = "#0b0d12", "#1f2430", "#22d3ee", "#f472b6", "#f5f7fa"


def color(h: str, a: float = 1.0) -> int:
    h = h.lstrip("#")
    return skia.ColorSetARGB(int(255 * a), int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def stroke(c: str, w: float, a: float = 1.0, blur: float = 0.0) -> skia.Paint:
    p = skia.Paint(AntiAlias=True, Color=color(c, a), Style=skia.Paint.kStroke_Style, StrokeWidth=w)
    p.setStrokeCap(skia.Paint.kRound_Cap)
    p.setStrokeJoin(skia.Paint.kRound_Join)
    if blur:
        p.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, blur))
    return p


def path(pts) -> skia.Path:
    p = skia.Path()
    p.moveTo(*pts[0])
    for x, y in pts[1:]:
        p.lineTo(x, y)
    return p


def draw(with_text: bool) -> skia.Image:
    surface = skia.Surface(S, S)
    c = surface.getCanvas()
    c.clear(color(BG))
    glow = skia.Paint(AntiAlias=True)
    glow.setShader(skia.GradientShader.MakeRadial(skia.Point(S / 2, S * 0.46), S * 0.52,
                                                  [color("#16203a"), color(BG)]))
    c.drawCircle(S / 2, S / 2, S / 2, glow)
    top, bottom = (250, 640) if with_text else (300, 780)
    x0, x1 = 215, 865
    for k in range(4):                                     # faint gridlines, as in the video
        y = top + (bottom - top) * k / 3
        c.drawLine(x0, y, x1, y, stroke(GRID, 6, 1.0))
    xs = [x0 + (x1 - x0) * i / 9 for i in range(10)]
    main = [0.18, 0.30, 0.24, 0.46, 0.40, 0.62, 0.50, 0.58, 0.84, 0.96]   # a rise with dips: the "melody"
    second = [0.62, 0.66, 0.58, 0.52, 0.56, 0.44, 0.40, 0.30, 0.34, 0.24]  # a falling comparison line
    Y = lambda v: bottom - v * (bottom - top)  # noqa: E731
    p2 = [(x, Y(v)) for x, v in zip(xs, second)]
    c.drawPath(path(p2), stroke(PINK, 16, 0.35, 10))
    c.drawPath(path(p2), stroke(PINK, 10, 0.75))
    p1 = [(x, Y(v)) for x, v in zip(xs, main)]
    area = path(p1)
    area.lineTo(p1[-1][0], bottom)
    area.lineTo(p1[0][0], bottom)
    area.close()
    fill = skia.Paint(AntiAlias=True)
    fill.setShader(skia.GradientShader.MakeLinear([skia.Point(0, top), skia.Point(0, bottom)],
                                                  [color(CYAN, 0.38), color(CYAN, 0.0)]))
    c.drawPath(area, fill)
    c.drawPath(path(p1), stroke(CYAN, 44, 0.55, 22))
    c.drawPath(path(p1), stroke(CYAN, 22))
    c.drawPath(path(p1), stroke("#ffffff", 6, 0.45))
    hx, hy = p1[-1]
    head = skia.Paint(AntiAlias=True, Color=color(CYAN, 0.6))
    head.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, 24))
    c.drawCircle(hx, hy, 52, head)
    c.drawCircle(hx, hy, 30, skia.Paint(AntiAlias=True, Color=color(CYAN)))
    c.drawCircle(hx, hy, 13, skia.Paint(AntiAlias=True, Color=color("#ffffff")))
    if with_text:
        tf = skia.Typeface.MakeFromFile(str(font_files()["black"]))
        size = 168
        font = skia.Font(tf, size)
        w = font.measureText("Graphony")
        while w > 740:
            size -= 4
            font = skia.Font(tf, size)
            w = font.measureText("Graphony")
        c.drawString("Graphony", (S - w) / 2, 820, font, skia.Paint(AntiAlias=True, Color=color(TEXT)))
    return surface.makeImageSnapshot()


def main() -> int:
    out = ROOT / "branding"
    out.mkdir(exist_ok=True)
    draw(True).save(str(out / "profile_pic.png"), skia.kPNG)
    draw(False).save(str(out / "profile_pic_mark.png"), skia.kPNG)
    print("wrote", out / "profile_pic.png", "and", out / "profile_pic_mark.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
