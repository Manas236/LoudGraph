"""SINGLE source of truth for timing (section 6).

Layout of a video with N countries:

    | intro | country 1                      | country 2 ... | end card |
            | transition | draw .......| hold |

Inside the draw window every axis year gets a time ("knot"). A normal year step lasts
d = base_draw / n_steps. An EVENT is a data year whose move from the previous data year is at
least `event_threshold` of that country's range; the step(s) leading into it last `event_slowmo`
times longer (slow motion), so the slot grows. Data-year onsets are the knots of their years.

audio.py places note onsets (and event runs) at these times; render.py moves the line head with
year_pos(t), which inverts the same knots. Nothing else computes timing.
"""
from __future__ import annotations

import bisect
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import get_config


@dataclass
class Slot:
    index: int
    iso3: str
    start: float
    end: float
    trans_end: float
    draw_start: float
    draw_end: float
    x_start: int
    x_end: int
    years: list[int] = field(default_factory=list)
    onsets: list[float] = field(default_factory=list)
    knots: list[float] = field(default_factory=list)   # time of every integer year x_start..x_end
    events: list[dict] = field(default_factory=list)   # {"year","index","kind","move","t_from","t_to"}

    def __post_init__(self):
        if not self.knots:  # timelines saved before slow-mo existed: years were evenly spaced
            n = self.x_end - self.x_start
            self.knots = [self.draw_start + i * (self.draw_end - self.draw_start) / n for i in range(n + 1)]

    def year_pos(self, t: float) -> float:
        """Fractional x-axis year the line head has reached at time t (clamped)."""
        k = self.knots
        if t <= k[0]:
            return float(self.x_start)
        if t >= k[-1]:
            return float(self.x_end)
        i = bisect.bisect_right(k, t) - 1
        return self.x_start + i + (t - k[i]) / (k[i + 1] - k[i])

    def onset_of(self, year: float) -> float:
        k = self.knots
        f = year - self.x_start
        if f <= 0:
            return k[0]
        if f >= len(k) - 1:
            return k[-1]
        i = int(math.floor(f))
        return k[i] + (f - i) * (k[i + 1] - k[i])

    def reached_index(self, t: float) -> int:
        """Index of the last data year whose onset is <= t (-1 if none yet)."""
        return bisect.bisect_right(self.onsets, t + 1e-9) - 1

    def event_for(self, year: int) -> dict | None:
        for e in self.events:
            if e["year"] == year:
                return e
        return None


@dataclass
class Timeline:
    fps: int
    intro: float
    per_country: float
    transition: float
    hold: float
    end_card: float
    slots: list[Slot]
    end_start: float
    duration: float
    n_frames: int
    event_threshold: float = 0.25
    event_slowmo: float = 3.0

    def slot_at(self, t: float) -> Slot | None:
        for s in self.slots:
            if s.start <= t < s.end:
                return s
        return None

    def all_onsets(self) -> list[tuple[float, int, int]]:
        return [(o, s.index, y) for s in self.slots for y, o in zip(s.years, s.onsets)]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Timeline":
        d = dict(d)
        d["slots"] = [Slot(**s) for s in d["slots"]]
        return cls(**d)

    def save(self, p: Path) -> None:
        p.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")

    @classmethod
    def load(cls, p: Path) -> "Timeline":
        return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))


def _timing(cfg: dict | None) -> tuple[dict, dict]:
    cfg = cfg or get_config()
    return cfg["timing"], cfg["video"]


def per_country_seconds(n: int, cfg: dict | None = None) -> float:
    """Default per-country duration (before slow-mo), stretched when too few countries would
    undershoot min_seconds."""
    t, v = _timing(cfg)
    fixed = t["intro"] + t["end_card"]
    need = (v["min_seconds"] - fixed) / n if n else t["per_country"]
    return max(t["per_country"], need)


