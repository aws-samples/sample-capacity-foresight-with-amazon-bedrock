# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Cached boto3 clients with sensible retry and timeout settings."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config

_BASE = Config(retries={"mode": "standard", "max_attempts": 5}, connect_timeout=5, read_timeout=30)
# Worst case per model is 2 tries x 45 s = 90 s. recommend() uses that to stay inside the Lambda timeout.
# In botocore's "standard" mode `max_attempts` counts the first try too; "total_max_attempts" is unambiguous.
_BEDROCK = Config(retries={"mode": "standard", "total_max_attempts": 2}, connect_timeout=5, read_timeout=45)


@lru_cache(maxsize=None)
def client(service: str) -> Any:
    return boto3.client(service, config=_BEDROCK if service == "bedrock-runtime" else _BASE)


def resource_clients() -> dict[str, Any]:
    """Clients used to describe resources."""
    return {name: client(name) for name in ("ec2", "rds", "autoscaling")}
