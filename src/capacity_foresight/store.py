# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Write JSON Lines to Amazon S3 in the layout the Athena tables expect (dt=YYYY-MM-DD)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable


def object_key(prefix: str, run_id: str, discriminator: str, when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    digest = hashlib.sha256(discriminator.encode()).hexdigest()[:10]
    return f"{prefix}/dt={when:%Y-%m-%d}/{run_id}-{digest}.jsonl"


def write_jsonl(s3: Any, bucket: str, key: str, rows: Iterable[dict[str, Any]]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    body = "\n".join(json.dumps(r, separators=(",", ":"), default=str) for r in rows) + "\n"
    s3.put_object(Bucket=bucket, Key=key, Body=body.encode("utf-8"), ContentType="application/x-ndjson")
    return len(rows)