def total_seconds(n: int, cfg: dict | None = None) -> float:
    """Length without slow-mo events."""
    t, _ = _timing(cfg)
    return t["intro"] + n * per_country_seconds(n, cfg) + t["end_card"]


def country_count_bounds(cfg: dict | None = None) -> tuple[int, int]:
    """(n_min, n_max) countries that fill min_seconds..max_seconds at the default per-country time."""
    t, v = _timing(cfg)
    fixed = t["intro"] + t["end_card"]
    n_min = math.ceil((v["min_seconds"] - fixed) / t["per_country"] - 1e-9)
    n_max = math.floor((v["max_seconds"] - fixed) / t["per_country"] + 1e-9)
    return n_min, n_max


def find_events(years: list[int], values: list[float] | None, threshold: float) -> list[dict]:
    if not values or len(values) < 2:
        return []
    rng = max(values) - min(values)
    if rng <= 0:
        return []
    out = []
    for i in range(1, len(values)):
        move = (values[i] - values[i - 1]) / rng
        if abs(move) >= threshold:
            out.append({"year": int(years[i]), "index": i, "prev_year": int(years[i - 1]),
                        "kind": "fall" if move < 0 else "jump", "move": round(move, 4)})
    return out


def build(countries: list[tuple], x_start: int, x_end: int, cfg: dict | None = None) -> Timeline:
    """countries: [(iso3, [years]) or (iso3, [years], [values])] in play order.
    Values enable event slow-mo; without them every step has normal length."""
    cfg = cfg or get_config()
    t, v = _timing(cfg)
    if x_end <= x_start:
        raise ValueError("x_end must be after x_start")
    thr = t.get("event_threshold", 0.25)
    slow = t.get("event_slowmo", 3.0)
    n = len(countries)
    per = per_country_seconds(n, cfg)
    n_steps = x_end - x_start
    d = (per - t["transition"] - t["hold"]) / n_steps
    slots = []
    cursor = t["intro"]
    for i, c in enumerate(countries):
        iso3, years = c[0], c[1]
        values = c[2] if len(c) > 2 else None
        pairs = [(int(y), (values[j] if values else None)) for j, y in enumerate(years) if x_start <= y <= x_end]
        ys = [p[0] for p in pairs]
        vs = [p[1] for p in pairs] if values else None
        events = find_events(ys, vs, thr)
        slow_years = {y for e in events for y in range(e["prev_year"] + 1, e["year"] + 1)}
        start = cursor
        trans_end = start + t["transition"]
        knots = [trans_end]
        for y in range(x_start + 1, x_end + 1):
            knots.append(knots[-1] + d * (slow if y in slow_years else 1.0))
        draw_end = knots[-1]
        end = draw_end + t["hold"]
        s = Slot(index=i, iso3=iso3, start=round(start, 6), end=round(end, 6), trans_end=round(trans_end, 6),
                 draw_start=round(trans_end, 6), draw_end=round(draw_end, 6), x_start=x_start, x_end=x_end,
                 years=ys, knots=[round(k, 6) for k in knots])
        s.onsets = [round(s.onset_of(y), 6) for y in ys]
        for e in events:
            e["t_from"] = round(s.onset_of(e["prev_year"]), 6)
            e["t_to"] = round(s.onset_of(e["year"]), 6)
        s.events = events
        slots.append(s)
        cursor = end
    end_start = cursor
    duration = end_start + t["end_card"]
    fps = v["fps"]
    return Timeline(fps=fps, intro=t["intro"], per_country=per, transition=t["transition"], hold=t["hold"],
                    end_card=t["end_card"], slots=slots, end_start=round(end_start, 6),
                    duration=round(duration, 6), n_frames=int(math.ceil(duration * fps - 1e-9)),
                    event_threshold=thr, event_slowmo=slow)


def fits(tl: Timeline, cfg: dict | None = None) -> bool:
    _, v = _timing(cfg)
    return v["min_seconds"] - 1e-6 <= tl.duration <= v["max_seconds"] + 1e-6
