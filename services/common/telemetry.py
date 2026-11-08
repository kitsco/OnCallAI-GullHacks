"""Shared logging and metrics for OnCall AI services.

- log_event(): one JSON line to stdout. Docker's awslogs driver ships stdout to CloudWatch Logs.
- Metrics: counts requests/errors/latency in memory and sends them to CloudWatch every
  10 seconds (only when METRICS_ENABLED=1, so laptops work without AWS).
"""
import asyncio
import datetime
import json
import os

SERVICE = os.getenv("SERVICE_NAME", "unknown")
RELEASE = os.getenv("RELEASE", "v1")
# A charge that succeeds but takes longer than this counts as "at risk":
# shoppers abandon slow checkouts. Failed charges (5xx) are always at risk.
SLOW_THRESHOLD_MS = float(os.getenv("SLOW_THRESHOLD_MS", "1000"))


def utcnow_iso() -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log_event(**fields) -> None:
    """Write one JSON log line (contract 1 in the teammate guide)."""
    record = {"ts": utcnow_iso(), "service": SERVICE, "release": RELEASE}
    record.update(fields)
    print(json.dumps(record, default=str), flush=True)


def p95(values):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, int(round(0.95 * len(ordered))) - 1)
    return ordered[index]


class Metrics:
    """In-memory counters, flushed to CloudWatch (namespace OnCallAI, dimension Service)."""

    def __init__(self, dependency_metric: str | None = None):
        self.enabled = os.getenv("METRICS_ENABLED", "0") == "1"
        self.namespace = os.getenv("METRICS_NAMESPACE", "OnCallAI")
        self.dependency_metric = dependency_metric  # e.g. "FraudLatencyP95"
        self._reset()

    def _reset(self):
        self.requests = 0
        self.errors = 0
        self.failed_amount = 0.0
        self.slow_amount = 0.0
        self.latencies = []
        self.dep_latencies = []

    def record(self, status: int, latency_ms: float, amount=None, dep_ms=None):
        self.requests += 1
        if status >= 500:
            self.errors += 1
            if amount:
                self.failed_amount += float(amount)
        elif latency_ms >= SLOW_THRESHOLD_MS and amount:
            self.slow_amount += float(amount)
        self.latencies.append(latency_ms)
        if dep_ms is not None:
            self.dep_latencies.append(dep_ms)

    def _datum(self, name, value, unit):
        return {
            "MetricName": name,
            "Dimensions": [{"Name": "Service", "Value": SERVICE}],
            "Value": float(value),
            "Unit": unit,
            "StorageResolution": 1,  # high resolution: alarms can use 10-second periods
        }

    def _take_snapshot(self):
        snap = {
            "requests": self.requests,
            "errors": self.errors,
            "failed_amount": self.failed_amount,
            "slow_amount": self.slow_amount,
            "p95": p95(self.latencies),
            "dep_p95": p95(self.dep_latencies),
        }
        self._reset()
        return snap

    def build_metric_data(self, snap):
        data = [
            self._datum("Requests", snap["requests"], "Count"),
            self._datum("Errors", snap["errors"], "Count"),
            self._datum("FailedAmountUSD", snap["failed_amount"], "None"),
            self._datum("SlowAmountUSD", snap["slow_amount"], "None"),
        ]
        if snap["p95"] is not None:
            data.append(self._datum("LatencyP95", snap["p95"], "Milliseconds"))
        if self.dependency_metric and snap["dep_p95"] is not None:
            data.append(self._datum(self.dependency_metric, snap["dep_p95"], "Milliseconds"))
        return data

    async def run(self, interval: int = 10):
        if not self.enabled:
            log_event(event="metrics_disabled")
            return
        import boto3  # imported here so laptops without AWS never need it at startup

        client = boto3.client("cloudwatch")
        log_event(event="metrics_enabled", namespace=self.namespace)
        while True:
            await asyncio.sleep(interval)
            data = self.build_metric_data(self._take_snapshot())
            try:
                await asyncio.to_thread(client.put_metric_data, Namespace=self.namespace, MetricData=data)
            except Exception as exc:  # never let metrics crash the service
                log_event(event="metrics_error", error=repr(exc))
