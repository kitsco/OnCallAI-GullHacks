"""Break-it and reset commands for the OnCall AI demo. Runs ON THE SERVER.

Command line (from the repo root):
    python3.11 scripts/faults.py bad_deploy | rush | slow_fraud | db_leak | reset | status

From Python (the real dashboard imports these):
    from scripts.faults import trigger_fault, reset

These are the "chaos" side only. The agent never calls this file: it fixes things through
its own limited actions (SSM), and it cannot read the chaos flags.
"""
import datetime
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / ".env"
REGION = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-2"))
LOCUST_URL = os.getenv("LOCUST_URL", "http://localhost:8089")
NORMAL_USERS = int(os.getenv("NORMAL_USERS", "20"))
RUSH_USERS = int(os.getenv("RUSH_USERS", "300"))

NORMAL_FLAGS = {"fraud_latency_ms": "0", "fraud_fallback": "off", "leak_db_connections": "off"}


# ---------------- helpers ----------------

def _run(args, check=True):
    print("$", " ".join(args))
    result = subprocess.run(args, cwd=REPO, text=True, capture_output=True)
    if check and result.returncode != 0:
        print(result.stdout[-2000:], result.stderr[-2000:], sep="\n")
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(args)}")
    return result


def _compose(*args):
    return _run(["docker", "compose", *args])


def _sql(statement: str) -> str:
    result = _compose("exec", "-T", "postgres", "psql", "-U", "oncall", "-d", "payments", "-At", "-c", statement)
    return result.stdout.strip()


def set_flag(name: str, value: str):
    if name not in NORMAL_FLAGS:
        raise ValueError(f"unknown flag {name!r}")
    safe = value.replace("'", "")
    _sql(f"UPDATE chaos_flags SET value='{safe}', updated_at=now() WHERE name='{name}';")
    print(f"  flag {name} = {safe}")


def get_flags() -> dict:
    rows = _sql("SELECT name, value FROM chaos_flags ORDER BY name;")
    return dict(line.split("|", 1) for line in rows.splitlines() if "|" in line)


def current_tag() -> str:
    for line in ENV_FILE.read_text().splitlines():
        if line.strip().startswith("IMAGE_TAG="):
            return line.split("=", 1)[1].strip()
    return "v1"


def set_tag(tag: str):
    """Change IMAGE_TAG in .env (so every later `docker compose up` keeps the same version)."""
    lines = ENV_FILE.read_text().splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith("IMAGE_TAG="):
            lines[i] = f"IMAGE_TAG={tag}"
            found = True
    if not found:
        lines.insert(0, f"IMAGE_TAG={tag}")
    ENV_FILE.write_text("\n".join(lines) + "\n")
    print(f"  .env IMAGE_TAG = {tag}")


def record_deploy(version: str, by: str = "faults.py"):
    """Write the deploy to DynamoDB `deployments`. The agent reads this as deploy history."""
    try:
        import boto3

        boto3.resource("dynamodb", region_name=REGION).Table("deployments").put_item(Item={
            "service": "payments-api",
            "deployed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "version": version,
            "by": by,
        })
        print(f"  recorded deploy payments-api {version}")
    except Exception as exc:  # the demo must not stop because of this
        print(f"  WARNING: could not record deploy: {exc!r}")


def set_shoppers(users: int, spawn_rate: int = 50):
    data = urllib.parse.urlencode({"user_count": users, "spawn_rate": spawn_rate, "host": "http://nginx"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{LOCUST_URL}/swarm", data=data), timeout=5) as resp:
            print(f"  locust -> {users} shoppers ({resp.status})")
    except Exception as exc:
        print(f"  WARNING: could not reach Locust at {LOCUST_URL}: {exc!r}")


# ---------------- the faults ----------------

def bad_deploy():
    """Ship payments-api v2 (currency rounding bug, ~30% of charges fail)."""
    set_tag("v2")
    _compose("up", "-d", "payments-api")
    record_deploy("v2")


def rush():
    """Black Friday: 300 shoppers instead of 20."""
    set_shoppers(RUSH_USERS)


def slow_fraud():
    """The fraud model stalls: fraud-check waits 1.5 s per request."""
    set_flag("fraud_latency_ms", "1500")


def db_leak():
    """payments-api starts leaking a DB connection on every 5th charge."""
    set_flag("leak_db_connections", "on")


FAULTS = {"bad_deploy": bad_deploy, "rush": rush, "slow_fraud": slow_fraud, "db_leak": db_leak}


def trigger_fault(name: str):
    if name not in FAULTS:
        raise ValueError(f"unknown fault {name!r}; use one of {list(FAULTS)}")
    print(f"== fault: {name}")
    FAULTS[name]()


def reset():
    """Everything back to healthy: flags normal, v1, 2 fresh copies, fraud-check restarted, 20 shoppers."""
    print("== reset")
    for name, value in NORMAL_FLAGS.items():
        set_flag(name, value)
    was = current_tag()
    set_tag("v1")
    # --force-recreate gives fresh payments-api containers: closes leaked DB connections
    # and goes back to the 2 copies declared in docker-compose.yml.
    _compose("up", "-d", "--force-recreate", "payments-api")
    _compose("restart", "fraud-check")
    if was != "v1":
        record_deploy("v1", by="reset")
    set_shoppers(NORMAL_USERS)
    print("== healthy again in about 30 s")


def status():
    print("IMAGE_TAG:", current_tag())
    print("flags:", json.dumps(get_flags()))
    print(_compose("ps", "--format", "table {{.Service}}\t{{.Status}}").stdout)


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command in FAULTS:
        trigger_fault(command)
    elif command == "reset":
        reset()
    elif command == "status":
        status()
    else:
        print(__doc__)
        sys.exit(1)
