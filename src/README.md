# Source Code

This folder contains the core application code for Capacity Foresight, organized into three directories:

```
src/
├── capacity_foresight/   # Core library modules
├── config/               # Configuration files
└── handlers/             # Lambda function entry points
```

## Architecture Overview

The solution runs as a Step Functions workflow with four Lambda steps:

```
┌─────────┐    ┌─────────┐    ┌───────────┐    ┌────────┐
│ Discover│ -> │ Analyze │ -> │ Recommend │ -> │ Notify │
└─────────┘    └─────────┘    └───────────┘    └────────┘
    │              │               │               │
    │              │               │               │
    v              v               v               v
  Tag API      CloudWatch      Bedrock          SNS
                  S3           DynamoDB          S3
```

## Configuration (`config/`)

### thresholds.json

Defines which metrics to monitor and when to alert. This is the primary configuration file for tuning the forecasting behavior.

**Structure:**

```json
{
  "version": "1.0.0",
  "defaults": { ... },
  "metrics": [ ... ]
}
```

**Default Settings:**

| Setting | Default | Description |
|---------|---------|-------------|
| `lookback_days` | 28 | Days of history to analyze (max 90) |
| `min_points` | 7 | Minimum daily points required for forecasting |
| `min_r2` | 0.3 | Minimum R² for trend confidence (0-1) |
| `weekly_seasonality` | true | Remove weekly patterns from the trend |
| `detect_level_shift` | true | Detect and trim data after resizes/failovers |
| `interval_z` | 1.28 | Z-score for confidence interval (~80%) |
| `urgency_bands_hours` | `{CRITICAL: 24, HIGH: 48, MEDIUM: 96}` | Runway thresholds for urgency levels |

**Metric Rules:**

Each metric rule defines:

| Field | Description |
|-------|-------------|
| `resource_type` | EC2, RDS, or EBS |
| `metric_name` | CloudWatch metric name |
| `threshold` | Value that triggers an alert |
| `unit` | Percent, Bytes, or None |
| `direction` | ABOVE (rising toward threshold) or BELOW (falling toward threshold) |
| `horizon_hours` | How far ahead to forecast (1-720 hours) |

**Built-in Rules:**

| Resource | Metric | Threshold | Direction | Horizon |
|----------|--------|-----------|-----------|---------|
| EC2 | CPUUtilization | 85% | ABOVE | 72h |
| RDS | CPUUtilization | 85% | ABOVE | 72h |
| RDS | FreeStorageSpace | 10 GiB | BELOW | 168h |
| RDS | FreeableMemory | 512 MiB | BELOW | 48h |
| EBS | BurstBalance | 20% | BELOW | 24h |

### Updating the Configuration

After modifying `thresholds.json`, redeploy the stack to apply the changes:

```bash
./scripts/deploy.sh
```

The `thresholds.json` file is bundled inside the Lambda deployment package. When you run `deploy.sh`, it packages the updated configuration, uploads it to S3, and updates the Lambda functions via CloudFormation.

Make sure your environment variables are set:

```bash
export AWS_REGION=us-east-1
export NOTIFICATION_EMAIL=your-email@example.com
export ARTIFACTS_BUCKET=your-artifacts-bucket
```

**Validate before deploying:** You can check your changes locally before deploying:

```bash
python -c "from capacity_foresight.config import load_config; load_config()"
```

This raises a `ConfigError` if the JSON structure or values are invalid.

## Lambda Handlers (`handlers/`)

Each handler is a Lambda function entry point that performs one step of the workflow.

### discover.py — Step 1: Resource Discovery

Finds resources to evaluate using the Resource Groups Tagging API.

**Input:**
```json
{
  "demo": false  // Optional: use synthetic resources instead
}
```

**Output:**
```json
{
  "run_id": "20250101T120000Z-abc123",
  "demo": false,
  "resources": [
    {"resource_type": "EC2", "resource_id": "i-0123...", "arn": "arn:aws:ec2:..."}
  ]
}
```

**Behavior:**
- Queries for resources tagged with `capacity-foresight=monitor` (configurable)
- Supports EC2 instances, RDS DB instances, and EBS volumes
- Respects `MaxResources` limit (default 500)
- Demo mode returns synthetic resources for testing

