"""The agent's reasoning (Part B): Gemini investigates with read-only tools, then returns a
diagnosis and ONE proposed fix, which code validates against the allowed actions.

Two Gemini calls:
  1. Investigate: Gemini gets the alarm + evidence snapshot + runbooks and may call tools
     (each tool call also appears as a live step on the dashboard).
  2. Conclude: Gemini turns its findings into strict JSON (schema below), which we validate.
If Gemini is unavailable (no key, rate limit, network), simple fallback rules diagnose
from the snapshot instead, and the incident says so. The demo never stalls.
"""
import json
import logging
import time
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from agent import evidence
from agent.config import (AI_BUDGET_S, ALLOWED_ACTIONS, GEMINI_API_KEY, GEMINI_CALL_TIMEOUT_S,
                          GEMINI_FALLBACK_MODELS, GEMINI_MODEL, MAX_TOOL_CALLS, REPO_ROOT, SCALE_MAX, SCALE_MIN)

# The SDK prints a long advisory every call when tools are used; it is harmless here.
logging.getLogger("google_genai.models").setLevel(logging.ERROR)


class AIBudgetExceeded(Exception):
    pass

RootCause = Literal["bad_deploy", "traffic_spike", "slow_dependency", "db_connections", "unknown"]
Action = Literal["rollback", "scale", "restart", "fallback", "none"]
Service = Literal["payments-api", "fraud-check", "none"]


class ProposedAction(BaseModel):
    action: Action
    service: Service
    count: Optional[int] = None
    explain: str


class Diagnosis(BaseModel):
    root_cause: RootCause
    summary: str
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    why_not_other_fixes: str = ""
    expected_effect: str = ""
    proposed_action: ProposedAction


# Plain schema for Gemini's JSON mode (validated afterwards by the stricter model above).
class _DiagnosisOut(BaseModel):
    root_cause: str = Field(description="one of: bad_deploy, traffic_spike, slow_dependency, db_connections, unknown")
    summary: str = Field(description="one sentence: what broke and why, with the key number")
    evidence: list[str] = Field(description="2 to 4 short facts with numbers that prove it")
    confidence: float = Field(description="0.0 to 1.0")
    why_not_other_fixes: str = Field(description="one sentence: why the obvious alternative fix would not work")
    expected_effect: str = Field(description="what should happen after the fix, with a number")
    action: str = Field(description="one of: rollback, scale, restart, fallback, none")
    service: str = Field(description="payments-api or fraud-check (or none)")
    count: int = Field(description="number of copies, only for scale; otherwise 0")
    explain: str = Field(description="the fix in one short sentence for a human approver")


SYSTEM_PROMPT = """You are OnCall AI, an on-call site reliability engineer for a payment API.
An alarm has fired. Find the ROOT CAUSE from evidence, then propose exactly ONE fix.

The system: shoppers -> nginx -> payments-api (2 copies) -> fraud-check (fraud scoring), and Postgres.
You never see which fault was injected. Work only from metrics, logs, deploy history and alarms.

Allowed fixes (nothing else is possible):
- rollback, service payments-api: go back to the last good version (v1). For bad deploys.
- scale, service payments-api, count 1-6: change the number of copies. For real traffic overload.
- restart, service payments-api or fraud-check: fresh containers. For leaks or stuck processes.
- fallback, service fraud-check: payments-api skips fraud-check for charges under $200 and
  fraud-check is restarted. For a slow or failing fraud-check.
- none: if the evidence is unclear. Say what a human should check.

Rules:
- Compare "now" with "baseline". Say which numbers changed and which did not.
- High latency with LOW CPU means the service is waiting on something (a dependency), not overloaded:
  scaling will not help. High latency with HIGH CPU and more requests means overload.
- Errors that start right after a deploy and only come from the new release point to a bad deploy.
- Climbing database connections plus "too many clients" errors point to a connection leak.
- Be concise. Use numbers. Do not invent data you did not see.
"""


def _runbooks() -> str:
    folder = REPO_ROOT / "runbooks"
    texts = []
    for path in sorted(folder.glob("*.md")):
        if path.name.lower() == "readme.md" or path.name.startswith("fingerprints"):
            continue
        texts.append(f"--- runbook {path.stem} ---\n{path.read_text()[:2000]}")
    return "\n".join(texts) if texts else "(no runbooks written yet)"


