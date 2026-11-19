#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Create the Capacity Foresight QuickSight analysis and dashboard from code.

Run scripts/setup_quicksight.py first. This builds the three sheets described in quicksight/DASHBOARD_GUIDE.md:
the risk board, the trend and forecast, and the recommendations.

    python scripts/create_dashboard.py --stack-name capacity-foresight

Safe to re-run: the analysis and dashboard are updated in place.
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import boto3
from botocore.exceptions import ClientError, NoCredentialsError, CredentialRetrievalError

# Handle optional CRT dependency for SSO login
try:
    from botocore.exceptions import MissingDependencyException
except ImportError:
    MissingDependencyException = None

ANALYSIS_ID = "capacity-foresight-analysis"
DASHBOARD_ID = "capacity-foresight-dashboard"
ANALYSIS_ACTIONS = [
    "quicksight:RestoreAnalysis", "quicksight:UpdateAnalysisPermissions", "quicksight:DeleteAnalysis",
    "quicksight:DescribeAnalysisPermissions", "quicksight:QueryAnalysis", "quicksight:DescribeAnalysis",
    "quicksight:UpdateAnalysis",
]
DASHBOARD_ACTIONS = [
    "quicksight:DescribeDashboard", "quicksight:ListDashboardVersions", "quicksight:UpdateDashboardPermissions",
    "quicksight:QueryDashboard", "quicksight:UpdateDashboard", "quicksight:DeleteDashboard",
    "quicksight:DescribeDashboardPermissions", "quicksight:UpdateDashboardPublishedVersion",
]
URGENCY_COLORS = {"CRITICAL": "#C0392B", "HIGH": "#E67E22", "MEDIUM": "#F1C40F", "LOW": "#3498DB"}


def col(dataset: str, name: str) -> dict:
    return {"DataSetIdentifier": dataset, "ColumnName": name}


def dim(field_id: str, dataset: str, name: str) -> dict:
    return {"CategoricalDimensionField": {"FieldId": field_id, "Column": col(dataset, name)}}


def date_dim(field_id: str, dataset: str, name: str, granularity: str = "DAY") -> dict:
    return {"DateDimensionField": {"FieldId": field_id, "Column": col(dataset, name), "DateGranularity": granularity}}


def measure(field_id: str, dataset: str, name: str, agg: str = "MAX", fmt: dict | None = None) -> dict:
    field = {"FieldId": field_id, "Column": col(dataset, name), "AggregationFunction": {"SimpleNumericalAggregation": agg}}
    if fmt:
        field["FormatConfiguration"] = fmt
    return {"NumericalMeasureField": field}


def count_measure(field_id: str, dataset: str, name: str) -> dict:
    return {"CategoricalMeasureField": {"FieldId": field_id, "Column": col(dataset, name), "AggregationFunction": "COUNT"}}


def number_format(decimals: int = 1) -> dict:
    return {"FormatConfiguration": {"NumberDisplayFormatConfiguration": {
        "DecimalPlacesConfiguration": {"DecimalPlaces": decimals}}}}


def visual_title(text: str) -> dict:
    return {"Visibility": "VISIBLE", "FormatText": {"PlainText": text}}


