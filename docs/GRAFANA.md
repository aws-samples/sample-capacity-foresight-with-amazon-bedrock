# Using Grafana instead of QuickSight

The dashboard layer is deliberately separate from the pipeline. Everything the pipeline produces lands in Amazon S3 as JSON Lines, is exposed through three Athena views, and the dashboard only reads those views. Swap the visualization tool and nothing upstream changes.

> **What has been tested.** Every query below was run against a live Athena workgroup and returns the columns shown. The Grafana side (adding the data source, building panels) has **not** been tested; the steps follow the [Amazon Managed Grafana Athena documentation](https://docs.aws.amazon.com/grafana/latest/userguide/Athena-using-the-data-source.html). The QuickSight path is tested end to end.

## 1. Connect Grafana to Athena

Use either Amazon Managed Grafana or your own Grafana with the [Athena data source plugin](https://grafana.com/docs/plugins/grafana-athena-datasource/latest/).

| Setting | Value (from the stack outputs) |
|---|---|
| Data source | Amazon Athena |
| Region | The stack's Region |
| Database | `GlueDatabase` output (default `capacity_foresight`) |
| Workgroup | `AthenaWorkGroupName` output |
| Output location | Leave empty: the workgroup enforces `s3://<DataBucketName>/athena-results/` |

Give the identity Grafana uses the same least-privilege permissions the QuickSight role gets. `scripts/setup_quicksight.py` prints the exact policy in `role_policy()`: Athena queries on this workgroup only, Glue read on this database only, S3 read on the data bucket and write to `athena-results/` only. Do not attach a broad managed policy if you can avoid it.

## 2. Panels

Each query was run in Athena and returns the columns listed.

**Current findings (table)** `urgency, resource_type, resource_id, metric_name, status, runway_hours, runway_earliest_hours, runway_latest_hours, route, top_action`
```sql
SELECT urgency, resource_type, resource_id, metric_name, status,
       ROUND(runway_hours,1) AS runway_hours,
       ROUND(runway_earliest_hours,1) AS runway_earliest_hours,
       ROUND(runway_latest_hours,1) AS runway_latest_hours, route, top_action
FROM capacity_foresight.v_latest_findings
WHERE hours_since_seen <= 12
ORDER BY urgency_rank DESC, runway_hours ASC
```
Set `hours_since_seen` to about twice your schedule interval so resolved findings drop off.

**Findings by urgency (pie or bar gauge)** `urgency, findings`
```sql
SELECT urgency, count(*) AS findings
FROM capacity_foresight.v_latest_findings WHERE hours_since_seen <= 12 GROUP BY urgency
```

**Shortest runway (stat)** `shortest_runway_hours`
```sql
SELECT min(runway_hours) AS shortest_runway_hours
FROM capacity_foresight.v_latest_findings WHERE hours_since_seen <= 12
```

**Dashboard variable `series`** (query variable) `series_label`
```sql
SELECT DISTINCT series_label FROM capacity_foresight.v_latest_series ORDER BY 1
```

**Observed, forecast and threshold (time series)** `time, observed, forecast, threshold`
```sql
SELECT ts AS time,
       max(CASE WHEN kind = 'observed'  THEN value END) AS observed,
       max(CASE WHEN kind = 'forecast'  THEN value END) AS forecast,
       max(CASE WHEN kind = 'threshold' THEN value END) AS threshold
FROM capacity_foresight.v_latest_series
WHERE series_label = '${series}'
GROUP BY ts ORDER BY ts
```
The wide shape (one column per line) is what Grafana time series panels expect. Style `threshold` as a dashed red line. Replace the literal in the verified query with the `${series}` variable.

**Findings per day (stacked bars)** `time, urgency, findings`
```sql
SELECT date_trunc('day', finding_time) AS time, urgency, count(*) AS findings
FROM capacity_foresight.v_finding_history GROUP BY 1, 2 ORDER BY 1
```

**Alert delivery (pie)** `delivery, alerts`
```sql
SELECT delivery, count(*) AS alerts FROM capacity_foresight.v_finding_history GROUP BY delivery
```

**Which model wrote the plan (bar)** `model_name, recommendation_source, plans`
```sql
SELECT model_name, recommendation_source, count(*) AS plans
FROM capacity_foresight.v_finding_history GROUP BY 1, 2
```

## 3. Things to watch

- **Units.** Storage and memory are in bytes, CPU in percent. Use a separate panel or a per-series unit override rather than mixing them, as the QuickSight version does with its series selector.
- **Query cost.** Each refresh runs Athena queries. Set the dashboard refresh to minutes, not seconds. The workgroup caps a query at 10 GiB scanned, and the views filter on the `dt` partition.
- **Row-level access.** The views include a `route` column. To limit a team to its own resources, filter on it in a Grafana variable or use the permission model of your Grafana edition. This is a convenience, not a security boundary, because the data source itself can read everything.

## 4. Alternatives

The views work with any tool that speaks SQL to Athena: Amazon Managed Grafana, self-hosted Grafana, Amazon QuickSight, or a notebook. Nothing in the pipeline depends on the choice.
