#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Replay the forecast on saved or live metric data, without sending any alert.

Use it to tune thresholds, horizons and the fit settings before you enable alerting.

From a file (CSV with `timestamp,value` columns, or a JSON list of {"timestamp","value"}):
    python scripts/replay.py --file cpu.csv --resource-type EC2 --metric CPUUtilization

Live from CloudWatch (read-only, uses your current AWS credentials and Region):
    python scripts/replay.py --live --resource-id i-0123456789abcdef0 --resource-type EC2 --metric CPUUtilization

What-if overrides: --threshold 70 --horizon-hours 120 --min-r2 0.5 --no-seasonality --no-level-shift --lookback-days 56 (live only)
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from capacity_foresight.config import ThresholdConfig, load_config  # noqa: E402
from capacity_foresight.forecast import assess  # noqa: E402


def parse_timestamp(raw: str) -> datetime:
    raw = str(raw).strip()
    try:
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)  # epoch seconds
    except ValueError:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_points(path: Path) -> list[tuple[datetime, float]]:
    if path.suffix.lower() == ".json":
        rows = json.loads(path.read_text(encoding="utf-8"))
    else:
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    return sorted((parse_timestamp(r["timestamp"]), float(r["value"])) for r in rows)


def apply_overrides(config: ThresholdConfig, rule, args):
    d = config.defaults
    d = dataclasses.replace(
        d,
        min_r2=args.min_r2 if args.min_r2 is not None else d.min_r2,
        weekly_seasonality=d.weekly_seasonality and not args.no_seasonality,
        detect_level_shift=d.detect_level_shift and not args.no_level_shift,
        lookback_days=args.lookback_days or d.lookback_days,
    )
    rule = dataclasses.replace(
        rule,
        threshold=args.threshold if args.threshold is not None else rule.threshold,
        horizon_hours=args.horizon_hours or rule.horizon_hours,
    )
    return dataclasses.replace(config, defaults=d), rule


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", type=Path, help="CSV or JSON of timestamp,value")
    src.add_argument("--live", action="store_true", help="read from CloudWatch")
    p.add_argument("--resource-id", help="required with --live")
    p.add_argument("--resource-type", required=True, choices=["EC2", "RDS", "EBS"])
    p.add_argument("--metric", required=True)
    p.add_argument("--config", type=Path, help="alternative thresholds.json")
    p.add_argument("--threshold", type=float)
    p.add_argument("--horizon-hours", type=int)
    p.add_argument("--min-r2", type=float)
    p.add_argument("--lookback-days", type=int)
    p.add_argument("--no-seasonality", action="store_true")
    p.add_argument("--no-level-shift", action="store_true")
    args = p.parse_args(argv)

    config = load_config(args.config)
    matches = [r for r in config.rules if r.resource_type == args.resource_type and r.metric_name == args.metric]
    if not matches:
        print(f"No rule for {args.resource_type}/{args.metric} in the configuration.", file=sys.stderr)
        return 2
    config, rule = apply_overrides(config, matches[0], args)

    if args.live:
        if not args.resource_id:
            p.error("--resource-id is required with --live")
        import boto3  # imported here so file replay works without AWS access

        from capacity_foresight.metrics import fetch_hourly

        points = fetch_hourly(boto3.client("cloudwatch"), rule, args.resource_id, config.defaults.lookback_days)
    else:
        points = load_points(args.file)
    if not points:
        print("No datapoints.", file=sys.stderr)
        return 2

    result = assess(rule, config, points, max(t for t, _ in points))
    print(f"Rule        : {rule.resource_type} {rule.metric_name} {rule.direction} {rule.threshold:g} {rule.unit}, horizon {rule.horizon_hours}h")
    print(f"Datapoints  : {len(points)} from {points[0][0]:%Y-%m-%d} to {points[-1][0]:%Y-%m-%d}")
    print(f"Status      : {result.status}")
    print(f"Current     : {result.current_value}")
    print(f"Projected   : {result.projected_value_at_horizon}")
    print(f"Runway (h)  : {result.runway_hours}  (earliest {result.runway_earliest_hours}, latest {result.runway_latest_hours})")
    print(f"Urgency     : {result.urgency}")
    print(f"Trend       : {result.slope_per_day} per day, r2={result.r2}, {result.n_points} daily points")
    print(f"Flags       : seasonal={result.seasonal} level_shift={result.level_shift} accelerating={result.accelerating}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
