# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Daily digest: summarise recent findings by route and email them."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from capacity_foresight import aws, digest, notifier
from capacity_foresight.config import Settings

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

WINDOW_HOURS = 24


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    settings = Settings.from_env()
    now = datetime.now(timezone.utc)
    rows = digest.collect_recent(aws.client("s3"), settings.bucket, now, WINDOW_HOURS)
    if not rows:
        LOG.info("no findings in the last %d hours; no digest sent", WINDOW_HOURS)
        return {"digests": 0, "findings": 0}
    sns = aws.client("sns")
    groups = digest.group_by_route(rows)
    if not settings.digest_per_route:
        # One email for everyone. Each line carries its route, so no one gets several summaries a day.
        merged = sorted((r for g in groups.values() for r in g), key=digest.rank)
        groups = {None: merged}
    for route, group in groups.items():
        subject, message = digest.format_digest(group, WINDOW_HOURS, route)
        top = group[0].get("urgency", "LOW")
        sns.publish(
            TopicArn=settings.topic_arn, Subject=subject, Message=message,
            MessageAttributes=notifier.message_attributes(top, "mixed", route, kind="digest"),
        )
    LOG.info("sent %d digest(s) covering %d findings", len(groups), len(rows))
    return {"digests": len(groups), "findings": len(rows)}
