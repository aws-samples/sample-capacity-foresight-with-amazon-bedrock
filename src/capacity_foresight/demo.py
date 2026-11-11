# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synthetic resources so you can try the whole pipeline without waiting for a real breach.

Start an execution with input {"demo": true}. No real resources are read or modified.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from .config import MetricRule

GIB = 1024**3
DEMO_PREFIX = "demo-"

# resource_id -> (resource_type, facts)
_DEMO = {
    "demo-rds-orders": (
        "RDS",
        {
            "db_instance_class": "db.r6g.large", "engine": "postgres", "engine_version": "16.4",
            "status": "available", "allocated_storage_gib": 200, "max_allocated_storage_gib": None,
            "storage_type": "gp3", "iops": 3000, "multi_az": True, "is_aurora_member": False,
            "read_replica_count": 0, "performance_insights_enabled": True,
            "tags": {"Name": "orders-db", "Application": "orders"}, "route": "orders-team",
        },
    ),
    "demo-ec2-web": (
        "EC2",
        {
            "instance_type": "m5.large", "state": "running", "availability_zone": "us-east-1a",
            "architecture": "x86_64", "ebs_optimized": True,
            "auto_scaling_group": {"min": 2, "max": 2, "desired": 2},
            "tags": {"Name": "web-frontend", "Application": "storefront"}, "route": "storefront-team",
        },
    ),
    "demo-rds-reporting": (
        "RDS",
        {
            "db_instance_class": "db.m6g.large", "engine": "mysql", "engine_version": "8.0.39",
            "status": "available", "allocated_storage_gib": 100, "max_allocated_storage_gib": 500,
            "storage_type": "gp3", "iops": 3000, "multi_az": False, "is_aurora_member": False,
            "read_replica_count": 0, "performance_insights_enabled": False,
            "tags": {"Name": "reporting-db", "Application": "analytics"}, "route": "analytics-team",
        },
    ),
}


def is_demo(resource_id: str) -> bool:
    return resource_id.startswith(DEMO_PREFIX)


def demo_resources() -> list[dict[str, str]]:
    return [
        {"resource_type": rtype, "resource_id": rid, "arn": f"demo::{rid}"}
        for rid, (rtype, _) in sorted(_DEMO.items())
    ]


def demo_facts(resource_id: str) -> dict:
    return _DEMO[resource_id][1]


def demo_hourly(rule: MetricRule, resource_id: str, lookback_days: int, now: datetime | None = None):
    """Deterministic hourly series, defined relative to `now`. Two demo resources trend toward a breach."""
    now = (now or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    hours = lookback_days * 24
    out = []
    for i in range(hours + 1):
        ts = now - timedelta(hours=hours - i)
        days_ago = (hours - i) / 24.0
        if resource_id == "demo-rds-orders" and rule.metric_name == "FreeStorageSpace":
            # Free space shrinking 3.4 GiB per day: about a day from the 10 GiB threshold.
            value = (16.0 + 3.4 * days_ago) * GIB + 0.3 * GIB * math.sin(i / 3.0)
        elif resource_id == "demo-rds-reporting" and rule.metric_name == "FreeStorageSpace":
            value = 80 * GIB + 0.2 * GIB * math.sin(i / 5.0)  # healthy, flat
        elif resource_id == "demo-ec2-web" and rule.metric_name == "CPUUtilization":
            # Each calendar day has one crest, rising 1.6 points per day and 10% lower at weekends. Keying the
            # crest to the day (not to the current hour) keeps the demo result the same at any time of day.
            day_index = (now.date() - ts.date()).days
            weekend = 0.9 if ts.weekday() >= 5 else 1.0
            crest = (82.0 - 1.6 * day_index) * weekend
            shape = 0.55 + 0.45 * max(0.0, math.sin(math.pi * (ts.hour - 6) / 12))
            value = crest * shape
        elif rule.metric_name == "CPUUtilization":
            value = 30 + 3 * math.sin(i / 4.0)  # healthy, flat
        else:
            return []  # this demo resource has no data for the metric
        out.append((ts, value))
    return out
