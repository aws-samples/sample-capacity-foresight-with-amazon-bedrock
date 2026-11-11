#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Delete the stack and the retained data and access-log buckets, including every object version
# and delete marker (both buckets are versioned). Destructive: asks for confirmation.
set -euo pipefail
STACK_NAME="${STACK_NAME:-capacity-foresight}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
: "${REGION:?Set AWS_REGION}"

output() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

DATA_BUCKET="$(output DataBucketName)"
LOG_BUCKET="$(output AccessLogBucketName)"   # "None" on stacks created before access logging was added
[[ -n "$DATA_BUCKET" && "$DATA_BUCKET" != "None" ]] || { echo "Could not read DataBucketName from stack '${STACK_NAME}'. Aborting." >&2; exit 1; }
BUCKETS=("$DATA_BUCKET")
[[ -n "$LOG_BUCKET" && "$LOG_BUCKET" != "None" ]] && BUCKETS+=("$LOG_BUCKET")

echo "This will delete stack '${STACK_NAME}' in ${REGION} and empty and delete: ${BUCKETS[*]}"
read -r -p "Type the stack name to confirm: " ANSWER
[[ "$ANSWER" == "$STACK_NAME" ]] || { echo "Aborted."; exit 1; }

aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_NAME"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK_NAME"

# Reads "key<TAB>version" lines on stdin and deletes up to 1000 versions in one call.
# Succeeds only when it deleted something, so the caller can loop until a bucket is empty.
delete_versions() {
  local bucket="$1" objects
  objects="$(awk -F'\t' 'NF == 2 && $1 != "None" { printf "%s{Key=%s,VersionId=%s}", (n++ ? "," : ""), $1, $2 }')"
  [[ -n "$objects" ]] || return 1
  aws s3api delete-objects --region "$REGION" --bucket "$bucket" --delete "Objects=[${objects}],Quiet=true" > /dev/null
}

empty_bucket() {
  local bucket="$1" deleted kind
  while :; do
    deleted=0
    for kind in Versions DeleteMarkers; do
      if aws s3api list-object-versions --region "$REGION" --bucket "$bucket" --max-items 1000 \
           --query "${kind}[].[Key,VersionId]" --output text | delete_versions "$bucket"; then
        deleted=1
      fi
    done
    [[ "$deleted" == 1 ]] || break
  done
}

for bucket in "${BUCKETS[@]}"; do
  empty_bucket "$bucket"
  aws s3api delete-bucket --region "$REGION" --bucket "$bucket"
done
echo "Deleted. Also delete the QuickSight datasets and analysis you created, and the packaging objects under s3://\${ARTIFACTS_BUCKET}/${STACK_NAME}/."