def definition(default_series: str = "") -> dict:
    findings, series, history = "findings", "series", "history"

    kpi_critical = {"KPIVisual": {
        "VisualId": "kpi-critical", "Title": visual_title("Critical findings"),
        "ChartConfiguration": {"FieldWells": {"Values": [count_measure("k1", findings, "resource_id")]}},
    }}
    kpi_runway = {"KPIVisual": {
        "VisualId": "kpi-runway", "Title": visual_title("Shortest runway (hours)"),
        "ChartConfiguration": {"FieldWells": {"Values": [measure("k2", findings, "runway_hours", "MIN", number_format(0))]}},
    }}
    urgency_donut = {"PieChartVisual": {
        "VisualId": "urgency-donut", "Title": visual_title("Findings by urgency"),
        "ChartConfiguration": {
            "FieldWells": {"PieChartAggregatedFieldWells": {
                "Category": [dim("p1", findings, "urgency")], "Values": [count_measure("p2", findings, "resource_id")]}},
            "DonutOptions": {"ArcOptions": {"ArcThickness": "MEDIUM"}},
            "VisualPalette": {"ColorMap": [{"Element": {"FieldId": "p1", "FieldValue": k}, "Color": v} for k, v in URGENCY_COLORS.items()]},
        },
    }}
    runway_bar = {"BarChartVisual": {
        "VisualId": "runway-bar", "Title": visual_title("Runway by resource (hours)"),
        "ChartConfiguration": {
            "FieldWells": {"BarChartAggregatedFieldWells": {
                "Category": [dim("b1", findings, "resource_id")],
                "Values": [measure("b2", findings, "runway_hours", "MIN", number_format(0))],
                "Colors": [dim("b3", findings, "urgency")]}},
            "Orientation": "HORIZONTAL",
            # Breached resources have a runway of 0, so their bars have no length. Labels keep them visible.
            "DataLabels": {"Visibility": "VISIBLE", "Overlap": "DISABLE_OVERLAP"},
            "SortConfiguration": {"CategorySort": [{"FieldSort": {"FieldId": "b2", "Direction": "ASC"}}]},
            "VisualPalette": {"ColorMap": [{"Element": {"FieldId": "b3", "FieldValue": k}, "Color": v} for k, v in URGENCY_COLORS.items()]},
        },
    }}
    risk_table = {"TableVisual": {
        "VisualId": "risk-table", "Title": visual_title("Risk table"),
        "ChartConfiguration": {
            "FieldWells": {"TableAggregatedFieldWells": {
                "GroupBy": [dim("t1", findings, "urgency"), dim("t2", findings, "resource_type"), dim("t3", findings, "resource_id"),
                            dim("t4", findings, "metric_name"), dim("t5", findings, "status"), dim("t10", findings, "route")],
                "Values": [measure("t6", findings, "runway_hours", "MIN", number_format(0)),
                           measure("t7", findings, "runway_earliest_hours", "MIN", number_format(0)),
                           measure("t8", findings, "runway_latest_hours", "MAX", number_format(0)),
                           measure("t9", findings, "urgency_rank", "MAX", number_format(0))]}},
            "SortConfiguration": {"RowSort": [{"FieldSort": {"FieldId": "t9", "Direction": "DESC"}}]},
        },
    }}

    forecast_line = {"LineChartVisual": {
        "VisualId": "forecast-line", "Title": visual_title("Observed, forecast and threshold"),
        "ChartConfiguration": {
            "FieldWells": {"LineChartAggregatedFieldWells": {
                "Category": [date_dim("l1", series, "ts", "DAY")],
                "Values": [measure("l2", series, "value", "MAX", number_format(1))],
                "Colors": [dim("l4", series, "kind")]}},
            "Type": "LINE",
            "Legend": {"Visibility": "VISIBLE", "Position": "BOTTOM"},
        },
    }}
    series_details = {"TableVisual": {
        "VisualId": "series-details", "Title": visual_title("Selected series"),
        "ChartConfiguration": {"FieldWells": {"TableAggregatedFieldWells": {
            "GroupBy": [dim("s1", series, "kind")],
            "Values": [measure("s2", series, "value", "MIN", number_format(1)), measure("s3", series, "value", "MAX", number_format(1))]}},
            "FieldOptions": {"SelectedFieldOptions": [
                {"FieldId": "s1", "CustomLabel": "Line"}, {"FieldId": "s2", "CustomLabel": "Lowest value"},
                {"FieldId": "s3", "CustomLabel": "Highest value"}], "Order": ["s1", "s2", "s3"]}},
    }}

    plan_table = {"TableVisual": {
        "VisualId": "plan-table", "Title": visual_title("AI recommendation per finding"),
        "ChartConfiguration": {
            "FieldWells": {"TableAggregatedFieldWells": {
                "GroupBy": [dim("r1", findings, "resource_id"), dim("r2", findings, "urgency"), dim("r5", findings, "top_action"),
                            dim("r6", findings, "recommendation_source"), dim("r7", findings, "model_name"),
                            dim("r3", findings, "summary")],
                "Values": []}},
            "FieldOptions": {"SelectedFieldOptions": [
                {"FieldId": "r1", "Width": "190px"}, {"FieldId": "r2", "Width": "90px"}, {"FieldId": "r5", "Width": "330px"},
                {"FieldId": "r6", "Width": "110px"}, {"FieldId": "r7", "Width": "110px"}, {"FieldId": "r3", "Width": "700px"}],
                "Order": ["r1", "r2", "r5", "r6", "r7", "r3"]},
        },
    }}
    history_bar = {"BarChartVisual": {
        "VisualId": "history-bar", "Title": visual_title("Findings over time"),
        "ChartConfiguration": {"FieldWells": {"BarChartAggregatedFieldWells": {
            "Category": [date_dim("h1", history, "finding_time", "DAY")],
            "Values": [count_measure("h2", history, "resource_id")], "Colors": [dim("h3", history, "urgency")]}},
            "Orientation": "VERTICAL", "BarsArrangement": "STACKED",
            "VisualPalette": {"ColorMap": [{"Element": {"FieldId": "h3", "FieldValue": k}, "Color": v} for k, v in URGENCY_COLORS.items()]}},
    }}
    delivery_donut = {"PieChartVisual": {
        "VisualId": "delivery-donut", "Title": visual_title("Alert delivery"),
        "ChartConfiguration": {"FieldWells": {"PieChartAggregatedFieldWells": {
            "Category": [dim("d1", history, "delivery")], "Values": [count_measure("d2", history, "resource_id")]}},
            "DonutOptions": {"ArcOptions": {"ArcThickness": "MEDIUM"}}},
    }}
    source_bar = {"BarChartVisual": {
        "VisualId": "source-bar", "Title": visual_title("Who wrote the plan (model and source)"),
        "ChartConfiguration": {"FieldWells": {"BarChartAggregatedFieldWells": {
            "Category": [dim("m1", history, "model_name")], "Values": [count_measure("m2", history, "resource_id")],
            "Colors": [dim("m3", history, "recommendation_source")]}}, "Orientation": "HORIZONTAL"},
    }}

    def layout(*cells):
        return [{"Configuration": {"GridLayout": {"Elements": [
            {"ElementId": vid, "ElementType": "VISUAL", "ColumnIndex": c, "ColumnSpan": w, "RowSpan": h}
            for vid, c, w, h in cells]}}}]

    critical_filter = {"CategoryFilter": {
        "FilterId": "only-critical", "Column": col(findings, "urgency"),
        "Configuration": {"FilterListConfiguration": {"MatchOperator": "CONTAINS", "CategoryValues": ["CRITICAL"]}}}}
    sheets = [
        {"SheetId": "risk", "Name": "Risk board",
         "Visuals": [kpi_critical, kpi_runway, urgency_donut, runway_bar, risk_table],
         "Layouts": layout(("kpi-critical", 0, 6, 6), ("kpi-runway", 6, 6, 6), ("urgency-donut", 12, 12, 6),
                           ("runway-bar", 0, 12, 10), ("risk-table", 12, 24, 10))},
        {"SheetId": "trend", "Name": "Trend and forecast",
         "Visuals": [forecast_line, series_details],
         "ParameterControls": [{"Dropdown": {
             "ParameterControlId": "series-control", "Title": "Resource / metric", "SourceParameterName": "SeriesLabel",
             "Type": "SINGLE_SELECT", "SelectableValues": {"LinkToDataSetColumn": col(series, "series_label")}}}],
         "Layouts": layout(("forecast-line", 0, 26, 14), ("series-details", 26, 10, 14))},
        {"SheetId": "plans", "Name": "Recommendations",
         "Visuals": [plan_table, history_bar, delivery_donut, source_bar],
         "Layouts": layout(("plan-table", 0, 36, 12), ("history-bar", 0, 14, 10), ("delivery-donut", 14, 10, 10), ("source-bar", 24, 12, 10))},
    ]
    scope = {"SelectedSheets": {"SheetVisualScopingConfigurations": [
        {"SheetId": "risk", "Scope": "SELECTED_VISUALS", "VisualIds": ["kpi-critical"]}]}}
    series_filter = {"CategoryFilter": {
        "FilterId": "selected-series", "Column": col(series, "series_label"),
        "Configuration": {"CustomFilterConfiguration": {
            "MatchOperator": "EQUALS", "ParameterName": "SeriesLabel", "NullOption": "NON_NULLS_ONLY"}}}}
    trend_scope = {"SelectedSheets": {"SheetVisualScopingConfigurations": [{"SheetId": "trend", "Scope": "ALL_VISUALS"}]}}
    parameter = {"StringParameterDeclaration": {
        "ParameterValueType": "SINGLE_VALUED", "Name": "SeriesLabel",
        "DefaultValues": {"StaticValues": [default_series or "none"]},
        "ValueWhenUnset": {"ValueWhenUnsetOption": "RECOMMENDED_VALUE"}}}
    return {"DataSetIdentifierDeclarations": [], "Sheets": sheets, "ParameterDeclarations": [parameter],
            "FilterGroups": [
                {"FilterGroupId": "critical-only", "Filters": [critical_filter],
                 "ScopeConfiguration": scope, "Status": "ENABLED", "CrossDataset": "SINGLE_DATASET"},
                {"FilterGroupId": "selected-series", "Filters": [series_filter],
                 "ScopeConfiguration": trend_scope, "Status": "ENABLED", "CrossDataset": "SINGLE_DATASET"}]}


