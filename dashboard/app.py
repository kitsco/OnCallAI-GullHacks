import pandas as pd
import streamlit as st
 
from data import (
    get_health, get_history, get_incidents,
    approve, reject, trigger_fault, reset,
)
 
st.set_page_config(layout="wide", page_title="OnCall AI")
st.title("OnCall AI")
 
# ---------- Health rules: the limits the status badge checks ----------
# (warning limit, critical limit) for each measured number
LIMITS = {
    "p95_ms":         (300, 500),   # ms: 95% of charges must finish faster than this
    "error_rate_pct": (1.0, 5.0),   # % of charges that fail
    "cpu_pct":        (70, 90),     # payments-api CPU
}
TRAFFIC_SURGE = 2.0   # warn if charges/min is more than 2x the 15-minute median
 
# Changes smaller than this are treated as random noise (shown grey, not red/green)
NOISE = {"charges_per_min": 100, "error_rate_pct": 0.3, "p95_ms": 15}
 
 
def check_health(health, history):
    """Compare each number against its limits. Returns (level, reasons)."""
    api = health["services"]["payments-api"]
    readings = {
        "p95_ms": (health["p95_ms"], "p95 latency", " ms"),
        "error_rate_pct": (health["error_rate_pct"], "Error rate", "%"),
        "cpu_pct": (api["cpu_pct"], "payments-api CPU", "%"),
    }
    critical, warning = [], []
    for key, (value, name, unit) in readings.items():
        warn_at, crit_at = LIMITS[key]
        if value > crit_at:
            critical.append(f"{name} {value:,}{unit} is above the {crit_at:,}{unit} limit")
        elif value > warn_at:
            warning.append(f"{name} {value:,}{unit} is above the {warn_at:,}{unit} warning level")
 
    typical = history["charges_per_min"].median()
    if health["charges_per_min"] > TRAFFIC_SURGE * typical:
        ratio = health["charges_per_min"] / typical
        warning.append(f"Traffic is {ratio:.1f}x the 15-minute normal")
 
    if health["status"] == "incident" and not critical:
        critical.append("An alarm is firing")
 
    if critical:
        return "critical", critical + warning
    if warning:
        return "warning", warning
    return "ok", []
 
 
def trend(history, col, minutes, bad_when_up):
    """Change in `col` over the last `minutes`, coloured by whether it's good or bad."""
    steps_back = 6 * minutes            # history points are 10 s apart
    past = history[col].iloc[-(steps_back + 1)]
    change = history[col].iloc[-1] - past
    text = f"{change:+,.1f}" if col == "error_rate_pct" else f"{change:+,.0f}"
    if abs(change) < NOISE[col] or bad_when_up is None:
        colour = "gray"
    elif (change > 0) == bad_when_up:
        colour = "red"
    else:
        colour = "green"
    return f":{colour}[{minutes}m {text}]"
 
 
def ai_trace_panel(inc, d):
    """Proof that a real AI made the diagnosis: models, timings, tokens, tool calls, raw output."""
    trace = inc.get("ai_trace") or {}
    if not trace:
        return
    inv, con = trace.get("investigation") or {}, trace.get("conclusion") or {}
    calls = trace.get("tool_calls") or []
    if d.get("source") == "gemini" and inv:
        tokens = (inv.get("total_tokens") or 0) + (con.get("total_tokens") or 0)
        seconds = (inv.get("seconds") or 0) + (con.get("seconds") or 0)
        st.caption(f"Diagnosed by Gemini (`{con.get('model') or inv.get('model')}`) · 2 API calls · "
                   f"{len(calls)} tool calls requested by the model · {tokens:,} tokens · {seconds:.1f} s")
    else:
        st.caption("Diagnosed by backup rules (AI unavailable): no Gemini call succeeded")
    with st.expander("Raw AI trace: what Gemini actually did"):
        for a in trace.get("attempts") or []:
            st.markdown(f"- Tried `{a['model']}`: {a['error']}")
        if calls:
            st.markdown("**Tools Gemini chose to call** (function calling, in order):")
            for c in calls:
                args = ", ".join(f"{k}={v}" for k, v in (c.get("args") or {}).items())
                st.code(f"{c.get('at', '')}  {c['tool']}({args})\n-> {c.get('result', '')}", language="text")
        if trace.get("findings"):
            st.markdown("**Gemini's own findings** (verbatim):")
            st.markdown("> " + trace["findings"].replace("\n", "\n> "))
        if trace.get("raw_json"):
            st.markdown("**Gemini's structured answer** (raw JSON, validated by our code before use):")
            st.code(trace["raw_json"], language="json")
        if inv:
            st.caption(f"Investigation: {inv.get('model')} · {inv.get('seconds')} s · "
                       f"{inv.get('prompt_tokens')} prompt / {inv.get('output_tokens')} output tokens")
        if con:
            st.caption(f"Conclusion: {con.get('model')} · {con.get('seconds')} s · "
                       f"{con.get('prompt_tokens')} prompt / {con.get('output_tokens')} output tokens")


