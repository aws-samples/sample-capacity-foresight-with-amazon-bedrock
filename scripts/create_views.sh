#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Create the Athena views that the QuickSight datasets read. Safe to re-run.
set -euo pipefail

cd "$(dirname "$0")/.."
STACK_NAME="${STACK_NAME:-capacity-foresight}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
: "${REGION:?Set AWS_REGION}"

output() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
DB="$(output GlueDatabase)"
WG="$(output AthenaWorkGroupName)"

for file in athena/views/*.sql; do
  sql="$(sed "s/__DB__/${DB}/g" "$file")"
  qid="$(aws athena start-query-execution --region "$REGION" --work-group "$WG" \
    --query-string "$sql" --query QueryExecutionId --output text)"
  while :; do
    state="$(aws athena get-query-execution --region "$REGION" --query-execution-id "$qid" \
      --query QueryExecution.Status.State --output text)"
    case "$state" in
      SUCCEEDED) echo "created view from $file"; break ;;
      FAILED|CANCELLED)
        aws athena get-query-execution --region "$REGION" --query-execution-id "$qid" \
          --query QueryExecution.Status.StateChangeReason --output text >&2
        exit 1 ;;
      *) sleep 1 ;;
    esac
  done
done
