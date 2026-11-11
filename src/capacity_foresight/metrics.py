# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Read hourly metric history from Amazon CloudWatch."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import MetricRule

_NAMESPACE = {"EC2": "AWS/EC2", "RDS": "AWS/RDS", "EBS": "AWS/EBS"}
_DIMENSION = {"EC2": "InstanceId", "RDS": "DBInstanceIdentifier", "EBS": "VolumeId"}


def fetch_hourly(
    cloudwatch: Any,
    rule: MetricRule,
    resource_id: str,
    lookback_days: int,
    now: datetime | None = None,
) -> list[tuple[datetime, float]]:
    """Hourly datapoints using the statistic that exposes the risky tail."""
    now = now or datetime.now(timezone.utc)
    query = {
        "Id": "m1",
        "MetricStat": {
            "Metric": {
                "Namespace": _NAMESPACE[rule.resource_type],
                "MetricName": rule.metric_name,
                "Dimensions": [{"Name": _DIMENSION[rule.resource_type], "Value": resource_id}],
            },
            "Period": 3600,
            "Stat": rule.statistic,
        },
        "ReturnData": True,
    }
    points: list[tuple[datetime, float]] = []
    paginator = cloudwatch.get_paginator("get_metric_data")
    for page in paginator.paginate(
        MetricDataQueries=[query],
        StartTime=now - timedelta(days=lookback_days),
        EndTime=now,
        ScanBy="TimestampAscending",
    ):
        for result in page.get("MetricDataResults", []):
            points.extend(zip(result.get("Timestamps", []), result.get("Values", [])))
    return sorted(points)
