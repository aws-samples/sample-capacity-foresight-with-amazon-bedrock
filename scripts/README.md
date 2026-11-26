# Scripts

This folder contains deployment, demo, maintenance, and utility scripts for Capacity Foresight.

## Quick Reference

| Script | Purpose | When to Use |
|--------|---------|-------------|
| `deploy.sh` | Package and deploy the stack | Initial setup or updates |
| `run_demo.sh` | Run with synthetic resources | Test without real AWS resources |
| `cleanup.sh` | Delete stack and all data | Remove the solution completely |
| `setup_quicksight.py` | Create QuickSight data source | After deploy, before dashboard |
| `create_dashboard.py` | Create QuickSight dashboard | After setup_quicksight |
| `create_views.sh` | Create Athena views | Called by deploy.sh automatically |
| `replay.py` | Test forecasting locally | Tune thresholds before alerting |
| `run_eval.py` | Evaluate AI recommendations | After prompt or model changes |

## Deployment Scripts

### deploy.sh

Packages Lambda code, deploys the CloudFormation stack, and creates Athena views. This is the main deployment entry point.

**Required environment variables:**
- `NOTIFICATION_EMAIL` — Email address to receive capacity alerts
- `ARTIFACTS_BUCKET` — Existing S3 bucket for Lambda code packaging

**Optional environment variables:**
- `STACK_NAME` — Stack name (default: `capacity-foresight`)
- `AWS_REGION` or `AWS_DEFAULT_REGION` — Target AWS Region
- `BEDROCK_MODEL_ID` — Model or inference profile ID to use
- `BEDROCK_FOUNDATION_MODEL_ID` — Base model ID (required if setting BEDROCK_MODEL_ID)
- `EXTRA_PARAMETER_OVERRIDES` — Space-separated `Key=Value` pairs for additional stack parameters

**Usage:**
```bash
export NOTIFICATION_EMAIL=alerts@example.com
export ARTIFACTS_BUCKET=my-artifacts-bucket
export AWS_REGION=us-east-1
./scripts/deploy.sh
```

**With custom model and extra parameters:**
```bash
export BEDROCK_MODEL_ID=anthropic.claude-sonnet-4-5-20250929-v1:0
export BEDROCK_FOUNDATION_MODEL_ID=anthropic.claude-sonnet-4-5-20250929-v1:0
export EXTRA_PARAMETER_OVERRIDES="RoutingTagKey=Team EnableDigest=true"
./scripts/deploy.sh
```

### cleanup.sh

Deletes the CloudFormation stack and empties/deletes the retained S3 buckets (data bucket and access log bucket). **This is destructive and irreversible.**

**Required environment variables:**
- `AWS_REGION` or `AWS_DEFAULT_REGION`

**Optional:**
- `STACK_NAME` (default: `capacity-foresight`)

**Usage:**
```bash
export AWS_REGION=us-east-1
./scripts/cleanup.sh
# Type the stack name when prompted to confirm
```

The script handles versioned buckets by deleting all object versions and delete markers before removing the buckets.

### create_views.sh

Creates the Athena views that QuickSight datasets read from. Automatically called by `deploy.sh`, but can be run separately if views need recreation.

**Required environment variables:**
- `AWS_REGION` or `AWS_DEFAULT_REGION`

**Optional:**
- `STACK_NAME` (default: `capacity-foresight`)

**Usage:**
```bash
export AWS_REGION=us-east-1
./scripts/create_views.sh
```

## Demo and Testing Scripts

### run_demo.sh

Runs the Step Functions workflow once using synthetic demo resources. No real AWS resources are read or modified, but it **does call Amazon Bedrock** (incurs charges) and **sends real emails** to the subscribed notification address.

**Required environment variables:**
- `AWS_REGION` or `AWS_DEFAULT_REGION`

**Optional:**
- `STACK_NAME` (default: `capacity-foresight`)

**Usage:**
```bash
export AWS_REGION=us-east-1
./scripts/run_demo.sh
```

**Expected output:**
- Two alert emails: one for `demo-rds-orders` (storage) and one for `demo-ec2-web` (CPU)
- New rows in the dashboard datasets

Re-running within the suppression window does not send duplicate emails.

### replay.py

Replays the forecasting algorithm on saved or live metric data **without sending any alerts**. Use it to tune thresholds, horizons, and fit settings before enabling alerting.

**From a file (CSV or JSON with `timestamp,value` columns):**
```bash
python scripts/replay.py --file cpu.csv --resource-type EC2 --metric CPUUtilization
```

**Live from CloudWatch (read-only):**
```bash
python scripts/replay.py --live --resource-id i-0123456789abcdef0 \
  --resource-type EC2 --metric CPUUtilization
```

**What-if overrides:**
```bash
python scripts/replay.py --live --resource-id i-0123456789abcdef0 \
  --resource-type EC2 --metric CPUUtilization \
  --threshold 70 --horizon-hours 120 --min-r2 0.5 \
  --no-seasonality --no-level-shift --lookback-days 56
```