def most_urgent_series(session, stack_name: str) -> str:
    """The 'resource / metric' label of the most urgent current finding, used as the dashboard's default."""
    cfn, athena = session.client("cloudformation"), session.client("athena")
    try:
        out = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["Outputs"]}
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ValidationError" and "does not exist" in str(exc):
            raise SystemExit(f"Error: CloudFormation stack '{stack_name}' does not exist.\n"
                           f"Deploy the stack first using: ./scripts/deploy.sh\n"
                           f"Or specify a different stack name with --stack-name")
        raise
    database = out["GlueDatabase"]
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", database):  # an identifier cannot be a bound parameter in Athena
        raise SystemExit(f"unexpected Glue database name {database!r}")
    sql = (f"SELECT concat(resource_id, ' / ', metric_name) FROM {database}.v_latest_findings "  # nosec B608
           "WHERE runway_hours > 0 ORDER BY urgency_rank DESC, runway_hours ASC LIMIT 1")
    qid = athena.start_query_execution(QueryString=sql, WorkGroup=out["AthenaWorkGroupName"])["QueryExecutionId"]
    for _ in range(40):
        state = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)
    if state != "SUCCEEDED":
        return ""
    rows = athena.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"]
    return rows[1]["Data"][0].get("VarCharValue", "") if len(rows) > 1 else ""