def make_tools(record_step):
    """Read-only tools for Gemini. Each call is also recorded as a dashboard step."""

    def check_metric(service: str, metric: str) -> str:
        """Summarize one metric over the last 15 minutes (now vs baseline vs peak).

        Args:
            service: payments-api, fraud-check, or postgres
            metric: one of Requests, Errors, LatencyP95, FraudLatencyP95, CPU, Memory, Replicas,
                Restarts, DBConnections, FailedAmountUSD, SlowAmountUSD
        """
        if not (evidence.valid_service(service) and evidence.valid_metric(metric)):
            return "Unknown service or metric."
        result = evidence.summarize_metric(service, metric)["text"]
        record_step(f"Checked {result}")
        return result

    def check_recent_deploys(service: str) -> str:
        """List deploys of a service in the last 60 minutes, newest first.

        Args:
            service: payments-api or fraud-check
        """
        result = evidence.deploys_text(service, 60)
        record_step(result)
        return result

    def search_error_logs(service: str) -> str:
        """Group the 5xx errors in a service's logs from the last 10 minutes by error message and release.

        Args:
            service: payments-api or fraud-check
        """
        result = evidence.top_errors(service, 10)
        record_step(f"Searched logs. {result}")
        return result

    def check_request_timing(service: str) -> str:
        """From the logs: request count, p95 latency, and average time spent waiting on fraud-check (last 3 min).

        Args:
            service: payments-api or fraud-check
        """
        result = evidence.request_timing(service, 3)
        record_step(f"Checked request timing. {result}")
        return result

    return [check_metric, check_recent_deploys, search_error_logs, check_request_timing]


def _client():
    from google import genai
    from google.genai import types

    return genai.Client(api_key=GEMINI_API_KEY,
                        http_options=types.HttpOptions(timeout=GEMINI_CALL_TIMEOUT_S * 1000))


_last_good = {"model": None}


def _models():
    seen, order = set(), []
    for m in [_last_good["model"], GEMINI_MODEL, *GEMINI_FALLBACK_MODELS]:
        if not m:
            continue
        if m not in seen:
            seen.add(m)
            order.append(m)
    return order


def _call(client, deadline, contents, config):
    """One Gemini request: tries each model once (overloaded/rate-limited -> next model), within the deadline."""
    last = None
    for model in _models():
        if time.time() > deadline:
            raise AIBudgetExceeded(f"AI time budget of {AI_BUDGET_S} s used up")
        try:
            response = client.models.generate_content(model=model, contents=contents, config=config)
            _last_good["model"] = model  # start with the model that last worked
            return response, model
        except Exception as exc:
            last = exc
            text = repr(exc)
            print(f"  gemini error on {model}: {text[:160]}", flush=True)
            if not any(code in text for code in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "404", "NOT_FOUND",
                                                  "imeout", "Deadline")):
                raise  # a real error (bad request, bad key): don't hide it behind other models
    raise last


def _investigate(client, deadline, alarm_text, snapshot_text, record_step) -> str:
    from google.genai import types

    prompt = (f"ALARM:\n{alarm_text}\n\nEVIDENCE SNAPSHOT:\n{snapshot_text}\n\nRUNBOOKS:\n{_runbooks()}\n\n"
              f"Investigate. Use the tools to confirm or rule out causes (at most {MAX_TOOL_CALLS} calls), "
              "then write your findings: the root cause, the key numbers, and the one fix you recommend.")
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=make_tools(record_step),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=MAX_TOOL_CALLS),
        temperature=0.2,
    )
    response, model = _call(client, deadline, prompt, config)
    print(f"  investigation done with {model}", flush=True)
    return response.text or ""


def _conclude(client, deadline, alarm_text, snapshot_text, findings) -> Diagnosis:
    from google.genai import types

    prompt = (f"ALARM:\n{alarm_text}\n\nEVIDENCE SNAPSHOT:\n{snapshot_text}\n\nYOUR FINDINGS:\n{findings}\n\n"
              "Return the final diagnosis as JSON.")
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=_DiagnosisOut,
        temperature=0,
    )
    response, _ = _call(client, deadline, prompt, config)
    raw = json.loads(response.text)
    return Diagnosis(
        root_cause=raw["root_cause"], summary=raw["summary"], evidence=raw.get("evidence", []),
        confidence=max(0.0, min(1.0, float(raw.get("confidence", 0.5)))),
        why_not_other_fixes=raw.get("why_not_other_fixes", ""), expected_effect=raw.get("expected_effect", ""),
        proposed_action=ProposedAction(action=raw["action"], service=raw["service"],
                                       count=raw.get("count") or None, explain=raw["explain"]),
    )


