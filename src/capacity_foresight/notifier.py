# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""De-duplicated email notifications through Amazon SNS."""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from botocore.exceptions import ClientError

from .config import URGENCY_ORDER
from .facts import safe_route


def dedupe_key(finding: dict[str, Any]) -> str:
    raw = "|".join(
        str(finding[k]) for k in ("account_id", "region", "resource_type", "resource_id", "metric_name")
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def urgency_rank(urgency: str) -> int:
    return URGENCY_ORDER.index(urgency)


def claim(ddb: Any, table: str, finding: dict[str, Any], urgency: str, ttl_days: int, now: float | None = None) -> bool:
    """Atomically claim the right to notify.

    True when this is the first alert for the finding, the previous alert expired, or the
    urgency got worse since the last alert. False means an equal or more urgent alert is already out.
    """
    now = int(now if now is not None else time.time())
    rank = urgency_rank(urgency)
    try:
        ddb.put_item(
            TableName=table,
            Item={
                "pk": {"S": dedupe_key(finding)},
                "urgency_rank": {"N": str(rank)},
                "resource_id": {"S": finding["resource_id"]},
                "metric_name": {"S": finding["metric_name"]},
                "notified_at": {"N": str(now)},
                "expires_at": {"N": str(now + ttl_days * 86400)},  # DynamoDB TTL attribute
            },
            ConditionExpression="attribute_not_exists(pk) OR expires_at < :now OR urgency_rank < :rank",
            ExpressionAttributeValues={":now": {"N": str(now)}, ":rank": {"N": str(rank)}},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def release(ddb: Any, table: str, finding: dict[str, Any]) -> None:
    """Undo a claim when the notification could not be sent."""
    ddb.delete_item(TableName=table, Key={"pk": {"S": dedupe_key(finding)}})


RESOLVE_AFTER_HOURS = 24  # a clear reading only ends the suppression once the alert is at least this old


def resolve(ddb: Any, table: str, finding_key: dict[str, Any], now: float | None = None) -> bool:
    """Forget an earlier alert once the metric is healthy again, so a recurrence is announced.

    Without this a problem fixed on day 2 and back on day 5 would stay silent until the suppression
    period ends. The minimum age keeps a metric that flaps around its threshold from alerting every run.
    Returns True when a record was removed.
    """
    now = int(now if now is not None else time.time())
    try:
        ddb.delete_item(
            TableName=table,
            Key={"pk": {"S": dedupe_key(finding_key)}},
            ConditionExpression="attribute_exists(pk) AND notified_at <= :cutoff",
            ExpressionAttributeValues={":cutoff": {"N": str(now - RESOLVE_AFTER_HOURS * 3600)}},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _fmt(value: Any, unit: str | None) -> str:
    if value is None:
        return "n/a"
    if unit == "Bytes":
        return f"{value / 1024**3:,.1f} GiB"
    if unit == "Percent":
        return f"{value:,.1f}%"
    return f"{value:,.2f}"


def runway_phrase(finding: dict[str, Any]) -> str:
    if finding.get("status") == "BREACHED":
        return "has crossed its threshold"
    runway = finding.get("runway_hours") or 0
    return "projected to breach within the hour" if runway < 1 else f"projected to breach in ~{runway:.0f}h"


def subject(finding: dict[str, Any], rec: dict[str, Any]) -> str:
    when = runway_phrase(finding)
    text = f"[{rec['urgency']}] {finding['resource_type']} {finding['resource_id']} {finding['metric_name']} {when}"
    # SNS subjects must be printable ASCII on one line, at most 100 characters.
    return re.sub(r"[^\x20-\x7e]", "?", text)[:100]


def _runway_line(finding: dict[str, Any]) -> str:
    if finding.get("status") == "BREACHED":
        return "already breached"
    central = round(finding.get("runway_hours") or 0)
    early, late = finding.get("runway_earliest_hours"), finding.get("runway_latest_hours")
    if early is None or (late is not None and round(early) == round(late)):
        return f"{central} hours"
    upper = f"{round(late)}" if late is not None else "beyond the data's confidence"
    return f"{central} hours (likely between {round(early)} and {upper})"


def _fit_line(finding: dict[str, Any]) -> str:
    if finding.get("r2") is None:
        return "not enough history to fit a trend"
    return f"r2={finding['r2']:.2f} over {finding.get('n_points')} days"


def _notes(finding: dict[str, Any]) -> list[str]:
    notes = []
    if finding.get("accelerating"):
        notes.append("the last 7 days are trending faster than the longer-run trend")
    if finding.get("seasonal") is False:
        notes.append("the weekly pattern was not removed (it needs about 10 days of data), so the date may be early or late")
    if finding.get("level_shift"):
        notes.append("a recent level shift was detected (resize, failover or migration); only data after it is used")
    return notes


def body(finding: dict[str, Any], rec: dict[str, Any], source: str) -> str:
    unit = finding.get("unit")
    lines = [
        rec["summary"],
        "",
        f"Resource      : {finding['resource_type']} {finding['resource_id']} ({finding['account_id']}, {finding['region']})",
        f"Metric        : {finding['metric_name']} ({finding['direction'].lower()} {_fmt(finding['threshold'], unit)})",
        f"Current       : {_fmt(finding.get('current_value'), unit)} (latest reading)"
        + (f", {_fmt(finding['last_daily_value'], unit)} (last full day's {'peak' if finding['direction'] == 'ABOVE' else 'low'})"
           if finding.get("last_daily_value") is not None else ""),
        f"Projected     : {_fmt(finding.get('projected_value_at_horizon'), unit)} in {finding.get('horizon_hours')}h",
        f"Runway        : {_runway_line(finding)}",
        f"Trend fit     : {_fit_line(finding)} (confidence: {rec['confidence']})",
    ]
    lines += [f"Note          : {n}" for n in _notes(finding)]
    lines += [
        "",
        "Probable cause",
        f"  {rec['probable_cause']}",
        "",
        "Recommended actions (most recommended first)",
    ]
    for i, a in enumerate(rec["actions"], 1):
        downtime = "downtime expected" if a["downtime_expected"] else "no downtime"
        cost = a.get("cost_impact", "UNKNOWN")
        lines += [
            f"  {i}. {a['title']}  [{a['action_type']}, risk {a['risk']}, {downtime}, cost impact {cost}]",
            f"     {a['detail']}",
        ]
    if rec["verification_steps"]:
        lines += ["", "How to verify"] + [f"  - {s}" for s in rec["verification_steps"]]
    lines += [
        "",
        "-" * 60,
        "Recommendations are AI-generated advice." if source in ("BEDROCK", "CACHE")
        else "AI analysis was unavailable; this is a basic notice.",
        "Review before acting. Capacity Foresight never changes your resources.",
        f"Run: {finding['run_id']}",
    ]
    return "\n".join(lines)


def message_attributes(urgency: str, resource_type: str, route: str | None, kind: str = "alert") -> dict[str, dict[str, str]]:
    """SNS message attributes, so each team can subscribe with a filter policy (for example route=orders-team)."""
    attrs = {
        "kind": {"DataType": "String", "StringValue": kind},
        "urgency": {"DataType": "String", "StringValue": urgency},
        "resource_type": {"DataType": "String", "StringValue": resource_type},
    }
    safe = safe_route(route) if route else None
    if safe:
        attrs["route"] = {"DataType": "String", "StringValue": safe}
    return attrs


def publish(
    sns: Any, topic_arn: str, finding: dict[str, Any], rec: dict[str, Any], source: str, route: str | None = None
) -> None:
    sns.publish(
        TopicArn=topic_arn,
        Subject=subject(finding, rec),
        Message=body(finding, rec, source),
        MessageAttributes=message_attributes(rec["urgency"], finding["resource_type"], route),
    )
