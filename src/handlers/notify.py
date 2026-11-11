# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Step 4: email the operator once per finding and record the finding for the dashboard."""

from __future__ import annotations

import json
import logging
from typing import Any

from capacity_foresight import aws, notifier
from capacity_foresight.config import Settings
from capacity_foresight.store import object_key, write_jsonl

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    settings = Settings.from_env()
    finding = event["finding"]
    rec = event["recommendation"]
    source = event["recommendation_source"]
    route = event.get("route")

    # Findings below the immediate-alert urgency are not emailed now; the daily digest carries them.
    if notifier.urgency_rank(rec["urgency"]) < notifier.urgency_rank(settings.min_immediate_urgency):
        send, delivery = False, "digest_only"
    else:
        send = notifier.claim(
            aws.client("dynamodb"), settings.table_name, finding, rec["urgency"], settings.suppression_days
        )
        delivery = "immediate" if send else "suppressed"
        if send:
            try:
                notifier.publish(aws.client("sns"), settings.topic_arn, finding, rec, source, route)
            except Exception:
                # Release the claim so the next attempt or run can still alert.
                notifier.release(aws.client("dynamodb"), settings.table_name, finding)
                raise

    row = {
        **{k: finding.get(k) for k in (
            "run_id", "finding_ts", "account_id", "region", "resource_type", "resource_id", "metric_name",
            "threshold", "direction", "horizon_hours", "status", "current_value",
            "projected_value_at_horizon", "runway_hours", "runway_earliest_hours", "runway_latest_hours",
            "slope_per_day", "r2", "n_points", "accelerating", "level_shift",
        )},
        "urgency": rec["urgency"],
        "confidence": rec["confidence"],
        "summary": rec["summary"],
        "probable_cause": rec["probable_cause"],
        "top_action": rec["actions"][0]["title"],
        "actions": json.dumps(rec["actions"]),
        "recommendation_source": source,
        "model_id": event.get("model_id", ""),
        "route": route,
        "delivery": delivery,
        "notified": send,
    }
    write_jsonl(
        aws.client("s3"), settings.bucket,
        object_key("findings", finding["run_id"], f"{finding['resource_id']}|{finding['metric_name']}"),
        [row],
    )
    LOG.info("finding %s/%s delivery=%s", finding["resource_id"], finding["metric_name"], delivery)
    return {"notified": send, "delivery": delivery}
