"""Tiny drawing abstraction with two backends.

SkiaCanvas  - anti-aliased paths, real blur for glow (preferred).
PillowCanvas - fallback: draws at 2x and downsamples; glow is approximated with wide translucent strokes.

Every text() call records its bounding box in `self.text_boxes` so tests can assert safe zones.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def pick_backend(pref: str = "auto") -> tuple[str, str]:
    if pref in ("auto", "skia"):
        try:
            import skia
            s = skia.Surface(8, 8)
            s.getCanvas().drawCircle(4, 4, 2, skia.Paint(AntiAlias=True))
            return "skia", f"skia-python {skia.__version__}"
        except Exception as e:  # noqa: BLE001 - any import/runtime failure means fallback
            if pref == "skia":
                raise
            return "pillow", f"skia unavailable ({type(e).__name__}: {e}); Pillow 2x fallback"
    return "pillow", "forced by config"


def make_canvas(w: int, h: int, fonts: dict[str, Path], pref: str = "auto"):
    name, why = pick_backend(pref)
    c = SkiaCanvas(w, h, fonts) if name == "skia" else PillowCanvas(w, h, fonts)
    c.backend_note = why
    return c


class BaseCanvas:
    backend = "base"
    backend_note = ""

    def __init__(self, w: int, h: int, fonts: dict[str, Path]):
        self.w, self.h = w, h
        self.fonts = fonts
        self.text_boxes: list[tuple[str, float, float, float, float, str]] = []
        self.images: dict[str, object] = {}

    def _record(self, s, x0, y0, x1, y1, tag):
        self.text_boxes.append((s, x0, y0, x1, y1, tag))

    def fit_size(self, s: str, font: str, max_w: float, start: int, minimum: int) -> int:
        size = start
        while size > minimum and self.text_width(s, font, size) > max_w:
            size -= 2
        return size

    def wrap(self, s: str, font: str, size: int, max_w: float) -> list[str]:
        words, lines, cur = s.split(), [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if self.text_width(t, font, size) <= max_w or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines


# ====================================================================== skia

class SkiaCanvas(BaseCanvas):
    backend = "skia"

    def __init__(self, w, h, fonts):
        super().__init__(w, h, fonts)
        import skia
        self.sk = skia
        self.info = skia.ImageInfo.Make(w, h, skia.kRGBA_8888_ColorType, skia.kPremul_AlphaType)
        self.surface = skia.Surface.MakeRaster(self.info)
        self.c = self.surface.getCanvas()
        self.typefaces = {k: skia.Typeface.MakeFromFile(str(p)) for k, p in fonts.items()}
        self._fonts: dict = {}
        self.sampling = skia.SamplingOptions(skia.FilterMode.kLinear, skia.MipmapMode.kLinear)
        self._buf = np.empty((h, w, 4), dtype=np.uint8)

    def _color(self, color, alpha=1.0):
        r, g, b = hex_rgb(color) if isinstance(color, str) else color
        return self.sk.ColorSetARGB(int(round(255 * max(0, min(1, alpha)))), r, g, b)

    def _font(self, name, size):
        k = (name, size)
        if k not in self._fonts:
            f = self.sk.Font(self.typefaces[name], size)
            f.setSubpixel(True)
            f.setEdging(self.sk.Font.Edging.kAntiAlias)
            self._fonts[k] = f
        return self._fonts[k]

    def _paint(self, color, alpha=1.0, stroke=None, blur=0.0, cap="round"):
        sk = self.sk
        p = sk.Paint(AntiAlias=True, Color=self._color(color, alpha))
        if stroke is not None:
            p.setStyle(sk.Paint.kStroke_Style)
            p.setStrokeWidth(stroke)
            p.setStrokeCap(sk.Paint.kRound_Cap if cap == "round" else sk.Paint.kButt_Cap)
            p.setStrokeJoin(sk.Paint.kRound_Join)
        if blur > 0:
            p.setMaskFilter(sk.MaskFilter.MakeBlur(sk.kNormal_BlurStyle, blur))
        return p

    def clear(self, color):
        self.c.clear(self._color(color))
        self.text_boxes = []

    def rect(self, x, y, w, h, color, alpha=1.0):
        self.c.drawRect(self.sk.Rect.MakeXYWH(x, y, w, h), self._paint(color, alpha))

    def rrect(self, x, y, w, h, r, fill=None, stroke=None, stroke_w=2.0, alpha=1.0):
        rr = self.sk.RRect.MakeRectXY(self.sk.Rect.MakeXYWH(x, y, w, h), r, r)
        if fill:
            self.c.drawRRect(rr, self._paint(fill, alpha))
        if stroke:
            self.c.drawRRect(rr, self._paint(stroke, alpha, stroke=stroke_w))

    def circle(self, cx, cy, r, color, alpha=1.0, blur=0.0, stroke=None):
        self.c.drawCircle(cx, cy, r, self._paint(color, alpha, stroke=stroke, blur=blur))

    def line(self, x1, y1, x2, y2, color, width=2.0, alpha=1.0):
        self.c.drawLine(x1, y1, x2, y2, self._paint(color, alpha, stroke=width, cap="butt"))

    def polyline(self, pts, color, width, alpha=1.0, blur=0.0):
        if len(pts) < 2:
            return
        path = self.sk.Path()
        path.moveTo(*pts[0])
        for p in pts[1:]:
            path.lineTo(*p)
        self.c.drawPath(path, self._paint(color, alpha, stroke=width, blur=blur))

    def area(self, pts, base_y, color, alpha_top, alpha_bottom, y_top):
        if len(pts) < 2:
            return
        sk = self.sk
        path = sk.Path()
        path.moveTo(pts[0][0], base_y)
        for p in pts:
            path.lineTo(*p)
        path.lineTo(pts[-1][0], base_y)
        path.close()
        paint = sk.Paint(AntiAlias=True)
        paint.setShader(sk.GradientShader.MakeLinear(
            [sk.Point(0, y_top), sk.Point(0, base_y)],
            [self._color(color, alpha_top), self._color(color, alpha_bottom)]))
        self.c.drawPath(path, paint)

    def text_width(self, s, font, size):
        return self._font(font, size).measureText(s)

    def text(self, x, y, s, font, size, color, anchor="l", alpha=1.0, tag="text"):
        f = self._font(font, size)
        w = f.measureText(s)
        x0 = x - w / 2 if anchor == "m" else (x - w if anchor == "r" else x)
        self.c.drawString(s, x0, y, f, self._paint(color, alpha))
        m = f.getMetrics()
        self._record(s, x0, y + m.fAscent, x0 + w, y + m.fDescent, tag)
        return w

    def load_image(self, key, p: Path):
        if key not in self.images:
            self.images[key] = self.sk.Image.open(str(p))
        return key

    def image(self, key, x, y, w, h, radius=0.0, alpha=1.0):
        sk = self.sk
        img = self.images[key]
        rect = sk.Rect.MakeXYWH(x, y, w, h)
        self.c.save()
        if radius:
            self.c.clipRRect(sk.RRect.MakeRectXY(rect, radius, radius), True)
        p = sk.Paint(AntiAlias=True)
        p.setAlphaf(alpha)
        self.c.drawImageRect(img, rect, self.sampling, p)
        self.c.restore()

    def snapshot(self):
        # Snapshots are numpy arrays: a full-frame skia drawImage costs ~130 ms on the CPU raster
        # backend, while writePixels is a memcpy (<1 ms). The frame is opaque, so premul == straight.
        arr = np.empty((self.h, self.w, 4), dtype=np.uint8)
        self.surface.readPixels(self.info, arr)
        return arr

    def draw_snapshot(self, snap, alpha=1.0, dx=0.0):
        alpha = max(0.0, min(1.0, alpha))
        if alpha <= 0.001:
            return
        if alpha < 0.999:
            self.surface.readPixels(self.info, self._buf)
            cur = self._buf.astype(np.int16)
            snap = (cur + ((snap.astype(np.int16) - cur) * alpha)).astype(np.uint8)
        self.c.writePixels(self.info, np.ascontiguousarray(snap), self.w * 4, 0, 0)

    def rgb_bytes(self) -> bytes:
        self.surface.readPixels(self.info, self._buf)
        return self._buf[:, :, :3].tobytes()

    def save_png(self, p: Path):
        self.surface.makeImageSnapshot().save(str(p), self.sk.kPNG)


# ====================================================================== Pillow fallback

class PillowCanvas(BaseCanvas):
    backend = "pillow"
    S = 2  # supersampling factor

    def __init__(self, w, h, fonts):
        super().__init__(w, h, fonts)
        from PIL import Image, ImageDraw, ImageFont
        self.Image, self.ImageDraw, self.ImageFont = Image, ImageDraw, ImageFont
        self.img = Image.new("RGB", (w * self.S, h * self.S))
        self.d = ImageDraw.Draw(self.img, "RGBA")
        self._fonts: dict = {}

    def _c(self, color, alpha=1.0):
        r, g, b = hex_rgb(color) if isinstance(color, str) else color
        return (r, g, b, int(round(255 * max(0, min(1, alpha)))))

    def _font(self, name, size):
        k = (name, size)
        if k not in self._fonts:
            self._fonts[k] = self.ImageFont.truetype(str(self.fonts[name]), int(round(size * self.S)))
        return self._fonts[k]

    def clear(self, color):
        self.img.paste(self._c(color)[:3], (0, 0, self.img.width, self.img.height))
        self.text_boxes = []

    def rect(self, x, y, w, h, color, alpha=1.0):
        S = self.S
        self.d.rectangle([x * S, y * S, (x + w) * S, (y + h) * S], fill=self._c(color, alpha))

    def rrect(self, x, y, w, h, r, fill=None, stroke=None, stroke_w=2.0, alpha=1.0):
        S = self.S
        box = [x * S, y * S, (x + w) * S, (y + h) * S]
        self.d.rounded_rectangle(box, radius=r * S, fill=self._c(fill, alpha) if fill else None,
                                 outline=self._c(stroke, alpha) if stroke else None,
                                 width=int(round(stroke_w * S)) if stroke else 0)

    def circle(self, cx, cy, r, color, alpha=1.0, blur=0.0, stroke=None):
        S = self.S
        if blur > 0:  # approximate a blurred disc with fading rings
            for k in range(4, 0, -1):
                rr = r * 0.6 + blur * k / 5
                self.d.ellipse([(cx - rr) * S, (cy - rr) * S, (cx + rr) * S, (cy + rr) * S],
                               fill=self._c(color, alpha * 0.14))
            return
        box = [(cx - r) * S, (cy - r) * S, (cx + r) * S, (cy + r) * S]
        if stroke:
            self.d.ellipse(box, outline=self._c(color, alpha), width=int(round(stroke * S)))
        else:
            self.d.ellipse(box, fill=self._c(color, alpha))

    def line(self, x1, y1, x2, y2, color, width=2.0, alpha=1.0):
        S = self.S
        self.d.line([x1 * S, y1 * S, x2 * S, y2 * S], fill=self._c(color, alpha), width=max(1, int(round(width * S))))

    def polyline(self, pts, color, width, alpha=1.0, blur=0.0):
        if len(pts) < 2:
            return
        S = self.S
        sp = [(x * S, y * S) for x, y in pts]
        if blur > 0:
            for k in (2.2, 1.6):
                self.d.line(sp, fill=self._c(color, alpha * 0.25), width=int(width * k * S), joint="curve")
            return
        self.d.line(sp, fill=self._c(color, alpha), width=max(1, int(round(width * S))), joint="curve")
        r = width * S / 2
        for x, y in (sp[0], sp[-1]):
            self.d.ellipse([x - r, y - r, x + r, y + r], fill=self._c(color, alpha))

    def area(self, pts, base_y, color, alpha_top, alpha_bottom, y_top):
        if len(pts) < 2:
            return
        S = self.S
        poly = [(pts[0][0] * S, base_y * S)] + [(x * S, y * S) for x, y in pts] + [(pts[-1][0] * S, base_y * S)]
        self.d.polygon(poly, fill=self._c(color, (alpha_top + alpha_bottom) / 2))

    def text_width(self, s, font, size):
        return self._font(font, size).getlength(s) / self.S

    def text(self, x, y, s, font, size, color, anchor="l", alpha=1.0, tag="text"):
        S = self.S
        f = self._font(font, size)
        a = {"l": "ls", "m": "ms", "r": "rs"}[anchor]
        r, g, b, _ = self._c(color)
        if alpha < 1:  # Pillow ignores fill alpha for text on RGB images: blend against the pixel under it
            br, bg_, bb = self.img.getpixel((min(int(x * S), self.img.width - 1), min(int(y * S), self.img.height - 1)))
            r, g, b = (int(c0 + (c1 - c0) * alpha) for c0, c1 in ((br, r), (bg_, g), (bb, b)))
        self.d.text((x * S, y * S), s, font=f, fill=(r, g, b), anchor=a)
        x0, y0, x1, y1 = self.d.textbbox((x * S, y * S), s, font=f, anchor=a)
        self._record(s, x0 / S, y0 / S, x1 / S, y1 / S, tag)
        return (x1 - x0) / S

    def load_image(self, key, p: Path):
        if key not in self.images:
            self.images[key] = self.Image.open(p).convert("RGBA")
        return key

    def image(self, key, x, y, w, h, radius=0.0, alpha=1.0):
        S = self.S
        im = self.images[key].resize((int(w * S), int(h * S)), self.Image.LANCZOS)
        mask = self.Image.new("L", im.size, 0)
        self.ImageDraw.Draw(mask).rounded_rectangle([0, 0, im.size[0] - 1, im.size[1] - 1], radius=radius * S,
                                                     fill=int(255 * alpha))
        a = im.getchannel("A").point(lambda v: v)
        from PIL import ImageChops
        mask = ImageChops.multiply(mask, a)
        self.img.paste(im.convert("RGB"), (int(x * S), int(y * S)), mask)

    def snapshot(self):
        return self.img.copy()

    def draw_snapshot(self, snap, alpha=1.0, dx=0.0):
        alpha = max(0.0, min(1.0, alpha))
        if alpha >= 0.999 and not dx:
            self.img.paste(snap)
        elif alpha > 0:
            self.img.paste(self.Image.blend(self.img, snap, alpha))

    def rgb_bytes(self) -> bytes:
        return self.img.reduce(self.S).tobytes()

    def save_png(self, p: Path):
        self.img.reduce(self.S).save(p)