### analyze.py — Step 2: Metric Analysis

Forecasts each metric for one resource and detects predicted breaches.

**Input:**
```json
{
  "run_id": "...",
  "demo": false,
  "resource": {"resource_type": "RDS", "resource_id": "mydb", "arn": "..."}
}
```

**Output:**
```json
{
  "findings": [
    {
      "resource_id": "mydb",
      "metric_name": "FreeStorageSpace",
      "status": "BREACH_PREDICTED",
      "runway_hours": 48.5,
      "urgency": "HIGH",
      ...
    }
  ],
  "metric_errors": 0
}
```

**Pipeline:**
1. Fetch 28 days of hourly data from CloudWatch
2. Reduce to daily extremes (max for ABOVE, min for BELOW)
3. Detect and trim after level shifts (resizes/failovers)
4. Remove weekly seasonality when enough data exists
5. Fit OLS trend line with uncertainty
6. Project forward and calculate runway to threshold
7. Assign urgency based on runway hours

**Status Values:**
- `OK` — No breach predicted within horizon
- `BREACH_PREDICTED` — Threshold crossing expected
- `BREACHED` — Already past threshold
- `INSUFFICIENT_DATA` — Not enough history
- `LOW_CONFIDENCE` — Trend R² below minimum

### recommend.py — Step 3: AI Recommendations

Calls Amazon Bedrock to generate a remediation plan based on the finding and resource configuration.

**Input:**
```json
{
  "demo": false,
  "finding": { ... }  // From analyze step
}
```

**Output:**
```json
{
  "finding": { ... },
  "recommendation": {
    "summary": "...",
    "urgency": "HIGH",
    "actions": [ ... ],
    ...
  },
  "recommendation_source": "BEDROCK",  // or "CACHE", "FALLBACK"
  "model_id": "anthropic.claude-sonnet-4-5-20250929-v1:0",
  "route": "orders-team"
}
```

**Features:**
- Collects resource facts (instance type, storage, tags, etc.)
- Optionally enriches with Compute Optimizer, RDS events, CloudTrail changes
- Caches recommendations to avoid redundant model calls
- Falls back through multiple models if primary fails
- Never loses an alert—uses a plain fallback if all models fail

### notify.py — Step 4: Alert Delivery

Sends email alerts and records findings for the dashboard.

**Input:**
```json
{
  "finding": { ... },
  "recommendation": { ... },
  "recommendation_source": "BEDROCK",
  "model_id": "...",
  "route": "orders-team"
}
```

**Output:**
```json
{
  "notified": true,
  "delivery": "immediate"  // or "suppressed", "digest_only"
}
```

**Delivery Modes:**
- `immediate` — Email sent now
- `suppressed` — Duplicate within suppression window
- `digest_only` — Below urgency threshold for immediate alerts

### digest.py — Daily Summary

Sends a daily digest email summarizing all recent findings. Runs on a schedule, not as part of the main workflow.

**Features:**
- Collects findings from S3 within the last 24 hours
- Groups by route (team) if `DigestPerRoute` is enabled
- Ranks by urgency, then runway
- Includes AI-generated summaries

## Core Library (`capacity_foresight/`)

### config.py — Configuration Loading

Parses and validates `thresholds.json` and reads runtime settings from environment variables.

**Key Classes:**

| Class | Purpose |
|-------|---------|
| `MetricRule` | One metric to monitor (type, threshold, direction, horizon) |
| `Defaults` | Forecasting parameters (lookback, min_r2, seasonality, urgency bands) |
| `ThresholdConfig` | Complete parsed configuration |
| `Settings` | Runtime settings from Lambda environment variables |

**Runtime Settings (from environment):**

