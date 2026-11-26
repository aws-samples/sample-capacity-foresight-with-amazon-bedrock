# Prerequisites

Capacity Foresight reads from services you already run. Set these up before you deploy. None of them are created by the stack.

## 1. AWS account and tools

- An AWS account and a Region where Amazon Bedrock and your chosen model are available.
- AWS CLI v2, configured with credentials that can create CloudFormation, IAM, Lambda, Step Functions, S3, DynamoDB, SNS, Glue and Athena resources.
- Python 3.13 with pip (to run the scripts and unit tests locally).
- An existing S3 bucket in the same Region for packaging the Lambda code (`ARTIFACTS_BUCKET`).

```bash
# Verify AWS CLI is installed and configured
aws --version
aws sts get-caller-identity

# Check your current region
aws configure get region

# Verify Python is installed (use python3 on macOS/Linux)
python --version

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install Python dependencies
pip install -r requirements-dev.txt

# If using AWS SSO login, you may also need the CRT dependency:
pip install "botocore[crt]"

export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=$AWS_REGION  # Some SDKs use this instead of AWS_REGION
export ARTIFACTS_BUCKET=my-capacity-foresight-artifacts-$(aws sts get-caller-identity --query Account --output text)

# Create an S3 bucket for artifacts (if you don't have one)
aws s3 mb s3://$ARTIFACTS_BUCKET --region $AWS_REGION

# Or verify an existing bucket
aws s3 ls s3://$ARTIFACTS_BUCKET --region $AWS_REGION
```

## 2. Amazon Bedrock model access

- Choose a model that supports **tool use** with the Converse API. The default is the Anthropic Claude Sonnet 5.5 global inference profile. The recommender adapts to newer models: it drops options a model rejects (a forced tool choice, or `temperature`) and retries once. Check the [supported models table](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference-supported-models-features.html) for your Region.
- Confirm you can invoke the model before deploying (console playground or `aws bedrock-runtime converse`).
- Set `BedrockModelId` to the model ID or inference profile ID you tested, and `BedrockFoundationModelId` to the base model behind it (the same value if you call the model directly). IAM is scoped to exactly these.
- **Plan for model retirement.** Models are retired on a published schedule. Set `BedrockFallbackModelId` (and its `BedrockFallbackFoundationModelId`) to a second model so alerts keep their AI plans if the first stops working, and review the [model lifecycle page](https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html) when you deploy. The `RecommendationFallback` alarm tells you when no model worked.
- Optional: set `BedrockHighRiskModelId` (and its foundation model ID) to use a larger model only for CRITICAL and HIGH findings.
- **Guardrail.** By default the stack creates an [Amazon Bedrock Guardrail](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html) with the prompt-attack filter and applies it to every call. Confirm your model supports guardrails with the Converse API in your Region. Set `CreateGuardrail=false` to bring your own (`GuardrailId`, `GuardrailVersion`) or to run without one. If the guardrail blocks a request, no other model is tried, because the same guardrail would block it. The plain notice is sent and the `GuardrailIntervention` metric is recorded instead of `RecommendationFallback`, so the fallback alarm does not fire.
- Data handling: the model receives the metric trend, the resource's configuration (instance class, storage size, engine, pending modifications and so on), the values of the tag keys you list in `ContextTagKeys`, and the optional signals in section 2a. Review this against your data policies. Resource IDs and tag values are sent; raw metric history, identities and credentials are not.

```bash
# List available Claude models in your region
aws bedrock list-foundation-models --region us-east-1 \
  --query "modelSummaries[?contains(modelId, 'claude')].{modelId:modelId, modelName:modelName}" \
  --output table

# Test the default model (Claude Sonnet 5.5 global inference profile)
aws bedrock-runtime converse \
  --model-id global.anthropic.claude-sonnet-5-5 \
  --messages '[{"role":"user","content":[{"text":"Hello"}]}]' \
  --region us-east-1

# Or test a specific model directly
aws bedrock-runtime converse \
  --model-id anthropic.claude-sonnet-5-5 \
  --messages '[{"role":"user","content":[{"text":"Hello"}]}]' \
  --region us-east-1

# If you get AccessDeniedException, enable model access in the Bedrock console:
# https://console.aws.amazon.com/bedrock/home#/modelaccess

# Set the model environment variables for deployment
export BEDROCK_MODEL_ID=global.anthropic.claude-sonnet-5-5
export BEDROCK_FOUNDATION_MODEL_ID=anthropic.claude-sonnet-5-5

# Optional: set a fallback model for resilience
export BEDROCK_FALLBACK_MODEL_ID=anthropic.claude-sonnet-4-5-20250929-v1:0
export BEDROCK_FALLBACK_FOUNDATION_MODEL_ID=anthropic.claude-sonnet-4-5-20250929-v1:0
```

