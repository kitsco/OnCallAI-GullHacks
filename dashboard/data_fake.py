"""Dashboard data layer: FAKE VERSION (no AWS, no server needed). Used when ONCALL_DATA is not "real".

The dashboard pages only ever call these 7 functions. Later this file is swapped for a real
version with the SAME names and return shapes (CloudWatch, DynamoDB, scripts/faults.py),
and the pages keep working unchanged.

    get_health()             -> dict   (tiles + status badge)
    get_history(minutes=15)  -> list   (line charts, one point every 10 s)
    get_incidents()          -> list   (incident cards, newest first)
    approve(incident_id)     -> None
    reject(incident_id)      -> None
    trigger_fault(name)      -> None   name: bad_deploy | rush | slow_fraud | db_leak
    reset()                  -> None

How the fake behaves (so every dashboard state can be seen):
  - Click a fault: the numbers go bad right away and revenue at risk starts climbing.
  - ~20 s later an incident opens (INVESTIGATING); steps appear one by one every ~5 s.
  - ~45 s after the fault: AWAITING_APPROVAL with a diagnosis and a proposed fix.
  - Approve: REMEDIATING -> VERIFYING -> RESOLVED over ~20 s, numbers recover, postmortem appears.
  - Reject: REJECTED (the fault stays until reset()).
  - reset(): everything back to normal at once.

You may change the numbers and wording in here freely. Keep the function names and shapes.
"""
import random
import time
from datetime import datetime, timezone

FAULTS = ("bad_deploy", "rush", "slow_fraud", "db_leak")
STEP_INTERVAL_S = 5
OPEN_AFTER_S = 20
DIAGNOSIS_AFTER_S = 45
AVG_CHARGE_USD = 150

# ---------- what the fake agent "finds" for each fault ----------
SCRIPTS = {
    "bad_deploy": {
        "alarm": "oncall-payments-error-rate",
        "title": "payments-api errors",
        "steps": [
            "Error rate on payments-api is 31% (normal: under 1%)",
            "Checked deploys: payments-api v2 deployed 1 min before errors started",
            "Searched logs: 412 errors, all 'KeyError: usd', all from release=v2",
            "fraud-check and Postgres look healthy",
        ],
        "diagnosis": {"root_cause": "bad_deploy",
                      "summary": "payments-api v2 has a currency bug: 31% of charges fail with KeyError: 'usd', only on v2",
                      "confidence": 0.93},
        "action": {"action": "rollback", "service": "payments-api",
                   "explain": "Roll payments-api back to v1, the last version that was healthy"},
    },
    "rush": {
        "alarm": "oncall-payments-cpu",
        "title": "payments-api overloaded",
        "steps": [
            "Requests up 6x in 2 minutes (Black Friday traffic)",
            "payments-api CPU 96% on both copies",
            "Checked deploys: none in the last 30 min",
            "fraud-check latency normal (40 ms): the dependency is fine",
        ],
        "diagnosis": {"root_cause": "traffic_spike",
                      "summary": "Real traffic surge; 2 copies of payments-api are CPU-bound",
                      "confidence": 0.9},
        "action": {"action": "scale", "service": "payments-api", "count": 4,
                   "explain": "Scale payments-api from 2 to 4 copies"},
    },
    "slow_fraud": {
        "alarm": "oncall-payments-latency",
        "title": "payments-api slow",
        "steps": [
            "p95 latency on payments-api is 1,740 ms (normal: about 40 ms)",
            "Checked deploys: none in the last 30 min",
            "payments-api CPU 22%: not overloaded, so scaling won't help",
            "fraud-check calls take 1,510 ms (normal: 40 ms)",
        ],
        "diagnosis": {"root_cause": "slow_dependency",
                      "summary": "fraud-check is slow; payments-api is waiting on it, not overloaded",
                      "confidence": 0.88},
        "action": {"action": "fallback", "service": "fraud-check",
                   "explain": "Turn on the fraud fallback for charges under $200 and restart fraud-check"},
    },
    "db_leak": {
        "alarm": "oncall-db-connections",
        "title": "database connections exhausted",
        "steps": [
            "Postgres connections at 92 of 100 and climbing",
            "Searched logs: 'too many clients already' errors starting",
            "Checked deploys: none in the last 30 min",
            "Only payments-api connections are growing",
        ],
        "diagnosis": {"root_cause": "db_connections",
                      "summary": "payments-api is leaking database connections",
                      "confidence": 0.85},
        "action": {"action": "restart", "service": "payments-api",
                   "explain": "Restart payments-api to release the leaked connections; file a bug for the leak"},
    },
}