def guardrail_problem(action: ProposedAction):
    """Returns None if the fix is allowed, else the reason it is not."""
    if action.action == "none":
        return None
    if action.service not in ALLOWED_ACTIONS.get(action.action, set()):
        return f"'{action.action}' is not allowed on {action.service}"
    if action.action == "scale" and not (action.count and SCALE_MIN <= action.count <= SCALE_MAX):
        return f"scale count must be {SCALE_MIN}-{SCALE_MAX}"
    return None


def rule_based(snap: dict) -> Diagnosis:
    """Backup diagnosis from the snapshot numbers (used only if Gemini is unavailable)."""
    m = snap["metrics"]

    def now(key, default=0.0):
        value = m.get(key, {}).get("now")
        return default if value is None else value

    def base(key, default=0.0):
        value = m.get(key, {}).get("baseline")
        return default if value is None else value

    requests, errors = now("payments-api/Requests"), now("payments-api/Errors")
    error_rate = 100 * errors / requests if requests else 0
    latency, fraud_latency = now("payments-api/LatencyP95"), now("payments-api/FraudLatencyP95")
    cpu, db = now("payments-api/CPU"), now("postgres/DBConnections")
    new_release = any(d.get("version") not in (None, "v1") for d in snap["deploys"][:1])

    def make(cause, summary, evidence_list, conf, action, service, explain, count=None):
        return Diagnosis(root_cause=cause, summary=summary, evidence=evidence_list, confidence=conf,
                         why_not_other_fixes="(rule-based backup diagnosis; AI was unavailable)",
                         expected_effect="alarm returns to OK within about 2 minutes",
                         proposed_action=ProposedAction(action=action, service=service, count=count, explain=explain))

    if new_release and error_rate > 2:
        return make("bad_deploy", f"Errors at {error_rate:.0f}% right after a new payments-api release",
                    [snap["deploys_text"], snap["errors"]],
                    0.7, "rollback", "payments-api", "Roll payments-api back to v1")
    if db > 60:
        return make("db_connections", f"Postgres connections at {db:.0f} and climbing",
                    [m["postgres/DBConnections"]["text"], snap["errors"]], 0.65,
                    "restart", "payments-api", "Restart payments-api to release leaked connections")
    if latency > 500 and fraud_latency > 500 and cpu < 50:
        return make("slow_dependency", f"payments-api is waiting on fraud-check ({fraud_latency:.0f} ms) while CPU is only {cpu:.0f}%",
                    [m["payments-api/FraudLatencyP95"]["text"], m["payments-api/CPU"]["text"]], 0.7,
                    "fallback", "fraud-check", "Turn on the fraud fallback and restart fraud-check")
    if cpu > 60 or requests > 2 * max(base("payments-api/Requests"), 1):
        return make("traffic_spike", f"Traffic surge: requests {requests:.0f} per 30 s vs {base('payments-api/Requests'):.0f} normally, CPU {cpu:.0f}%",
                    [m["payments-api/Requests"]["text"], m["payments-api/CPU"]["text"]], 0.65,
                    "scale", "payments-api", "Scale payments-api to 4 copies", count=4)
    return make("unknown", "The evidence does not match a known pattern", [snap["text"][:300]], 0.3,
                "none", "none", "No automatic fix: a human should investigate")


def diagnose(alarm_text: str, snap: dict, record_step):
    """Returns (Diagnosis, source) where source is 'gemini' or 'rules'."""
    if GEMINI_API_KEY:
        try:
            client = _client()
            deadline = time.time() + AI_BUDGET_S
            findings = _investigate(client, deadline, alarm_text, snap["text"], record_step)
            diag = _conclude(client, deadline, alarm_text, snap["text"], findings)
            problem = guardrail_problem(diag.proposed_action)
            if problem:
                record_step(f"Guardrail blocked the proposed fix ({problem}); asking a human instead")
                diag.proposed_action = ProposedAction(action="none", service="none",
                                                      explain=f"Proposed fix blocked by guardrail: {problem}")
            return diag, "gemini"
        except AIBudgetExceeded:
            record_step(f"AI took longer than {AI_BUDGET_S} s; using backup rules")
        except (ValidationError, KeyError, ValueError) as exc:
            record_step(f"AI answer was not valid ({type(exc).__name__}); using backup rules")
        except Exception as exc:
            record_step(f"AI unavailable ({type(exc).__name__}); using backup rules")
    else:
        record_step("No GEMINI_API_KEY set; using backup rules")
    return rule_based(snap), "rules"
