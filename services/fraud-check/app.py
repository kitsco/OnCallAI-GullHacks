"""fraud-check: scores each charge for fraud risk. payments-api waits on it.

POST /score  -> {"risk_score": 0.0-1.0, "decision": "approve" | "decline"}
GET  /health -> liveness check

Fault built in: chaos flag fraud_latency_ms (0 normally, 1500 when "the fraud model stalls").
"""
import asyncio
import hashlib
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from common.flags import FlagCache
from common.telemetry import Metrics, log_event

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://oncall:oncall-dev-pw@postgres:5432/payments")
DECLINE_ABOVE = float(os.getenv("DECLINE_ABOVE", "0.8"))

pool: AsyncConnectionPool | None = None
flags = FlagCache()
metrics = Metrics()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=3, open=False)
    await pool.open()
    flags.pool = pool
    tasks = [asyncio.create_task(flags.run()), asyncio.create_task(metrics.run())]
    log_event(event="startup")
    yield
    for task in tasks:
        task.cancel()
    await pool.close()


app = FastAPI(title="fraud-check", lifespan=lifespan)


@app.middleware("http")
async def telemetry_middleware(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    start = time.perf_counter()
    error = None
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        status = 500
        response = JSONResponse({"error": "internal_error"}, status_code=500)
    latency_ms = round((time.perf_counter() - start) * 1000, 1)
    log_event(request_id=request_id, path=request.url.path, status=status, latency_ms=latency_ms, error=error)
    metrics.record(status, latency_ms)
    return response


class ScoreIn(BaseModel):
    amount: float
    card_token: str
    merchant_id: str


def risk_score(body: ScoreIn) -> float:
    """Made-up but stable score: bigger charges and some tokens look riskier."""
    token_hash = int(hashlib.sha256(body.card_token.encode()).hexdigest(), 16)
    base = (token_hash % 40) / 100  # 0.00 to 0.39
    size = min(body.amount / 1000, 0.5)  # up to 0.5 for large charges
    return round(min(base + size, 1.0), 3)


@app.post("/score")
async def score(body: ScoreIn):
    delay_ms = flags.get_int("fraud_latency_ms")
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000)  # the "fraud model stalls" fault
    value = risk_score(body)
    return {"risk_score": value, "decision": "decline" if value > DECLINE_ABOVE else "approve"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "fraud-check"}