# ---------- in-memory state (lives as long as the Streamlit server) ----------
_state = {
    "fault": None,          # active fault name
    "fault_started": None,  # time.time() when it started
    "fixed_at": None,       # when the approved fix "took effect"
    "replicas": 2,
    "incidents": [],        # full incident records
    "counter": 6,           # next incident number
    "episodes": [],         # (fault, start, end) for the history charts
}


def _now() -> float:
    return time.time()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clock(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")


def _seed_past_incident():
    """One resolved incident from earlier, so the history page has something to show."""
    t = _now() - 3 * 3600
    return {
        "incident_id": "inc-0005",
        "status": "RESOLVED",
        "opened_at": _iso(t),
        "title": "payments-api errors",
        "alarms": ["oncall-payments-error-rate"],
        "steps": [{"ts": _clock(t + 10 + 5 * i), "text": s} for i, s in enumerate(SCRIPTS["bad_deploy"]["steps"])],
        "diagnosis": SCRIPTS["bad_deploy"]["diagnosis"],
        "proposed_action": SCRIPTS["bad_deploy"]["action"],
        "approved": True,
        "result": "Rolled back to v1. Error rate back under 1% after 1 min 40 s.",
        "postmortem": "Summary: v2 shipped a currency-rounding bug. Impact: 31% of charges failed for 3 min "
                      "($4,210 at risk). Fix: rollback to v1. Follow-up: add a unit test for currency keys.",
        "revenue_at_risk_usd": 4210,
    }


_state["incidents"].append(_seed_past_incident())


def _fault_active_at(t: float):
    """Which fault (if any) was hurting the system at time t."""
    for fault, start, end in _state["episodes"]:
        if start <= t and (end is None or t < end):
            return fault, t - start
    return None, 0


def _metrics_at(t: float) -> dict:
    rnd = random.Random(int(t // 10))  # stable per 10-second point
    charges = 1200 + rnd.uniform(-60, 60)
    error = 0.3 + rnd.uniform(-0.2, 0.2)
    p95 = 42 + rnd.uniform(-6, 8)
    fault, age = _fault_active_at(t)
    if fault == "bad_deploy":
        error = 31 + rnd.uniform(-3, 3)
    elif fault == "rush":
        charges = 7200 + rnd.uniform(-300, 300)
        p95 = 900 + rnd.uniform(-80, 120)
        error = 2 + rnd.uniform(-0.5, 0.5)
    elif fault == "slow_fraud":
        p95 = 1740 + rnd.uniform(-60, 60)
    elif fault == "db_leak":
        error = min(20.0, age / 6) + rnd.uniform(0, 0.5)
        p95 = 120 + rnd.uniform(-10, 10)
    return {"charges_per_min": round(charges), "error_rate_pct": round(max(error, 0), 1), "p95_ms": round(p95)}


def _active_incident():
    for inc in _state["incidents"]:
        if inc["status"] not in ("RESOLVED", "REJECTED"):
            return inc
    return None


def _revenue_at_risk(start: float, end: float) -> float:
    """Same definition as the real version: failed charges + charges slower than 1 s."""
    total, t = 0.0, start
    while t < end:
        m = _metrics_at(t)
        per_10s = m["charges_per_min"] / 6
        failed = per_10s * m["error_rate_pct"] / 100
        slow = (per_10s - failed) if m["p95_ms"] >= 1000 else 0
        total += (failed + slow) * AVG_CHARGE_USD
        t += 10
    return round(total)


def _advance():
    """Move the fake incident forward based on how much time has passed."""
    now = _now()
    fault, started = _state["fault"], _state["fault_started"]
    inc = _active_incident()

    if fault and inc is None and now - started >= OPEN_AFTER_S and not _state.get("opened_for") == started:
        script = SCRIPTS[fault]
        inc = {
            "incident_id": f"inc-{_state['counter']:04d}",
            "status": "INVESTIGATING",
            "opened_at": _iso(started + OPEN_AFTER_S),
            "title": script["title"],
            "alarms": [script["alarm"]],
            "steps": [],
            "diagnosis": None,
            "proposed_action": None,
            "approved": None,
            "result": None,
            "postmortem": None,
            "revenue_at_risk_usd": 0,
        }
        _state["counter"] += 1
        _state["opened_for"] = started
        _state["incidents"].insert(0, inc)

    if inc and fault:
        script = SCRIPTS[fault]
        opened = started + OPEN_AFTER_S
        shown = min(len(script["steps"]), int((now - opened) // STEP_INTERVAL_S) + 1)
        inc["steps"] = [{"ts": _clock(opened + STEP_INTERVAL_S * i), "text": s}
                        for i, s in enumerate(script["steps"][:shown])]
        if inc["status"] == "INVESTIGATING" and now - started >= DIAGNOSIS_AFTER_S:
            inc["status"] = "AWAITING_APPROVAL"
            inc["diagnosis"] = dict(script["diagnosis"])
            inc["proposed_action"] = dict(script["action"])

    if inc and inc["status"] in ("REMEDIATING", "VERIFYING"):
        approved_at = inc["_approved_at"]
        if now - approved_at >= 8 and inc["status"] == "REMEDIATING":
            inc["status"] = "VERIFYING"
            _state["fixed_at"] = approved_at + 8
            _end_episode(approved_at + 8)
            if inc["proposed_action"]["action"] == "scale":
                _state["replicas"] = inc["proposed_action"].get("count", 4)
        if now - approved_at >= 20 and inc["status"] == "VERIFYING":
            inc["status"] = "RESOLVED"
            minutes, seconds = divmod(int(now - started), 60)
            inc["result"] = f"{inc['proposed_action']['explain']}. Alarm cleared; recovered {minutes} min {seconds} s after the fault began."
            inc["revenue_at_risk_usd"] = _revenue_at_risk(started, _state["fixed_at"])
            inc["postmortem"] = (f"Summary: {inc['diagnosis']['summary']}. "
                                 f"Impact: about ${inc['revenue_at_risk_usd']:,} in charges at risk. "
                                 f"Fix: {inc['proposed_action']['explain'].lower()}. "
                                 "Follow-up: add a test or alert that catches this earlier.")
            _state["fault"] = None
            _state["last_risk"] = inc["revenue_at_risk_usd"]

    if inc and _state["fault_started"]:
        end = _state["fixed_at"] or now
        inc["revenue_at_risk_usd"] = _revenue_at_risk(_state["fault_started"], end)


def _end_episode(at: float):
    for i, (fault, start, end) in enumerate(_state["episodes"]):
        if end is None:
            _state["episodes"][i] = (fault, start, at)


# ---------------- the 7 functions the dashboard uses ----------------

def get_health() -> dict:
    _advance()
    now = _now()
    m = _metrics_at(now)
    inc = _active_incident()
    fault, _ = _fault_active_at(now)
    if fault and _state["fault_started"]:
        at_risk = _revenue_at_risk(_state["fault_started"], now)
    elif inc:
        at_risk = inc["revenue_at_risk_usd"]
    else:
        at_risk = _state.get("last_risk", 0)  # keeps showing the last incident's total until reset()
    cpu = 96 if fault == "rush" else (22 if fault == "slow_fraud" else 35)
    return {
        "status": "incident" if (inc or fault) else "ok",
        "charges_per_min": m["charges_per_min"],
        "error_rate_pct": m["error_rate_pct"],
        "p95_ms": m["p95_ms"],
        "revenue_at_risk_usd": at_risk,
        "services": {
            "payments-api": {"replicas": _state["replicas"], "cpu_pct": cpu},
            "fraud-check": {"replicas": 1, "cpu_pct": 12},
        },
    }


def get_history(minutes: int = 15) -> list:
    _advance()
    now = _now()
    points = []
    t = now - minutes * 60
    while t <= now:
        m = _metrics_at(t)
        points.append({"ts": _iso(t), **m})
        t += 10
    return points


def get_incidents() -> list:
    _advance()
    # hide internal fields (those starting with "_")
    return [{k: v for k, v in inc.items() if not k.startswith("_")} for inc in _state["incidents"]]


def approve(incident_id: str) -> None:
    for inc in _state["incidents"]:
        if inc["incident_id"] == incident_id and inc["status"] == "AWAITING_APPROVAL":
            inc["approved"] = True
            inc["status"] = "REMEDIATING"
            inc["_approved_at"] = _now()


def reject(incident_id: str) -> None:
    for inc in _state["incidents"]:
        if inc["incident_id"] == incident_id and inc["status"] == "AWAITING_APPROVAL":
            inc["approved"] = False
            inc["status"] = "REJECTED"
            inc["result"] = "Fix rejected by a human. The fault is still active."


def trigger_fault(name: str) -> None:
    if name not in FAULTS:
        raise ValueError(f"unknown fault {name!r}; use one of {FAULTS}")
    if _state["fault"]:
        return  # one fault at a time, like the real demo
    now = _now()
    _state.update(fault=name, fault_started=now, fixed_at=None)
    _state["episodes"].append((name, now, None))


def reset() -> None:
    now = _now()
    _end_episode(now)
    inc = _active_incident()
    if inc:
        inc["status"] = "RESOLVED" if inc.get("approved") else "REJECTED"
        inc["result"] = inc.get("result") or "Closed by reset."
    _state.update(fault=None, fault_started=None, fixed_at=None, replicas=2, last_risk=0)