def declarations(account: str, region: str) -> list[dict]:
    base = f"arn:aws:quicksight:{region}:{account}:dataset"
    return [{"Identifier": "findings", "DataSetArn": f"{base}/cf-latest-findings"},
            {"Identifier": "series", "DataSetArn": f"{base}/cf-latest-series"},
            {"Identifier": "history", "DataSetArn": f"{base}/cf-finding-history"}]


def wait_for(describe, label: str) -> dict:
    """Poll until a QuickSight object leaves its IN_PROGRESS state; raise with the service's errors on failure."""
    for _ in range(80):
        obj = describe()
        status = obj["Status"]
        if status.endswith("IN_PROGRESS"):
            time.sleep(3)
            continue
        if status.endswith("FAILED"):
            errors = "; ".join(f"{e['Type']}: {e['Message']}" for e in obj.get("Errors") or [])
            raise SystemExit(f"{label} {status}: {errors}")
        return obj
    raise SystemExit(f"{label}: timed out")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--region", default=None)
    p.add_argument("--stack-name", default="capacity-foresight")
    p.add_argument("--default-series", help="series shown first on the trend sheet (default: the most urgent finding)")
    p.add_argument("--principal", help="QuickSight user ARN that owns the objects (default: the only registered user)")
    args = p.parse_args(argv)

    session = boto3.Session(region_name=args.region)
    region = session.region_name

    # Validate AWS credentials before proceeding
    try:
        account = session.client("sts").get_caller_identity()["Account"]
    except NoCredentialsError:
        print("Error: No AWS credentials found.", file=sys.stderr)
        print("Configure credentials using one of:", file=sys.stderr)
        print("  - aws configure", file=sys.stderr)
        print("  - aws sso login", file=sys.stderr)
        print("  - Export AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY", file=sys.stderr)
        return 1
    except CredentialRetrievalError as e:
        print(f"Error: Failed to retrieve AWS credentials: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        # Handle MissingDependencyException for SSO login without CRT
        if MissingDependencyException and isinstance(e, MissingDependencyException):
            print("Error: Missing dependency for AWS SSO login.", file=sys.stderr)
            print("Install the CRT dependency with:", file=sys.stderr)
            print('  pip install "botocore[crt]"', file=sys.stderr)
            print("Or use a different credential method (access keys, instance profile).", file=sys.stderr)
            return 1
        if "MissingDependencyException" in type(e).__name__:
            print("Error: Missing dependency for AWS SSO login.", file=sys.stderr)
            print("Install the CRT dependency with:", file=sys.stderr)
            print('  pip install "botocore[crt]"', file=sys.stderr)
            print("Or use a different credential method (access keys, instance profile).", file=sys.stderr)
            return 1
        raise

    qs = session.client("quicksight")

    # Verify QuickSight access and check for region mismatch
    try:
        qs.describe_account_subscription(AwsAccountId=account)
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        error_msg = exc.response["Error"]["Message"]
        if error_code == "AccessDeniedException" and "identity region" in error_msg:
            # Extract the correct region from the error message
            match = re.search(r"use the (\S+) endpoint", error_msg)
            correct_region = match.group(1) if match else "us-east-1"
            print(f"Error: QuickSight identity region mismatch.", file=sys.stderr)
            print(f"Your QuickSight identity is in {correct_region}, but you're using {region}.", file=sys.stderr)
            print(f"Run again with: --region {correct_region}", file=sys.stderr)
            return 1
        print("Error: This account has no QuickSight subscription.", file=sys.stderr)
        print("Subscribe first (see docs/PREREQUISITES.md).", file=sys.stderr)
        return 1

    principal = args.principal
    if not principal:
        users = qs.list_users(AwsAccountId=account, Namespace="default")["UserList"]
        if len(users) != 1:
            raise SystemExit("Pass --principal: found %d QuickSight users." % len(users))
        principal = users[0]["Arn"]

    default_series = args.default_series if args.default_series is not None else most_urgent_series(session, args.stack_name)
    print(f"trend sheet default series: {default_series or '(none yet)'}")
    defn = definition(default_series)
    defn["DataSetIdentifierDeclarations"] = declarations(account, region)

    try:
        qs.describe_analysis(AwsAccountId=account, AnalysisId=ANALYSIS_ID)
        qs.update_analysis(AwsAccountId=account, AnalysisId=ANALYSIS_ID, Name="Capacity Foresight", Definition=defn)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        qs.create_analysis(AwsAccountId=account, AnalysisId=ANALYSIS_ID, Name="Capacity Foresight", Definition=defn,
                           Permissions=[{"Principal": principal, "Actions": ANALYSIS_ACTIONS}])
    time.sleep(3)
    analysis = wait_for(lambda: qs.describe_analysis(AwsAccountId=account, AnalysisId=ANALYSIS_ID)["Analysis"], "analysis")
    print(f"analysis {ANALYSIS_ID}: {analysis['Status']}")

    try:
        qs.describe_dashboard(AwsAccountId=account, DashboardId=DASHBOARD_ID)
        qs.update_dashboard(AwsAccountId=account, DashboardId=DASHBOARD_ID, Name="Capacity Foresight", Definition=defn)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        qs.create_dashboard(AwsAccountId=account, DashboardId=DASHBOARD_ID, Name="Capacity Foresight", Definition=defn,
                            Permissions=[{"Principal": principal, "Actions": DASHBOARD_ACTIONS}])
    # describe_dashboard returns the PUBLISHED version, so read the newest one from the version list.
    time.sleep(3)
    newest = max(qs.list_dashboard_versions(AwsAccountId=account, DashboardId=DASHBOARD_ID)["DashboardVersionSummaryList"],
                 key=lambda v: v["VersionNumber"])["VersionNumber"]

    def version_state() -> dict:
        return qs.describe_dashboard(AwsAccountId=account, DashboardId=DASHBOARD_ID, VersionNumber=newest)["Dashboard"]["Version"]

    d = wait_for(version_state, "dashboard")
    version = newest
    qs.update_dashboard_published_version(AwsAccountId=account, DashboardId=DASHBOARD_ID, VersionNumber=version)
    print(f"dashboard {DASHBOARD_ID}: version {version} published")
    print(f"open: https://{region}.quicksight.aws.amazon.com/sn/dashboards/{DASHBOARD_ID}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
