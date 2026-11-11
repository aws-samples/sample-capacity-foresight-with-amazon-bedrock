# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Threshold configuration and runtime settings."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RESOURCE_TYPES = ("EC2", "RDS", "EBS")
DIRECTIONS = ("ABOVE", "BELOW")
URGENCY_ORDER = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "thresholds.json"


class ConfigError(ValueError):
    """Raised when thresholds.json is malformed."""


@dataclass(frozen=True)
class MetricRule:
    resource_type: str
    metric_name: str
    threshold: float
    unit: str
    direction: str
    horizon_hours: int

    @property
    def statistic(self) -> str:
        """Use the statistic that exposes the risky tail: Maximum for ABOVE, Minimum for BELOW."""
        return "Maximum" if self.direction == "ABOVE" else "Minimum"


@dataclass(frozen=True)
class Defaults:
    lookback_days: int = 28
    min_points: int = 7
    min_r2: float = 0.3
    weekly_seasonality: bool = True
    detect_level_shift: bool = True
    interval_z: float = 1.28  # about an 80% interval on the slope
    # Hours of runway at or below which a finding is at least this urgent.
    urgency_bands_hours: dict[str, int] = field(
        default_factory=lambda: {"CRITICAL": 24, "HIGH": 48, "MEDIUM": 96}
    )


@dataclass(frozen=True)
class ThresholdConfig:
    version: str
    defaults: Defaults
    rules: tuple[MetricRule, ...]

    def rules_for(self, resource_type: str) -> list[MetricRule]:
        return [r for r in self.rules if r.resource_type == resource_type]


def parse_config(raw: dict[str, Any]) -> ThresholdConfig:
    """Validate and parse a thresholds document. Fails loudly on bad input."""
    if not isinstance(raw, dict):
        raise ConfigError("thresholds config must be a JSON object")
    d = raw.get("defaults", {})
    bands = d.get("urgency_bands_hours", Defaults().urgency_bands_hours)
    for name, hours in bands.items():
        if name not in URGENCY_ORDER or not isinstance(hours, (int, float)) or hours <= 0:
            raise ConfigError(f"invalid urgency band {name!r}: {hours!r}")
    defaults = Defaults(
        lookback_days=int(d.get("lookback_days", 28)),
        min_points=int(d.get("min_points", 7)),
        min_r2=float(d.get("min_r2", 0.3)),
        weekly_seasonality=bool(d.get("weekly_seasonality", True)),
        detect_level_shift=bool(d.get("detect_level_shift", True)),
        interval_z=float(d.get("interval_z", 1.28)),
        urgency_bands_hours={k: int(v) for k, v in bands.items()},
    )
    if not 3 <= defaults.min_points <= defaults.lookback_days:
        raise ConfigError("min_points must be between 3 and lookback_days")
    if not 0 <= defaults.min_r2 <= 1:
        raise ConfigError("min_r2 must be between 0 and 1")
    if not 0 <= defaults.interval_z <= 4:
        raise ConfigError("interval_z must be between 0 and 4")
    if defaults.lookback_days > 90:
        raise ConfigError("lookback_days must be 90 or less")

    rules = []
    seen = set()
    for i, m in enumerate(raw.get("metrics", [])):
        try:
            rule = MetricRule(
                resource_type=m["resource_type"],
                metric_name=m["metric_name"],
                threshold=float(m["threshold"]),
                unit=str(m.get("unit", "None")),
                direction=m["direction"],
                horizon_hours=int(m["horizon_hours"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"metrics[{i}] is invalid: {exc!r}") from exc
        if rule.resource_type not in RESOURCE_TYPES:
            raise ConfigError(f"metrics[{i}]: unsupported resource_type {rule.resource_type!r}")
        if rule.direction not in DIRECTIONS:
            raise ConfigError(f"metrics[{i}]: direction must be one of {DIRECTIONS}")
        if not 1 <= rule.horizon_hours <= 24 * 30:
            raise ConfigError(f"metrics[{i}]: horizon_hours must be 1..720")
        key = (rule.resource_type, rule.metric_name)
        if key in seen:
            raise ConfigError(f"duplicate rule for {key}")
        seen.add(key)
        rules.append(rule)
    if not rules:
        raise ConfigError("no metrics configured")
    return ThresholdConfig(str(raw.get("version", "0")), defaults, tuple(rules))


def load_config(path: Path | str | None = None) -> ThresholdConfig:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    with p.open(encoding="utf-8") as fh:
        return parse_config(json.load(fh))


@dataclass(frozen=True)
class Settings:
    """Runtime settings, read from Lambda environment variables set by the template."""

    bucket: str
    table_name: str
    topic_arn: str
    model_id: str
    high_risk_model_id: str
    fallback_model_id: str
    account_id: str
    region: str
    discovery_tag_key: str
    discovery_tag_value: str
    context_tag_keys: tuple[str, ...]
    max_resources: int
    suppression_days: int
    guardrail_id: str
    guardrail_version: str
    routing_tag_key: str
    enrichment_sources: tuple[str, ...]
    recommendation_cache_hours: int
    min_immediate_urgency: str
    digest_per_route: bool = False

    def models_for(self, urgency: str) -> list[str]:
        """Models to try, in order: the risk-appropriate model, then the fallback. Blank entries are skipped."""
        high = urgency in ("CRITICAL", "HIGH")
        primary = (self.high_risk_model_id if high and self.high_risk_model_id else self.model_id)
        return list(dict.fromkeys(m for m in (primary, self.fallback_model_id) if m))

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = env if env is not None else os.environ
        return cls(
            bucket=e.get("DATA_BUCKET", ""),
            table_name=e.get("SUPPRESSION_TABLE", ""),
            topic_arn=e.get("TOPIC_ARN", ""),
            model_id=e.get("BEDROCK_MODEL_ID", ""),
            high_risk_model_id=e.get("BEDROCK_HIGH_RISK_MODEL_ID", ""),
            fallback_model_id=e.get("BEDROCK_FALLBACK_MODEL_ID", ""),
            account_id=e.get("ACCOUNT_ID", ""),
            region=e.get("AWS_REGION", e.get("AWS_DEFAULT_REGION", "us-east-1")),
            discovery_tag_key=e.get("DISCOVERY_TAG_KEY", "capacity-foresight"),
            discovery_tag_value=e.get("DISCOVERY_TAG_VALUE", "monitor"),
            context_tag_keys=tuple(
                k.strip() for k in e.get("CONTEXT_TAG_KEYS", "Name").split(",") if k.strip()
            ),
            max_resources=int(e.get("MAX_RESOURCES", "500")),
            suppression_days=int(e.get("SUPPRESSION_DAYS", "7")),
            guardrail_id=e.get("GUARDRAIL_ID", ""),
            guardrail_version=e.get("GUARDRAIL_VERSION", ""),
            routing_tag_key=e.get("ROUTING_TAG_KEY", ""),
            enrichment_sources=tuple(
                x.strip() for x in e.get("ENRICHMENT_SOURCES", "").split(",") if x.strip()
            ),
            recommendation_cache_hours=int(e.get("RECOMMENDATION_CACHE_HOURS", "0")),
            min_immediate_urgency=e.get("MIN_IMMEDIATE_URGENCY", "LOW"),
            digest_per_route=e.get("DIGEST_PER_ROUTE", "false").lower() == "true",
        )
