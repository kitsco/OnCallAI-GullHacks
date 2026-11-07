"""Chaos flags: read from the Postgres table chaos_flags every 5 seconds.

Flipping a row in that table (scripts/faults.py) turns a fault on or off
in every running copy of the service within about 5 seconds.
"""
import asyncio

from common.telemetry import log_event

DEFAULT_FLAGS = {
    "fraud_latency_ms": "0",
    "fraud_fallback": "off",
    "leak_db_connections": "off",
}


class FlagCache:
    def __init__(self, pool=None):
        self.pool = pool
        self.values = dict(DEFAULT_FLAGS)

    def get(self, name: str) -> str:
        return self.values.get(name, DEFAULT_FLAGS.get(name, ""))

    def is_on(self, name: str) -> bool:
        return self.get(name).lower() == "on"

    def get_int(self, name: str) -> int:
        try:
            return int(self.get(name))
        except ValueError:
            return 0

    async def refresh(self):
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT name, value FROM chaos_flags")
            rows = await cur.fetchall()
        new_values = {**DEFAULT_FLAGS, **{name: value for name, value in rows}}
        if new_values != self.values:
            log_event(event="flags_changed", flags=new_values)
        self.values = new_values

    async def run(self, interval: int = 5):
        while True:
            try:
                await self.refresh()
            except Exception as exc:
                log_event(event="flag_poll_error", error=repr(exc))
            await asyncio.sleep(interval)