**Options:**
| Flag | Description |
|------|-------------|
| `--file PATH` | CSV or JSON file with timestamp,value data |
| `--live` | Read from CloudWatch (mutually exclusive with --file) |
| `--resource-id ID` | Resource ID (required with --live) |
| `--resource-type TYPE` | EC2, RDS, or EBS |
| `--metric NAME` | Metric name (e.g., CPUUtilization) |
| `--config PATH` | Alternative thresholds.json |
| `--threshold FLOAT` | Override threshold value |
| `--horizon-hours INT` | Override forecast horizon |
| `--min-r2 FLOAT` | Override minimum R² for trend fit |
| `--lookback-days INT` | Override lookback period |
| `--no-seasonality` | Disable weekly seasonality detection |
| `--no-level-shift` | Disable level shift detection |

### run_eval.py

Runs a quality check of AI recommendations against golden test cases. Calls Amazon Bedrock with your chosen model, applies automatic validation checks, and writes detailed results to `evals/results/` for human review.

**Usage:**
```bash
python scripts/run_eval.py --model-id anthropic.claude-sonnet-4-5-20250929-v1:0
```

**With guardrail:**
```bash
python scripts/run_eval.py --model-id anthropic.claude-sonnet-4-5-20250929-v1:0 \
  --guardrail-id abc123 --guardrail-version 1
```

**Multiple runs per case (to check consistency):**
```bash
python scripts/run_eval.py --model-id anthropic.claude-sonnet-4-5-20250929-v1:0 --runs 3
```

**Options:**
| Flag | Description |
|------|-------------|
| `--model-id ID` | Model or inference profile ID (required) |
| `--guardrail-id ID` | Bedrock guardrail ID |
| `--guardrail-version V` | Guardrail version |
| `--runs N` | Repeat each case N times (default: 1) |
| `--cases PATH` | Alternative golden cases file (default: `evals/golden_cases.json`) |

**Validation checks performed:**
- Action types are within allowed list
- No more than 5 actions per recommendation
- No URLs in output
- Urgency not below the runway floor
- Expected action types present
- Required terms mentioned
- Forbidden terms absent

## QuickSight Scripts

These scripts set up the optional QuickSight dashboard. Run them **after** deploying the stack.

### setup_quicksight.py

Creates the IAM role, Athena data source, and three datasets needed for the QuickSight dashboard. The IAM role is scoped specifically to this stack's S3 bucket, Athena workgroup, and Glue database.

**Prerequisites:**
- QuickSight subscription in the account
- Stack deployed with Athena views created

**Usage:**
```bash
python scripts/setup_quicksight.py --stack-name capacity-foresight
```

**If QuickSight identity region differs from stack region:**
```bash
python scripts/setup_quicksight.py --stack-name capacity-foresight --region us-east-1
```

**Options:**
| Flag | Description |
|------|-------------|
| `--stack-name NAME` | CloudFormation stack name (default: `capacity-foresight`) |
| `--region REGION` | AWS region (defaults to configured region) |
| `--principal ARN` | QuickSight user ARN to own objects (default: only registered user) |

**Created resources:**
- IAM role: `capacity-foresight-quicksight-athena`
- Data source: `capacity-foresight-athena`
- Datasets: `cf-latest-findings`, `cf-latest-series`, `cf-finding-history`

Safe to re-run — updates existing resources.

### create_dashboard.py

Creates the QuickSight analysis and dashboard with three sheets: Risk Board, Trend and Forecast, and Recommendations.

**Prerequisites:**
- Run `setup_quicksight.py` first

**Usage:**
```bash
python scripts/create_dashboard.py --stack-name capacity-foresight
```

**With specific default series:**
```bash
python scripts/create_dashboard.py --stack-name capacity-foresight \
  --default-series "i-0123456789abcdef0 / CPUUtilization"
```

**Options:**
| Flag | Description |
|------|-------------|
| `--stack-name NAME` | CloudFormation stack name (default: `capacity-foresight`) |
| `--region REGION` | AWS region (defaults to configured region) |
| `--default-series LABEL` | Default series for trend sheet (default: most urgent finding) |
| `--principal ARN` | QuickSight user ARN to own objects |

**Created resources:**
- Analysis: `capacity-foresight-analysis`
- Dashboard: `capacity-foresight-dashboard`

Safe to re-run — updates existing resources.

## Typical Workflow

### Initial Deployment

```bash
# 1. Set required variables
export AWS_REGION=us-east-1
export NOTIFICATION_EMAIL=alerts@example.com
export ARTIFACTS_BUCKET=my-artifacts-bucket

# 2. Deploy the stack
./scripts/deploy.sh

# 3. Confirm the SNS subscription email

# 4. Test with synthetic data
./scripts/run_demo.sh

# 5. (Optional) Set up QuickSight dashboard
python scripts/setup_quicksight.py --stack-name capacity-foresight
python scripts/create_dashboard.py --stack-name capacity-foresight
```

### Tuning Before Production

```bash
# Test forecasting on historical data
python scripts/replay.py --live --resource-id i-0123456789abcdef0 \
  --resource-type EC2 --metric CPUUtilization

# Adjust thresholds and re-test
python scripts/replay.py --live --resource-id i-0123456789abcdef0 \
  --resource-type EC2 --metric CPUUtilization \
  --threshold 75 --horizon-hours 168
```

### Evaluating Model Changes

```bash
# Run evaluation against golden cases
python scripts/run_eval.py --model-id anthropic.claude-sonnet-4-5-20250929-v1:0 --runs 2

# Review results in evals/results/
```

### Cleanup

```bash
export AWS_REGION=us-east-1
./scripts/cleanup.sh
# Also delete QuickSight datasets/analysis and S3 packaging objects manually
```
