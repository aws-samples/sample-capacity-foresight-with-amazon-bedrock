# Security Policy

## Disclaimer

This repository contains sample code for demonstration and learning. It is **not production-ready**.
Review and test it against your own security, compliance and operational requirements before any
production use. The authors and AWS make no warranty regarding its fitness for production workloads.

## Reporting Vulnerabilities

If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security
via our [vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/).
Please do **not** create a public GitHub issue.

## AWS Services Used

Amazon EventBridge Scheduler, AWS Step Functions, AWS Lambda (Python 3.13), Amazon CloudWatch,
Amazon Bedrock (Converse API, optional Amazon Bedrock Guardrails), Amazon DynamoDB, Amazon SNS,
Amazon S3, AWS KMS, AWS Glue, Amazon Athena, optional Amazon QuickSight, optional AWS Compute Optimizer,
Amazon RDS events and AWS CloudTrail lookups.

## Prerequisites and Permissions

Deploying the stack requires permissions to create IAM roles (`CAPABILITY_IAM`), KMS keys, S3 buckets,
a DynamoDB table, an SNS topic, Lambda functions, a Step Functions state machine, CloudWatch alarms and
log groups, Glue/Athena resources and (optionally) an Amazon Bedrock Guardrail. Amazon Bedrock model
access must be enabled for the models you configure. The functions are **read-only** against your
EC2, RDS and EBS resources: no statement grants a write action on monitored resources.

Describe and metric-read actions that have no resource type in the AWS Service Authorization Reference
(EC2, Auto Scaling, CloudWatch, Resource Groups Tagging, CloudTrail, Compute Optimizer) use
`Resource: "*"`. `rds:DescribeEvents`, granted when `EnableRdsEvents=true` (the default), also uses
`Resource: "*"`. Other RDS describe actions are scoped to this account and Region. All other permissions are
scoped to the stack's own bucket prefixes, table, topic, keys and log groups.

## Known Security Considerations

The following items were reviewed and accepted for this sample. Revisit them before any production use.

| Item | Why it is acceptable here | Production recommendation |
|------|---------------------------|---------------------------|
| Lambda functions are not in a VPC | They call only AWS APIs over TLS and expose no listener | Add `VpcConfig` with private subnets and interface/gateway endpoints for the services used |
| No per-function reserved concurrency | Invocation is synchronous from Step Functions, which bounds fan-out (`MaxConcurrency`) and retries | Set `ReservedConcurrentExecutions` per function sized from the Map concurrency |
| Lambda environment variables use the AWS-managed Lambda key | Variables hold resource names, model IDs and thresholds; no secrets | Set `KmsKeyArn` to the stack's key and keep any future secrets in AWS Secrets Manager |
| `kms:*` to the account root in key policies | Standard default that keeps keys manageable if stack roles are removed | Scope key administration to a named deployment role and key usage to the function roles |
| S3 buckets use SSE-S3 rather than a customer managed key | Stored data is derived metrics and model advice | Set `SSEAlgorithm: aws:kms` with a customer managed key if your policy requires it |
| DynamoDB table and KMS keys are deleted with the stack | The table is a TTL-expiring cache; `cleanup.sh` is expected to remove everything | Set `DeletionPolicy: Retain` on the keys so retained S3 access logs stay decryptable |

## Operational Notes

- Anyone who can tag a resource `capacity-foresight=monitor` adds it to scope and can add Amazon Bedrock
  calls. Bound cost with `MaxResources`, the recommendation cache, an AWS Budgets alert that covers
  your model's charges, and consider a tag policy on the discovery tag key. Third-party models such as
  the default Claude model are billed through AWS Marketplace under the model provider, not under
  Amazon Bedrock, so a budget filtered only to the Amazon Bedrock service does not include them.
- Anyone allowed `states:StartExecution` on the workflow can start a demo run that sends emails.
- Alert emails contain the account ID, resource IDs and model-generated advice. Use SNS filter policies
  so each team receives only its own alerts.
- Model output is advice. It is schema-validated, link-stripped and never executed by this code, but
  a human should review it before acting.

## Resource Cleanup

Run `./scripts/cleanup.sh`. It deletes the stack, then empties and deletes the retained data and
access-log buckets (all object versions and delete markers) after an interactive confirmation.
Delete any QuickSight analysis and datasets you created manually.

## Dependencies

The Lambda functions use only the Python standard library and the boto3 provided by the Lambda
runtime. Development dependencies are pinned in `requirements-dev.txt`. Run `pip-audit -r
requirements-dev.txt` periodically.
