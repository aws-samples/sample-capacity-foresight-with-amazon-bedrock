#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Set up QuickSight to read Capacity Foresight: service role, Athena data source and the three datasets.

Prerequisites (see docs/PREREQUISITES.md): a QuickSight subscription in this account, and the stack deployed
with its Athena views created (done by scripts/deploy.sh).

    python scripts/setup_quicksight.py --stack-name capacity-foresight

Safe to re-run. It creates only:
  * an IAM role for this data source, scoped to THIS stack's data bucket, Athena workgroup and Glue database;
  * one Athena data source that uses that role through `AthenaParameters.RoleArn`, and three direct-query
    datasets, owned by the principal you choose.

Using a per-data-source role means QuickSight's account-wide "AWS resources" access (which the console
sets up with broad S3 access) does not need to be configured at all.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import boto3
from botocore.exceptions import ClientError

ROLE_NAME = "capacity-foresight-quicksight-athena"
ROLE_PATH = "/"
DATASETS = {
    "cf-latest-findings": ("v_latest_findings", "Capacity Foresight: latest findings"),
    "cf-latest-series": ("v_latest_series", "Capacity Foresight: trend and forecast"),
    "cf-finding-history": ("v_finding_history", "Capacity Foresight: finding history"),
}
DATA_SOURCE_ID = "capacity-foresight-athena"
SOURCE_ACTIONS = [
    "quicksight:DescribeDataSource", "quicksight:DescribeDataSourcePermissions", "quicksight:PassDataSource",
    "quicksight:UpdateDataSource", "quicksight:DeleteDataSource", "quicksight:UpdateDataSourcePermissions",
]
SET_ACTIONS = [
    "quicksight:DescribeDataSet", "quicksight:DescribeDataSetPermissions", "quicksight:PassDataSet",
    "quicksight:DescribeIngestion", "quicksight:ListIngestions", "quicksight:UpdateDataSet",
    "quicksight:DeleteDataSet", "quicksight:CreateIngestion", "quicksight:CancelIngestion",
    "quicksight:UpdateDataSetPermissions",
]
_TYPES = {"string": "STRING", "double": "DECIMAL", "float": "DECIMAL", "int": "INTEGER", "integer": "INTEGER",
          "bigint": "INTEGER", "smallint": "INTEGER", "tinyint": "INTEGER", "boolean": "BOOLEAN",
          "timestamp": "DATETIME", "date": "DATETIME"}


