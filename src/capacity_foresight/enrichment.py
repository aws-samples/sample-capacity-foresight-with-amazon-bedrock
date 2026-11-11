# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Optional enrichment: extra AWS signals handed to the model as context.

Every source is independent, off unless enabled by a stack parameter, and best effort: a source that is
not set up (for example Compute Optimizer not opted in) or that fails is skipped and never blocks an alert.

What is sent to the model is limited on purpose:
  * compute_optimizer   - AWS's own rightsizing findings and the top candidate configurations.
  * rds_events          - recent RDS event messages for the instance (last 7 days).
  * cloudtrail_changes  - names and times of recent *write* API calls on the resource. Identities,
                          source IPs and request parameters are never read or sent.
All values are sanitised and bounded. They are still treated as untrusted data in the prompt.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .facts import clean_text

LOG = logging.getLogger(__name__)

SOURCES = ("compute_optimizer", "rds_events", "cloudtrail_changes")
MAX_OPTIONS = 3
MAX_EVENTS = 10
LOOKBACK_DAYS = 7
_EVENT_NAME = re.compile(r"^[A-Za-z0-9]{1,64}$")


def plain(value: Any, depth: int = 0) -> Any:
    """Reduce an API structure to bounded, JSON-safe primitives. Drops None and deep nesting."""
    if depth > 5:
        return None
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return clean_text(value, 120)
    if isinstance(value, dict):
        out = {k: plain(v, depth + 1) for k, v in list(value.items())[:20] if isinstance(k, str)}
        return {k: v for k, v in out.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [x for x in (plain(v, depth + 1) for v in list(value)[:MAX_OPTIONS]) if x is not None]
    return None


# --------------------------------------------------------------------------- Compute Optimizer


def _compute_optimizer(clients: dict[str, Any], finding: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    arn = finding.get("resource_arn")
    if not arn:
        return None
    co = clients["compute-optimizer"]
    rtype = finding["resource_type"]
    if rtype == "EC2":
        recs = co.get_ec2_instance_recommendations(instanceArns=[arn]).get("instanceRecommendations", [])
        if not recs:
            return None
        r = recs[0]
        return plain({
            "finding": r.get("finding"),
            "reason_codes": r.get("findingReasonCodes"),
            "current_performance_risk": r.get("currentPerformanceRisk"),
            "options": [
                {"instance_type": o.get("instanceType"), "performance_risk": o.get("performanceRisk"),
                 "rank": o.get("rank"), "migration_effort": o.get("migrationEffort")}
                for o in r.get("recommendationOptions", [])[:MAX_OPTIONS]
            ],
        })
    if rtype == "RDS":
        recs = co.get_rds_database_recommendations(resourceArns=[arn]).get("rdsDBRecommendations", [])
        if not recs:
            return None
        r = recs[0]
        return plain({
            "instance_finding": r.get("instanceFinding"),
            "storage_finding": r.get("storageFinding"),
            "instance_reason_codes": r.get("instanceFindingReasonCodes"),
            "storage_reason_codes": r.get("storageFindingReasonCodes"),
            "instance_options": [
                {"db_instance_class": o.get("dbInstanceClass"), "performance_risk": o.get("performanceRisk"),
                 "rank": o.get("rank")}
                for o in r.get("instanceRecommendationOptions", [])[:MAX_OPTIONS]
            ],
            "storage_options": [
                {"configuration": o.get("storageConfiguration"), "rank": o.get("rank")}
                for o in r.get("storageRecommendationOptions", [])[:MAX_OPTIONS]
            ],
        })
    if rtype == "EBS":
        recs = co.get_ebs_volume_recommendations(volumeArns=[arn]).get("volumeRecommendations", [])
        if not recs:
            return None
        r = recs[0]
        return plain({
            "finding": r.get("finding"),
            "current_performance_risk": r.get("currentPerformanceRisk"),
            "options": [
                {"configuration": o.get("configuration"), "performance_risk": o.get("performanceRisk"),
                 "rank": o.get("rank")}
                for o in r.get("volumeRecommendationOptions", [])[:MAX_OPTIONS]
            ],
        })
    return None


# --------------------------------------------------------------------------- RDS events


def _rds_events(clients: dict[str, Any], finding: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    if finding["resource_type"] != "RDS":
        return None
    resp = clients["rds"].describe_events(
        SourceIdentifier=finding["resource_id"],
        SourceType="db-instance",
        Duration=LOOKBACK_DAYS * 24 * 60,  # minutes
    )
    events = sorted(resp.get("Events", []), key=lambda e: e.get("Date") or now, reverse=True)[:MAX_EVENTS]
    if not events:
        return None
    return {
        "recent_events": [
            {
                "time": e["Date"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ") if e.get("Date") else None,
                "categories": [clean_text(c, 40) for c in e.get("EventCategories", [])[:3]],
                "message": clean_text(e.get("Message", ""), 200),
            }
            for e in events
        ]
    }


# --------------------------------------------------------------------------- CloudTrail


def _cloudtrail_changes(clients: dict[str, Any], finding: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    resp = clients["cloudtrail"].lookup_events(
        LookupAttributes=[{"AttributeKey": "ResourceName", "AttributeValue": finding["resource_id"]}],
        StartTime=now - timedelta(days=LOOKBACK_DAYS),
        EndTime=now,
        MaxResults=50,
    )
    changes = []
    for e in resp.get("Events", []):
        name = e.get("EventName", "")
        if str(e.get("ReadOnly", "")).lower() != "false" or not _EVENT_NAME.match(name):
            continue  # reads and odd names are dropped; only the event name and time are kept
        changes.append({"api_call": name, "time": e["EventTime"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")})
    changes = sorted(changes, key=lambda c: c["time"], reverse=True)[:MAX_EVENTS]
    return {"recent_changes": changes} if changes else None


_PROVIDERS: dict[str, Callable[..., dict[str, Any] | None]] = {
    "compute_optimizer": _compute_optimizer,
    "rds_events": _rds_events,
    "cloudtrail_changes": _cloudtrail_changes,
}

# Service client each provider needs (the handler builds only what is enabled).
CLIENT_FOR = {"compute_optimizer": "compute-optimizer", "rds_events": "rds", "cloudtrail_changes": "cloudtrail"}


def parse_sources(raw: str) -> tuple[str, ...]:
    """Parse 'rds_events,compute_optimizer'. Unknown names are ignored with a warning."""
    names = []
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        if name in _PROVIDERS:
            names.append(name)
        else:
            LOG.warning("unknown enrichment source %r ignored", name)
    return tuple(dict.fromkeys(names))


def collect(
    clients: dict[str, Any],
    enabled: tuple[str, ...],
    finding: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run each enabled provider, best effort. Returns {source_name: data} for sources that produced data."""
    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {}
    for name in enabled:
        try:
            data = _PROVIDERS[name](clients, finding, now)
        except Exception as exc:  # noqa: BLE001 - a missing opt-in or permission must not block the alert
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
            LOG.warning("enrichment %s unavailable: %s (check the role's permissions and the service set-up)", name, code)
            continue
        if data:
            out[name] = data
    return out
