# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Step 3: ask Amazon Bedrock for a remediation plan, grounded in the resource's real configuration."""

from __future__ import annotations

import logging
import time
from typing import Any

from capacity_foresight import aws, cache, demo as demo_mod, enrichment
from capacity_foresight.config import Settings
from capacity_foresight.facts import collect_facts
from capacity_foresight.recommender import fingerprint, recommend

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)


RESERVE_SECONDS = 30  # kept back for the cache write and the return


def _deadline(context: Any) -> float | None:
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    return time.monotonic() + remaining() / 1000.0 - RESERVE_SECONDS if remaining else None


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    deadline = _deadline(context)
    settings = Settings.from_env()
    finding = event["finding"]
    rid = finding["resource_id"]
    is_demo = bool(event.get("demo")) and demo_mod.is_demo(rid)

    facts: dict[str, Any] | None
    try:
        if is_demo:
            facts = dict(demo_mod.demo_facts(rid))
        else:
            facts = collect_facts(
                aws.resource_clients(), finding["resource_type"], rid,
                settings.context_tag_keys, settings.routing_tag_key,
            )
    except Exception:  # noqa: BLE001 - recommend with less context rather than not at all
        LOG.exception("could not describe %s %s", finding["resource_type"], rid)
        facts = None
    # The routing value is for notification routing only; it is never shown to the model.
    route = facts.pop("route", None) if facts else None

    signals: dict[str, Any] = {}
    if settings.enrichment_sources and not is_demo:
        clients = {enrichment.CLIENT_FOR[n]: aws.client(enrichment.CLIENT_FOR[n]) for n in settings.enrichment_sources}
        signals = enrichment.collect(clients, settings.enrichment_sources, finding)

    ddb = aws.client("dynamodb") if settings.recommendation_cache_hours > 0 else None
    models = settings.models_for(finding["urgency"])
    key = cache.cache_key(finding, facts, signals, f"{fingerprint()}|{','.join(models)}") if ddb else ""
    hit = None
    if ddb:
        try:
            hit = cache.get(ddb, settings.table_name, key, finding["urgency"])
        except Exception:  # noqa: BLE001 - the cache is an optimisation; it must never block an alert
            LOG.warning("recommendation cache read failed; continuing without it")
        if hit:
            LOG.info("recommendation cache hit for %s", rid)
            return {"finding": finding, "recommendation": hit[0], "recommendation_source": "CACHE",
                    "model_id": hit[1], "route": route}

    recommendation, source, model_used = recommend(
        aws.client("bedrock-runtime"),
        models,
        finding,
        facts,
        signals,
        settings.guardrail_id,
        settings.guardrail_version,
        deadline,
    )
    if ddb and source == "BEDROCK":
        try:
            cache.put(ddb, settings.table_name, key, recommendation, model_used, settings.recommendation_cache_hours)
        except Exception:  # noqa: BLE001
            LOG.warning("recommendation cache write failed; continuing")
    LOG.info("recommendation for %s via %s (%s) urgency=%s", rid, source, model_used or "none", recommendation["urgency"])
    return {"finding": finding, "recommendation": recommendation, "recommendation_source": source,
            "model_id": model_used, "route": route}
