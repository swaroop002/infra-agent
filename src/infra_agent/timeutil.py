"""Time windows, durations and interval arithmetic shared by tools, collector and ledger."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h|d|w)", re.IGNORECASE)
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_WORDS = {
    "hour": "1h", "day": "1d", "week": "1w", "month": "30d", "today": "1d",
    "yesterday": "1d", "fortnight": "14d",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_duration(text: str) -> timedelta:
    """Parse "90s", "15m", "24h", "7d", "1w", "1h30m", "past week"."""
    t = text.strip().lower()
    for word, dur in _WORDS.items():
        if re.fullmatch(rf"(last|past|this)?\s*{word}s?", t):
            t = dur
            break
    matches = _DURATION.findall(t)
    if not matches or _DURATION.sub("", t).strip():
        raise ValueError(f"Unrecognised duration {text!r}; use forms like 30m, 24h, 7d")
    seconds = sum(float(n) * _UNIT_SECONDS[u.lower()] for n, u in matches)
    if seconds <= 0:
        raise ValueError(f"Duration must be positive: {text!r}")
    return timedelta(seconds=seconds)


def prom_duration(delta: timedelta) -> str:
    """Render a timedelta as a Prometheus/Loki range literal."""
    s = int(delta.total_seconds())
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if s % size == 0 and s >= size:
            return f"{s // size}{unit}"
    return f"{max(s, 1)}s"


def human_duration(delta: timedelta | float) -> str:
    s = int(delta.total_seconds() if isinstance(delta, timedelta) else delta)
    if s < 60:
        return f"{s}s"
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            parts.append(f"{s // size}{unit}")
            s %= size
    return " ".join(parts[:2])


def parse_time(text: str) -> datetime:
    dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def range_literal(self) -> str:
        return prom_duration(self.duration)

    def previous(self) -> Window:
        return Window(self.start - self.duration, self.start)

    def to_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(timespec="seconds"),
                "end": self.end.isoformat(timespec="seconds"),
                "duration": human_duration(self.duration)}


def resolve_window(window: str | None = None, start: str | None = None, end: str | None = None,
                   *, default: str = "24h", now: datetime | None = None,
                   max_duration: timedelta | None = None) -> Window:
    now = now or utcnow()
    end_dt = parse_time(end) if end else now
    if start:
        start_dt = parse_time(start)
    else:
        start_dt = end_dt - parse_duration(window or default)
    if start_dt >= end_dt:
        raise ValueError("Window start must be before end")
    if max_duration and end_dt - start_dt > max_duration:
        start_dt = end_dt - max_duration
    return Window(start_dt, end_dt)


def pick_step(duration: timedelta, max_points: int = 1500, min_step: int = 15) -> int:
    """Seconds per point so a range query stays under max_points samples."""
    step = int(duration.total_seconds() // max_points) + 1
    return max(min_step, step)


@dataclass
class Interval:
    start: datetime
    end: datetime | None  # None = still ongoing

    def duration(self, now: datetime | None = None) -> timedelta:
        return (self.end or now or utcnow()) - self.start

    def to_dict(self, now: datetime | None = None) -> dict[str, str | None]:
        return {"start": self.start.isoformat(timespec="seconds"),
                "end": self.end.isoformat(timespec="seconds") if self.end else None,
                "duration": human_duration(self.duration(now)),
                "ongoing": self.end is None}


def down_intervals(samples: list[tuple[float, float]], step_s: int, window: Window,
                   *, is_down=lambda v: v < 1) -> tuple[list[Interval], float, float]:
    """Turn a range-query series into down intervals.

    Returns (intervals, down_seconds, observed_seconds). A gap longer than 2 steps is treated as
    "no data" (neither up nor down) so missing scrapes don't masquerade as downtime.
    """
    intervals: list[Interval] = []
    down_s = 0.0
    observed_s = 0.0
    current: Interval | None = None
    prev_ts: float | None = None
    for ts, value in sorted(samples):
        gap = prev_ts is not None and ts - prev_ts > 2 * step_s
        if gap and current is not None:
            current.end = datetime.fromtimestamp(prev_ts + step_s, UTC)
            intervals.append(current)
            current = None
        weight = step_s
        observed_s += weight
        if is_down(value):
            down_s += weight
            if current is None:
                current = Interval(datetime.fromtimestamp(ts, UTC), None)
        elif current is not None:
            current.end = datetime.fromtimestamp(ts, UTC)
            intervals.append(current)
            current = None
        prev_ts = ts
    if current is not None:
        last_ts = prev_ts or window.end.timestamp()
        # Still down at the last sample: ongoing only if the series reaches the window end.
        if window.end.timestamp() - last_ts <= 2 * step_s:
            intervals.append(current)
        else:
            current.end = datetime.fromtimestamp(last_ts + step_s, UTC)
            intervals.append(current)
    total = window.duration.total_seconds()
    return intervals, min(down_s, total), min(observed_s, total)


def overlap_seconds(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> float:
    lo, hi = max(a_start, b_start), min(a_end, b_end)
    return max(0.0, (hi - lo).total_seconds())
