# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("DATA_BUCKET", "test-bucket")
os.environ.setdefault("SUPPRESSION_TABLE", "test-table")
os.environ.setdefault("TOPIC_ARN", "arn:aws:sns:us-east-1:111122223333:test")
os.environ.setdefault("BEDROCK_MODEL_ID", "test-model")
os.environ.setdefault("ACCOUNT_ID", "111122223333")
