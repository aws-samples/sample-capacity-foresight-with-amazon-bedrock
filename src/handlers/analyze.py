# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Step 2: forecast every configured metric of one resource and report predicted breaches."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from capacity_foresight import aws, demo as demo_mod, notifier
from capacity_foresight.config import Settings, load_config
from capacity_foresight.forecast import assess
from capacity_foresight.metrics import fetch_hourly
from capacity_foresight.store import object_key, write_jsonl

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

_CONFIG = load_config()  # parsed once per execution environment


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    settings = Settings.from_env()
    resource = event["resource"]
    run_id = event["run_id"]
    demo = bool(event.get("demo"))
    now = datetime.now(timezone.utc)
    rtype, rid = resource["resource_type"], resource["resource_id"]

    findings: list[dict[str, Any]] = []
    series_rows: list[dict[str, Any]] = []
    errors = 0

    for rule in _CONFIG.rules_for(rtype):
        try:
            if demo:
                hourly = demo_mod.demo_hourly(rule, rid, _CONFIG.defaults.lookback_days, now)
            else:
                hourly = fetch_hourly(aws.client("cloudwatch"), rule, rid, _CONFIG.defaults.lookback_days, now)
            if not hourly:
                continue
            result = assess(rule, _CONFIG, hourly, now)
        except Exception:  # noqa: BLE001 - one bad metric must not hide the others
            LOG.exception("metric %s/%s failed for %s", rtype, rule.metric_name, rid)
            errors += 1
            continue

        base = {
            "run_id": run_id, "account_id": settings.account_id, "region": settings.region,
            "resource_type": rtype, "resource_id": rid, "metric_name": rule.metric_name,
            "threshold": rule.threshold,
        }
        series_rows += [
            {**base, "ts": ts.isoformat(), "kind": "observed", "value": v} for ts, v in result.observed
        ]
        series_rows += [
            {**base, "ts": ts.isoformat(), "kind": "forecast", "value": v} for ts, v in result.forecast
        ]
        LOG.info("%s %s %s -> %s", rtype, rid, rule.metric_name, result.status)
        if result.status == "OK" and not demo and settings.table_name:
            try:
                if notifier.resolve(aws.client("dynamodb"), settings.table_name, base):
                    LOG.info("%s %s %s is healthy again; earlier alert cleared", rtype, rid, rule.metric_name)
            except Exception:  # noqa: BLE001 - failing to clear an old alert must not fail the analysis
                LOG.exception("could not clear earlier alert for %s/%s", rid, rule.metric_name)

        if result.is_finding:
            findings.append(
                {
                    "run_id": run_id,
                    "finding_ts": now.isoformat(),
                    "account_id": settings.account_id,
                    "region": settings.region,
                    "resource_type": rtype,
                    "resource_id": rid,
                    "resource_arn": resource.get("arn"),
                    "metric_name": rule.metric_name,
                    "unit": rule.unit,
                    "threshold": rule.threshold,
                    "direction": rule.direction,
                    "horizon_hours": rule.horizon_hours,
                    "status": result.status,
                    "current_value": result.current_value,
                    "last_daily_value": result.last_daily_value,
                    "projected_value_at_horizon": result.projected_value_at_horizon,
                    "runway_hours": result.runway_hours,
                    "runway_earliest_hours": result.runway_earliest_hours,
                    "runway_latest_hours": result.runway_latest_hours,
                    "slope_per_day": result.slope_per_day,
                    "recent_slope_per_day": result.recent_slope_per_day,
                    "accelerating": result.accelerating,
                    "level_shift": result.level_shift,
                    "seasonal": result.seasonal,
                    "r2": result.r2,
                    "n_points": result.n_points,
                    "urgency": result.urgency,
                }
            )

    if series_rows:
        write_jsonl(aws.client("s3"), settings.bucket, object_key("series", run_id, rid, now), series_rows)
    return {"findings": findings, "metric_errors": errors}
