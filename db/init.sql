-- Runs once, the first time the Postgres volume is created.
-- To re-run it: docker compose down -v   (this deletes all charges)

CREATE TABLE IF NOT EXISTS charges (
    id            TEXT PRIMARY KEY,
    merchant_id   TEXT NOT NULL,
    amount_minor  BIGINT NOT NULL,          -- cents
    currency      TEXT NOT NULL,
    status        TEXT NOT NULL,            -- approved | declined
    fraud_score   DOUBLE PRECISION,         -- NULL when the fallback was used
    fraud_source  TEXT NOT NULL,            -- model | fallback
    release       TEXT NOT NULL,            -- v1 | v2
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chaos_flags (
    name        TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every flag starts at "normal".
INSERT INTO chaos_flags (name, value) VALUES
    ('fraud_latency_ms', '0'),
    ('fraud_fallback', 'off'),
    ('leak_db_connections', 'off')
ON CONFLICT (name) DO NOTHING;
