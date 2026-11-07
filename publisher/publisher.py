"""Metrics publisher: the numbers the services can't report about themselves.

Every 10 seconds it sends to CloudWatch (namespace OnCallAI, dimension Service):
  per watched service (payments-api, fraud-check):
    CPU       average CPU % across running copies (100 = one full CPU core)
    Memory    average memory % across running copies (of the container's limit)
    Replicas  number of running copies
    Restarts  container restarts in the last interval (a crash loop shows up here)
  for Service=postgres:
    DBConnections  open Postgres connections (the DB-leak fault drives this up)

It reads container stats through the Docker socket (mounted read-only) and
the connection count from Postgres. With METRICS_ENABLED=0 it only logs the values.
"""
import datetime
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import docker
import psycopg

PROJECT = os.getenv("COMPOSE_PROJECT", "oncall")
SERVICES = [s.strip() for s in os.getenv("WATCH_SERVICES", "payments-api,fraud-check").split(",") if s.strip()]
INTERVAL = int(os.getenv("PUBLISH_INTERVAL", "10"))
NAMESPACE = os.getenv("METRICS_NAMESPACE", "OnCallAI")
ENABLED = os.getenv("METRICS_ENABLED", "0") == "1"
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://oncall:oncall-dev-pw@postgres:5432/payments")


def log(**fields):
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    print(json.dumps({"ts": ts, "service": "publisher", **fields}, default=str), flush=True)


def cpu_percent(stats: dict) -> float:
    """CPU % from one docker stats sample (same formula as `docker stats`)."""
    cpu = stats.get("cpu_stats", {})
    pre = stats.get("precpu_stats", {})
    cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get("total_usage", 0)
    system_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
    online = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or []) or 1
    if cpu_delta > 0 and system_delta > 0:
        return round(cpu_delta / system_delta * online * 100, 1)
    return 0.0


def memory_percent(stats: dict) -> float:
    mem = stats.get("memory_stats", {})
    usage = mem.get("usage", 0)
    limit = mem.get("limit", 0)
    detail = mem.get("stats", {})
    cache = detail.get("inactive_file", detail.get("total_inactive_file", 0))
    if limit:
        return round(max(usage - cache, 0) / limit * 100, 1)
    return 0.0


class Publisher:
    def __init__(self):
        self.docker = docker.from_env()
        self.cloudwatch = None
        if ENABLED:
            import boto3

            self.cloudwatch = boto3.client("cloudwatch")
        self.last_restart_counts = {}  # container id -> RestartCount at last check
        self.db = None  # kept open on purpose: during the DB-leak fault new connections get refused

    def service_metrics(self, service: str) -> dict:
        containers = self.docker.containers.list(
            all=True,
            filters={"label": [f"com.docker.compose.project={PROJECT}", f"com.docker.compose.service={service}"]},
        )
        running = [c for c in containers if c.status == "running"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            samples = list(pool.map(lambda c: c.stats(stream=False), running))
        cpus = [cpu_percent(s) for s in samples]
        mems = [memory_percent(s) for s in samples]

        restarts = 0
        for c in containers:
            count = c.attrs.get("RestartCount", 0)
            previous = self.last_restart_counts.get(c.id)
            if previous is not None and count > previous:
                restarts += count - previous
            self.last_restart_counts[c.id] = count

        return {
            "CPU": round(sum(cpus) / len(cpus), 1) if cpus else 0.0,
            "Memory": round(sum(mems) / len(mems), 1) if mems else 0.0,
            "Replicas": len(running),
            "Restarts": restarts,
        }

    def db_connections(self):
        try:
            if self.db is None or self.db.closed:
                self.db = psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=3)
            row = self.db.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = 'payments'").fetchone()
            return row[0]
        except Exception as exc:
            log(event="db_connections_error", error=repr(exc))
            self.db = None
            return None

    def publish(self, by_service: dict):
        units = {"CPU": "Percent", "Memory": "Percent", "Replicas": "Count", "Restarts": "Count", "DBConnections": "Count"}
        data = []
        for service, values in by_service.items():
            for name, value in values.items():
                if value is None:
                    continue
                data.append({
                    "MetricName": name,
                    "Dimensions": [{"Name": "Service", "Value": service}],
                    "Value": float(value),
                    "Unit": units[name],
                    "StorageResolution": 1,
                })
        if self.cloudwatch and data:
            self.cloudwatch.put_metric_data(Namespace=NAMESPACE, MetricData=data)

    def run(self):
        log(event="startup", enabled=ENABLED, services=SERVICES, interval=INTERVAL)
        while True:
            started = time.time()
            by_service = {}
            for service in SERVICES:
                try:
                    by_service[service] = self.service_metrics(service)
                except Exception as exc:
                    log(event="docker_error", target=service, error=repr(exc))
            by_service["postgres"] = {"DBConnections": self.db_connections()}
            try:
                self.publish(by_service)
            except Exception as exc:
                log(event="publish_error", error=repr(exc))
            log(event="published", metrics=by_service)
            time.sleep(max(1.0, INTERVAL - (time.time() - started)))


if __name__ == "__main__":
    Publisher().run()
