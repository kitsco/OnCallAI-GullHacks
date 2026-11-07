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

## After pulling new code

```bash
git pull
./scripts/build_images.sh && docker compose up -d
```

## Useful commands

| What | Command |
| --- | --- |
| See logs | `docker compose logs -f payments-api` |
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