def role_policy(bucket: str, database: str, workgroup: str, region: str, account: str) -> dict:
    """Least-privilege policy: this bucket, this workgroup, this Glue database."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "AthenaQueries", "Effect": "Allow",
             "Action": ["athena:BatchGetQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults",
                        "athena:GetQueryResultsStream", "athena:StartQueryExecution", "athena:StopQueryExecution",
                        "athena:GetWorkGroup"],
             "Resource": f"arn:aws:athena:{region}:{account}:workgroup/{workgroup}"},
            {"Sid": "AthenaDiscovery", "Effect": "Allow",
             "Action": ["athena:ListWorkGroups", "athena:ListEngineVersions", "athena:ListDataCatalogs",
                        "athena:ListDatabases", "athena:ListTableMetadata", "athena:GetDataCatalog",
                        "athena:GetDatabase", "athena:GetTableMetadata"],
             "Resource": "*"},
            {"Sid": "GlueMetadata", "Effect": "Allow",
             "Action": ["glue:GetDatabase", "glue:GetDatabases", "glue:GetTable", "glue:GetTables",
                        "glue:GetPartition", "glue:GetPartitions", "glue:BatchGetPartition"],
             "Resource": [f"arn:aws:glue:{region}:{account}:catalog",
                          f"arn:aws:glue:{region}:{account}:database/{database}",
                          f"arn:aws:glue:{region}:{account}:table/{database}/*"]},
            {"Sid": "ReadDataBucket", "Effect": "Allow",
             "Action": ["s3:GetObject", "s3:GetObjectVersion"],
             "Resource": f"arn:aws:s3:::{bucket}/*"},
            {"Sid": "ListDataBucket", "Effect": "Allow",
             "Action": ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"],
             "Resource": f"arn:aws:s3:::{bucket}"},
            {"Sid": "WriteQueryResults", "Effect": "Allow",
             "Action": ["s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
             "Resource": f"arn:aws:s3:::{bucket}/athena-results/*"},
        ],
    }


def trust_policy(account: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow", "Principal": {"Service": "quicksight.amazonaws.com"}, "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": account}},
        }],
    }


def ensure_role(iam, policy: dict, account: str) -> str:
    """Create or update the data source role and return its ARN."""
    try:
        iam.get_role(RoleName=ROLE_NAME)
        print(f"role {ROLE_NAME} exists; updating its policy")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchEntity":
            raise
        iam.create_role(RoleName=ROLE_NAME, Path=ROLE_PATH, AssumeRolePolicyDocument=json.dumps(trust_policy(account)),
                        Description="Lets QuickSight read Capacity Foresight data through Athena",
                        Tags=[{"Key": "Purpose", "Value": "capacity-foresight"}])
        print(f"created role {ROLE_NAME}")
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="CapacityForesightAthenaAccess", PolicyDocument=json.dumps(policy))
    return iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]


def columns_for(glue, database: str, view: str) -> list[dict]:
    table = glue.get_table(DatabaseName=database, Name=view)["Table"]
    out = []
    for col in table["StorageDescriptor"]["Columns"]:
        base = col["Type"].split("(")[0].lower()
        if base not in _TYPES:
            raise SystemExit(f"unsupported column type {col['Type']!r} in {view}.{col['Name']}")
        out.append({"Name": col["Name"], "Type": _TYPES[base]})
    return out


def wait_source(qs, account: str) -> None:
    for _ in range(40):
        src = qs.describe_data_source(AwsAccountId=account, DataSourceId=DATA_SOURCE_ID)["DataSource"]
        if src["Status"] != "CREATION_IN_PROGRESS" and src["Status"] != "UPDATE_IN_PROGRESS":
            if src["Status"].endswith("FAILED"):
                raise SystemExit(f"data source {src['Status']}: {src.get('ErrorInfo')}")
            return
        time.sleep(3)
    raise SystemExit("timed out waiting for the data source")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stack-name", default="capacity-foresight")
    p.add_argument("--region", default=None, help="defaults to your configured Region")
    p.add_argument("--principal", help="QuickSight user ARN that owns the objects (default: the only registered user)")
    args = p.parse_args(argv)

    session = boto3.Session(region_name=args.region)
    region = session.region_name
    account = session.client("sts").get_caller_identity()["Account"]
    cfn, iam, glue, qs = (session.client(n) for n in ("cloudformation", "iam", "glue", "quicksight"))

    outputs = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName=args.stack_name)["Stacks"][0]["Outputs"]}
    bucket, database, workgroup = outputs["DataBucketName"], outputs["GlueDatabase"], outputs["AthenaWorkGroupName"]

    try:
        qs.describe_account_subscription(AwsAccountId=account)
    except ClientError:
        raise SystemExit("This account has no QuickSight subscription. Subscribe first (see docs/PREREQUISITES.md).")

    principal = args.principal
    if not principal:
        users = qs.list_users(AwsAccountId=account, Namespace="default")["UserList"]
        if len(users) != 1:
            raise SystemExit("Pass --principal: found %d QuickSight users." % len(users))
        principal = users[0]["Arn"]
    print(f"owner: {principal}")

    role_arn = ensure_role(iam, role_policy(bucket, database, workgroup, region, account), account)
    print("waiting for IAM to propagate")
    time.sleep(15)

    source_arn = f"arn:aws:quicksight:{region}:{account}:datasource/{DATA_SOURCE_ID}"
    try:
        status = qs.describe_data_source(AwsAccountId=account, DataSourceId=DATA_SOURCE_ID)["DataSource"]["Status"]
    except ClientError:
        status = None
    if status and status.endswith("FAILED"):
        qs.delete_data_source(AwsAccountId=account, DataSourceId=DATA_SOURCE_ID)
        time.sleep(5)
        status = None
    if status is None:
        qs.create_data_source(
            AwsAccountId=account, DataSourceId=DATA_SOURCE_ID, Name="Capacity Foresight", Type="ATHENA",
            DataSourceParameters={"AthenaParameters": {"WorkGroup": workgroup, "RoleArn": role_arn}},
            Permissions=[{"Principal": principal, "Actions": SOURCE_ACTIONS}],
        )
    wait_source(qs, account)
    print("data source ready")

    for dataset_id, (view, name) in DATASETS.items():
        table = {
            "RelationalTable": {"DataSourceArn": source_arn, "Catalog": "AwsDataCatalog", "Schema": database,
                                "Name": view, "InputColumns": columns_for(glue, database, view)}
        }
        kwargs = dict(AwsAccountId=account, DataSetId=dataset_id, Name=name, PhysicalTableMap={"t": table},
                      ImportMode="DIRECT_QUERY")
        try:
            qs.create_data_set(**kwargs, Permissions=[{"Principal": principal, "Actions": SET_ACTIONS}])
            print(f"created dataset {dataset_id} from {view}")
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceExistsException":
                raise
            qs.update_data_set(**kwargs)
            print(f"updated dataset {dataset_id} from {view}")
    print("done. Datasets: " + ", ".join(DATASETS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