| Variable | Description |
|----------|-------------|
| `DATA_BUCKET` | S3 bucket for findings and series data |
| `SUPPRESSION_TABLE` | DynamoDB table for deduplication |
| `TOPIC_ARN` | SNS topic for alerts |
| `BEDROCK_MODEL_ID` | Primary model ID |
| `BEDROCK_FALLBACK_MODEL_ID` | Fallback model ID |
| `BEDROCK_HIGH_RISK_MODEL_ID` | Model for CRITICAL/HIGH findings |
| `DISCOVERY_TAG_KEY` / `DISCOVERY_TAG_VALUE` | Tag for resource discovery |
| `CONTEXT_TAG_KEYS` | Tags to include in model context |
| `ROUTING_TAG_KEY` | Tag for team-based alert routing |
| `ENRICHMENT_SOURCES` | Enabled enrichment sources |
| `RECOMMENDATION_CACHE_HOURS` | Cache TTL (0 to disable) |
| `MIN_IMMEDIATE_URGENCY` | Minimum urgency for immediate alerts |

### discovery.py — Resource Discovery

Uses the Resource Groups Tagging API to find monitored resources.

```python
from capacity_foresight.discovery import discover

resources = discover(tagging_client, "capacity-foresight", "monitor", limit=500)
# Returns: [{"resource_type": "EC2", "resource_id": "i-...", "arn": "..."}]
```

**Supported Resource Types:**
- `ec2:instance` → EC2
- `rds:db` → RDS
- `ec2:volume` → EBS

### metrics.py — CloudWatch Data Fetching

Fetches hourly metric data from CloudWatch.

```python
from capacity_foresight.metrics import fetch_hourly

points = fetch_hourly(cloudwatch_client, rule, "i-0123...", lookback_days=28)
# Returns: [(datetime, value), ...]
```

**Behavior:**
- Uses Maximum statistic for ABOVE metrics (captures peaks)
- Uses Minimum statistic for BELOW metrics (captures lows)
- Returns hourly datapoints sorted by time

### forecast.py — Trend Forecasting

The core forecasting engine. Deliberately simple and explainable—the model handles "what to do," not the numbers.

```python
from capacity_foresight.forecast import assess

result = assess(rule, config, hourly_points, now)
# Returns Assessment with status, runway, urgency, etc.
```

**Pipeline:**
1. **Daily extremes** — Collapse hourly to one value per day (max/min based on direction)
2. **Level shift detection** — Drop data before resize/failover events
3. **Weekly seasonality** — Remove per-weekday patterns if enough data
4. **OLS fit** — Linear regression with standard error on slope
5. **Projection** — Forecast to horizon, adding worst-weekday seasonality back
6. **Runway calculation** — Hours to breach with uncertainty range

**Assessment Fields:**

| Field | Description |
|-------|-------------|
| `status` | OK, BREACH_PREDICTED, BREACHED, INSUFFICIENT_DATA, LOW_CONFIDENCE |
| `runway_hours` | Central estimate of hours to breach |
| `runway_earliest_hours` | Lower bound considering uncertainty |
| `runway_latest_hours` | Upper bound (None if may never breach) |
| `urgency` | CRITICAL, HIGH, MEDIUM, or LOW |
| `slope_per_day` | Trend rate (positive = increasing) |
| `r2` | Trend fit quality (0-1) |
| `accelerating` | True if recent trend is faster than long-run |
| `level_shift` | True if a resize/failover was detected |
| `seasonal` | True if weekly pattern was removed |

### facts.py — Resource Configuration

Collects real configuration from AWS APIs for model context.

```python
from capacity_foresight.facts import collect_facts

facts = collect_facts(clients, "RDS", "mydb", context_keys=("Name",), route_key="Team")
```

**Collected Facts by Type:**

| EC2 | RDS | EBS |
|-----|-----|-----|
| instance_type | db_instance_class | volume_type |
| state | engine/version | size_gib |
| availability_zone | allocated_storage_gib | iops |
| architecture | max_allocated_storage_gib | throughput_mibps |
| ebs_optimized | storage_type/iops | state |
| auto_scaling_group | multi_az | attached_instance_ids |
| tags | pending_modifications | tags |
| | read_replica_count | |

### enrichment.py — Optional AWS Signals

Adds extra context from AWS services (all optional and best-effort).

```python
from capacity_foresight.enrichment import collect, parse_sources

enabled = parse_sources("compute_optimizer,rds_events,cloudtrail_changes")
signals = collect(clients, enabled, finding)
```

**Available Sources:**

