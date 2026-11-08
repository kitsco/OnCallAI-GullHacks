"""The ONLY fixes OnCall AI can run. Called by the SSM document OnCall-Remediate (never directly by the AI).

    python3.11 scripts/remediate.py rollback payments-api
    python3.11 scripts/remediate.py scale    payments-api 4
    python3.11 scripts/remediate.py restart  payments-api|fraud-check
    python3.11 scripts/remediate.py fallback fraud-check

The SSM document already limits the arguments to these values (AWS enforces it);
this script checks them again before touching anything.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import faults  # noqa: E402  (same helpers: .env tag, flags, compose, deploy records)

ALLOWED = {
    ("rollback", "payments-api"),
    ("scale", "payments-api"),
    ("restart", "payments-api"),
    ("restart", "fraud-check"),
    ("fallback", "fraud-check"),
}


def rollback():
    faults.set_tag("v1")
    faults._compose("up", "-d", "payments-api")
    faults.record_deploy("v1", by="oncall-agent")


def scale(count: int):
    faults._compose("up", "-d", "--no-recreate", "--scale", f"payments-api={count}", "payments-api")


def restart(service: str):
    # fresh containers: releases leaked DB connections / stuck processes
    faults._compose("up", "-d", "--force-recreate", service)


def fallback():
    faults.set_flag("fraud_fallback", "on")


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    action, service = argv[1], argv[2]
    count = int(argv[3]) if len(argv) > 3 and argv[3].isdigit() else 2
    if (action, service) not in ALLOWED:
        print(f"REFUSED: {action} on {service} is not an allowed fix")
        return 3
    if action == "scale" and not 1 <= count <= 6:
        print("REFUSED: scale count must be 1-6")
        return 3
    print(f"== remediate: {action} {service}" + (f" {count}" if action == "scale" else ""))
    if action == "rollback":
        rollback()
    elif action == "scale":
        scale(count)
    elif action == "restart":
        restart(service)
    elif action == "fallback":
        fallback()
    print("== done")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
