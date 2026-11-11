-- Observed daily extremes and the forecast line from the most recent run of each series, plus the threshold
-- as a third "kind" so a chart can draw all three lines from one value column (one value field per colour).
CREATE OR REPLACE VIEW __DB__.v_latest_series AS
WITH latest AS (
  SELECT
    run_id, account_id, region, resource_type, resource_id, metric_name,
    concat(resource_id, ' / ', metric_name) AS series_label,
    threshold, kind, value,
    CAST(from_iso8601_timestamp(ts) AS timestamp) AS ts
  FROM (
    SELECT s.*,
           max(run_id) OVER (
             PARTITION BY account_id, region, resource_type, resource_id, metric_name
           ) AS latest_run
    FROM __DB__.capacity_series s
    WHERE dt >= date_format(current_date - INTERVAL '7' DAY, '%Y-%m-%d')
  )
  WHERE run_id = latest_run
)
SELECT run_id, account_id, region, resource_type, resource_id, metric_name, series_label, threshold, kind, value, ts
FROM latest
UNION ALL
SELECT run_id, account_id, region, resource_type, resource_id, metric_name, series_label, threshold,
       'threshold' AS kind, threshold AS value, ts
FROM latest
WHERE kind = 'observed'
