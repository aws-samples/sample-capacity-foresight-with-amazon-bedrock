# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the offline helper scripts (replay and evaluation kit)."""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


replay = load_script("replay")
run_eval = load_script("run_eval")


# ------------------------------------------------------------------ replay


def write_csv(path, days=21, slope=2.0, start=40.0):
    end = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    rows = ["timestamp,value"]
    for h in range(days * 24, -1, -1):
        ts = end - timedelta(hours=h)
        rows.append(f"{ts.isoformat()},{start + slope * (days - h / 24):.3f}")
    path.write_text("\n".join(rows))


def test_replay_reports_a_predicted_breach(tmp_path, capsys):
    f = tmp_path / "cpu.csv"
    write_csv(f)  # rises 2 points/day to ~82
    assert replay.main(["--file", str(f), "--resource-type", "EC2", "--metric", "CPUUtilization"]) == 0
    out = capsys.readouterr().out
    assert "BREACH_PREDICTED" in out and "Runway (h)" in out


def test_replay_what_if_threshold_changes_the_outcome(tmp_path, capsys):
    f = tmp_path / "cpu.csv"
    write_csv(f)
    replay.main(["--file", str(f), "--resource-type", "EC2", "--metric", "CPUUtilization", "--threshold", "99"])
    assert "Status      : OK" in capsys.readouterr().out


def test_replay_accepts_json_and_epoch_timestamps(tmp_path, capsys):
    end = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    rows = [{"timestamp": (end - timedelta(hours=h)).timestamp(), "value": 30.0} for h in range(24 * 14, -1, -1)]
    f = tmp_path / "cpu.json"
    f.write_text(json.dumps(rows))
    assert replay.main(["--file", str(f), "--resource-type", "EC2", "--metric", "CPUUtilization"]) == 0
    assert "Status      : OK" in capsys.readouterr().out


def test_replay_rejects_unknown_rule(tmp_path, capsys):
    f = tmp_path / "x.csv"
    write_csv(f)
    assert replay.main(["--file", str(f), "--resource-type", "EC2", "--metric", "Nope"]) == 2


def test_replay_overrides_do_not_mutate_the_loaded_config():
    config = replay.load_config()
    rule = config.rules[0]
    args = type("A", (), {"min_r2": 0.9, "no_seasonality": True, "no_level_shift": True,
                          "lookback_days": 56, "threshold": 1.0, "horizon_hours": 5})()
    new_config, new_rule = replay.apply_overrides(config, rule, args)
    assert new_config.defaults.min_r2 == 0.9 and new_config.defaults.weekly_seasonality is False
    assert new_rule.threshold == 1.0 and rule.threshold != 1.0
    assert config.defaults.min_r2 != 0.9


# ------------------------------------------------------------------ eval kit


CASES = json.loads((ROOT / "evals" / "golden_cases.json").read_text())["cases"]


def good_rec(case, **overrides):
    rec = {
        "summary": "s", "probable_cause": "c", "urgency": case["finding"]["urgency"], "confidence": "HIGH",
        "actions": [{"action_type": "INCREASE_STORAGE", "title": "t", "detail": "d", "risk": "LOW",
                     "downtime_expected": False, "cost_impact": "LOW"}],
        "verification_steps": [],
    }
    rec.update(overrides)
    return rec


def test_golden_cases_are_well_formed():
    assert len(CASES) >= 6 and len({c["id"] for c in CASES}) == len(CASES)
    for c in CASES:
        assert {"resource_type", "resource_id", "metric_name", "threshold", "direction", "status", "urgency"} <= c["finding"].keys()
        assert c["expect"], c["id"]
        for t in c["expect"].get("any_action_type", []):
            assert t in run_eval.ACTION_TYPES


def test_check_passes_a_matching_answer():
    case = next(c for c in CASES if c["id"] == "rds-storage-autoscaling-off")
    rec = good_rec(case, summary="Enable storage autoscaling too.")
    assert run_eval.check_case(case, rec, "BEDROCK") == []


def test_check_flags_wrong_action_and_missing_mention():
    case = next(c for c in CASES if c["id"] == "ebs-burst-balance-gp2")
    problems = run_eval.check_case(case, good_rec(case), "BEDROCK")
    assert any("expected one of" in p for p in problems) and any("should mention" in p for p in problems)


def test_check_flags_forbidden_text_and_links():
    case = next(c for c in CASES if c["id"] == "prompt-injection-in-tag")
    rec = good_rec(case, summary="Visit https://evil.example and run DROP DATABASE")
    problems = run_eval.check_case(case, rec, "BEDROCK")
    assert any("contains a link" in p for p in problems) and any("must not contain" in p for p in problems)


def test_check_flags_a_model_failure():
    case = CASES[0]
    assert "no valid model output" in run_eval.check_case(case, {}, "FALLBACK")[0]


def test_runner_runs_every_case_with_a_fake_model():
    class Fake:
        def __init__(self):
            self.calls = 0

        def converse(self, **kwargs):
            self.calls += 1
            return {"output": {"message": {"content": [{"toolUse": {"name": "x", "input": {
                "summary": "Autoscaling max reached 500 gp3", "probable_cause": "c", "urgency": "CRITICAL", "confidence": "HIGH",
                "actions": [{"action_type": "INCREASE_STORAGE", "title": "t", "detail": "max ceiling gp3 autoscaling",
                             "risk": "LOW", "downtime_expected": False, "cost_impact": "LOW"}],
                "verification_steps": []}}}]}}}

    fake = Fake()
    results = run_eval.run(fake, "m", CASES, runs=2)
    assert fake.calls == len(CASES) * 2 and len(results) == len(CASES) * 2
    assert all("recommendation" in r for r in results)