def incident_card(inc):
    status = inc["status"]
    inc_id = inc["incident_id"]
    with st.container(border=True):
        st.subheader(f"{inc_id} · {inc['title']}")
        st.caption(f"Status: {status}  ·  Alarm: {', '.join(inc['alarms'])}  ·  "
                   f"Revenue at risk: ${inc['revenue_at_risk_usd']:,}")
 
        investigating = status == "INVESTIGATING"
        with st.status("Investigating" if investigating else "Investigation steps",
                       state="running" if investigating else "complete",
                       expanded=True):
            for step in inc["steps"]:
                st.write(f"{step['ts']} · {step['text']}")
 
        if inc["diagnosis"]:
            d, fix = inc["diagnosis"], inc["proposed_action"]
            st.info(
                f"**Root cause:** {d['root_cause']}\n\n"
                f"**Summary:** {d['summary']}\n\n"
                f"**Confidence:** {d['confidence']:.0%}\n\n"
                f"**Proposed fix:** {fix['explain']}"
            )
            ai_trace_panel(inc, d)
 
        if status == "AWAITING_APPROVAL":
            a, r = st.columns(2)
            a.button("Approve", type="primary", key=f"approve-{inc_id}",
                     on_click=approve, args=(inc_id,))
            r.button("Reject", key=f"reject-{inc_id}",
                     on_click=reject, args=(inc_id,))
        elif status == "REMEDIATING":
            st.warning("Applying the fix…")
        elif status == "VERIFYING":
            st.warning("Fix applied → verifying recovery…")
        elif status == "RESOLVED":
            st.success(inc["result"])
            if inc["postmortem"]:
                with st.expander("Postmortem"):
                    st.write(inc["postmortem"])
        elif status == "REJECTED":
            st.warning(inc["result"])
 
 
@st.fragment(run_every=2)
def live_page():
    health = get_health()
    history = pd.DataFrame(get_history(minutes=15))
    history["ts"] = pd.to_datetime(history["ts"])
    history = history.set_index("ts")
    api = health["services"]["payments-api"]
    fraud = health["services"]["fraud-check"]
 
    # Part 1: status badge, decided by checking the numbers against LIMITS
    level, reasons = check_health(health, history)
    if level == "critical":
        st.error("**Incident**  \n" + "  \n".join(reasons))
    elif level == "warning":
        st.warning("**Degraded**  \n" + "  \n".join(reasons))
    else:
        st.success("All systems normal")
 
    # Part 2: four tiles, each with its change over 1, 3 and 5 minutes
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Charges / min", f"{health['charges_per_min']:,}")
        st.markdown(" · ".join(trend(history, "charges_per_min", m, None) for m in (1, 3, 5)))
    with c2:
        st.metric("Error rate", f"{health['error_rate_pct']}%")
        st.markdown(" · ".join(trend(history, "error_rate_pct", m, True) for m in (1, 3, 5)))
    with c3:
        st.metric("p95 latency", f"{health['p95_ms']:,} ms")
        st.markdown(" · ".join(trend(history, "p95_ms", m, True) for m in (1, 3, 5)))
    with c4:
        st.metric("Revenue at risk", f"${health['revenue_at_risk_usd']:,}")
        label = "this incident" if level != "ok" else "last incident (until Reset)"
        st.caption(f"Failed charges × dollar amount, {label}")
 
    st.caption(f"payments-api: {api['replicas']} copies, CPU {api['cpu_pct']}%  ·  "
               f"fraud-check: {fraud['replicas']} copy, CPU {fraud['cpu_pct']}%")
 
    # Part 3: two line charts (last 15 minutes)
    left, right = st.columns(2)
    with left:
        st.subheader("p95 latency (ms)")
        st.line_chart(history["p95_ms"])
    with right:
        st.subheader("Error rate (%)")
        st.line_chart(history["error_rate_pct"])
 
    # Part 4: break-it buttons
    st.subheader("Break it")
    b1, b2, b3, b4, b5 = st.columns(5)
    b1.button("Bad deploy", on_click=trigger_fault, args=("bad_deploy",))
    b2.button("Black Friday rush", on_click=trigger_fault, args=("rush",))
    b3.button("Slow fraud check", on_click=trigger_fault, args=("slow_fraud",))
    b4.button("DB connection leak", on_click=trigger_fault, args=("db_leak",))
    b5.button("Reset", on_click=reset)
 
    # Part 5: incident card, showing only what's relevant right now
    incidents = get_incidents()
    newest = incidents[0] if incidents else None
    still_broken = health["status"] == "incident"
 
    if newest and (newest["status"] not in ("RESOLVED", "REJECTED")
                   or (newest["status"] == "REJECTED" and still_broken)):
        incident_card(newest)
    elif still_broken:
        with st.container(border=True):
            st.subheader("Alarm firing")
            st.write("The numbers are out of range. Waiting for the agent to open an incident…")
    else:
        st.caption("No open incidents.")
        if newest:
            with st.expander(f"Last incident: {newest['incident_id']} ({newest['status']})"):
                incident_card(newest)
 
 
live_page()
