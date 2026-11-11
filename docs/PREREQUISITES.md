# Prerequisites

Capacity Foresight reads from services you already run. Set these up before you deploy. None of them are created by the stack.

## 1. AWS account and tools

- An AWS account and a Region where Amazon Bedrock and your chosen model are available.
- AWS CLI v2, configured with credentials that can create CloudFormation, IAM, Lambda, Step Functions, S3, DynamoDB, SNS, Glue and Athena resources.
- Python 3.13 (only to run the unit tests locally).
- An existing S3 bucket in the same Region for packaging the Lambda code (`ARTIFACTS_BUCKET`).

## 2. Amazon Bedrock model access

- Choose a model that supports **tool use** with the Converse API. The default is the Anthropic Claude Sonnet 5.5 global inference profile. The recommender adapts to newer models: it drops options a model rejects (a forced tool choice, or `temperature`) and retries once. Check the [supported models table](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference-supported-models-features.html) for your Region.
- Confirm you can invoke the model before deploying (console playground or `aws bedrock-runtime converse`).
- Set `BedrockModelId` to the model ID or inference profile ID you tested, and `BedrockFoundationModelId` to the base model behind it (the same value if you call the model directly). IAM is scoped to exactly these.
- **Plan for model retirement.** Models are retired on a published schedule. Set `BedrockFallbackModelId` (and its `BedrockFallbackFoundationModelId`) to a second model so alerts keep their AI plans if the first stops working, and review the [model lifecycle page](https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html) when you deploy. The `RecommendationFallback` alarm tells you when no model worked.
- Optional: set `BedrockHighRiskModelId` (and its foundation model ID) to use a larger model only for CRITICAL and HIGH findings.
- **Guardrail.** By default the stack creates an [Amazon Bedrock Guardrail](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html) with the prompt-attack filter and applies it to every call. Confirm your model supports guardrails with the Converse API in your Region. Set `CreateGuardrail=false` to bring your own (`GuardrailId`, `GuardrailVersion`) or to run without one. If the guardrail blocks a request the model call counts as failed, the next model is tried, and finally the plain notice is sent.
- Data handling: the model receives the metric trend, the resource's configuration (instance class, storage size, engine, pending modifications and so on), the values of the tag keys you list in `ContextTagKeys`, and the optional signals in section 2a. Review this against your data policies. Resource IDs and tag values are sent; raw metric history, identities and credentials are not.

### 2a. Optional AWS signals (each is independent)

Not every account uses every service, so each signal is a separate switch and everything is best effort. A signal that is off, not set up, or failing is skipped and never blocks an alert.

| Signal | Parameter (default) | What the model gets | Set-up needed |
|---|---|---|---|
| RDS events | `EnableRdsEvents` (`true`) | Up to 10 RDS event messages from the last 7 days for the instance | None |
| Compute Optimizer | `EnableComputeOptimizer` (`false`) | AWS rightsizing finding, reason codes and the top 3 candidate configurations for the resource | [Opt in to Compute Optimizer](https://docs.aws.amazon.com/compute-optimizer/latest/ug/getting-started.html) for the account. A resource needs roughly 30 hours of metrics before it gets a recommendation. |
| CloudTrail changes | `EnableCloudTrailChanges` (`false`) | API names and times of up to 10 recent write calls on the resource (for example `ModifyDBInstance`). No identities, IPs or parameters. | None. Uses CloudTrail event history (90 days, management events). `LookupEvents` is rate limited to a few calls per second per account. |

AWS Health events and Service Quotas are not included. Health needs a Business, Enterprise On-Ramp or Enterprise Support plan, and quotas are a different kind of metric.

## 3. Amazon CloudWatch metrics

Capacity Foresight reads standard metrics. Confirm they exist for the resources you want to monitor.

| Resource | Namespace | Metrics used | Notes |
|---|---|---|---|
| EC2 instance | `AWS/EC2` | `CPUUtilization` | Basic monitoring (5 minute) is enough. |
| RDS instance | `AWS/RDS` | `CPUUtilization`, `FreeStorageSpace`, `FreeableMemory` | Published by default. |
| EBS volume | `AWS/EBS` | `BurstBalance` | Published only for volumes that use burst credits (gp2, st1, sc1). Volumes without it are skipped. |

The forecast looks back 28 days. It needs at least `min_points` days (default 7) of history for a metric, and about 10 days (with every weekday seen) to remove the weekly pattern. New resources are skipped until then, except that one already past its threshold is reported immediately.

To monitor memory or disk on EC2, install the CloudWatch agent and add a rule in `src/config/thresholds.json` for the custom namespace. The sample ships rules for the metrics above only.

## 4. Tag the resources you want monitored

Add the tag `capacity-foresight=monitor` (key and value are parameters) to each EC2 instance, RDS DB instance and EBS volume to include. The [Resource Groups Tagging API](https://docs.aws.amazon.com/resourcegroupstagging/latest/APIReference/overview.html) finds them, so no resource lists to maintain.

```bash
aws ec2 create-tags --resources i-0123456789abcdef0 --tags Key=capacity-foresight,Value=monitor
aws rds add-tags-to-resource --resource-name arn:aws:rds:REGION:ACCOUNT:db:mydb \
  --tags Key=capacity-foresight,Value=monitor
```

## 5. A dashboard (optional)

The email path works without one. Pick the tool you already use:

**Amazon QuickSight**

1. Subscribe in the account. The Enterprise edition is what new accounts get. For the smallest set-up, use `IAM_ONLY` authentication and a single user at the Author level (the person who subscribes is registered at a higher, more expensive level by default; change it with `update-user --role AUTHOR`). QuickSight is billed per user per month plus usage, so check the [pricing page](https://aws.amazon.com/quicksight/pricing/).
2. Run `python scripts/setup_quicksight.py --stack-name <stack>`. It creates a role that can read only this stack's bucket, Athena workgroup and Glue database, and passes it to the Athena data source, so you do **not** need to grant QuickSight access to S3 in the console.
3. Run `python scripts/create_dashboard.py --stack-name <stack>`.
4. Share dashboards with groups, not individuals, and limit who can edit datasets. Findings contain account IDs, resource IDs and advice text.

**Grafana**: follow [`GRAFANA.md`](GRAFANA.md).

Both read the same Athena views. See [`ARCHITECTURE.md`](ARCHITECTURE.md).

## 6. Email

You need a mailbox that can receive the SNS subscription confirmation. The alert email is plain text sent by Amazon SNS. To send each team only its own alerts, tag resources with the owner and set `RoutingTagKey`, then subscribe each team with an SNS filter policy (see the README).

## 7. Quotas to be aware of

- Step Functions Standard payloads are limited to 256 KB. The workflow passes the resource list as one payload, so `MaxResources` is capped at 1000 (default 500). For larger fleets, switch the outer Map to a Distributed Map reading from S3.
- Amazon Bedrock on-demand throttling applies to the model you pick. The workflow calls the model with a concurrency of 2 and the SDK retries with backoff. The recommendation cache removes repeat calls for unchanged findings. If a call still fails, the fallback model is tried, and finally a plain email is sent so no alert is lost.
- Budget: set an AWS Budgets alert on Amazon Bedrock spend. Many tagged resources breaching at once means many model calls.
