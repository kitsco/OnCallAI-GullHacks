"""Fake Black Friday shoppers for OnCall AI.

Each Shopper sends POST /charges with a random amount, a fake test token, and one of
5 fake merchants, waiting 0.5 to 2 s between charges (about 0.8 charges/s per shopper).
  20 shoppers  ~ 16 charges/s   (normal day; always running)
  300 shoppers ~ 240 charges/s  (the "rush" fault)

Runs on the server as the `locust` Compose service (web UI on localhost:8089).
The dashboard and scripts/faults.py change the number of shoppers through
Locust's web API: POST http://localhost:8089/swarm  (user_count, spawn_rate).
"""
import random

from locust import HttpUser, between, task

MERCHANTS = ["m_shoes", "m_books", "m_tech", "m_toys", "m_food"]


class Shopper(HttpUser):
    wait_time = between(0.5, 2)

    @task
    def buy(self):
        payload = {
            "amount": round(random.uniform(5, 500), 2),
            "currency": "USD",
            "card_token": f"tok_test_{random.randint(1000, 999999)}",  # fake token, never a card number
            "merchant_id": random.choice(MERCHANTS),
        }
        # Group every charge under one name in Locust's stats table.
        self.client.post("/charges", json=payload, name="POST /charges")
