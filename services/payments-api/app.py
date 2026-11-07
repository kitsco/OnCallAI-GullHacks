"""payments-api: the service merchants call to charge a card.

POST /charges  -> validate, ask fraud-check for a risk score, save to Postgres, return the result
GET  /health   -> liveness check

Faults built in (controlled by chaos flags or the image tag, never by the agent):
- RELEASE=v2 (image tag v2): currency rounding bug, about 30% of charges fail with KeyError
- leak_db_connections=on: every 5th charge opens a DB connection and never returns it
- fraud_fallback=on: the FIX for a slow fraud-check; charges under $200 skip fraud-check
"""
import asyncio
import os
import random
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from common.flags import FlagCache
from common.telemetry import RELEASE, Metrics, log_event

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://oncall:oncall-dev-pw@postgres:5432/payments")
FRAUD_URL = os.getenv("FRAUD_URL", "http://fraud-check:8000")
FRAUD_TIMEOUT_S = float(os.getenv("FRAUD_TIMEOUT_S", "3"))
FALLBACK_MAX_USD = float(os.getenv("FALLBACK_MAX_USD", "200"))
LEAK_EVERY = int(os.getenv("LEAK_EVERY", "5"))

MINOR_UNITS = {"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0}

pool: AsyncConnectionPool | None = None
http: httpx.AsyncClient | None = None
flags = FlagCache()
metrics = Metrics(dependency_metric="FraudLatencyP95")
leaked_connections = []  # the DB-leak fault parks connections here and never closes them
leak_counter = 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, http
    pool = AsyncConnectionPool(DATABASE_URL, min_size=2, max_size=10, open=False)
    await pool.open()
    flags.pool = pool
    http = httpx.AsyncClient(timeout=FRAUD_TIMEOUT_S)
    tasks = [asyncio.create_task(flags.run()), asyncio.create_task(metrics.run())]
    log_event(event="startup")
    yield
    for task in tasks:
        task.cancel()
    await http.aclose()
    await pool.close()


app = FastAPI(title="payments-api", lifespan=lifespan)


@app.middleware("http")
async def telemetry_middleware(request: Request, call_next):
    """Times every request, writes one JSON log line, and records metrics."""
    if request.url.path == "/health":
        return await call_next(request)
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request.state.ctx = {}
    start = time.perf_counter()
    error = None
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception as exc:  # unhandled bug -> 500, with the error recorded in the log
        error = f"{type(exc).__name__}: {exc}"
        status = 500
        response = JSONResponse({"error": "internal_error", "request_id": request_id}, status_code=500)
    latency_ms = round((time.perf_counter() - start) * 1000, 1)
    ctx = request.state.ctx
    error = error or ctx.get("error")
    log_event(
        request_id=request_id,
        path=request.url.path,
        status=status,
        latency_ms=latency_ms,
        fraud_ms=ctx.get("fraud_ms"),
        amount=ctx.get("amount"),
        error=error,
    )
    metrics.record(status, latency_ms, amount=ctx.get("amount"), dep_ms=ctx.get("fraud_ms"))
    response.headers["x-request-id"] = request_id
    return response


class ChargeIn(BaseModel):
    amount: float = Field(gt=0, le=10000)
    currency: str = "USD"
    card_token: str
    merchant_id: str


def to_minor_units(amount: float, currency: str) -> int:
    """Dollars -> cents (or the currency's smallest unit)."""
    if RELEASE == "v2":
        # v2 "optimization" (the injected bug): a cache key that is sometimes lower-cased,
        # so the lookup fails with KeyError: 'usd' on about 30% of charges.
        key = currency.lower() if random.random() < 0.3 else currency
        return int(round(amount * 10 ** MINOR_UNITS[key]))
    return int(round(amount * 10 ** MINOR_UNITS[currency]))


async def score_with_fraud_check(body: ChargeIn):
    """Returns (risk_score, decision, source, fraud_ms)."""
    if flags.is_on("fraud_fallback") and body.amount < FALLBACK_MAX_USD:
        # Fallback: simple rule instead of the fraud model. Trades a little risk for not losing sales.
        return None, "approve", "fallback", None
    start = time.perf_counter()
    resp = await http.post(
        f"{FRAUD_URL}/score",
        json={"amount": body.amount, "card_token": body.card_token, "merchant_id": body.merchant_id},
    )
    fraud_ms = round((time.perf_counter() - start) * 1000, 1)
    resp.raise_for_status()
    data = resp.json()
    return data["risk_score"], data["decision"], "model", fraud_ms


INSERT_SQL = (
    "INSERT INTO charges (id, merchant_id, amount_minor, currency, status, fraud_score, fraud_source, release) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
)


async def save_charge(params: tuple):
    global leak_counter
    if flags.is_on("leak_db_connections"):
        leak_counter += 1
        if leak_counter % LEAK_EVERY == 0:
            # Injected bug: open a fresh connection and never close it.
            conn = await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)
            leaked_connections.append(conn)
            await conn.execute(INSERT_SQL, params)
            return
    async with pool.connection() as conn:
        await conn.execute(INSERT_SQL, params)


@app.post("/charges")
async def create_charge(body: ChargeIn, request: Request):
    ctx = request.state.ctx
    ctx["amount"] = body.amount
    currency = body.currency.upper()
    if currency not in MINOR_UNITS:
        raise HTTPException(status_code=400, detail="unsupported currency")
    if not body.card_token.startswith("tok_test_"):
        raise HTTPException(status_code=400, detail="use test tokens that start with tok_test_")

    amount_minor = to_minor_units(body.amount, currency)

    try:
        risk_score, decision, source, fraud_ms = await score_with_fraud_check(body)
    except httpx.HTTPError as exc:
        ctx["error"] = f"FraudCheckUnavailable: {type(exc).__name__}"
        raise HTTPException(status_code=503, detail="fraud check unavailable")
    ctx["fraud_ms"] = fraud_ms

    status = "approved" if decision == "approve" else "declined"
    charge_id = "ch_" + uuid.uuid4().hex[:16]
    await save_charge((charge_id, body.merchant_id, amount_minor, currency, status, risk_score, source, RELEASE))
    return {
        "charge_id": charge_id,
        "status": status,
        "amount": body.amount,
        "currency": currency,
        "fraud_source": source,
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "payments-api", "release": RELEASE}
