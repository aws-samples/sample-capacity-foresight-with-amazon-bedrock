#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Package and deploy Capacity Foresight, then create the Athena views.
#
# Required environment variables:
#   NOTIFICATION_EMAIL   address that receives alerts
#   ARTIFACTS_BUCKET     existing S3 bucket in the target Region used for packaging Lambda code
# Optional:
#   STACK_NAME (default capacity-foresight), AWS_REGION
#   BEDROCK_MODEL_ID and BEDROCK_FOUNDATION_MODEL_ID  model/profile to call and the base model behind it (set both)
#   EXTRA_PARAMETER_OVERRIDES  space-separated Key=Value pairs for any other stack parameter, for example
#                              "RoutingTagKey=Team EnableDigest=true EnableComputeOptimizer=true"
# Any remaining arguments are passed to `aws cloudformation deploy` (but not --parameter-overrides, which
# would replace the overrides above; use EXTRA_PARAMETER_OVERRIDES instead).
set -euo pipefail

cd "$(dirname "$0")/.."
: "${NOTIFICATION_EMAIL:?Set NOTIFICATION_EMAIL}"
: "${ARTIFACTS_BUCKET:?Set ARTIFACTS_BUCKET}"
STACK_NAME="${STACK_NAME:-capacity-foresight}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
: "${REGION:?Set AWS_REGION}"

OVERRIDES=("NotificationEmail=${NOTIFICATION_EMAIL}")
if [[ -n "${BEDROCK_MODEL_ID:-}" || -n "${BEDROCK_FOUNDATION_MODEL_ID:-}" ]]; then
  : "${BEDROCK_MODEL_ID:?Set BEDROCK_MODEL_ID together with BEDROCK_FOUNDATION_MODEL_ID}"
  : "${BEDROCK_FOUNDATION_MODEL_ID:?Set BEDROCK_FOUNDATION_MODEL_ID together with BEDROCK_MODEL_ID}"
  OVERRIDES+=("BedrockModelId=${BEDROCK_MODEL_ID}" "BedrockFoundationModelId=${BEDROCK_FOUNDATION_MODEL_ID}")
fi

if [[ -n "${EXTRA_PARAMETER_OVERRIDES:-}" ]]; then
  read -r -a EXTRA <<< "${EXTRA_PARAMETER_OVERRIDES}"
  OVERRIDES+=("${EXTRA[@]}")
fi
for arg in "$@"; do
  if [[ "$arg" == "--parameter-overrides" ]]; then
    echo "Do not pass --parameter-overrides; set EXTRA_PARAMETER_OVERRIDES instead." >&2
    exit 2
  fi
done

# Keep bytecode caches out of the Lambda packages.
find src -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

echo "==> Packaging"
aws cloudformation package --region "$REGION" \
  --template-file template.yaml \
  --s3-bucket "$ARTIFACTS_BUCKET" --s3-prefix "$STACK_NAME" \
  --output-template-file packaged.yaml

echo "==> Deploying stack ${STACK_NAME} in ${REGION}"
# --s3-bucket is required: the template is larger than the 51,200 bytes CloudFormation accepts in a direct upload.
aws cloudformation deploy --region "$REGION" \
  --template-file packaged.yaml --stack-name "$STACK_NAME" \
  --s3-bucket "$ARTIFACTS_BUCKET" --s3-prefix "$STACK_NAME" \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --parameter-overrides "${OVERRIDES[@]}" "$@"

echo "==> Creating Athena views"
STACK_NAME="$STACK_NAME" AWS_REGION="$REGION" ./scripts/create_views.sh

cat <<EOF

Done. Next steps:
  1. Confirm the SNS subscription in the email sent to ${NOTIFICATION_EMAIL}.
  2. Try it without real resources:  ./scripts/run_demo.sh
  3. Tag resources to monitor (default tag capacity-foresight=monitor).
EOF
