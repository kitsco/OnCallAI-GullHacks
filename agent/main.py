"""OnCall AI agent: the main loop. Run on the server, from the repo root, in its own tmux window:

    python3.11 -m agent.main

Part A (intake + evidence): read pages from SQS, open or join an incident, gather the snapshot.
Part B (brain): Gemini investigates with read-only tools, proposes ONE allowed fix -> AWAITING_APPROVAL.
Part C (hands): after a human approves on the dashboard (status REMEDIATING), run the fix through
the SSM document, wait for it, verify the alarms return to OK, write the postmortem.

Incident status flow:
  INVESTIGATING -> AWAITING_APPROVAL -> (dashboard Approve) REMEDIATING -> VERIFYING -> RESOLVED
                                     -> (dashboard Reject) REJECTED   (VERIFYING may also -> FAILED)
                -> NEEDS_HUMAN (no safe automatic fix)
"""
import json
import time
import traceback
from datetime import datetime, timedelta, timezone

from agent import actions, brain, evidence, store
from agent.aws import identity, queue_url, sqs
from agent.config import GEMINI_API_KEY, GEMINI_MODEL, SSM_DOCUMENT, VERIFY_SETTLE_S, VERIFY_TIMEOUT_S

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
        ai_trace=dict(brain.TRACE),  # proof of the real Gemini calls: models, timings, tokens, tool calls, raw output
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


def _parse(ts: str):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def _seconds_since(ts: str) -> float:
    start = _parse(ts)
    return (datetime.now(timezone.utc) - start).total_seconds() if start else 0.0


def finish(inc: dict, status: str, result: str):
    """Close an incident: final revenue, timings, postmortem."""
    inc_id = inc["incident_id"]
    now = store.now_iso()
    opened = _parse(inc.get("opened_at"))
    fields = {"status": status, "result": result, "resolved_at": now}
    if opened:
        fields["revenue_at_risk_usd"] = evidence.revenue_at_risk(opened - LEAD_TIME)
        fields["time_to_recover_s"] = round(_seconds_since(inc["opened_at"]))
    if inc.get("diagnosed_at"):
        fields["time_to_diagnose_s"] = round((_parse(inc["diagnosed_at"]) - opened).total_seconds()) if opened else None
    store.update(inc_id, **{k: v for k, v in fields.items() if v is not None})
    latest = store.get(inc_id) or {**inc, **fields}
    store.update(inc_id, postmortem=brain.write_postmortem(latest))
    log(f"{inc_id}: {status}. {result}")


def start_fix(inc: dict):
    inc_id = inc["incident_id"]
    pa = inc.get("proposed_action") or {}
    proposed = brain.ProposedAction(action=pa.get("action", "none"), service=pa.get("service", "none"),
                                    count=pa.get("count"), explain=pa.get("explain", ""))
    problem = brain.guardrail_problem(proposed)
    if problem or proposed.action == "none":
        store.add_step(inc_id, f"Refused to run the fix: {problem or 'no fix proposed'}")
        finish(inc, "FAILED", "No allowed fix to run.")
        return
    try:
        command_id = actions.run_fix(proposed.action, proposed.service, proposed.count)
    except Exception as exc:
        store.add_step(inc_id, f"AWS refused the fix: {type(exc).__name__}: {str(exc)[:160]}")
        finish(inc, "FAILED", "The fix could not be started.")
        return
    store.update(inc_id, command_id=command_id, fix_started_at=store.now_iso())
    store.add_step(inc_id, f"Approved by a human. Running '{proposed.explain}' via SSM document "
                           f"{SSM_DOCUMENT} (command {command_id[:8]})")
    log(f"{inc_id}: started {proposed.action} {proposed.service} as SSM command {command_id}")


def check_fix(inc: dict):
    inc_id = inc["incident_id"]
    status, output = actions.fix_status(inc["command_id"])
    elapsed = _seconds_since(inc.get("fix_started_at"))
    if status == "Success":
        store.add_step(inc_id, f"Fix applied in {elapsed:.0f} s (SSM status: Success)")
        store.add_step(inc_id, "Verifying: waiting for the alarms to return to OK")
        store.update(inc_id, status="VERIFYING", verify_started_at=store.now_iso())
    elif status in ("Failed", "Cancelled", "TimedOut", "Cancelling") or elapsed > 300:
        store.add_step(inc_id, f"Fix did not complete (SSM status: {status}). {output[-200:]}")
        finish(inc, "FAILED", f"The fix failed to run (SSM status {status}).")


def check_recovery(inc: dict):
    inc_id = inc["incident_id"]
    elapsed = _seconds_since(inc.get("verify_started_at"))
    if elapsed < VERIFY_SETTLE_S:
        return
    states = evidence.alarm_states(inc.get("alarms", []))
    still = [name for name, state in states.items() if state == "ALARM"]
    if not still:
        total = _seconds_since(inc.get("opened_at"))
        store.add_step(inc_id, f"Verified: all alarms back to OK, {elapsed:.0f} s after the fix")
        finish(inc, "RESOLVED", f"{(inc.get('proposed_action') or {}).get('explain', 'Fix')}. "
                                f"Alarms cleared {elapsed:.0f} s after the fix; {total / 60:.1f} min from page to recovery.")
    elif elapsed > VERIFY_TIMEOUT_S:
        store.add_step(inc_id, f"Still in alarm after {VERIFY_TIMEOUT_S // 60} min: {', '.join(still)}")
        finish(inc, "FAILED", f"The fix ran, but {', '.join(still)} did not recover. A human should take over.")


def handle_approved():
    """Part C: move approved incidents forward one step each loop (never blocks for long)."""
    for inc in store.all_incidents():
        try:
            status = inc.get("status")
            if status == "REMEDIATING" and inc.get("approved") and not inc.get("command_id"):
                start_fix(inc)
            elif status == "REMEDIATING" and inc.get("command_id"):
                check_fix(inc)
            elif status == "VERIFYING":
                check_recovery(inc)
        except Exception:
            traceback.print_exc()


def main():
    log(f"OnCall AI agent starting. Model: {GEMINI_MODEL}. Gemini key: {'set' if GEMINI_API_KEY else 'MISSING'}")
    log(f"AWS identity: {identity()}")
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
