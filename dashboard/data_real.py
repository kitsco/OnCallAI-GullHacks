"""Dashboard data layer: REAL VERSION. Runs on the EC2 server (needs its IAM role and Docker).

Same 7 functions and return shapes as data_fake.py:
    get_health()            <- CloudWatch metrics (services + publisher) and CloudWatch alarm states
    get_history(minutes)    <- CloudWatch metrics, one point every 10 s
    get_incidents()         <- DynamoDB table "incidents" (written by the agent)
    approve(id) / reject(id)-> DynamoDB table "incidents" (the agent watches for the approval)
    trigger_fault(name)     -> scripts/faults.py (really breaks the system)
    reset()                 -> scripts/faults.py reset

Revenue at risk = dollar value of failed charges + charges slower than 1 s (shoppers abandon),
summed over the current (or most recent) bad stretch, until reset().
"""
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import boto3

REGION = os.getenv("AWS_REGION", "us-east-2")
NAMESPACE = "OnCallAI"
STEP_S = 10                 # metric resolution
LAG_S = 20                  # skip the newest 20 s: those buckets are still filling up
CACHE_TTL_S = 4             # the page refreshes every 2 s; AWS is asked at most every 4 s
QUIET_POINTS_TO_END = 6     # 60 s with nothing at risk = the bad stretch is over

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from scripts import faults  # noqa: E402  (the same commands as the command line)

cloudwatch = boto3.client("cloudwatch", region_name=REGION)
incidents_table = boto3.resource("dynamodb", region_name=REGION).Table("incidents")

_cache = {}
_state = {"reset_at": None, "busy": False}
_lock = threading.Lock()


# ---------------- small helpers ----------------

