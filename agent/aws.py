"""AWS clients, created once. Credentials come from the server's IAM role (no keys)."""
from decimal import Decimal

import boto3

from agent.config import AGENT_AWS_PROFILE, DEPLOYMENTS_TABLE, INCIDENTS_TABLE, QUEUE_NAME, REGION

# With AGENT_AWS_PROFILE set, every AWS call the agent makes uses its own narrow IAM role
# (oncall-agent-role), assumed from the server role and refreshed automatically by boto3.
session = boto3.Session(profile_name=AGENT_AWS_PROFILE) if AGENT_AWS_PROFILE else boto3.Session()

cloudwatch = session.client("cloudwatch", region_name=REGION)
logs = session.client("logs", region_name=REGION)
sqs = session.client("sqs", region_name=REGION)
ssm = session.client("ssm", region_name=REGION)
sts = session.client("sts", region_name=REGION)
_dynamodb = session.resource("dynamodb", region_name=REGION)
incidents = _dynamodb.Table(INCIDENTS_TABLE)
deployments = _dynamodb.Table(DEPLOYMENTS_TABLE)

_queue_url = None


def queue_url() -> str:
    global _queue_url
    if _queue_url is None:
        _queue_url = sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]
    return _queue_url


def to_dynamo(value):
    """DynamoDB rejects Python floats; convert them (recursively) to Decimal."""
    if isinstance(value, float):
        return Decimal(str(round(value, 4)))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_dynamo(v) for v in value]
    return value


def from_dynamo(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    return value


def identity() -> str:
    """Which IAM role the agent is really using (printed at startup)."""
    try:
        return sts.get_caller_identity()["Arn"]
    except Exception as exc:
        return f"unknown ({exc!r})"
