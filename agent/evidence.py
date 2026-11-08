"""Evidence gathering: read-only looks at metrics, logs, deploys and alarms.

Everything here only READS. The agent has no way to see the chaos flags (the injected fault):
it must work out the cause from the same signals a human on-call engineer would use.
"""
import time
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import Key

from agent.aws import cloudwatch, deployments, from_dynamo, logs
from agent.config import LOG_GROUPS, METRICS, NAMESPACE, SERVICES

# Which statistic makes sense for each metric when several copies report at once.
STAT = {"Requests": "Sum", "Errors": "Sum", "FailedAmountUSD": "Sum", "SlowAmountUSD": "Sum",
        "Restarts": "Sum", "LatencyP95": "Maximum", "FraudLatencyP95": "Maximum",
        "CPU": "Average", "Memory": "Average", "Replicas": "Maximum", "DBConnections": "Maximum"}
UNIT = {"LatencyP95": " ms", "FraudLatencyP95": " ms", "CPU": "%", "Memory": "%",
        "FailedAmountUSD": " USD", "SlowAmountUSD": " USD"}
PERIOD = 30          # seconds per data point
RECENT_POINTS = 4    # "now" = the last 2 minutes


def _utcnow():
    return datetime.now(timezone.utc)


def metric_points(service: str, metric: str, minutes: int = 15):
    """[(timestamp, value)] oldest first, one point per 30 s."""
    end = _utcnow()
    resp = cloudwatch.get_metric_data(
        MetricDataQueries=[{
            "Id": "m",
            "MetricStat": {
                "Metric": {"Namespace": NAMESPACE, "MetricName": metric,
                           "Dimensions": [{"Name": "Service", "Value": service}]},
                "Period": PERIOD, "Stat": STAT.get(metric, "Average"),
            },
        }],
        StartTime=end - timedelta(minutes=minutes), EndTime=end, ScanBy="TimestampAscending",
    )
    result = resp["MetricDataResults"][0]
    return list(zip(result["Timestamps"], result["Values"]))


def summarize_metric(service: str, metric: str, minutes: int = 15) -> dict:
    """Compares the last 2 minutes ("now") with the 13 minutes before ("baseline")."""
    points = metric_points(service, metric, minutes)
    if not points:
        return {"service": service, "metric": metric, "now": None, "baseline": None,
                "peak": None, "changed_at": None,
                "text": f"{service} {metric}: no data in the last {minutes} min"}
    values = [v for _, v in points]
    recent, before = values[-RECENT_POINTS:], values[:-RECENT_POINTS] or values[:1]
    agg = sum if STAT.get(metric) == "Sum" else max
    now = agg(recent) / (len(recent) if STAT.get(metric) == "Sum" else 1)
    baseline = (sum(before) / len(before))
    peak = max(values)
    # first point where the value moved far from baseline
    changed_at = None
    threshold = max(abs(baseline) * 2, 1.0)
    for ts, v in points:
        if abs(v - baseline) > threshold:
            changed_at = ts.strftime("%H:%M:%S")
            break
    unit = UNIT.get(metric, "")
    per = " per 30 s" if STAT.get(metric) == "Sum" and metric not in ("Restarts",) else ""
    text = (f"{service} {metric}: now {now:,.1f}{unit}{per}, baseline {baseline:,.1f}{unit}, "
            f"peak {peak:,.1f}{unit}" + (f", first jumped at {changed_at} UTC" if changed_at else ", stable"))
    return {"service": service, "metric": metric, "now": round(now, 1), "baseline": round(baseline, 1),
            "peak": round(peak, 1), "changed_at": changed_at, "text": text}


def recent_deploys(service: str = "payments-api", minutes: int = 60) -> list:
    since = (_utcnow() - timedelta(minutes=minutes)).isoformat()
    resp = deployments.query(
        KeyConditionExpression=Key("service").eq(service) & Key("deployed_at").gte(since),
        ScanIndexForward=False, Limit=10,
    )
    return [from_dynamo(i) for i in resp.get("Items", [])]


def deploys_text(service: str = "payments-api", minutes: int = 60) -> str:
    items = recent_deploys(service, minutes)
    if not items:
        return f"No deploys of {service} in the last {minutes} min."
    lines = [f"{d['deployed_at'][11:19]} UTC: {service} {d.get('version')} (by {d.get('by', '?')})" for d in items]
    return f"Deploys of {service} in the last {minutes} min (newest first): " + "; ".join(lines)


