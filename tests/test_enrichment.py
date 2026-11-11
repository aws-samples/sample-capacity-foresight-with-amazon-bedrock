# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from datetime import datetime, timedelta, timezone

from capacity_foresight import enrichment
from capacity_foresight.facts import collect_facts

NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
RDS_FINDING = {"resource_type": "RDS", "resource_id": "db1", "resource_arn": "arn:aws:rds:us-east-1:111122223333:db:db1"}
EC2_FINDING = {"resource_type": "EC2", "resource_id": "i-1", "resource_arn": "arn:aws:ec2:us-east-1:111122223333:instance/i-1"}


class FakeCO:
    def get_ec2_instance_recommendations(self, instanceArns):
        return {"instanceRecommendations": [{
            "finding": "UNDER_PROVISIONED", "findingReasonCodes": ["CPUUnderprovisioned"], "currentPerformanceRisk": "High",
            "recommendationOptions": [
                {"instanceType": f"m5.{n}", "performanceRisk": 1.0, "rank": i + 1, "migrationEffort": "Low",
                 "projectedUtilizationMetrics": [{"name": "CPU"}]} for i, n in enumerate(["xlarge", "2xlarge", "4xlarge", "8xlarge"])
            ]}]}

    def get_rds_database_recommendations(self, resourceArns):
        return {"rdsDBRecommendations": [{
            "instanceFinding": "Optimized", "storageFinding": "Underprovisioned",
            "storageFindingReasonCodes": ["EBSVolumeAllocatedStorageUnderprovisioned"],
            "instanceRecommendationOptions": [{"dbInstanceClass": "db.r6g.xlarge", "performanceRisk": 1.0, "rank": 1}],
            "storageRecommendationOptions": [{"storageConfiguration": {"storageType": "gp3", "allocatedStorage": 300}, "rank": 1}],
        }]}


class FakeRds:
    def describe_events(self, SourceIdentifier, SourceType, Duration):
        assert SourceType == "db-instance" and Duration == 7 * 24 * 60
        return {"Events": [
            {"Message": f"event {i}", "EventCategories": ["configuration change"], "Date": NOW - timedelta(hours=i)}
            for i in range(15)
        ]}


class FakeCloudTrail:
    def lookup_events(self, LookupAttributes, StartTime, EndTime, MaxResults):
        assert LookupAttributes == [{"AttributeKey": "ResourceName", "AttributeValue": "db1"}]
        return {"Events": [
            {"EventName": "ModifyDBInstance", "ReadOnly": "false", "EventTime": NOW - timedelta(hours=3),
             "Username": "someone@example.com", "AccessKeyId": "AKIAEXAMPLE", "CloudTrailEvent": "{...}"},
            {"EventName": "DescribeDBInstances", "ReadOnly": "true", "EventTime": NOW},
            {"EventName": "Bad Name; drop", "ReadOnly": "false", "EventTime": NOW},
        ]}


class Boom:
    def __getattr__(self, name):
        def fail(*a, **k):
            raise RuntimeError("OptInRequiredException")
        return fail


CLIENTS = {"compute-optimizer": FakeCO(), "rds": FakeRds(), "cloudtrail": FakeCloudTrail()}


def test_parse_sources_ignores_unknown_and_blank():
    assert enrichment.parse_sources("rds_events, compute_optimizer,,nope,rds_events") == ("rds_events", "compute_optimizer")
    assert enrichment.parse_sources("") == ()


def test_nothing_enabled_means_nothing_called():
    assert enrichment.collect(CLIENTS, (), RDS_FINDING, NOW) == {}


def test_compute_optimizer_ec2_keeps_top_three_options():
    out = enrichment.collect(CLIENTS, ("compute_optimizer",), EC2_FINDING, NOW)["compute_optimizer"]
    assert out["finding"] == "UNDER_PROVISIONED"
    assert [o["instance_type"] for o in out["options"]] == ["m5.xlarge", "m5.2xlarge", "m5.4xlarge"]
    assert "projectedUtilizationMetrics" not in str(out)


def test_compute_optimizer_rds_includes_storage_and_instance_options():
    out = enrichment.collect(CLIENTS, ("compute_optimizer",), RDS_FINDING, NOW)["compute_optimizer"]
    assert out["storage_finding"] == "Underprovisioned"
    assert out["storage_options"][0]["configuration"]["allocatedStorage"] == 300


