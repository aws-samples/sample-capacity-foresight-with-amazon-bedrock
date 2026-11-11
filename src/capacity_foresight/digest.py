# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Daily digest: one summary email per route, built from the findings already stored in S3."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import URGENCY_ORDER
from .facts import safe_route

MAX_OBJECTS = 2000
MAX_ROWS_PER_DIGEST = 40


def _days(now: datetime, hours: int) -> list[str]:
    start = (now - timedelta(hours=hours)).date()
    return [(start + timedelta(days=i)).isoformat() for i in range((now.date() - start).days + 1)]


def collect_recent(s3: Any, bucket: str, now: datetime, hours: int = 24) -> list[dict[str, Any]]:
    """Newest finding per (resource, metric) recorded within the window."""
    cutoff = now - timedelta(hours=hours)
    latest: dict[tuple, dict[str, Any]] = {}
    read = 0
    for day in _days(now, hours):
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"findings/dt={day}/"):
            for obj in page.get("Contents", []):
                if read >= MAX_OBJECTS:
                    break
                read += 1
                body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().decode("utf-8")
                for line in body.splitlines():
                    try:
                        row = json.loads(line)
                        when = datetime.fromisoformat(row["finding_ts"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    if when < cutoff:
                        continue
                    key = (row.get("account_id"), row.get("region"), row.get("resource_type"),
                           row.get("resource_id"), row.get("metric_name"))
                    if key not in latest or when > datetime.fromisoformat(latest[key]["finding_ts"]):
                        latest[key] = row
    return list(latest.values())


def rank(row: dict[str, Any]) -> tuple[int, float]:
    urgency = row.get("urgency", "LOW")
    return (-URGENCY_ORDER.index(urgency) if urgency in URGENCY_ORDER else 0,
            row.get("runway_earliest_hours") if row.get("runway_earliest_hours") is not None
            else (row.get("runway_hours") if row.get("runway_hours") is not None else 1e9))


def group_by_route(rows: list[dict[str, Any]]) -> dict[str | None, list[dict[str, Any]]]:
    groups: dict[str | None, list[dict[str, Any]]] = {}
    for row in rows:
        # Rows come from S3; never trust the stored route value.
        groups.setdefault(safe_route(row["route"]) if row.get("route") else None, []).append(row)
    return {k: sorted(v, key=rank) for k, v in groups.items()}


def format_digest(rows: list[dict[str, Any]], hours: int, route: str | None) -> tuple[str, str]:
    counts = {u: sum(1 for r in rows if r.get("urgency") == u) for u in reversed(URGENCY_ORDER)}
    summary = ", ".join(f"{n} {u.lower()}" for u, n in counts.items() if n)
    subject = f"Capacity Foresight digest: {len(rows)} finding{'s' if len(rows) != 1 else ''} ({summary})"
    if route:
        subject = f"[{route}] {subject}"
    lines = [f"Open capacity findings from the last {hours} hours, most urgent first.", ""]
    for r in rows[:MAX_ROWS_PER_DIGEST]:
        status = "already breached" if r.get("status") == "BREACHED" else (
            f"~{round(r['runway_hours'])}h to breach" if r.get("runway_hours") is not None else "breach predicted")
        owner = f" (team: {safe_route(r['route'])})" if route is None and r.get("route") and safe_route(r["route"]) else ""
        lines += [
            f"[{r.get('urgency')}] {r.get('resource_type')} {r.get('resource_id')} {r.get('metric_name')}: {status}{owner}",
            f"    {r.get('summary', '')}",
            f"    Top action: {r.get('top_action', 'n/a')}  (alert: {r.get('delivery', 'n/a')})",
        ]
    if len(rows) > MAX_ROWS_PER_DIGEST:
        lines.append(f"... and {len(rows) - MAX_ROWS_PER_DIGEST} more. Open the dashboard for the full list.")
    lines += ["", "-" * 60, "Summaries are AI-generated advice. Review before acting."]
    # SNS subjects must be printable ASCII on one line, at most 100 characters.
    return re.sub(r"[^\x20-\x7e]", "?", subject)[:100], "\n".join(lines)
