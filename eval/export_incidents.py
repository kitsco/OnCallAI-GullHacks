"""Evaluation export: turns test runs into a results table. Run on the server from the repo root:

    python3.11 eval/export_incidents.py            # all runs
    python3.11 eval/export_incidents.py --since 07:30   # only runs after 07:30 UTC today

Inputs (both automatic):
  - eval/fault_log.csv   written by scripts/faults.py every time a fault is injected (dashboard or CLI)
  - DynamoDB "incidents" written by the agent (opened, diagnosed, fix started, resolved, ...)
Each injected fault is matched to the first incident opened within 10 minutes after it.

Outputs:
  - eval/results.csv     one row per run (open in Sheets/Excel)
  - a summary table printed in Markdown, ready to paste into the slides or Devpost
"""
import csv
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3

REPO = Path(__file__).resolve().parent.parent
FAULT_LOG = REPO / "eval" / "fault_log.csv"
OUT = REPO / "eval" / "results.csv"
EXPECTED = {"bad_deploy": "bad_deploy", "rush": "traffic_spike",
            "slow_fraud": "slow_dependency", "db_leak": "db_connections"}
MATCH_WINDOW = timedelta(minutes=10)


def parse(ts):
    if not ts:
        return None
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def seconds(a, b):
    return round((b - a).total_seconds()) if a and b else None


def load_faults(since):
    if not FAULT_LOG.exists():
        sys.exit(f"No {FAULT_LOG} yet: inject a fault first (dashboard or scripts/faults.py).")
    rows = list(csv.DictReader(FAULT_LOG.open()))
    faults = [(parse(r["injected_at"]), r["fault"]) for r in rows]
    return [f for f in faults if not since or f[0] >= since]


def load_incidents():
    table = boto3.resource("dynamodb", region_name="us-east-2").Table("incidents")
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    items.sort(key=lambda i: i.get("opened_at", ""))
    return items


def build_rows(faults, incidents):
    rows, used = [], set()
    for injected_at, fault in faults:
        match = None
        for inc in incidents:
            opened = parse(inc.get("opened_at"))
            if inc["incident_id"] not in used and opened and injected_at <= opened <= injected_at + MATCH_WINDOW:
                match = inc
                break
        row = {"run": len(rows) + 1, "fault": fault, "injected_at": injected_at.strftime("%H:%M:%S"),
               "expected_cause": EXPECTED.get(fault, "?")}
        if not match:
            rows.append({**row, "incident_id": "", "status": "NOT DETECTED"})
            continue
        used.add(match["incident_id"])
        diag = match.get("diagnosis") or {}
        action = match.get("proposed_action") or {}
        opened, diagnosed = parse(match.get("opened_at")), parse(match.get("diagnosed_at"))
        fix_started, resolved = parse(match.get("fix_started_at")), parse(match.get("resolved_at"))
        detect = seconds(injected_at, opened)
        diagnose = seconds(opened, diagnosed)
        approval = seconds(diagnosed, fix_started)
        fix_to_ok = seconds(fix_started, resolved)
        machine = sum(x for x in (detect, diagnose, fix_to_ok) if x is not None) if fix_to_ok is not None else None
        rows.append({
            **row,
            "incident_id": match["incident_id"],
            "diagnosed_cause": diag.get("root_cause", ""),
            "correct": "Y" if diag.get("root_cause") == EXPECTED.get(fault) else "N",
            "brain": diag.get("source", ""),
            "confidence": float(diag["confidence"]) if diag.get("confidence") is not None else "",
            "fix": f"{action.get('action', '')} {action.get('service', '')}".strip(),
            "status": match.get("status", ""),
            "detect_s": detect, "diagnose_s": diagnose, "approval_wait_s": approval,
            "fix_to_recovered_s": fix_to_ok, "machine_time_s": machine,
            "revenue_at_risk_usd": int(match.get("revenue_at_risk_usd", 0) or 0),
        })
    return rows


def median(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(values)) if values else None


def fmt(v, unit=" s"):
    return "n/a" if v is None else f"{v}{unit}"


def main():
    since = None
    if "--since" in sys.argv:
        hh, mm = sys.argv[sys.argv.index("--since") + 1].split(":")
        today = datetime.now(timezone.utc)
        since = today.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    rows = build_rows(load_faults(since), load_incidents())
    if not rows:
        sys.exit("No runs found.")
    fields = ["run", "fault", "injected_at", "incident_id", "expected_cause", "diagnosed_cause", "correct", "brain",
              "confidence", "fix", "status", "detect_s", "diagnose_s", "approval_wait_s", "fix_to_recovered_s",
              "machine_time_s", "revenue_at_risk_usd"]
    with OUT.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    detected = [r for r in rows if r.get("incident_id")]
    correct = [r for r in detected if r.get("correct") == "Y"]
    resolved = [r for r in detected if r.get("status") == "RESOLVED"]
    print(f"\nWrote {OUT} ({len(rows)} runs)\n")
    print("| Fault | Runs | Detected | Correct diagnosis | Resolved | Median detect | Median diagnose | Median fix→OK | Median machine time |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for fault in sorted({r["fault"] for r in rows}):
        rs = [r for r in rows if r["fault"] == fault]
        print(f"| {fault} | {len(rs)} | {sum(1 for r in rs if r.get('incident_id'))} | "
              f"{sum(1 for r in rs if r.get('correct') == 'Y')} | {sum(1 for r in rs if r.get('status') == 'RESOLVED')} | "
              f"{fmt(median(r.get('detect_s') for r in rs))} | {fmt(median(r.get('diagnose_s') for r in rs))} | "
              f"{fmt(median(r.get('fix_to_recovered_s') for r in rs))} | {fmt(median(r.get('machine_time_s') for r in rs))} |")
    print(f"| **All** | {len(rows)} | {len(detected)} | {len(correct)} | {len(resolved)} | "
          f"{fmt(median(r.get('detect_s') for r in rows))} | {fmt(median(r.get('diagnose_s') for r in rows))} | "
          f"{fmt(median(r.get('fix_to_recovered_s') for r in rows))} | {fmt(median(r.get('machine_time_s') for r in rows))} |")
    if detected:
        print(f"\nDiagnosis accuracy: {len(correct)}/{len(detected)} = {100 * len(correct) / len(detected):.0f}%"
              f" · AI (Gemini) answered {sum(1 for r in detected if r.get('brain') == 'gemini')}/{len(detected)}"
              f" · median machine time {fmt(median(r.get('machine_time_s') for r in rows))}"
              " (fault → detected → diagnosed, plus fix → verified; excludes the human approval wait)")


if __name__ == "__main__":
    main()