def test_compute_optimizer_needs_an_arn():
    assert enrichment.collect(CLIENTS, ("compute_optimizer",), {**RDS_FINDING, "resource_arn": None}, NOW) == {}


def test_rds_events_are_capped_newest_first_and_only_for_rds():
    out = enrichment.collect(CLIENTS, ("rds_events",), RDS_FINDING, NOW)["rds_events"]["recent_events"]
    assert len(out) == 10 and out[0]["message"] == "event 0"
    assert enrichment.collect(CLIENTS, ("rds_events",), EC2_FINDING, NOW) == {}


def test_cloudtrail_sends_write_calls_names_and_times_only():
    out = enrichment.collect(CLIENTS, ("cloudtrail_changes",), RDS_FINDING, NOW)["cloudtrail_changes"]
    assert out == {"recent_changes": [{"api_call": "ModifyDBInstance", "time": "2026-11-05T09:00Z"}]}
    text = str(out)
    assert "someone@example.com" not in text and "AKIA" not in text


def test_a_failing_source_is_skipped_and_others_still_run():
    clients = {**CLIENTS, "compute-optimizer": Boom()}
    out = enrichment.collect(clients, ("compute_optimizer", "rds_events"), RDS_FINDING, NOW)
    assert list(out) == ["rds_events"]


def test_plain_bounds_depth_length_and_drops_none():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": 1}}}}}}}, "s": "x" * 500, "n": None, "l": list(range(10))}
    out = enrichment.plain(deep)
    assert len(out["s"]) == 120 and "n" not in out and len(out["l"]) == 3
    assert "'h'" not in str(out)  # nesting beyond the limit is dropped


# ------------------------------------------------------------------ facts


class FakeEc2:
    def describe_instances(self, InstanceIds):
        return {"Reservations": [{"Instances": [{
            "InstanceType": "m5.large", "State": {"Name": "running"}, "Placement": {"AvailabilityZone": "a"},
            "Tags": [{"Key": "Name", "Value": "web"}, {"Key": "Team", "Value": "Orders Team!"},
                     {"Key": "aws:autoscaling:groupName", "Value": "asg"}, {"Key": "Secret", "Value": "s3cr3t"}],
        }]}]}


class FakeAsg:
    def describe_auto_scaling_groups(self, AutoScalingGroupNames):
        return {"AutoScalingGroups": [{"MinSize": 1, "MaxSize": 2, "DesiredCapacity": 2}]}


class FakeRdsDescribe:
    def describe_db_instances(self, DBInstanceIdentifier):
        return {"DBInstances": [{
            "DBInstanceArn": "arn:x", "DBInstanceClass": "db.m6g.large", "AllocatedStorage": 100,
            "PendingModifiedValues": {"AllocatedStorage": 200, "MasterUserPassword": "hunter2", "DBInstanceClass": "db.m6g.xlarge"},
        }]}

    def list_tags_for_resource(self, ResourceName):
        return {"TagList": [{"Key": "Team", "Value": "data"}, {"Key": "Name", "Value": "db"}]}


def test_ec2_facts_include_asg_route_and_only_allowed_tags():
    facts = collect_facts({"ec2": FakeEc2(), "autoscaling": FakeAsg()}, "EC2", "i-1", ("Name",), "Team")
    assert facts["tags"] == {"Name": "web"} and facts["route"] == "OrdersTeam"
    assert facts["auto_scaling_group"] == {"min": 1, "max": 2, "desired": 2}
    assert "s3cr3t" not in str(facts)


def test_rds_facts_include_only_allow_listed_pending_modifications():
    facts = collect_facts({"rds": FakeRdsDescribe()}, "RDS", "db1", ("Name",), "Team")
    assert facts["pending_modifications"] == {"AllocatedStorage": 200, "DBInstanceClass": "db.m6g.xlarge"}
    assert "hunter2" not in str(facts) and facts["route"] == "data"


def test_no_routing_key_means_no_route():
    assert collect_facts({"rds": FakeRdsDescribe()}, "RDS", "db1", ("Name",))["route"] is None
