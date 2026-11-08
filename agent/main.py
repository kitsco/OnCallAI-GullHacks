"""OnCall AI agent: the main loop. Run on the server, from the repo root, in its own tmux window:

    python3.11 -m agent.main

Part A (intake + evidence): read pages from SQS, open or join an incident, gather the snapshot.
Part B (brain): Gemini investigates with read-only tools, proposes ONE allowed fix -> AWAITING_APPROVAL.
Part C (hands: run the approved fix, verify, postmortem) plugs into handle_approved() later.
"""
import json
import time
import traceback
from datetime import datetime, timedelta, timezone

from agent import brain, evidence, store
from agent.aws import queue_url, sqs
from agent.config import GEMINI_API_KEY, GEMINI_MODEL

REVENUE_EVERY_S = 20
LEAD_TIME = timedelta(minutes=3)  # faults start ~1.5-3 min before the alarm fires


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_alarm(message_body: str):
    """SQS body = the EventBridge event. Returns (alarm_name, state, reason) or None."""
    try:
        event = json.loads(message_body)
        detail = event["detail"]
        return detail["alarmName"], detail["state"]["value"], detail["state"].get("reason", "")
    except (ValueError, KeyError, TypeError):
        return None


def investigate(incident: dict, alarm_name: str, reason: str):
    inc_id = incident["incident_id"]
    seen = set()

    def record_step(text: str):
        if text in seen:  # the AI may repeat a check; show each finding once
            return
        seen.add(text)
        store.add_step(inc_id, text)

    record_step(f"Paged by alarm {alarm_name}")
    snap = evidence.snapshot()
    m = snap["metrics"]
    # A few deterministic first steps so the card fills in immediately.
    for key in ("payments-api/LatencyP95", "payments-api/Errors", "payments-api/CPU", "postgres/DBConnections"):
        if key in m:
            record_step(f"Snapshot: {m[key]['text']}")
    record_step(f"Snapshot: {snap['deploys_text']}")

    alarm_text = f"{alarm_name}: {reason}"
    diag, source = brain.diagnose(alarm_text, snap, record_step)
    action = diag.proposed_action
    proposed = {"action": action.action, "service": action.service, "explain": action.explain}
    if action.count:
        proposed["count"] = action.count
    store.update(
        inc_id,
        status="AWAITING_APPROVAL" if action.action != "none" else "NEEDS_HUMAN",
        diagnosis={
            "root_cause": diag.root_cause,
            "summary": diag.summary,
            "confidence": diag.confidence,
            "evidence": diag.evidence,
            "why_not_other_fixes": diag.why_not_other_fixes,
            "expected_effect": diag.expected_effect,
            "source": source,
        },
        proposed_action=proposed,
        diagnosed_at=store.now_iso(),
    )
    log(f"{inc_id}: {diag.root_cause} ({source}, confidence {diag.confidence:.0%}) -> {proposed}")


def handle_message(msg: dict):
    parsed = parse_alarm(msg["Body"])
    if not parsed:
        log("ignored a message that is not an alarm event")
        return
    alarm_name, state, reason = parsed
    if state != "ALARM":
        return
    current = store.open_incident()
    if current:
        store.add_alarm(current["incident_id"], alarm_name)
        log(f"{alarm_name} joined open incident {current['incident_id']}")
        return
    incident = store.create(alarm_name)
    log(f"opened {incident['incident_id']} for {alarm_name}")
    try:
        investigate(incident, alarm_name, reason)
    except Exception as exc:
        traceback.print_exc()
        store.add_step(incident["incident_id"], f"Investigation crashed: {type(exc).__name__}. A human should take over.")
        store.update(incident["incident_id"], status="NEEDS_HUMAN")


def update_revenue():
    inc = store.open_incident()
    if not inc or not inc.get("opened_at"):
        return
    opened = datetime.fromisoformat(inc["opened_at"].replace("Z", "+00:00"))
    store.update(inc["incident_id"], revenue_at_risk_usd=evidence.revenue_at_risk(opened - LEAD_TIME))


def close_if_recovered():
    """If the alarms cleared on their own (or after Reset) and nothing is being fixed, close the incident."""
    inc = store.open_incident()
    if not inc or inc.get("status") in ("REMEDIATING", "VERIFYING", "INVESTIGATING"):
        return
    stamp = inc.get("diagnosed_at") or inc.get("opened_at")
    if not stamp:
        return
    age = datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if age > timedelta(minutes=2) and not evidence.firing_alarms():
        store.update(inc["incident_id"], status="RESOLVED",
                     result="Alarms cleared without an automatic fix (recovered or reset).")
        log(f"{inc['incident_id']}: closed, alarms cleared")


def handle_approved():
    """Part C plugs in here: run approved fixes (status REMEDIATING), verify, write postmortem."""
    return


def main():
    log(f"OnCall AI agent starting. Model: {GEMINI_MODEL}. Gemini key: {'set' if GEMINI_API_KEY else 'MISSING'}")
    url = queue_url()
    log(f"listening on {url}")
    last_revenue = 0.0
    while True:
        try:
            resp = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=5, WaitTimeSeconds=10)
            for msg in resp.get("Messages", []):
                try:
                    handle_message(msg)
                finally:
                    sqs.delete_message(QueueUrl=url, ReceiptHandle=msg["ReceiptHandle"])
            handle_approved()
            if time.time() - last_revenue > REVENUE_EVERY_S:
                update_revenue()
                close_if_recovered()
                last_revenue = time.time()
        except KeyboardInterrupt:
            log("stopped")
            return
        except Exception:
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main()
