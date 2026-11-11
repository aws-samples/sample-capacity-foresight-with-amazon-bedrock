# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from datetime import datetime, timedelta, timezone

import pytest

from capacity_foresight.config import ConfigError, load_config, parse_config
from capacity_foresight.demo import demo_hourly
from capacity_foresight.forecast import assess, daily_extremes, fit_line, urgency_for_runway

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
CFG = load_config()


def rule(rtype, metric):
    return next(r for r in CFG.rules if r.resource_type == rtype and r.metric_name == metric)


def hourly(fn, days=14):
    return [(NOW - timedelta(hours=days * 24 - i), fn(i)) for i in range(days * 24 + 1)]


def test_daily_extremes_uses_max_for_above_and_min_for_below():
    pts = [(NOW.replace(hour=h), float(h)) for h in range(0, 24)]
    assert daily_extremes(pts, "ABOVE")[0][1] == 23.0
    assert daily_extremes(pts, "BELOW")[0][1] == 0.0


def test_fit_line_recovers_slope():
    pts = [(NOW + timedelta(days=d), 10 + 2 * d) for d in range(10)]
    fit = fit_line(pts)
    assert fit.slope_per_hour * 24 == pytest.approx(2.0)
    assert fit.r2 == pytest.approx(1.0)


def test_fit_line_degenerate_returns_none():
    assert fit_line([(NOW, 1.0)]) is None


def test_rising_cpu_predicts_breach_with_runway():
    # Daily peak rises 1.6 points/day from 60; ends near 80.8, threshold 85.
    a = assess(rule("EC2", "CPUUtilization"), CFG, demo_hourly(rule("EC2", "CPUUtilization"), "demo-ec2-web", 14, NOW), NOW)
    assert a.status == "BREACH_PREDICTED"
    assert 24 < a.runway_hours < 72
    assert a.urgency in ("MEDIUM", "HIGH")


def test_shrinking_free_storage_predicts_breach():
    r = rule("RDS", "FreeStorageSpace")
    a = assess(r, CFG, demo_hourly(r, "demo-rds-orders", 14, NOW), NOW)
    assert a.status == "BREACH_PREDICTED"
    assert a.runway_hours < 72
    assert a.urgency in ("HIGH", "CRITICAL")


def test_flat_series_is_ok():
    r = rule("EC2", "CPUUtilization")
    a = assess(r, CFG, hourly(lambda i: 30.0), NOW)
    assert a.status == "OK"
    assert not a.is_finding


def test_wrong_direction_trend_is_ok():
    # CPU falling: never a breach for an ABOVE rule.
    r = rule("EC2", "CPUUtilization")
    a = assess(r, CFG, hourly(lambda i: 70 - i * 0.02), NOW)
    assert a.status == "OK"


def test_already_breached_is_critical_zero_runway():
    r = rule("EC2", "CPUUtilization")
    a = assess(r, CFG, hourly(lambda i: 90.0), NOW)
    assert a.status == "BREACHED" and a.runway_hours == 0 and a.urgency == "CRITICAL"


def test_insufficient_data():
    r = rule("EC2", "CPUUtilization")
    short = [(NOW - timedelta(hours=h), 50.0) for h in range(48)]
    assert assess(r, CFG, short, NOW).status == "INSUFFICIENT_DATA"


def test_noisy_trend_is_not_a_finding():
    r = rule("EC2", "CPUUtilization")
    # Calendar days alternate between 40 and 80 with a tiny upward drift: poor linear fit.
    series = [
        (NOW - timedelta(hours=h), (80.0 if (NOW - timedelta(hours=h)).toordinal() % 2 else 40.0) - h * 0.001)
        for h in range(14 * 24, -1, -1)
    ]
    a = assess(r, CFG, series, NOW)
    assert a.r2 < CFG.defaults.min_r2
    assert not a.is_finding


