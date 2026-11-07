"""Creates (or updates) every AWS resource OnCall AI's paging path needs. Safe to re-run.

  1. CloudWatch alarms (names start with "oncall-")  -> detect the faults
  2. SQS queue "oncall-incidents"                    -> holds pages until the agent reads them
  3. EventBridge rule "oncall-alarm-to-sqs"          -> any oncall- alarm entering ALARM -> the queue
  4. DynamoDB tables "incidents" and "deployments"   -> agent state + deploy history

Run on the EC2 server (its IAM role has the permissions):
    python3.11 -m pip install --user boto3
    python3.11 infra/setup_aws.py
"""
import json
import os

import boto3
from botocore.exceptions import ClientError

REGION = os.getenv("AWS_REGION", "us-east-2")
NAMESPACE = "OnCallAI"
QUEUE_NAME = "oncall-incidents"
RULE_NAME = "oncall-alarm-to-sqs"
PERIOD = 30  # seconds; our metrics are high resolution, so 30-second alarm periods are allowed

cloudwatch = boto3.client("cloudwatch", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)
events = boto3.client("events", region_name=REGION)
dynamodb = boto3.client("dynamodb", region_name=REGION)


def simple_alarm(alarm_name, description, metric_name, service, stat, threshold, evaluations=2):
    """Alarm on one metric: fires when `evaluations` periods in a row cross the threshold."""
    cloudwatch.put_metric_alarm(
        AlarmName=alarm_name,
        AlarmDescription=description,
        Namespace=NAMESPACE,
        MetricName=metric_name,
        Dimensions=[{"Name": "Service", "Value": service}],
        Statistic=stat,
        Period=PERIOD,
        EvaluationPeriods=evaluations,
        DatapointsToAlarm=evaluations,
        Threshold=threshold,
        ComparisonOperator="GreaterThanThreshold",
        TreatMissingData="notBreaching",
    )
    print(f"  alarm  {alarm_name}")


def error_rate_alarm():
    """Errors / Requests as a percentage (metric math), so it means the same at any traffic level."""
    name = "oncall-payments-error-rate"
    cloudwatch.put_metric_alarm(
        AlarmName=name,
        AlarmDescription="payments-api: more than 5% of charges are failing with 5xx",
        Metrics=[
            {
                "Id": "errors",
                "MetricStat": {
                    "Metric": {"Namespace": NAMESPACE, "MetricName": "Errors",
                               "Dimensions": [{"Name": "Service", "Value": "payments-api"}]},
                    "Period": PERIOD,
                    "Stat": "Sum",
                },
                "ReturnData": False,
            },
            {
                "Id": "requests",
                "MetricStat": {
                    "Metric": {"Namespace": NAMESPACE, "MetricName": "Requests",
                               "Dimensions": [{"Name": "Service", "Value": "payments-api"}]},
                    "Period": PERIOD,
                    "Stat": "Sum",
                },
                "ReturnData": False,
            },
            {
                "Id": "error_rate",
                "Expression": "IF(requests > 0, 100 * errors / requests, 0)",
                "Label": "Error rate (%)",
                "ReturnData": True,
            },
        ],
        EvaluationPeriods=2,
        DatapointsToAlarm=2,
        Threshold=5,
        ComparisonOperator="GreaterThanThreshold",
        TreatMissingData="notBreaching",
    )
    print(f"  alarm  {name}")


def create_alarms():
    print("CloudWatch alarms:")
    error_rate_alarm()
    simple_alarm("oncall-payments-latency", "payments-api: p95 latency above 500 ms",
                 "LatencyP95", "payments-api", "Maximum", 500)
    simple_alarm("oncall-payments-cpu", "payments-api: average CPU above 70% of a core",
                 "CPU", "payments-api", "Average", 70)
    simple_alarm("oncall-db-connections", "postgres: more than 70 open connections (limit is 100)",
                 "DBConnections", "postgres", "Maximum", 70)
    simple_alarm("oncall-payments-restarts", "payments-api: containers are restarting",
                 "Restarts", "payments-api", "Sum", 0, evaluations=1)
    simple_alarm("oncall-fraud-restarts", "fraud-check: containers are restarting",
                 "Restarts", "fraud-check", "Sum", 0, evaluations=1)
    # Deliberately NO alarm on FraudLatencyP95: the agent has to work out that a slow
    # payments-api is caused by fraud-check. That reasoning is the point of the demo.


def create_queue():
    print("SQS queue:")
    url = sqs.create_queue(QueueName=QUEUE_NAME, Attributes={"MessageRetentionPeriod": "86400"})["QueueUrl"]
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    print(f"  queue  {url}")
    return url, arn


def create_rule(queue_url, queue_arn):
    print("EventBridge rule:")
    pattern = {
        "source": ["aws.cloudwatch"],
        "detail-type": ["CloudWatch Alarm State Change"],
        "detail": {
            "alarmName": [{"prefix": "oncall-"}],
            "state": {"value": ["ALARM"]},
        },
    }
    rule_arn = events.put_rule(Name=RULE_NAME, EventPattern=json.dumps(pattern), State="ENABLED")["RuleArn"]
    events.put_targets(Rule=RULE_NAME, Targets=[{"Id": "oncall-queue", "Arn": queue_arn}])
    # Let EventBridge (and only this rule) put messages on the queue.
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AllowOnCallRule",
            "Effect": "Allow",
            "Principal": {"Service": "events.amazonaws.com"},
            "Action": "sqs:SendMessage",
            "Resource": queue_arn,
            "Condition": {"ArnEquals": {"aws:SourceArn": rule_arn}},
        }],
    }
    sqs.set_queue_attributes(QueueUrl=queue_url, Attributes={"Policy": json.dumps(policy)})
    print(f"  rule   {rule_arn}")


def create_table(name, key_schema, attributes):
    try:
        dynamodb.create_table(
            TableName=name,
            KeySchema=key_schema,
            AttributeDefinitions=attributes,
            BillingMode="PAY_PER_REQUEST",
        )
        print(f"  table  {name} (created)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceInUseException":
            print(f"  table  {name} (already exists)")
        else:
            raise


def create_tables():
    print("DynamoDB tables:")
    create_table("incidents",
                 [{"AttributeName": "incident_id", "KeyType": "HASH"}],
                 [{"AttributeName": "incident_id", "AttributeType": "S"}])
    create_table("deployments",
                 [{"AttributeName": "service", "KeyType": "HASH"},
                  {"AttributeName": "deployed_at", "KeyType": "RANGE"}],
                 [{"AttributeName": "service", "AttributeType": "S"},
                  {"AttributeName": "deployed_at", "AttributeType": "S"}])


if __name__ == "__main__":
    print(f"Region: {REGION}")
    create_alarms()
    url, arn = create_queue()
    create_rule(url, arn)
    create_tables()
    print("\nDone. Queue URL for the agent:", url)
