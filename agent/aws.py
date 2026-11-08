"""AWS clients, created once. Credentials come from the server's IAM role (no keys)."""
from decimal import Decimal

import boto3

from agent.config import DEPLOYMENTS_TABLE, INCIDENTS_TABLE, QUEUE_NAME, REGION

cloudwatch = boto3.client("cloudwatch", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)
_dynamodb = boto3.resource("dynamodb", region_name=REGION)
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