def test_urgency_bands():
    bands = CFG.defaults.urgency_bands_hours
    assert urgency_for_runway(10, bands) == "CRITICAL"
    assert urgency_for_runway(30, bands) == "HIGH"
    assert urgency_for_runway(80, bands) == "MEDIUM"
    assert urgency_for_runway(500, bands) == "LOW"


def test_config_rejects_bad_direction():
    with pytest.raises(ConfigError):
        parse_config({"metrics": [{"resource_type": "EC2", "metric_name": "x", "threshold": 1,
                                   "direction": "SIDEWAYS", "horizon_hours": 24}]})


def test_config_rejects_duplicates_and_empty():
    m = {"resource_type": "EC2", "metric_name": "x", "threshold": 1, "direction": "ABOVE", "horizon_hours": 24}
    with pytest.raises(ConfigError):
        parse_config({"metrics": [m, m]})
    with pytest.raises(ConfigError):
        parse_config({"metrics": []})


def test_predicted_with_zero_runway_not_worded_as_breached():
    from capacity_foresight.notifier import runway_phrase
    assert "within the hour" in runway_phrase({"status": "BREACH_PREDICTED", "runway_hours": 0.0})
    assert "crossed" in runway_phrase({"status": "BREACHED", "runway_hours": 0.0})


def test_projections_are_clamped_to_physical_bounds():
    from capacity_foresight.forecast import clamp
    r = rule("RDS", "FreeStorageSpace")
    a = assess(r, CFG, demo_hourly(r, "demo-rds-orders", 28, NOW), NOW)
    assert a.projected_value_at_horizon >= 0
    assert all(v >= 0 for _, v in a.forecast)
    cpu = rule("EC2", "CPUUtilization")
    assert clamp(150.0, cpu) == 100.0 and clamp(-5.0, cpu) == 0.0


# ----------------------------------------------------------------------------- v2 forecast features


def daily_series(fn, days=28, hour=12):
    """One datapoint per day at `hour`, value fn(days_ago)."""
    return [(NOW.replace(hour=hour) - timedelta(days=d), fn(d)) for d in range(days, -1, -1)]


def test_runway_interval_brackets_central_estimate():
    r = rule("EC2", "CPUUtilization")
    # Noisy rising trend so the slope has a real standard error.
    pts = daily_series(lambda d: 78 - 2.5 * d + (3 if d % 3 == 0 else -2))
    a = assess(r, CFG, pts, NOW)
    assert a.status == "BREACH_PREDICTED"
    assert a.runway_earliest_hours <= a.runway_hours
    assert a.runway_latest_hours is None or a.runway_latest_hours >= a.runway_hours


def test_urgency_floor_uses_earliest_runway():
    r = rule("EC2", "CPUUtilization")
    pts = daily_series(lambda d: 78 - 3.0 * d + ((4 if d % 2 == 0 else -4) if d else 0))
    a = assess(r, CFG, pts, NOW)
    from capacity_foresight.forecast import urgency_for_runway
    from capacity_foresight.config import URGENCY_ORDER
    bands = CFG.defaults.urgency_bands_hours
    rank = URGENCY_ORDER.index
    # Urgency comes from the growth-rate earliest: at least what the central runway implies, never more than the widest range implies.
    assert rank(urgency_for_runway(a.runway_hours, bands)) <= rank(a.urgency) <= rank(urgency_for_runway(a.runway_earliest_hours, bands))


def test_weekly_seasonality_is_detected_and_flat_weekly_pattern_is_not_a_breach():
    r = rule("EC2", "CPUUtilization")
    # No trend. Weekdays peak at 80, weekends at 50. Never reaches 85.
    pts = daily_series(lambda d: 50.0 if (NOW - timedelta(days=d)).weekday() >= 5 else 80.0)
    a = assess(r, CFG, pts, NOW)
    assert a.seasonal is True
    assert a.status == "OK"


