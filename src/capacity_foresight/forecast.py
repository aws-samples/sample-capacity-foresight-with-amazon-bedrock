# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Trend forecast and breach detection.

The forecast is deliberately simple and explainable. The model (Amazon Bedrock) is not used for the
numbers. It is used for what to do about them.

Pipeline for one metric series:
  1. Reduce hourly data to daily extremes (max for ABOVE metrics, min for BELOW metrics). Averages hide
     the crest: a service that idles at 20% and peaks at 80% every afternoon never looks close to 85%.
  2. Drop data before the last persistent level shift (a resize, failover or migration resets the trend).
  3. Remove weekly seasonality (per-weekday mean residual) when there are at least 10 daily points and every weekday has a sample.
  4. Fit an ordinary least squares line, with a standard error on the slope.
  5. Project over the metric's horizon. Seasonality is added back at its worst weekday, because the
     breach that matters is the one on the busiest (or emptiest) day.
  6. Report the runway (hours to breach) with an earliest/latest range that allows for the growth-rate
     uncertainty and for how far a single day's peak scatters around the trend. Urgency uses only the
     growth-rate earliest, so a noisy series does not inflate alerts.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from .config import MetricRule, ThresholdConfig, URGENCY_ORDER

Point = tuple[datetime, float]

MIN_POINTS_FOR_SEASONALITY = 10  # fewer than 14 complete days still gives every weekday at least one sample
MIN_HOURS_FIRST_DAY = 18  # the oldest day is dropped if the lookback window cut it short
MIN_SAMPLES_PER_WEEKDAY = 1
ACCELERATION_RATIO = 2.0  # recent slope at least this many times the long-run slope


@dataclass(frozen=True)
class Fit:
    slope_per_hour: float
    intercept: float  # value at x=0 where x = hours since `origin`
    r2: float
    n: int
    origin: datetime
    slope_se_per_hour: float = 0.0
    resid_sd: float = 0.0  # scatter of daily values around the line, in metric units


@dataclass(frozen=True)
class Assessment:
    """Outcome of evaluating one (resource, metric)."""

    status: str  # OK | BREACH_PREDICTED | BREACHED | INSUFFICIENT_DATA | LOW_CONFIDENCE
    runway_hours: float | None
    current_value: float | None
    projected_value_at_horizon: float | None
    slope_per_day: float | None
    r2: float | None
    n_points: int
    urgency: str | None
    observed: tuple[Point, ...] = ()
    forecast: tuple[Point, ...] = ()
    runway_earliest_hours: float | None = None
    runway_latest_hours: float | None = None  # None means "may not breach within the data's confidence"
    recent_slope_per_day: float | None = None
    accelerating: bool = False
    level_shift: bool = False
    seasonal: bool = False
    last_daily_value: float | None = None  # extreme of the most recent complete day (the crest, not the latest reading)

    @property
    def is_finding(self) -> bool:
        return self.status in ("BREACH_PREDICTED", "BREACHED")


# ----------------------------------------------------------------------------- building blocks


def daily_extremes(points: Iterable[Point], direction: str, now: datetime | None = None) -> list[Point]:
    """Collapse points into one value per UTC day: max for ABOVE, min for BELOW.

    With `now`, incomplete days are left out, because a partial day understates the crest (or overstates
    the headroom) and would drag the trend: the current day, and the first day if the lookback window
    cut it short. The latest reading is still checked on its own for an already-crossed threshold.
    """
    pick = max if direction == "ABOVE" else min
    buckets: dict[datetime, float] = {}
    hours: dict[datetime, set[int]] = {}
    for ts, value in points:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue
        utc = ts.astimezone(timezone.utc)
        day = utc.replace(hour=0, minute=0, second=0, microsecond=0)
        buckets[day] = pick(buckets[day], value) if day in buckets else value
        hours.setdefault(day, set()).add(utc.hour)
    if now is not None and buckets:
        today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        buckets.pop(today, None)
        first = min(buckets, default=None)
        if first is not None and len(hours.get(first, ())) < MIN_HOURS_FIRST_DAY:
            buckets.pop(first)
    return sorted(buckets.items())


def crest_offset_hours(points: Iterable[Point], daily: Sequence[Point], direction: str) -> float:
    """Average hour of day at which the daily extreme occurs, for the days in `daily`.

    Daily values are stamped at midnight, but a CPU crest lands in the afternoon. Without this shift a
    trend that reaches the threshold looks half a day earlier than the crest that would actually cross it.
    """
    pick = max if direction == "ABOVE" else min
    wanted = {ts for ts, _ in daily}
    by_day: dict[datetime, list[tuple[float, int]]] = {}
    for ts, v in points:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        utc = ts.astimezone(timezone.utc)
        day = utc.replace(hour=0, minute=0, second=0, microsecond=0)
        if day in wanted:
            by_day.setdefault(day, []).append((v, utc.hour))
    hours = [pick(vals)[1] for vals in by_day.values()]
    return sum(hours) / len(hours) if hours else 0.0


