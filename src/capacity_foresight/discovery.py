# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tag-based resource discovery using the Resource Groups Tagging API."""

from __future__ import annotations

from typing import Any

_TYPE_FILTERS = ["ec2:instance", "rds:db", "ec2:volume"]


def parse_arn(arn: str) -> dict[str, str] | None:
    """Map an ARN to {resource_type, resource_id, arn}. Returns None for unsupported ARNs."""
    parts = arn.split(":", 5)
    if len(parts) < 6:
        return None
    service, resource = parts[2], parts[5]
    if service == "ec2" and resource.startswith("instance/"):
        return {"resource_type": "EC2", "resource_id": resource.split("/", 1)[1], "arn": arn}
    if service == "ec2" and resource.startswith("volume/"):
        return {"resource_type": "EBS", "resource_id": resource.split("/", 1)[1], "arn": arn}
    if service == "rds" and resource.startswith("db:"):
        return {"resource_type": "RDS", "resource_id": resource.split(":", 1)[1], "arn": arn}
    return None


def discover(tagging: Any, tag_key: str, tag_value: str, limit: int) -> list[dict[str, str]]:
    """Return up to `limit` tagged resources, sorted for stable ordering."""
    found: dict[str, dict[str, str]] = {}
    paginator = tagging.get_paginator("get_resources")
    for page in paginator.paginate(
        TagFilters=[{"Key": tag_key, "Values": [tag_value]}],
        ResourceTypeFilters=_TYPE_FILTERS,
    ):
        for mapping in page.get("ResourceTagMappingList", []):
            parsed = parse_arn(mapping["ResourceARN"])
            if parsed:
                found[parsed["arn"]] = parsed
    return sorted(found.values(), key=lambda r: (r["resource_type"], r["resource_id"]))[:limit]
