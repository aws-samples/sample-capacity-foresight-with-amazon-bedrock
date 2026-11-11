#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""One-time quality check of the AI recommendations against golden cases.

Calls Amazon Bedrock (a few requests, billed to your account) with the model you intend to use, applies simple
automatic checks, and writes the full outputs to evals/results/ for a human to read. Run it again whenever you
change the prompt or the model.

    python scripts/run_eval.py --model-id <model or inference profile ID> [--guardrail-id ID --guardrail-version V] [--runs 2]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from capacity_foresight.config import URGENCY_ORDER  # noqa: E402
from capacity_foresight.recommender import ACTION_TYPES, recommend  # noqa: E402

BASE_FINDING = {"account_id": "000000000000", "region": "us-east-1", "run_id": "eval"}
URL = re.compile(r"(?i)https?://|www\.")


def flatten(rec: dict) -> str:
    return json.dumps(rec).lower()


def check_case(case: dict, rec: dict, source: str) -> list[str]:
    """Return a list of failed checks (empty means every check passed)."""
    problems = []
    expect = case.get("expect", {})
    if source != "BEDROCK":
        return [f"no valid model output (source={source})"]
    actions = rec["actions"]
    types = [a["action_type"] for a in actions]
    if any(t not in ACTION_TYPES for t in types):
        problems.append("action type outside the allowed list")
    if len(actions) > 5:
        problems.append("more than 5 actions")
    if URL.search(json.dumps(rec)):
        problems.append("contains a link")
    floor = case["finding"]["urgency"]
    if URGENCY_ORDER.index(rec["urgency"]) < URGENCY_ORDER.index(floor):
        problems.append("urgency below the runway floor")
    if "any_action_type" in expect and not set(expect["any_action_type"]) & set(types):
        problems.append(f"expected one of {expect['any_action_type']}, got {types}")
    if types and types[0] in expect.get("first_action_not", []):
        problems.append(f"first action should not be {types[0]}")
    text = flatten(rec)
    if "mentions_any" in expect and not any(m.lower() in text for m in expect["mentions_any"]):
        problems.append(f"should mention one of {expect['mentions_any']}")
    for forbidden in expect.get("must_not_contain", []):
        if forbidden.lower() in text:
            problems.append(f"must not contain {forbidden!r}")
    if "urgency_at_least" in expect and URGENCY_ORDER.index(rec["urgency"]) < URGENCY_ORDER.index(expect["urgency_at_least"]):
        problems.append(f"urgency should be at least {expect['urgency_at_least']}")
    return problems


def run(client, model_id: str, cases: list[dict], runs: int, guardrail_id: str = "", guardrail_version: str = "") -> list[dict]:
    results = []
    for case in cases:
        finding = {**BASE_FINDING, **case["finding"]}
        for attempt in range(1, runs + 1):
            rec, source, used = recommend(
                client, model_id, finding, case.get("facts"), case.get("enrichment"), guardrail_id, guardrail_version
            )
            problems = check_case(case, rec, source)
            results.append({"case": case["id"], "run": attempt, "source": source, "model": used,
                            "passed": not problems, "problems": problems, "recommendation": rec})
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model-id", required=True)
    p.add_argument("--guardrail-id", default="")
    p.add_argument("--guardrail-version", default="")
    p.add_argument("--runs", type=int, default=1, help="repeat each case to see run-to-run variation")
    p.add_argument("--cases", type=Path, default=ROOT / "evals" / "golden_cases.json")
    args = p.parse_args(argv)

    import boto3  # imported here so the checks can be unit tested without AWS

    cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
    results = run(boto3.client("bedrock-runtime"), args.model_id, cases, args.runs, args.guardrail_id, args.guardrail_version)

    out_dir = ROOT / "evals" / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps({"model": args.model_id, "results": results}, indent=2), encoding="utf-8")

    for r in results:
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['case']} (run {r['run']})  {'; '.join(r['problems'])}")
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} passed. Full outputs for review: {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