def fit_line(points: Sequence[Point]) -> Fit | None:
    """Ordinary least squares over (hours since first point, value). None if degenerate."""
    n = len(points)
    if n < 2:
        return None
    origin = points[0][0]
    xs = [(ts - origin).total_seconds() / 3600.0 for ts, _ in points]
    ys = [v for _, v in points]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 if ss_tot == 0 else max(0.0, 1.0 - ss_res / ss_tot)
    se = math.sqrt(max(ss_res, 0.0) / (n - 2) / sxx) if n > 2 else 0.0
    resid_sd = math.sqrt(max(ss_res, 0.0) / (n - 2)) if n > 2 else 0.0
    return Fit(slope, intercept, r2, n, origin, se, resid_sd)


def value_at(fit: Fit, when: datetime) -> float:
    return fit.intercept + fit.slope_per_hour * ((when - fit.origin).total_seconds() / 3600.0)


def clamp(value: float, rule: MetricRule) -> float:
    """Keep displayed projections physically possible (no negative bytes, no CPU above 100%)."""
    if rule.unit == "Percent":
        return min(100.0, max(0.0, value))
    if rule.unit in ("Bytes", "Count"):
        return max(0.0, value)
    return value


def crossed(value: float, rule: MetricRule) -> bool:
    return value >= rule.threshold if rule.direction == "ABOVE" else value <= rule.threshold


def urgency_for_runway(runway_hours: float, bands: dict[str, int]) -> str:
    """Minimum urgency implied by the runway. The AI may raise this, never lower it."""
    for name in ("CRITICAL", "HIGH", "MEDIUM"):
        if name in bands and runway_hours <= bands[name]:
            return name
    return "LOW"


def max_urgency(a: str, b: str) -> str:
    return a if URGENCY_ORDER.index(a) >= URGENCY_ORDER.index(b) else b


def trim_after_level_shift(daily: Sequence[Point]) -> tuple[list[Point], bool]:
    """Drop data before the last persistent level shift.

    A shift is a day-over-day jump much larger than the series' normal movement that also persists:
    the mean of the following days stays near the new level. The persistence test is what separates a
    resize or failover from a weekend dip, which jumps and then returns.
    """
    n = len(daily)
    if n < 9:
        return list(daily), False
    values = [v for _, v in daily]
    diffs = [values[i] - values[i - 1] for i in range(1, n)]  # diffs[i-1] is the jump into day i
    med = statistics.median(diffs)
    mad = statistics.median(abs(d - med) for d in diffs) * 1.4826
    scale = max(mad, 0.02 * max(abs(v) for v in values), 1e-9)
    # Compare window means on the detrended series, so a steady ramp is not mistaken for a shift.
    detrended = [v - med * j for j, v in enumerate(values)]
    cut = None
    for i in range(4, n - 3):  # need 4+ days before and 4+ days after
        jump = diffs[i - 1] - med
        if abs(jump) <= 6 * scale:
            continue
        window_before = detrended[max(0, i - 5):i]
        window_after = detrended[i:i + 5]
        before = sum(window_before) / len(window_before)
        after = sum(window_after) / len(window_after)
        if abs(after - before) >= 0.75 * abs(jump):
            cut = i
    if cut is None:
        return list(daily), False
    return list(daily[cut:]), True


def weekday_effects(daily: Sequence[Point], fit: Fit) -> dict[int, float]:
    """Mean residual per weekday (Monday=0), centred on zero. Empty if any weekday lacks samples."""
    residuals: dict[int, list[float]] = {d: [] for d in range(7)}
    for ts, v in daily:
        residuals[ts.weekday()].append(v - value_at(fit, ts))
    if any(len(r) < MIN_SAMPLES_PER_WEEKDAY for r in residuals.values()):
        return {}
    means = {d: sum(r) / len(r) for d, r in residuals.items()}
    centre = sum(means.values()) / 7
    return {d: m - centre for d, m in means.items()}


def _runway(delta: float, slope: float, direction: str) -> float | None:
    """Hours until `delta` (threshold minus current trend value) is covered at `slope`. None if never."""
    already_past = delta <= 0 if direction == "ABOVE" else delta >= 0
    if already_past:
        return 0.0
    toward = slope > 0 if direction == "ABOVE" else slope < 0
    if not toward:
        return None
    return delta / slope


# ----------------------------------------------------------------------------- evaluation


