# QuickSight dashboard guide

> **Faster path:** `scripts/setup_quicksight.py` and `scripts/create_dashboard.py` create the data source, the three datasets and the whole dashboard described below from code. Use this guide to understand what they build, or to build it by hand.

Why QuickSight: it queries Athena directly, needs no extra servers, and runs on the identity and sharing model you already use for AWS. This guide builds a three-sheet dashboard from the views that `scripts/create_views.sh` created.

> Prerequisite: QuickSight can read the Athena workgroup and the data bucket. See [`docs/PREREQUISITES.md`](../docs/PREREQUISITES.md) section 5.

## 1. Create the Athena data source

1. QuickSight > **Datasets** > **New dataset** > **Athena**.
2. Name: `capacity-foresight`. Workgroup: the `AthenaWorkGroupName` stack output. **Validate connection**, then create.
3. QuickSight needs permission to read the data bucket and the Athena results location. In the console that is **Manage QuickSight > Security & permissions > Manage** (Amazon Athena and the data bucket only). The script avoids this by giving the data source its own role.

## 2. Create three datasets

Choose **Directly query your data** for each (data volume is small, and direct query always shows the latest run).

| Dataset | Database / view | Used for |
|---|---|---|
| `cf_latest_findings` | `capacity_foresight` / `v_latest_findings` | Current risk board and recommendations |
| `cf_latest_series` | `capacity_foresight` / `v_latest_series` | Observed trend, forecast and threshold line |
| `cf_finding_history` | `capacity_foresight` / `v_finding_history` | Findings over time |

In `cf_latest_findings`, check that `finding_time` is a **Date** and `runway_hours` is a **Decimal**.

Add one calculated field to `cf_latest_findings`:

```
is_current = ifelse({hours_since_seen} <= 12, 'Current', 'Resolved or stale')
```

Set the number to roughly twice your schedule interval (6 hour schedule: 12).

## 3. Build the sheets

### Sheet 1: Risk board

| Visual | Type | Fields | Notes |
|---|---|---|---|
| Critical findings | KPI | Count of `resource_id`, with a filter `urgency` = CRITICAL scoped to this visual | A KPI cannot be coloured by a field that is not aggregated, so filter instead |
| Findings by urgency | Donut | Count of `resource_id` grouped by `urgency` | Color CRITICAL red, HIGH orange, MEDIUM yellow, LOW blue |
| Shortest runway | KPI | Min of `runway_hours` | A value of 0 means something has already breached |
| Risk table | Table | `resource_type`, `resource_id`, `metric_name`, `urgency`, `runway_hours`, `runway_earliest_hours`, `runway_latest_hours`, `current_value`, `confidence`, `top_action`, `route` | Sort by `urgency_rank` desc, then `runway_earliest_hours` asc |
| Runway by resource | Horizontal bar | X `runway_hours`, Y `resource_id`, color `urgency` | Turn on data labels: a resource that has already breached has a runway of 0, so its bar has no length and would otherwise look missing |

Filter on `is_current = 'Current'` (sheet-level). Add a filter control on `route` so each team sees its own resources. `accelerating` and `level_shift` arrive as 0/1 integers in QuickSight, so filter on `= 1`.

### Sheet 2: Trend and forecast

| Visual | Type | Fields |
|---|---|---|
| Observed, forecast and threshold | Line chart | X `ts` (day), Value `value` (max), Color `kind` |
| Selected series | Table | `kind` with the min and max of `value` |

The view `v_latest_series` returns three kinds of row: `observed`, `forecast` and `threshold`. Plot all three from the single `value` column and colour by `kind`, so the threshold is drawn as its own line.

Two things that do not work, found while building this dashboard from code:

- **Do not add `threshold` as a second measure.** QuickSight allows only one value field when a colour field is used.
- **Do not use a dynamic reference line for the threshold.** A reference line driven by a data column made the sheet fail with a query error. The threshold-as-a-series approach avoids it.

Add a **parameter** `SeriesLabel` (text) with a **dropdown control** linked to `series_label`, and a filter on both visuals set to "equals" that parameter. Without it, the chart plots every resource on one axis, and bytes (storage) swamp percentages (CPU). Default the parameter to your most urgent series.

### Sheet 3: Recommendations

| Visual | Type | Fields |
|---|---|---|
| AI plan | Table | `resource_id`, `summary`, `probable_cause`, `top_action`, `recommendation_source` |
| Findings over time | Stacked bar | X `finding_time` (day), Value count of `resource_id`, Color `urgency`; dataset `cf_finding_history` |
| Alert delivery | Donut | Count by `delivery` (`immediate`, `suppressed`, `digest_only`) |
| Which model wrote the plan | Bar | Count by `recommendation_source` and `model_id` (`BEDROCK`, `CACHE`, `FALLBACK`) | A rise in `FALLBACK` means the AI layer needs attention |

The full action list (with risk and downtime flags) is stored as JSON in the `actions` column. Show the table row as a text tooltip, or open the email for the formatted version.

## 4. Publish and share

1. **Publish** the analysis as a dashboard.
2. Share with the on-call group, not individuals.
3. Optional: schedule an email report from the dashboard.

## Tips

- Run `./scripts/run_demo.sh` first so every visual has data.
- Datasets use direct query, so Athena charges apply per view. The data is small (one JSON line per datapoint), and the views filter on the `dt` partition.
- Grafana users: use the Athena data source plugin and the same three views.
