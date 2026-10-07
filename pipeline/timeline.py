"""SINGLE source of truth for timing (section 6).

Layout of a video with N countries:

    | intro | country 1                      | country 2 ... | end card |
            | transition | draw .......| hold |

Every data year of a country gets an onset time inside its draw window, placed by its year
position on the shared x-axis (x_start..x_end), so evenly spaced when there are no gaps.
audio.py places note onsets at these times and render.py moves the line head through
these same times. Nothing else computes timing.
"""
from __future__ import annotations

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

    def year_pos(self, t: float) -> float:
        """Fractional x-axis year the line head has reached at time t (clamped)."""
        if t <= self.draw_start:
            return float(self.x_start)
        if t >= self.draw_end:
            return float(self.x_end)
        f = (t - self.draw_start) / (self.draw_end - self.draw_start)
        return self.x_start + f * (self.x_end - self.x_start)

    def onset_of(self, year: float) -> float:
        f = (year - self.x_start) / (self.x_end - self.x_start)
        return self.draw_start + f * (self.draw_end - self.draw_start)

    def reached_index(self, t: float) -> int:
        """Index of the last data year whose onset is <= t (-1 if none yet)."""
        k = -1
        for i, o in enumerate(self.onsets):
            if o <= t + 1e-9:
                k = i
            else:
                break
        return k


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
    """Default per-country duration, stretched when too few countries would undershoot min_seconds."""
    t, v = _timing(cfg)
    fixed = t["intro"] + t["end_card"]
    need = (v["min_seconds"] - fixed) / n if n else t["per_country"]
    return max(t["per_country"], need)


def total_seconds(n: int, cfg: dict | None = None) -> float:
    t, _ = _timing(cfg)
    return t["intro"] + n * per_country_seconds(n, cfg) + t["end_card"]


def country_count_bounds(cfg: dict | None = None) -> tuple[int, int]:
    """(n_min, n_max) countries that fill min_seconds..max_seconds at the default per-country time."""
    t, v = _timing(cfg)
    fixed = t["intro"] + t["end_card"]
    n_min = math.ceil((v["min_seconds"] - fixed) / t["per_country"] - 1e-9)
    n_max = math.floor((v["max_seconds"] - fixed) / t["per_country"] + 1e-9)
    return n_min, n_max


def build(countries: list[tuple[str, list[int]]], x_start: int, x_end: int, cfg: dict | None = None) -> Timeline:
    """countries: [(iso3, [data years...]), ...] in play order."""
    cfg = cfg or get_config()
    t, v = _timing(cfg)
    if x_end <= x_start:
        raise ValueError("x_end must be after x_start")
    n = len(countries)
    per = per_country_seconds(n, cfg)
    slots = []
    cursor = t["intro"]
    for i, (iso3, years) in enumerate(countries):
        start, end = cursor, cursor + per
        trans_end = start + t["transition"]
        draw_start, draw_end = trans_end, end - t["hold"]
        s = Slot(index=i, iso3=iso3, start=round(start, 6), end=round(end, 6), trans_end=round(trans_end, 6),
                 draw_start=round(draw_start, 6), draw_end=round(draw_end, 6), x_start=x_start, x_end=x_end)
        ys = [int(y) for y in years if x_start <= y <= x_end]
        s.years = ys
        s.onsets = [round(s.onset_of(y), 6) for y in ys]
        slots.append(s)
        cursor = end
    end_start = cursor
    duration = end_start + t["end_card"]
    fps = v["fps"]
    return Timeline(fps=fps, intro=t["intro"], per_country=per, transition=t["transition"], hold=t["hold"],
                    end_card=t["end_card"], slots=slots, end_start=round(end_start, 6),
                    duration=round(duration, 6), n_frames=int(math.ceil(duration * fps - 1e-9)))
