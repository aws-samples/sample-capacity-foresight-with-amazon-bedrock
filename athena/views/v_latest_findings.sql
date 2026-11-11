-- One row per (resource, metric): the most recent finding, with typed columns for QuickSight.
-- __DB__ is replaced by scripts/create_views.sh.
CREATE OR REPLACE VIEW __DB__.v_latest_findings AS
SELECT
  run_id,
  CAST(from_iso8601_timestamp(finding_ts) AS timestamp) AS finding_time,
  date_diff('hour', CAST(from_iso8601_timestamp(finding_ts) AS timestamp), current_timestamp) AS hours_since_seen,
  account_id,
  region,
  resource_type,
  resource_id,
  metric_name,
  direction,
  threshold,
  status,
  current_value,
  projected_value_at_horizon,
  horizon_hours,
  runway_hours,
  runway_earliest_hours,
  runway_latest_hours,
  runway_hours / 24.0 AS runway_days,
  slope_per_day,
  r2,
  n_points,
  accelerating,
  level_shift,
  urgency,
  CASE urgency WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END AS urgency_rank,
  confidence,
  summary,
  probable_cause,
  top_action,
  actions,
  recommendation_source,
  model_id,
  COALESCE(NULLIF(regexp_extract(model_id, 'claude-(.+)$', 1), ''), model_id) AS model_name,
  route,
  delivery,
  notified
FROM (
  SELECT f.*,
         row_number() OVER (
           PARTITION BY account_id, region, resource_type, resource_id, metric_name
           ORDER BY finding_ts DESC
         ) AS rn
  FROM __DB__.capacity_findings f
  WHERE dt >= date_format(current_date - INTERVAL '30' DAY, '%Y-%m-%d')
)
WHERE rn = 1
