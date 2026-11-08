"""Incident records in DynamoDB (table "incidents"). Format = contract 4 in the teammate guide.

The dashboard reads these records; the agent writes them. Approve/Reject on the dashboard
change `status` (to REMEDIATING / REJECTED) and `approved`.
"""
from datetime import datetime, timezone

from agent.aws import from_dynamo, incidents, to_dynamo
from agent.config import CLOSED_STATUSES

ALARM_TITLES = {
    "oncall-payments-latency": "payments-api slow",
    "oncall-payments-error-rate": "payments-api errors",
    "oncall-payments-cpu": "payments-api overloaded",
    "oncall-db-connections": "database connections high",
    "oncall-payments-restarts": "payments-api restarting",
    "oncall-fraud-restarts": "fraud-check restarting",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def clock() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def all_incidents() -> list:
    items, kwargs = [], {}
    while True:
        resp = incidents.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return [from_dynamo(i) for i in items]


def open_incident():
    """The incident still being worked on, if any (newest first)."""
    open_ones = [i for i in all_incidents() if i.get("status") not in CLOSED_STATUSES]
    open_ones.sort(key=lambda i: i.get("opened_at", ""), reverse=True)
    return open_ones[0] if open_ones else None


def get(incident_id: str):
    item = incidents.get_item(Key={"incident_id": incident_id}).get("Item")
    return from_dynamo(item) if item else None


def create(alarm_name: str) -> dict:
    stamp = datetime.now(timezone.utc)
    item = {
        "incident_id": "inc-" + stamp.strftime("%m%d-%H%M%S"),
        "status": "INVESTIGATING",
        "opened_at": now_iso(),
        "title": ALARM_TITLES.get(alarm_name, alarm_name),
        "alarms": [alarm_name],
        "steps": [],
        "diagnosis": None,
        "proposed_action": None,
        "approved": None,
        "result": None,
        "postmortem": None,
        "revenue_at_risk_usd": 0,
    }
    incidents.put_item(Item=to_dynamo(item))
    return item


def add_alarm(incident_id: str, alarm_name: str):
    inc = get(incident_id)
    if inc and alarm_name not in inc.get("alarms", []):
        incidents.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET alarms = list_append(alarms, :a)",
            ExpressionAttributeValues={":a": [alarm_name]},
        )


def add_step(incident_id: str, text: str):
    """One line in the incident card's investigation list (shows up live on the dashboard)."""
    incidents.update_item(
        Key={"incident_id": incident_id},
        UpdateExpression="SET steps = list_append(steps, :s)",
        ExpressionAttributeValues={":s": [{"ts": clock(), "text": text}]},
    )
    print(f"  step: {text}", flush=True)


def update(incident_id: str, **fields):
    names, values, parts = {}, {}, []
    for i, (key, value) in enumerate(fields.items()):
        names[f"#k{i}"] = key
        values[f":v{i}"] = to_dynamo(value)
        parts.append(f"#k{i} = :v{i}")
    incidents.update_item(
        Key={"incident_id": incident_id},
        UpdateExpression="SET " + ", ".join(parts),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