def assess(
    rule: MetricRule,
    config: ThresholdConfig,
    hourly: Sequence[Point],
    now: datetime | None = None,
) -> Assessment:
    """Evaluate one metric series against its rule."""
    now = now or datetime.now(timezone.utc)
    d = config.defaults
    daily = daily_extremes(hourly, rule.direction, now)
    latest = max(hourly, key=lambda p: p[0])[1]  # latest raw observation is the "current" value

    if crossed(latest, rule):
        return Assessment("BREACHED", 0.0, latest, None, None, None, len(daily), "CRITICAL", tuple(daily))

    offset = timedelta(hours=crest_offset_hours(hourly, daily, rule.direction))
    daily = [(ts + offset, v) for ts, v in daily]
    level_shift = False
    if d.detect_level_shift:
        daily, level_shift = trim_after_level_shift(daily)
    if len(daily) < d.min_points:
        return Assessment(
            "INSUFFICIENT_DATA", None, latest, None, None, None, len(daily), None, tuple(daily),
            level_shift=level_shift,
        )

    series: list[Point] = list(daily)
    seasonal = False
    shift = 0.0
    fit = fit_line(series)
    if fit is not None and d.weekly_seasonality and len(daily) >= MIN_POINTS_FOR_SEASONALITY:
        effects = weekday_effects(daily, fit)
        if effects:
            series = [(ts, v - effects[ts.weekday()]) for ts, v in daily]
            fit = fit_line(series)
            seasonal = True
            # The breach that matters happens on the worst weekday.
            shift = max(effects.values()) if rule.direction == "ABOVE" else min(effects.values())
    if fit is None:
        return Assessment(
            "INSUFFICIENT_DATA", None, latest, None, None, None, len(daily), None, tuple(daily),
            level_shift=level_shift,
        )

    horizon_end = now + timedelta(hours=rule.horizon_hours)
    projected_end = clamp(value_at(fit, horizon_end) + shift, rule)
    slope_per_day = fit.slope_per_hour * 24
    step = max(1, rule.horizon_hours // 24)
    forecast = tuple(
        (now + timedelta(hours=h), clamp(value_at(fit, now + timedelta(hours=h)) + shift, rule))
        for h in range(0, rule.horizon_hours + 1, step)
    )

    recent = fit_line(series[-7:]) if len(series) >= 7 else None
    recent_per_day = recent.slope_per_hour * 24 if recent else None
    toward_now = fit.slope_per_hour > 0 if rule.direction == "ABOVE" else fit.slope_per_hour < 0
    accelerating = bool(
        recent and toward_now and recent.slope_per_hour / fit.slope_per_hour >= ACCELERATION_RATIO
    )
    common = dict(
        recent_slope_per_day=recent_per_day, accelerating=accelerating,
        level_shift=level_shift, seasonal=seasonal, last_daily_value=daily[-1][1],
    )

    breach_in_window = crossed(value_at(fit, horizon_end) + shift, rule)
    if not (toward_now and breach_in_window):
        return Assessment(
            "OK", None, latest, projected_end, slope_per_day, fit.r2, fit.n, None,
            tuple(daily), forecast, **common,
        )
    if fit.r2 < d.min_r2:
        return Assessment(
            "LOW_CONFIDENCE", None, latest, projected_end, slope_per_day, fit.r2, fit.n, None,
            tuple(daily), forecast, **common,
        )

    # Runway: hours until the trend plus the worst weekday's effect reaches the threshold.
    delta = rule.threshold - (value_at(fit, now) + shift)
    central = _runway(delta, fit.slope_per_hour, rule.direction) or 0.0
    sign = 1.0 if rule.direction == "ABOVE" else -1.0
    # Two sources of uncertainty: the growth rate, and how far a single day's peak scatters around the trend.
    # Fitting seven weekday effects uses up degrees of freedom, so widen both when the weekly pattern was removed.
    inflate = math.sqrt((fit.n - 2) / max(fit.n - 8, 1)) if seasonal else 1.0
    spread = d.interval_z * fit.slope_se_per_hour * inflate
    scatter = d.interval_z * fit.resid_sd * inflate
    earliest_by_growth = _runway(delta, fit.slope_per_hour + sign * spread, rule.direction)
    earliest_by_growth = central if earliest_by_growth is None else min(earliest_by_growth, central)
    earliest = _runway(delta - sign * scatter, fit.slope_per_hour + sign * spread, rule.direction)
    latest_runway = _runway(delta + sign * scatter, fit.slope_per_hour - sign * spread, rule.direction)
    earliest = central if earliest is None else min(earliest, central)

    return Assessment(
        "BREACH_PREDICTED", round(central, 1), latest, projected_end, slope_per_day, fit.r2, fit.n,
        urgency_for_runway(earliest_by_growth, d.urgency_bands_hours), tuple(daily), forecast,
        runway_earliest_hours=round(earliest, 1),
        # Peaks arrive once a day, so the first real breach can trail the trend crossing by up to a day.
        runway_latest_hours=None if latest_runway is None else round(latest_runway + 24.0, 1),
        **common,
    )