### 2a. Optional AWS signals (each is independent)

Not every account uses every service, so each signal is a separate switch and everything is best effort. A signal that is off, not set up, or failing is skipped and never blocks an alert.

| Signal | Parameter (default) | What the model gets | Set-up needed |
|---|---|---|---|
| RDS events | `EnableRdsEvents` (`true`) | Up to 10 RDS event messages from the last 7 days for the instance | None |
| Compute Optimizer | `EnableComputeOptimizer` (`false`) | AWS rightsizing finding, reason codes and the top 3 candidate configurations for the resource | [Opt in to Compute Optimizer](https://docs.aws.amazon.com/compute-optimizer/latest/ug/getting-started.html) for the account. A resource needs roughly 30 hours of metrics before it gets a recommendation. |
| CloudTrail changes | `EnableCloudTrailChanges` (`false`) | API names and times of up to 10 recent write calls on the resource (for example `ModifyDBInstance`). No identities, IPs or parameters. | None. Uses CloudTrail event history (90 days, management events). `LookupEvents` is rate limited to a few calls per second per account. |

AWS Health events and Service Quotas are not included. Health needs a Business, Enterprise On-Ramp or Enterprise Support plan, and quotas are a different kind of metric.

```bash
# Check if Compute Optimizer is enabled (optional)
aws compute-optimizer get-enrollment-status --region us-east-1

# Enable Compute Optimizer if needed (optional)
aws compute-optimizer update-enrollment-status --status Active --region us-east-1

# Verify CloudTrail event history is available (optional)
aws cloudtrail lookup-events --max-results 1 --region us-east-1
```

## 3. Amazon CloudWatch metrics

Capacity Foresight reads standard metrics. Confirm they exist for the resources you want to monitor.

| Resource | Namespace | Metrics used | Notes |
|---|---|---|---|
| EC2 instance | `AWS/EC2` | `CPUUtilization` | Basic monitoring (5 minute) is enough. |
| RDS instance | `AWS/RDS` | `CPUUtilization`, `FreeStorageSpace`, `FreeableMemory` | Published by default. |
| EBS volume | `AWS/EBS` | `BurstBalance` | Published only for volumes that use burst credits (gp2, st1, sc1). Volumes without it are skipped. |

The forecast looks back 28 days. It needs at least `min_points` days (default 7) of history for a metric, and about 10 days (with every weekday seen) to remove the weekly pattern. New resources are skipped until then, except that one already past its threshold is reported immediately.

To monitor memory or disk on EC2, install the CloudWatch agent and add a rule in `src/config/thresholds.json` for the custom namespace. The sample ships rules for the metrics above only.

```bash
# Check EC2 CPUUtilization metrics for an instance (replace i-xxx with your instance ID)
aws cloudwatch get-metric-statistics \
  --namespace AWS/EC2 \
  --metric-name CPUUtilization \
  --dimensions Name=InstanceId,Value=i-0123456789abcdef0 \
  --start-time $(date -u -v-7d +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 3600 \
  --statistics Average \
  --region us-east-1

# Check RDS metrics for a DB instance (replace mydb with your DB identifier)
aws cloudwatch get-metric-statistics \
  --namespace AWS/RDS \
  --metric-name FreeStorageSpace \
  --dimensions Name=DBInstanceIdentifier,Value=mydb \
  --start-time $(date -u -v-7d +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 3600 \
  --statistics Average \
  --region us-east-1

# Check EBS BurstBalance for a volume (replace vol-xxx with your volume ID)
aws cloudwatch get-metric-statistics \
  --namespace AWS/EBS \
  --metric-name BurstBalance \
  --dimensions Name=VolumeId,Value=vol-0123456789abcdef0 \
  --start-time $(date -u -v-7d +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 3600 \
  --statistics Average \
  --region us-east-1

# List EC2 instances to find instance IDs
aws ec2 describe-instances --query "Reservations[*].Instances[*].[InstanceId,State.Name,Tags[?Key=='Name'].Value|[0]]" --output table --region us-east-1

# List RDS instances to find DB identifiers
aws rds describe-db-instances --query "DBInstances[*].[DBInstanceIdentifier,DBInstanceStatus,Engine]" --output table --region us-east-1

# List EBS volumes to find volume IDs
aws ec2 describe-volumes --query "Volumes[*].[VolumeId,State,VolumeType,Size]" --output table --region us-east-1
```

## 4. Tag the resources you want monitored

Add the tag `capacity-foresight=monitor` (key and value are parameters) to each EC2 instance, RDS DB instance and EBS volume to include. The [Resource Groups Tagging API](https://docs.aws.amazon.com/resourcegroupstagging/latest/APIReference/overview.html) finds them, so no resource lists to maintain.

```bash
# Tag an EC2 instance
aws ec2 create-tags \
  --resources i-0123456789abcdef0 \
  --tags Key=capacity-foresight,Value=monitor \
  --region us-east-1

# Tag an RDS DB instance
aws rds add-tags-to-resource \
  --resource-name arn:aws:rds:us-east-1:123456789012:db:mydb \
  --tags Key=capacity-foresight,Value=monitor \
  --region us-east-1

# Tag an EBS volume
aws ec2 create-tags \
  --resources vol-0123456789abcdef0 \
  --tags Key=capacity-foresight,Value=monitor \
  --region us-east-1

# Verify tagged resources using Resource Groups Tagging API
aws resourcegroupstaggingapi get-resources \
  --tag-filters Key=capacity-foresight,Values=monitor \
  --region us-east-1

# Tag multiple resources at once
aws ec2 create-tags \
  --resources i-0123456789abcdef0 i-0abcdef1234567890 vol-0123456789abcdef0 \
  --tags Key=capacity-foresight,Value=monitor \
  --region us-east-1

# Optional: add a routing tag for team-based alert filtering
aws ec2 create-tags \
  --resources i-0123456789abcdef0 \
  --tags Key=capacity-foresight,Value=monitor Key=Team,Value=platform \
  --region us-east-1
```

## 5. A dashboard (optional)

The email path works without one. Pick the tool you already use:

**Amazon QuickSight**

1. Subscribe in the account. The Enterprise edition is what new accounts get. For the smallest set-up, use `IAM_ONLY` authentication and a single user at the Author level (the person who subscribes is registered at a higher, more expensive level by default; change it with `update-user --role AUTHOR`). QuickSight is billed per user per month plus usage, so check the [pricing page](https://aws.amazon.com/quicksight/pricing/).
2. **Deploy the stack first** using `./scripts/deploy.sh` — the QuickSight scripts require the stack outputs (bucket, database, workgroup).
3. Run `python scripts/setup_quicksight.py --stack-name <stack>`. It creates a role that can read only this stack's bucket, Athena workgroup and Glue database, and passes it to the Athena data source, so you do **not** need to grant QuickSight access to S3 in the console.
4. Run `python scripts/create_dashboard.py --stack-name <stack>`.
5. Share dashboards with groups, not individuals, and limit who can edit datasets. Findings contain account IDs, resource IDs and advice text.

**Important:** QuickSight identity region may differ from your stack region. If you get a region mismatch error, add `--region <identity-region>` to the QuickSight scripts. The error message will tell you the correct region.

**Grafana**: follow [`GRAFANA.md`](GRAFANA.md).

Both read the same Athena views. See [`ARCHITECTURE.md`](ARCHITECTURE.md).

```bash
# Check if QuickSight is subscribed in the account
aws quicksight describe-account-subscription \
  --aws-account-id $(aws sts get-caller-identity --query Account --output text) \
  --region us-east-1

# Subscribe to QuickSight (if not already subscribed)
# Note: This is typically done via the AWS Console for first-time setup
# https://console.aws.amazon.com/quicksight/

# Deploy the stack FIRST (required before running QuickSight scripts)
# Replace your-email@example.com with your actual email address where you want to receive the capacity alerts.
export NOTIFICATION_EMAIL=your-email@example.com
./scripts/deploy.sh

# After deploying the stack, set up QuickSight data source and dashboard
export STACK_NAME=capacity-foresight

# If your QuickSight identity region differs from your stack region, add --region
# The scripts will tell you the correct region if there's a mismatch
python scripts/setup_quicksight.py --stack-name $STACK_NAME --region us-east-1
python scripts/create_dashboard.py --stack-name $STACK_NAME --region us-east-1

# List QuickSight users to verify setup
aws quicksight list-users \
  --aws-account-id $(aws sts get-caller-identity --query Account --output text) \
  --namespace default \
  --region us-east-1
```

## 6. Email

You need a mailbox that can receive the SNS subscription confirmation. The alert email is plain text sent by Amazon SNS. To send each team only its own alerts, tag resources with the owner and set `RoutingTagKey`, then subscribe each team with an SNS filter policy (see the README).

```bash
# Set the notification email for deployment
export NOTIFICATION_EMAIL=your-email@example.com

# After deployment, confirm the SNS subscription by clicking the link in the email

# Get the SNS topic ARN from the deployed stack
export STACK_NAME=capacity-foresight
export SNS_TOPIC_ARN=$(aws cloudformation describe-stacks \
  --stack-name $STACK_NAME \
  --query 'Stacks[0].Outputs[?OutputKey==`NotificationTopicArn`].OutputValue' \
  --output text \
  --region us-east-1)

echo "SNS Topic ARN: $SNS_TOPIC_ARN"

# Check subscription status
aws sns list-subscriptions-by-topic \
  --topic-arn "$SNS_TOPIC_ARN" \
  --region us-east-1

# Subscribe additional emails with a filter policy (for team-based routing)
aws sns subscribe \
  --topic-arn "$SNS_TOPIC_ARN" \
  --protocol email \
  --notification-endpoint platform-team@example.com \
  --attributes '{"FilterPolicy":"{\"route\":[\"platform\"]}"}' \
  --region us-east-1
```

## 7. Quotas to be aware of

- Step Functions Standard payloads are limited to 256 KB. The workflow passes the resource list as one payload, so `MaxResources` is capped at 1000 (default 500). For larger fleets, switch the outer Map to a Distributed Map reading from S3.
- Amazon Bedrock on-demand throttling applies to the model you pick. The workflow calls the model with a concurrency of 2 and the SDK retries with backoff. The recommendation cache removes repeat calls for unchanged findings. If a call still fails, the fallback model is tried, and finally a plain email is sent so no alert is lost.
- Budget: set an AWS Budgets alert on Amazon Bedrock spend. Many tagged resources breaching at once means many model calls.

```bash
# Check Step Functions registered state machines quota
aws service-quotas get-service-quota \
  --service-code states \
  --quota-code L-B66B0F91 \
  --region us-east-1

# Check Step Functions request size quota (256 KB payload limit)
aws service-quotas get-service-quota \
  --service-code states \
  --quota-code L-DAFB74D5 \
  --region us-east-1

# List all Step Functions quotas
aws service-quotas list-service-quotas \
  --service-code states \
  --region us-east-1

# Check Bedrock model invocation quotas. Note: This command take some time to provide the output
aws service-quotas list-service-quotas \
  --service-code bedrock \
  --region us-east-1

# Create a budget alert for Bedrock spend (recommended)
cat > /tmp/budget.json << 'EOF'
{
  "BudgetName": "bedrock-monthly-budget",
  "BudgetLimit": {
    "Amount": "100",
    "Unit": "USD"
  },
  "BudgetType": "COST",
  "CostFilters": {
    "Service": ["Amazon Bedrock"]
  },
  "TimeUnit": "MONTHLY"
}
EOF

aws budgets create-budget \
  --account-id $(aws sts get-caller-identity --query Account --output text) \
  --budget file:///tmp/budget.json

# Check current Bedrock usage (if Cost Explorer is enabled)
aws ce get-cost-and-usage \
  --time-period Start=$(date -u -v-30d +%Y-%m-%d),End=$(date -u +%Y-%m-%d) \
  --granularity MONTHLY \
  --metrics BlendedCost \
  --filter '{"Dimensions":{"Key":"SERVICE","Values":["Amazon Bedrock"]}}' \
  --region us-east-1
```