def _cached(key, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value


def _clear_cache():
    _cache.clear()


def _floor(dt: datetime) -> datetime:
    return dt.replace(microsecond=0) - timedelta(seconds=dt.second % STEP_S)


def _query(qid, metric, service, stat):
    return {
        "Id": qid,
        "MetricStat": {
            "Metric": {"Namespace": NAMESPACE, "MetricName": metric,
                       "Dimensions": [{"Name": "Service", "Value": service}]},
            "Period": STEP_S,
            "Stat": stat,
        },
        "ReturnData": True,
    }


def _get_series(queries, minutes):
    """Runs GetMetricData; returns ({query id: {timestamp: value}}, list of grid timestamps)."""
    end = _floor(datetime.now(timezone.utc) - timedelta(seconds=LAG_S))
    start = end - timedelta(minutes=minutes)
    series = {q["Id"]: {} for q in queries}
    token = None
    while True:
        kwargs = {"MetricDataQueries": queries, "StartTime": start, "EndTime": end,
                  "ScanBy": "TimestampAscending"}
        if token:
            kwargs["NextToken"] = token
        resp = cloudwatch.get_metric_data(**kwargs)
        for result in resp["MetricDataResults"]:
            for ts, value in zip(result["Timestamps"], result["Values"]):
                series[result["Id"]][_floor(ts)] = value
        token = resp.get("NextToken")
        if not token:
            break
    grid = []
    t = start
    while t < end:
        grid.append(t)
        t += timedelta(seconds=STEP_S)
    return series, grid


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _plain(value):
    """DynamoDB returns Decimal; the dashboard wants int/float."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


# ---------------- data fetchers ----------------

def _fetch_history(minutes=15):
    queries = [
        _query("req", "Requests", "payments-api", "Sum"),
        _query("err", "Errors", "payments-api", "Sum"),
        _query("p95", "LatencyP95", "payments-api", "Maximum"),
        _query("failed", "FailedAmountUSD", "payments-api", "Sum"),
        _query("slow", "SlowAmountUSD", "payments-api", "Sum"),
    ]
    series, grid = _get_series(queries, minutes)
    points, last_p95 = [], None
    for t in grid:
        req = series["req"].get(t, 0.0)
        err = series["err"].get(t, 0.0)
        p95 = series["p95"].get(t)
        if p95 is None:
            p95 = last_p95 if last_p95 is not None else 0.0  # no traffic in this bucket: carry forward
        last_p95 = p95
        points.append({
            "ts": _iso(t),
            "charges_per_min": round(req * 60 / STEP_S),
            "error_rate_pct": round(100 * err / req, 1) if req else 0.0,
            "p95_ms": round(p95),
            # extra columns (the pages can ignore them)
            "requests": int(req),
            "errors": int(err),
            "at_risk_usd": round(series["failed"].get(t, 0.0) + series["slow"].get(t, 0.0), 2),
        })
    return points


def _fetch_services():
    queries = []
    for i, service in enumerate(("payments-api", "fraud-check")):
        queries.append(_query(f"cpu{i}", "CPU", service, "Average"))
        queries.append(_query(f"rep{i}", "Replicas", service, "Maximum"))
    series, _ = _get_series(queries, minutes=3)

    def latest(qid, default):
        values = series[qid]
        return values[max(values)] if values else default

    return {
        "payments-api": {"replicas": int(latest("rep0", 0)), "cpu_pct": round(latest("cpu0", 0.0))},
        "fraud-check": {"replicas": int(latest("rep1", 0)), "cpu_pct": round(latest("cpu1", 0.0))},
    }


def _fetch_firing_alarms():
    resp = cloudwatch.describe_alarms(AlarmNamePrefix="oncall-", StateValue="ALARM")
    return [a["AlarmName"] for a in resp.get("MetricAlarms", [])]


def _fetch_incidents():
    items, kwargs = [], {}
    while True:
        resp = incidents_table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    defaults = {"title": "", "alarms": [], "steps": [], "diagnosis": None, "proposed_action": None,
                "approved": None, "result": None, "postmortem": None, "revenue_at_risk_usd": 0,
                "status": "OPEN", "opened_at": ""}
    incidents = [{**defaults, **_plain(item)} for item in items]
    incidents.sort(key=lambda inc: inc.get("opened_at", ""), reverse=True)
    return incidents


def _revenue_at_risk(points):
    """Sum of at-risk dollars over the current (or most recent) bad stretch, after the last reset."""
    reset_at = _state["reset_at"]
    values = [p["at_risk_usd"] if (reset_at is None or p["ts"] > reset_at) else 0.0 for p in points]
    i = len(values) - 1
    while i >= 0 and values[i] == 0:
        i -= 1
    if i < 0:
        return 0
    total, quiet = 0.0, 0
    while i >= 0:
        if values[i] > 0:
            total += values[i]
            quiet = 0
        else:
            quiet += 1
            if quiet >= QUIET_POINTS_TO_END:
                break
        i -= 1
    return round(total)


# ---------------- the 7 functions the dashboard uses ----------------

def get_history(minutes: int = 15) -> list:
    if minutes == 15:
        return _cached("history15", lambda: _fetch_history(15))
    return _fetch_history(minutes)


def get_health() -> dict:
    points = get_history(15)
    recent = points[-6:]                       # last 60 s
    last3 = points[-3:]                        # last 30 s
    requests = sum(p["requests"] for p in last3)
    errors = sum(p["errors"] for p in last3)
    firing = _cached("alarms", _fetch_firing_alarms)
    open_incident = any(inc["status"] not in ("RESOLVED", "REJECTED") for inc in get_incidents())
    return {
        "status": "incident" if (firing or open_incident) else "ok",
        "charges_per_min": sum(p["requests"] for p in recent),
        "error_rate_pct": round(100 * errors / requests, 1) if requests else 0.0,
        "p95_ms": max((p["p95_ms"] for p in last3), default=0),
        "revenue_at_risk_usd": _revenue_at_risk(points),
        "services": _cached("services", _fetch_services),
        "alarms_firing": firing,  # extra field: which oncall- alarms are in ALARM
    }


def get_incidents() -> list:
    return _cached("incidents", _fetch_incidents)


def approve(incident_id: str) -> None:
    """Approval is just a record in DynamoDB. The agent sees it and applies the fix."""
    try:
        incidents_table.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET approved = :t, #s = :next, approved_at = :now",
            ConditionExpression="#s = :awaiting",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":t": True, ":next": "REMEDIATING", ":awaiting": "AWAITING_APPROVAL",
                                       ":now": _iso(datetime.now(timezone.utc))},
        )
    except incidents_table.meta.client.exceptions.ConditionalCheckFailedException:
        pass  # already approved/rejected (double click)
    _clear_cache()


def reject(incident_id: str) -> None:
    try:
        incidents_table.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET approved = :f, #s = :rej, #r = :msg",
            ConditionExpression="#s = :awaiting",
            ExpressionAttributeNames={"#s": "status", "#r": "result"},
            ExpressionAttributeValues={":f": False, ":rej": "REJECTED", ":awaiting": "AWAITING_APPROVAL",
                                       ":msg": "Fix rejected by a human. The fault is still active."},
        )
    except incidents_table.meta.client.exceptions.ConditionalCheckFailedException:
        pass
    _clear_cache()


def _run_in_background(fn, *args):
    """Faults take a few seconds (Docker restarts); run them without freezing the page."""
    def worker():
        try:
            fn(*args)
        except Exception as exc:
            print(f"[data_real] {fn.__name__}{args} failed: {exc!r}", flush=True)
        finally:
            with _lock:
                _state["busy"] = False
            _clear_cache()

    with _lock:
        if _state["busy"]:
            print("[data_real] ignored: another fault/reset is still running", flush=True)
            return
        _state["busy"] = True
    threading.Thread(target=worker, daemon=True).start()


def trigger_fault(name: str) -> None:
    if name not in faults.FAULTS:
        raise ValueError(f"unknown fault {name!r}")
    _run_in_background(faults.trigger_fault, name)


def reset() -> None:
    _state["reset_at"] = _iso(datetime.now(timezone.utc).replace(microsecond=0))
    _run_in_background(faults.reset)
