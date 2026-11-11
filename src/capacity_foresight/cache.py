# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Cache recommendations so an unchanged finding does not call the model on every run.

Stored in the same DynamoDB table as the notification de-duplication records (key prefix `rec#`,
same TTL attribute). The cache key covers the resource, the metric, the status, the minimum urgency and a
hash of the resource facts and enrichment, so a configuration change or a worse urgency gets a fresh plan.
Cached text is re-validated on read, so a damaged entry is ignored rather than trusted.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from .recommender import RecommendationError, validate


def cache_key(
    finding: dict[str, Any],
    facts: dict[str, Any] | None,
    enrichment: dict[str, Any] | None,
    variant: str = "",
) -> str:
    """`variant` identifies how the answer was produced (prompt fingerprint and model list), so an upgrade
    or a model change never serves a plan written under the old ones."""
    material = json.dumps(
        [
            finding["account_id"], finding["region"], finding["resource_type"], finding["resource_id"],
            finding["metric_name"], finding["status"], finding["urgency"], facts, enrichment, variant,
        ],
        sort_keys=True, default=str,
    )
    return "rec#" + hashlib.sha256(material.encode()).hexdigest()


def get(ddb: Any, table: str, key: str, floor_urgency: str, now: float | None = None) -> tuple[dict[str, Any], str] | None:
    """Return (recommendation, model_id) or None."""
    item = ddb.get_item(TableName=table, Key={"pk": {"S": key}}).get("Item")
    if not item:
        return None
    if int(item["expires_at"]["N"]) < int(now if now is not None else time.time()):
        return None  # TTL deletion can lag behind expiry
    try:
        return validate(json.loads(item["rec"]["S"]), floor_urgency), item.get("model_id", {}).get("S", "")
    except (RecommendationError, ValueError, KeyError):
        return None


def put(ddb: Any, table: str, key: str, rec: dict[str, Any], model_id: str, ttl_hours: int, now: float | None = None) -> None:
    now = int(now if now is not None else time.time())
    ddb.put_item(
        TableName=table,
        Item={
            "pk": {"S": key},
            "rec": {"S": json.dumps(rec, separators=(",", ":"))},
            "model_id": {"S": model_id},
            "expires_at": {"N": str(now + ttl_hours * 3600)},
        },
    )