def _insights(service: str, query: str, minutes: int) -> list:
    group = LOG_GROUPS.get(service)
    if not group:
        return []
    end = int(time.time())
    qid = logs.start_query(logGroupName=group, startTime=end - minutes * 60, endTime=end,
                           queryString=query, limit=10)["queryId"]
    for _ in range(30):  # up to ~15 s
        resp = logs.get_query_results(queryId=qid)
        if resp["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        time.sleep(0.5)
    rows = []
    for row in resp.get("results", []):
        rows.append({f["field"]: f["value"] for f in row if not f["field"].startswith("@ptr")})
    return rows


def top_errors(service: str = "payments-api", minutes: int = 10) -> str:
    rows = _insights(service,
                     "filter status >= 500 | stats count(*) as n by error, release | sort n desc | limit 5",
                     minutes)
    if not rows:
        return f"No 5xx errors in {service} logs in the last {minutes} min."
    parts = [f"{r.get('n')} x '{r.get('error')}' (release {r.get('release')})" for r in rows]
    return f"Top errors in {service} logs, last {minutes} min: " + "; ".join(parts)


def request_timing(service: str = "payments-api", minutes: int = 3) -> str:
    rows = _insights(service,
                     'filter ispresent(latency_ms) and path != "/health" '
                     "| stats count(*) as n, pct(latency_ms, 95) as p95_ms, avg(fraud_ms) as avg_fraud_ms",
                     minutes)
    if not rows or not rows[0].get("n"):
        return f"No requests in {service} logs in the last {minutes} min."
    r = rows[0]
    fraud = r.get("avg_fraud_ms")
    fraud_text = f", average time waiting on fraud-check {float(fraud):,.0f} ms" if fraud else ""
    return (f"{service} logs, last {minutes} min: {r['n']} requests, p95 {float(r['p95_ms']):,.0f} ms"
            f"{fraud_text}.")


def firing_alarms() -> list:
    resp = cloudwatch.describe_alarms(AlarmNamePrefix="oncall-", StateValue="ALARM")
    return [a["AlarmName"] for a in resp.get("MetricAlarms", [])]


SNAPSHOT_METRICS = [
    ("payments-api", "Requests"), ("payments-api", "Errors"), ("payments-api", "LatencyP95"),
    ("payments-api", "FraudLatencyP95"), ("payments-api", "CPU"), ("payments-api", "Replicas"),
    ("payments-api", "Restarts"), ("fraud-check", "LatencyP95"), ("fraud-check", "CPU"),
    ("fraud-check", "Restarts"), ("postgres", "DBConnections"),
]


def snapshot() -> dict:
    """The fixed first look, before any AI: every key metric, deploys, errors, alarms."""
    metrics = {}
    for service, metric in SNAPSHOT_METRICS:
        try:
            metrics[f"{service}/{metric}"] = summarize_metric(service, metric)
        except Exception as exc:
            metrics[f"{service}/{metric}"] = {"text": f"{service} {metric}: unavailable ({exc!r})", "now": None}
    deploy_items = recent_deploys("payments-api", 30)
    pieces = {
        "deploys_text": deploys_text("payments-api", 30),
        "errors": top_errors("payments-api", 10),
        "timing": request_timing("payments-api", 3),
        "alarms": ", ".join(firing_alarms()) or "none",
    }
    text = "\n".join(["METRICS (now = last 2 min, baseline = the 13 min before):"]
                     + [f"- {m['text']}" for m in metrics.values()]
                     + [f"DEPLOYS: {pieces['deploys_text']}", f"ERROR LOGS: {pieces['errors']}",
                        f"REQUEST TIMING: {pieces['timing']}", f"ALARMS IN ALARM STATE: {pieces['alarms']}"])
    return {"metrics": metrics, "deploys": deploy_items, **pieces, "text": text}


def valid_service(service: str) -> bool:
    return service in SERVICES


def valid_metric(metric: str) -> bool:
    return metric in METRICS


def revenue_at_risk(since: datetime, until: datetime = None) -> float:
    """Dollars of failed + slow (over 1 s) charges between two times."""
    until = until or _utcnow()
    total = 0.0
    for metric in ("FailedAmountUSD", "SlowAmountUSD"):
        resp = cloudwatch.get_metric_data(
            MetricDataQueries=[{"Id": "m", "MetricStat": {
                "Metric": {"Namespace": NAMESPACE, "MetricName": metric,
                           "Dimensions": [{"Name": "Service", "Value": "payments-api"}]},
                "Period": 60, "Stat": "Sum"}}],
            StartTime=since, EndTime=until,
        )
        total += sum(resp["MetricDataResults"][0]["Values"])
    return round(total)