| Source | What It Provides | Setup Required |
|--------|------------------|----------------|
| `compute_optimizer` | Rightsizing findings and candidate configurations | Opt-in to Compute Optimizer |
| `rds_events` | Recent RDS event messages (7 days) | None |
| `cloudtrail_changes` | Recent write API calls (names and times only) | None |

### recommender.py — AI Recommendations

Calls Amazon Bedrock with the Converse API to generate remediation plans.

```python
from capacity_foresight.recommender import recommend

rec, source, model_used = recommend(
    bedrock_client, model_ids, finding, facts, enrichment,
    guardrail_id, guardrail_version, deadline
)
```

**Action Types:**

| Action | Description |
|--------|-------------|
| `INCREASE_STORAGE` | Grow allocated storage or volume size |
| `ENABLE_STORAGE_AUTOSCALING` | Turn on RDS storage autoscaling |
| `RESIZE_INSTANCE` | Move to a larger instance class |
| `SCALE_OUT` | Add instances or replicas |
| `ADJUST_AUTOSCALING_POLICY` | Change ASG limits or policies |
| `ADD_READ_REPLICA` | Offload reads to a replica |
| `CHANGE_VOLUME_TYPE` | Switch EBS volume type |
| `INCREASE_VOLUME_PERFORMANCE` | Raise IOPS or throughput |
| `TUNE_WORKLOAD` | Reduce demand through code/query changes |
| `CLEAN_UP_DATA` | Remove unneeded data |
| `IMPROVE_MONITORING` | Add alarms or metrics |
| `INVESTIGATE` | Find out more before acting |
| `NO_ACTION` | Nothing is needed |

**Guardrails:**
- Output validated against schema (enum types, bounded lengths)
- Urgency floor enforced (model cannot lower it)
- URLs stripped from model text
- Untrusted data delimited in prompt
- Falls back through multiple models
- Plain notice sent if all models fail

### notifier.py — Alert Delivery

Handles email sending and deduplication.

```python
from capacity_foresight.notifier import claim, publish

if claim(ddb, table, finding, urgency, ttl_days=7):
    publish(sns, topic_arn, finding, recommendation, source, route)
```

**Features:**
- Atomic deduplication via DynamoDB
- Suppression window (default 7 days)
- Re-alerts only if urgency worsens
- Clears suppression when metric returns to healthy
- SNS message attributes for filter policies (route, urgency)

#### Alert Suppression

The solution uses DynamoDB to prevent duplicate alerts for the same finding. This is automatic and time-based—there is no manual suppression or unsuppression mechanism.

**How It Works:**

1. **Claiming an alert** — Before sending an email, the notify handler calls `claim()` which atomically checks DynamoDB. An alert is sent only if:
   - No previous alert exists for this finding (first occurrence)
   - The previous alert's suppression window has expired
   - The urgency has increased (e.g., HIGH → CRITICAL)

2. **Suppression window** — Controlled by the `SuppressionDays` stack parameter (default: 7 days). An unchanged finding is emailed at most once in this period. The TTL is stored as `expires_at` on the DynamoDB item.

3. **Automatic resolution** — When the analyze step detects a metric has returned to a healthy state (`status=OK`), it calls `resolve()` to clear the suppression record. This allows the solution to alert again if the problem recurs, even within the suppression window. Resolution only happens if the alert is at least 24 hours old (the `RESOLVE_AFTER_HOURS` constant) to avoid alert storms from flapping metrics.

4. **Escalation re-alerts** — If urgency worsens (e.g., runway shortens from 72h to 20h, moving from MEDIUM to CRITICAL), a new alert is sent regardless of the suppression window.

**Suppression Key:**

Each finding is identified by a SHA-256 hash of:
- Account ID
- Region
- Resource type
- Resource ID
- Metric name

This means the same resource with different metrics (e.g., CPU and storage) track separately.

**DynamoDB Item Structure:**

| Attribute | Type | Description |
|-----------|------|-------------|
| `pk` | String | SHA-256 hash of the finding identity |
| `urgency_rank` | Number | Numeric rank (0=CRITICAL, 3=LOW) for escalation comparison |
| `resource_id` | String | For debugging/inspection |
| `metric_name` | String | For debugging/inspection |
| `notified_at` | Number | Unix timestamp when the alert was sent |
| `expires_at` | Number | Unix timestamp when the record expires (TTL attribute) |