def test_seasonality_advances_breach_to_the_worst_weekday():
    r = rule("EC2", "CPUUtilization")
    # Rising 2.5/day with weekdays 8 above and weekends 8 below the trend.
    weekly = lambda d: (74 - 2.5 * d) + (8 if (NOW - timedelta(days=d)).weekday() < 5 else -8)
    with_season = assess(r, CFG, daily_series(weekly), NOW)
    no_season_cfg = parse_config({**json_cfg(), "defaults": {**json_cfg()["defaults"], "weekly_seasonality": False}})
    without = assess(r, no_season_cfg, daily_series(weekly), NOW)
    assert with_season.seasonal and not without.seasonal
    assert with_season.status == without.status == "BREACH_PREDICTED"
    # The breach lands on a busy weekday, so the seasonal model must warn earlier.
    assert with_season.runway_hours < without.runway_hours


def test_steep_ramp_with_weekend_dips_is_not_trimmed_as_a_level_shift():
    r = rule("EC2", "CPUUtilization")
    weekly = lambda d: (74 - 2.5 * d) + (8 if (NOW - timedelta(days=d)).weekday() < 5 else -8)
    assert assess(r, CFG, daily_series(weekly), NOW).level_shift is False


def json_cfg():
    import json
    from capacity_foresight.config import DEFAULT_CONFIG_PATH
    return json.loads(DEFAULT_CONFIG_PATH.read_text())


def test_level_shift_is_trimmed_so_old_trend_does_not_pollute():
    from capacity_foresight.forecast import daily_extremes, trim_after_level_shift
    r = rule("RDS", "FreeStorageSpace")
    # Storage shrinking, then a resize at day 14 jumps free space up by 100 GiB, then shrinking again.
    gib = 1024**3
    def fn(d):
        base = (150 - 2.0 * (28 - d)) * gib if d > 14 else (250 - 2.0 * (14 - d)) * gib
        return base
    pts = daily_series(fn)
    daily = daily_extremes(pts, "BELOW")
    trimmed, shifted = trim_after_level_shift(daily)
    assert shifted and len(trimmed) < len(daily)
    a = assess(r, CFG, pts, NOW)
    assert a.level_shift is True


def test_weekend_dip_is_not_a_level_shift():
    from capacity_foresight.forecast import daily_extremes, trim_after_level_shift
    pts = daily_series(lambda d: 50.0 if (NOW - timedelta(days=d)).weekday() >= 5 else 80.0)
    _, shifted = trim_after_level_shift(daily_extremes(pts, "ABOVE"))
    assert shifted is False


def test_steady_ramp_is_not_a_level_shift():
    from capacity_foresight.forecast import daily_extremes, trim_after_level_shift
    pts = daily_series(lambda d: 20 + 2 * (28 - d))
    _, shifted = trim_after_level_shift(daily_extremes(pts, "ABOVE"))
    assert shifted is False


def test_recent_acceleration_is_flagged():
    r = rule("EC2", "CPUUtilization")
    # Slow for three weeks, steep for the last week.
    pts = daily_series(lambda d: 40 + 0.3 * (28 - d) if d > 7 else 40 + 0.3 * 21 + 3.0 * (7 - d))
    a = assess(r, CFG, pts, NOW)
    assert a.accelerating is True
    assert a.recent_slope_per_day > a.slope_per_day


def test_already_breached_is_reported_even_with_little_history():
    r = rule("EC2", "CPUUtilization")
    pts = [(NOW - timedelta(hours=h), 95.0) for h in range(24, -1, -1)]
    a = assess(r, CFG, pts, NOW)
    assert a.status == "BREACHED" and a.urgency == "CRITICAL"


def test_config_defaults_for_new_options():
    assert CFG.defaults.weekly_seasonality is True
    assert CFG.defaults.detect_level_shift is True
    assert CFG.defaults.lookback_days == 28


# ----------------------------------------------------------------------------- incomplete days (found by live testing)


def test_the_current_partial_day_is_not_used_in_the_fit():
    from capacity_foresight.forecast import daily_extremes
    pts = [(NOW - timedelta(hours=h), 50.0 + (h % 24)) for h in range(24 * 10, -1, -1)]
    with_now = daily_extremes(pts, "ABOVE", NOW)
    without = daily_extremes(pts, "ABOVE")
    assert len(with_now) < len(without)
    assert NOW.replace(hour=0, minute=0, second=0, microsecond=0) not in dict(with_now)


