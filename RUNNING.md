# Running the OnCall AI demo app

## On the EC2 server (first time)

```bash
cd ~/OnCallAI-GullHacks
git pull
cp .env.example .env
nano .env          # set POSTGRES_PASSWORD; UNCOMMENT the two SERVER ONLY lines
./scripts/build_images.sh        # builds payments-api v1 + v2 and fraud-check (3 to 5 min first time)
docker compose up -d
docker compose ps                # expect: postgres (healthy), fraud-check, 2x payments-api, nginx
./scripts/smoke_test.sh          # 10 charges -> {"charge_id": ..., "status": "approved", ...}
```

## AWS paging setup (once; safe to re-run)

```bash
python3.11 -m pip install --user boto3
python3.11 infra/setup_aws.py
```

Creates the oncall- CloudWatch alarms, the SQS queue oncall-incidents, the EventBridge rule
oncall-alarm-to-sqs, and the DynamoDB tables incidents and deployments.

| Alarm | Fires when (2 periods of 30 s in a row) |
| --- | --- |
| oncall-payments-error-rate | more than 5% of charges fail |
| oncall-payments-latency | payments-api p95 above 500 ms |
| oncall-payments-cpu | payments-api CPU above 70% of a core |
| oncall-db-connections | more than 70 Postgres connections |
| oncall-payments-restarts / oncall-fraud-restarts | any container restart (1 period) |

There is deliberately no alarm on fraud-check latency: the agent has to work that out.

## After pulling new code

```bash
git pull
./scripts/build_images.sh && docker compose up -d
```

## Faults and reset (scripts/faults.py)

```bash
python3.11 scripts/faults.py status        # version, flags, containers
python3.11 scripts/faults.py slow_fraud    # or bad_deploy | rush | db_leak
python3.11 scripts/faults.py reset         # back to healthy (run before EVERY test)
```

Traffic: the `locust` container runs 20 shoppers all the time. Its web UI is only on the server
(localhost:8089); `rush` raises it to 300 and `reset` puts it back to 20.

## Dashboard on the server (real data)

```bash
python3.11 -m pip install --user streamlit==1.67.0 pandas==3.1.1 boto3
# in its own tmux window (Ctrl+B, then C):
cd ~/OnCallAI-GullHacks
ONCALL_DATA=real python3.11 -m streamlit run dashboard/app.py --server.port 8501 --server.address 0.0.0.0 --server.headless true
```

Open http://52.14.248.26:8501 (hackathon Wi-Fi only). Buttons really break the system (scripts/faults.py),
tiles and charts come from CloudWatch, incidents from DynamoDB. On a laptop, plain
`streamlit run dashboard/app.py` uses the fake data instead.

Revenue at risk = failed charges + charges slower than 1 s (shoppers abandon slow checkouts),
over the current or most recent bad stretch, until Reset.

## Useful commands

| What | Command |
| --- | --- |
| See logs | `docker compose logs -f payments-api` |
| See what the publisher sends | `docker compose logs --tail 3 publisher` |
| Scale payments-api | `docker compose up -d --scale payments-api=4` |
| Flip a chaos flag | `docker compose exec -T postgres psql -U oncall -d payments -c "UPDATE chaos_flags SET value='1500' WHERE name='fraud_latency_ms';"` |
| See all flags | `docker compose exec -T postgres psql -U oncall -d payments -c "SELECT * FROM chaos_flags;"` |
| Deploy v2 (bad deploy) | set `IMAGE_TAG=v2` in `.env`, then `docker compose up -d payments-api` |
| Roll back | set `IMAGE_TAG=v1` in `.env`, then `docker compose up -d payments-api` |
| Count DB connections | `docker compose exec -T postgres psql -U oncall -d payments -c "SELECT count(*) FROM pg_stat_activity;"` |
| Wipe the database | `docker compose down -v` (deletes all charges, re-runs db/init.sql) |

Always change `IMAGE_TAG` in `.env`, never only on the command line: every `docker compose up`
reads `.env`, so a one-off command-line value gets silently undone by the next `up`.

## Chaos flags (table chaos_flags)

| Flag | Normal | Broken | Effect |
| --- | --- | --- | --- |
| fraud_latency_ms | 0 | 1500 | fraud-check waits 1.5 s |
| fraud_fallback | off | on | THE FIX: payments-api skips fraud-check for charges under $200 |
| leak_db_connections | off | on | every 5th charge leaks a DB connection (fix: restart payments-api) |

Services re-read the flags every 5 seconds.