**Delivery Status:**

The notify handler returns a `delivery` field indicating what happened:

| Status | Meaning |
|--------|---------|
| `immediate` | Alert was sent now |
| `suppressed` | Duplicate within suppression window, not sent |
| `digest_only` | Below `MinImmediateUrgency`, appears only in daily digest |

**Viewing Suppressed Findings:**

Suppressed findings are still recorded to S3 and visible in the `v_latest_findings` Athena view and QuickSight dashboard. The `delivery` column shows whether each finding was `immediate`, `suppressed`, or `digest_only`.

**Configuration:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SuppressionDays` | 7 | How long to suppress duplicate alerts |
| `MinImmediateUrgency` | LOW | Findings below this appear only in digest |

### cache.py — Recommendation Caching

Caches AI recommendations to avoid redundant model calls.

```python
from capacity_foresight.cache import cache_key, get, put

key = cache_key(finding, facts, enrichment, variant)
hit = get(ddb, table, key, floor_urgency)
if not hit:
    # Call model...
    put(ddb, table, key, recommendation, model_id, ttl_hours)
```

**Cache Key Includes:**
- Resource identity (account, region, type, id)
- Metric name and status
- Minimum urgency
- Resource facts and enrichment signals
- Prompt fingerprint and model list

### store.py — S3 Storage

Writes JSON Lines to S3 in the Athena-compatible layout.

```python
from capacity_foresight.store import object_key, write_jsonl

key = object_key("findings", run_id, discriminator)  # findings/dt=2025-01-01/...jsonl
write_jsonl(s3, bucket, key, rows)
```

### digest.py — Daily Digest Logic

Collects recent findings and formats digest emails.

```python
from capacity_foresight.digest import collect_recent, group_by_route, format_digest

rows = collect_recent(s3, bucket, now, hours=24)
groups = group_by_route(rows)
for route, findings in groups.items():
    subject, body = format_digest(findings, 24, route)
```

### demo.py — Synthetic Data

Provides synthetic resources and metric data for testing without real AWS resources.

```python
from capacity_foresight.demo import demo_resources, demo_facts, demo_hourly

resources = demo_resources()  # Returns 3 demo resources
facts = demo_facts("demo-rds-orders")
points = demo_hourly(rule, "demo-rds-orders", lookback_days, now)
```

**Demo Resources:**

| ID | Type | Scenario |
|----|------|----------|
| `demo-rds-orders` | RDS | Storage shrinking toward threshold |
| `demo-ec2-web` | EC2 | CPU rising with weekly pattern |
| `demo-rds-reporting` | RDS | Healthy, flat metrics |

### aws.py — Boto3 Client Management

Provides cached boto3 clients with sensible retry and timeout settings.

```python
from capacity_foresight.aws import client, resource_clients

cw = client("cloudwatch")  # Cached, with retries
clients = resource_clients()  # {"ec2": ..., "rds": ..., "autoscaling": ...}
```

**Timeout Configuration:**
- Standard services: 5s connect, 30s read, 5 retries
- Bedrock: 5s connect, 45s read, 2 attempts

## Adding Custom Metrics

To monitor a custom metric (e.g., memory from CloudWatch Agent):

1. **Add a rule to `config/thresholds.json`:**
```json
{
  "resource_type": "EC2",
  "metric_name": "mem_used_percent",
  "threshold": 90,
  "unit": "Percent",
  "direction": "ABOVE",
  "horizon_hours": 48
}
```

2. **Update the namespace/dimension mappings** in `metrics.py` if using a custom namespace.

3. **Tag resources** with `capacity-foresight=monitor`.

4. **Redeploy** the stack to pick up the configuration change.

## Testing

Run unit tests from the repository root:

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

Use `scripts/replay.py` to test forecasting on saved or live data without sending alerts:

```bash
python scripts/replay.py --live --resource-id i-0123... --resource-type EC2 --metric CPUUtilization
```
