#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Run the workflow once against synthetic resources. No real resources are read or changed.
# The demo still calls Amazon Bedrock and sends real emails to the subscribed address.
set -euo pipefail

STACK_NAME="${STACK_NAME:-capacity-foresight}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
: "${REGION:?Set AWS_REGION}"

SM_ARN="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='StateMachineArn'].OutputValue" --output text)"

EXEC_ARN="$(aws stepfunctions start-execution --region "$REGION" --state-machine-arn "$SM_ARN" \
  --input '{"demo": true}' --query executionArn --output text)"
echo "Started $EXEC_ARN"

while :; do
  STATUS="$(aws stepfunctions describe-execution --region "$REGION" --execution-arn "$EXEC_ARN" \
    --query status --output text)"
  [[ "$STATUS" == "RUNNING" ]] || break
  sleep 3
done
echo "Execution finished: $STATUS"
[[ "$STATUS" == "SUCCEEDED" ]] || exit 1
echo "Expect two emails (demo-rds-orders storage, demo-ec2-web CPU) and new rows in the dashboard datasets."
echo "Re-running within the suppression window sends no duplicate emails."
