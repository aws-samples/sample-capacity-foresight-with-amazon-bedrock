-- Every finding ever recorded (within retention), for trend and audit visuals.
CREATE OR REPLACE VIEW __DB__.v_finding_history AS
SELECT
  run_id,
  CAST(from_iso8601_timestamp(finding_ts) AS timestamp) AS finding_time,
  account_id, region, resource_type, resource_id, metric_name,
  status, urgency, confidence, runway_hours, runway_earliest_hours, r2,
  recommendation_source, model_id,
  COALESCE(NULLIF(regexp_extract(model_id, 'claude-(.+)$', 1), ''), model_id) AS model_name, route, delivery, notified, top_action
FROM __DB__.capacity_findings
