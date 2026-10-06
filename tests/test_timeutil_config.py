from datetime import UTC, datetime, timedelta

import pytest

from infra_agent.config import interpolate_env, load_settings
from infra_agent.timeutil import (
    Window,
    down_intervals,
    human_duration,
    parse_duration,
    prom_duration,
    resolve_window,
)


@pytest.mark.parametrize("text,seconds", [
    ("30m", 1800), ("24h", 86400), ("7d", 604800), ("1w", 604800), ("1h30m", 5400),
    ("past week", 604800), ("last day", 86400), ("90s", 90),
])
def test_parse_duration(text, seconds):
    assert parse_duration(text).total_seconds() == seconds


@pytest.mark.parametrize("bad", ["", "abc", "24", "-5m", "5 parsecs"])
def test_parse_duration_rejects(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_prom_and_human_duration():
    assert prom_duration(timedelta(hours=24)) == "1d"
    assert prom_duration(timedelta(minutes=90)) == "90m"
    assert human_duration(timedelta(minutes=135)) == "2h 15m"


def test_resolve_window_caps_duration():
    now = datetime(2026, 9, 12, 12, tzinfo=UTC)
    w = resolve_window("30d", now=now, max_duration=timedelta(days=7))
    assert w.duration == timedelta(days=7) and w.end == now


def test_down_intervals_ongoing_and_gaps():
    start = datetime(2026, 9, 12, 0, tzinfo=UTC)
    window = Window(start, start + timedelta(minutes=10))
    t0 = start.timestamp()
    samples = [(t0 + 60 * i, v) for i, v in enumerate([1, 1, 0, 0, 1, 1, 1, 0, 0, 0])]
    intervals, down_s, observed_s = down_intervals(samples, 60, window)
    assert len(intervals) == 2
    assert intervals[0].end is not None and intervals[1].end is None
    assert down_s == 300 and observed_s == 600


def test_down_intervals_gap_is_not_downtime():
    start = datetime(2026, 9, 12, 0, tzinfo=UTC)
    window = Window(start, start + timedelta(minutes=30))
    t0 = start.timestamp()
    samples = [(t0, 0), (t0 + 60, 0), (t0 + 1200, 1)]  # 19-minute hole in the data
    intervals, down_s, observed_s = down_intervals(samples, 60, window)
    assert down_s == 120
    assert intervals[0].end is not None


def test_env_interpolation():
    env = {"A": "x"}
    assert interpolate_env({"k": "${A}", "d": "${B:-dflt}", "e": "${B}"}, env) == {"k": "x", "d": "dflt", "e": ""}


def test_demo_config_loads(demo_settings):
    assert demo_settings.backends.metrics.type == "fake"
    assert demo_settings.resolve_path("examples").exists()


def test_example_config_parses(monkeypatch, tmp_path):
    from pathlib import Path

    settings = load_settings(Path(__file__).resolve().parents[1] / "config" / "agent.example.yaml")
    assert settings.backends.metrics.type == "prometheus"
    assert settings.backends.inventory[2].options()["projects"] == [""]
