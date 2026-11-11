# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Step 1: find the resources to evaluate."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from capacity_foresight import aws
from capacity_foresight.config import Settings
from capacity_foresight.demo import demo_resources
from capacity_foresight.discovery import discover

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    settings = Settings.from_env()
    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    demo = bool(event.get("demo"))
    if demo:
        resources = demo_resources()
    else:
        resources = discover(
            aws.client("resourcegroupstaggingapi"),
            settings.discovery_tag_key,
            settings.discovery_tag_value,
            settings.max_resources,
        )
        if len(resources) >= settings.max_resources:
            LOG.warning("Resource cap of %d reached; some tagged resources were skipped", settings.max_resources)
    LOG.info("run %s: %d resources (demo=%s)", run_id, len(resources), demo)
    return {"run_id": run_id, "demo": demo, "resources": resources}
