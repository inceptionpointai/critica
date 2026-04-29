-- Critica analytics tables — applied once to analytics-db.
--
-- Idempotent. Pass the writer password via psql -v pass='...'. The DO
-- block trick from qualitas doesn't work for password assignment because
-- psql doesn't expand :'pass' inside dollar-quoted bodies — use \gexec
-- to construct the statement on the client.
--
-- Convention (matches arc/ovs/spreaker/qualitas in this DB): the
-- `analytics` role owns the schema; the writer role gets only INSERT +
-- SELECT (SELECT is required for ON CONFLICT detection — without it,
-- ON CONFLICT DO NOTHING fails with a misleading "permission denied for
-- table" error).

BEGIN;

CREATE SCHEMA IF NOT EXISTS critica AUTHORIZATION analytics;

-- ── Slim row, one per /review call ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS critica.episode_reviews (
    request_id              UUID PRIMARY KEY,
    ts                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    spreaker_episode_id     TEXT,
    spreaker_show_id        TEXT,
    title                   TEXT,
    duration_s              NUMERIC(10, 2),
    detected_language       TEXT,

    pacing_wpm_mean         NUMERIC(7, 2),
    pacing_wpm_stdev        NUMERIC(7, 2),

    overall_score           NUMERIC(4, 2),
    grade                   TEXT,
    dimensions              JSONB,                 -- array of {name, score, rationale}
    summary                 TEXT,
    critique                TEXT,
    recommendations         JSONB,                 -- array of strings

    transcript_sha256       CHAR(64),
    model                   TEXT,
    input_tokens            INTEGER,
    output_tokens           INTEGER,
    cache_read_input_tokens INTEGER,

    elapsed_s               NUMERIC(7, 3),
    caller_fingerprint      TEXT,
    context_ref             TEXT
);
ALTER TABLE critica.episode_reviews OWNER TO analytics;
CREATE INDEX IF NOT EXISTS idx_critica_reviews_ts          ON critica.episode_reviews (ts DESC);
CREATE INDEX IF NOT EXISTS idx_critica_reviews_episode     ON critica.episode_reviews (spreaker_episode_id);
CREATE INDEX IF NOT EXISTS idx_critica_reviews_show        ON critica.episode_reviews (spreaker_show_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_critica_reviews_grade_ts    ON critica.episode_reviews (grade, ts DESC);

-- ── Fat blob, troubleshooting only ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS critica.episode_transcripts (
    request_id      UUID PRIMARY KEY
                      REFERENCES critica.episode_reviews(request_id) ON DELETE CASCADE,
    transcript_full TEXT,
    word_timings    JSONB,
    raw_metadata    JSONB
);
ALTER TABLE critica.episode_transcripts OWNER TO analytics;

-- ── Writer role ───────────────────────────────────────────────────────────
SELECT 'CREATE ROLE critica_writer LOGIN'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'critica_writer')
\gexec

SELECT format('ALTER ROLE critica_writer PASSWORD %L', :'pass')
\gexec

GRANT USAGE ON SCHEMA critica TO critica_writer;
GRANT INSERT, SELECT ON critica.episode_reviews, critica.episode_transcripts TO critica_writer;

-- Owner already has SELECT implicitly; make it explicit for parity.
GRANT USAGE ON SCHEMA critica TO analytics;
GRANT SELECT ON ALL TABLES IN SCHEMA critica TO analytics;
ALTER DEFAULT PRIVILEGES IN SCHEMA critica
    GRANT SELECT ON TABLES TO analytics;

COMMIT;
