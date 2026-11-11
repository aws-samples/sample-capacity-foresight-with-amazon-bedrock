# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Collect configuration facts about a resource so the model works from evidence, not guesses."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

MAX_TAG_VALUE = 128
# Control (Cc), format (Cf: bidi overrides, zero-width), line and paragraph separators (Zl, Zp).
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def clean_text(value: Any, limit: int = MAX_TAG_VALUE) -> str:
    """Make untrusted text safe to show: replace control and invisible formatting characters, then truncate.

    Bidirectional overrides and zero-width characters can make text read differently from what it is
    (for example a reversed file extension), so they are removed along with ordinary control characters.
    """
    text = "".join(" " if unicodedata.category(ch) in _INVISIBLE_CATEGORIES else ch for ch in str(value))
    return text[:limit]


def safe_route(value: Any) -> str | None:
    """Reduce a routing value to characters that are safe in an SNS attribute and an email subject."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "", str(value))[:64]
    return cleaned or None


def route_value(tags: dict[str, str], route_key: str) -> str | None:
    """Value of the routing tag, restricted to characters safe for an SNS message attribute."""
    if not route_key or route_key not in tags:
        return None
    return safe_route(tags[route_key])


def select_tags(tags: dict[str, str], allowed_keys: tuple[str, ...]) -> dict[str, str]:
    """Forward only explicitly allowed tag keys to the model."""
    return {k: clean_text(v) for k, v in tags.items() if k in allowed_keys}


def _tags_from_list(items: list[dict[str, str]] | None) -> dict[str, str]:
    return {i["Key"]: i["Value"] for i in (items or []) if "Key" in i}


def _ec2_facts(clients: dict[str, Any], resource_id: str, context_keys: tuple[str, ...], route_key: str) -> dict[str, Any]:
    reservations = clients["ec2"].describe_instances(InstanceIds=[resource_id])["Reservations"]
    inst = reservations[0]["Instances"][0]
    tags = _tags_from_list(inst.get("Tags"))
    facts: dict[str, Any] = {
        "instance_type": inst.get("InstanceType"),
        "state": inst.get("State", {}).get("Name"),
        "availability_zone": inst.get("Placement", {}).get("AvailabilityZone"),
        "architecture": inst.get("Architecture"),
        "ebs_optimized": inst.get("EbsOptimized"),
        "tags": select_tags(tags, context_keys),
        "route": route_value(tags, route_key),
    }
    asg_name = tags.get("aws:autoscaling:groupName")
    if asg_name:
        groups = clients["autoscaling"].describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        if groups["AutoScalingGroups"]:
            g = groups["AutoScalingGroups"][0]
            facts["auto_scaling_group"] = {
                "min": g["MinSize"], "max": g["MaxSize"], "desired": g["DesiredCapacity"],
            }
    return facts


def _rds_facts(clients: dict[str, Any], resource_id: str, context_keys: tuple[str, ...], route_key: str) -> dict[str, Any]:
    db = clients["rds"].describe_db_instances(DBInstanceIdentifier=resource_id)["DBInstances"][0]
    tag_list = clients["rds"].list_tags_for_resource(ResourceName=db["DBInstanceArn"]).get("TagList")
    tags = _tags_from_list(tag_list)
    pending = {k: v for k, v in (db.get("PendingModifiedValues") or {}).items()
               if k in ("DBInstanceClass", "AllocatedStorage", "StorageType", "Iops", "MultiAZ", "EngineVersion")}
    return {
        "db_instance_class": db.get("DBInstanceClass"),
        "engine": db.get("Engine"),
        "engine_version": db.get("EngineVersion"),
        "status": db.get("DBInstanceStatus"),
        "allocated_storage_gib": db.get("AllocatedStorage"),
        "max_allocated_storage_gib": db.get("MaxAllocatedStorage"),  # None => autoscaling off
        "storage_type": db.get("StorageType"),
        "iops": db.get("Iops"),
        "multi_az": db.get("MultiAZ"),
        "is_aurora_member": bool(db.get("DBClusterIdentifier")),
        "read_replica_count": len(db.get("ReadReplicaDBInstanceIdentifiers", [])),
        "performance_insights_enabled": db.get("PerformanceInsightsEnabled"),
        "pending_modifications": pending or None,
        "tags": select_tags(tags, context_keys),
        "route": route_value(tags, route_key),
    }


def _ebs_facts(clients: dict[str, Any], resource_id: str, context_keys: tuple[str, ...], route_key: str) -> dict[str, Any]:
    vol = clients["ec2"].describe_volumes(VolumeIds=[resource_id])["Volumes"][0]
    tags = _tags_from_list(vol.get("Tags"))
    return {
        "volume_type": vol.get("VolumeType"),
        "size_gib": vol.get("Size"),
        "iops": vol.get("Iops"),
        "throughput_mibps": vol.get("Throughput"),
        "state": vol.get("State"),
        "attached_instance_ids": [a.get("InstanceId") for a in vol.get("Attachments", [])],
        "tags": select_tags(tags, context_keys),
        "route": route_value(tags, route_key),
    }


_COLLECTORS = {"EC2": _ec2_facts, "RDS": _rds_facts, "EBS": _ebs_facts}


def collect_facts(
    clients: dict[str, Any],
    resource_type: str,
    resource_id: str,
    context_keys: tuple[str, ...],
    route_key: str = "",
) -> dict[str, Any]:
    """Describe the resource. Raises on API errors; the caller decides how to degrade.

    The returned dict includes a `route` value (from the routing tag) that is for notification routing
    only. Callers must remove it before the facts are shown to the model.
    """
    return _COLLECTORS[resource_type](clients, resource_id, context_keys, route_key)