def test_a_first_day_cut_short_by_the_window_is_dropped_but_a_full_one_is_kept():
    from capacity_foresight.forecast import daily_extremes
    start_partial = NOW.replace(hour=20) - timedelta(days=10)  # window starts at 20:00, only 4 hours that day
    partial = [(start_partial + timedelta(hours=h), 40.0) for h in range(0, 24 * 10)]
    days = [d for d, _ in daily_extremes(partial, "ABOVE", NOW)]
    assert start_partial.replace(hour=0, minute=0) not in days
    start_full = NOW.replace(hour=0) - timedelta(days=10)
    full = [(start_full + timedelta(hours=h), 40.0) for h in range(0, 24 * 10 + 12)]
    assert start_full in [d for d, _ in daily_extremes(full, "ABOVE", NOW)]


def test_runway_does_not_depend_on_the_time_of_day_beyond_the_expected_countdown():
    cpu = rule("EC2", "CPUUtilization")
    base = datetime(2026, 11, 5, tzinfo=timezone.utc)
    runways = []
    for hour in (1, 7, 13, 19):
        now = base.replace(hour=hour, minute=30)
        a = assess(cpu, CFG, demo_hourly(cpu, "demo-ec2-web", 28, now), now)
        assert a.status == "BREACH_PREDICTED"
        # the "clock" time at which the breach is predicted should barely move as the hour changes
        runways.append((now + timedelta(hours=a.runway_hours)).timestamp() / 3600)
    assert max(runways) - min(runways) < 3


def test_demo_resources_never_flip_to_ok_at_any_hour():
    for rid, rt, metric in (("demo-ec2-web", "EC2", "CPUUtilization"), ("demo-rds-orders", "RDS", "FreeStorageSpace")):
        r = rule(rt, metric)
        for day in range(7):
            for hour in range(0, 24, 3):
                now = datetime(2026, 11, 2 + day, hour, 30, tzinfo=timezone.utc)
                assert assess(r, CFG, demo_hourly(r, rid, 28, now), now).is_finding, (rid, now)


def test_weekly_pattern_is_removed_with_thirteen_days_of_history():
    import math
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 11, 6, 17, tzinfo=timezone.utc)
    start = (now - timedelta(days=12)).replace(hour=0)
    pts, t = [], start
    while t <= now:
        d = (t - start).total_seconds() / 86400
        pts.append((t, 55 + 0.9 * d + (-6 if t.weekday() >= 5 else 0) + 4 * math.sin(2 * math.pi * (t.hour - 6) / 24)))
        t += timedelta(hours=1)
    rule = next(r for r in CFG.rules_for("EC2") if r.metric_name == "CPUUtilization")
    a = assess(rule, CFG, pts, now)
    assert a.seasonal is True


def test_nine_daily_points_are_not_enough_to_remove_the_weekly_pattern():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 11, 6, 17, tzinfo=timezone.utc)
    start = (now - timedelta(days=9)).replace(hour=0)
    pts, t = [], start
    while t <= now:
        pts.append((t, 40 + 0.5 * (t - start).total_seconds() / 86400))
        t += timedelta(hours=1)
    rule = next(r for r in CFG.rules_for("EC2") if r.metric_name == "CPUUtilization")
    assert assess(rule, CFG, pts, now).seasonal is False


def test_range_allows_for_daily_peaks_arriving_once_a_day():
    r = rule("EC2", "CPUUtilization")
    pts = daily_series(lambda d: 78 - 2.5 * d + (1.0 if d % 2 else -1.0))
    a = assess(r, CFG, pts, NOW)
    assert a.status == "BREACH_PREDICTED"
    assert a.runway_latest_hours is not None and a.runway_latest_hours >= a.runway_hours + 24
    assert a.runway_earliest_hours <= a.runway_hours
