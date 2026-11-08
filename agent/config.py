"""Settings for the OnCall AI agent. Reads the repo's .env (GEMINI_API_KEY lives there, server only)."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path):
    """Minimal .env reader: KEY=VALUE lines; never overrides variables already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(REPO_ROOT / ".env")

REGION = os.getenv("AWS_REGION", "us-east-2")
NAMESPACE = "OnCallAI"
QUEUE_NAME = "oncall-incidents"
INCIDENTS_TABLE = "incidents"
DEPLOYMENTS_TABLE = "deployments"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")
MAX_TOOL_CALLS = int(os.getenv("AGENT_MAX_TOOL_CALLS", "6"))

SERVICES = ("payments-api", "fraud-check", "postgres")
METRICS = ("Requests", "Errors", "LatencyP95", "FraudLatencyP95", "CPU", "Memory",
           "Replicas", "Restarts", "DBConnections", "FailedAmountUSD", "SlowAmountUSD")
LOG_GROUPS = {"payments-api": "/oncall/payments-api", "fraud-check": "/oncall/fraud-check"}

# The ONLY fixes the agent may propose (and, in Part C, run), and where.
ALLOWED_ACTIONS = {
    "rollback": {"payments-api"},             # back to the last good version (v1)
    "scale": {"payments-api"},                # change the number of copies
    "restart": {"payments-api", "fraud-check"},
    "fallback": {"fraud-check"},              # rule-based fraud check for charges under $200
}
SCALE_MIN, SCALE_MAX = 1, 6

CLOSED_STATUSES = ("RESOLVED", "REJECTED", "FAILED")
