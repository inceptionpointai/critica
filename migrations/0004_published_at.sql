-- published_at: codify the column, backfill the Megaphone-era NULL rows,
-- and re-key the day-bucketed matviews to COALESCE(published_at, ts).
--
-- Background (2026-06-11): the episode publish date was fetched from both
-- Spreaker (`published_at`) and Megaphone (`pubdate`) but dropped before
-- db.record_review, so critica.episode_reviews.published_at — added and
-- backfilled externally for the April pilot rows — stayed NULL for every
-- producer row after the Megaphone cutover. The three matviews keyed on
-- date_trunc('day', published_at) therefore froze at 2026-04-30 and all
-- newer reviews collapsed into a single day IS NULL bucket, invisible to
-- the Superset time-series charts (Critica Reviews + Qualitas QC).
--
-- Three-part fix, all idempotent:
--   1. ALTER TABLE ... ADD COLUMN IF NOT EXISTS published_at — codifies
--      the externally-added column so fresh installs match prod.
--   2. One-time backfill of NULL rows via episode_transcripts.raw_metadata
--      ->> 'megaphone_episode_id' → megaphone.episodes.pubdate (guarded:
--      skipped where megaphone.episodes does not exist).
--   3. Re-key daily_score_summary / dimension_summary /
--      recommendation_themes to COALESCE(published_at, ts). Publish date
--      stays the primary semantic; ts is only the NULL-guard for sources
--      that genuinely lack a publish date (/review/transcript without the
--      optional published_at field, failed metadata lookups).
--
-- App-side counterpart: app/db.py now inserts published_at on every new
-- review. critica.refusal_log (view) already defends with an OR ts
-- predicate and needs no change; show_summary_30d / recent_failures are
-- ts-keyed and untouched. critica.refresh_all_views() refreshes by name
-- and survives the DROP/CREATE.

BEGIN;

-- ── 1. Codify the column ──────────────────────────────────────────────────
ALTER TABLE critica.episode_reviews
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;

-- ── 2. Backfill Megaphone-era NULL rows ───────────────────────────────────
-- Naturally idempotent: the WHERE published_at IS NULL predicate makes a
-- re-run a no-op. Guarded so the migration still applies on installs that
-- do not have the megaphone schema (it lives in the same analytics DB in
-- prod, synced by analytics/etl).
DO $$
BEGIN
    IF to_regclass('megaphone.episodes') IS NOT NULL THEN
        UPDATE critica.episode_reviews r
        SET published_at = e.pubdate
        FROM critica.episode_transcripts t
        JOIN megaphone.episodes e
          ON e.id = t.raw_metadata ->> 'megaphone_episode_id'
        WHERE t.request_id = r.request_id
          AND r.published_at IS NULL
          AND e.pubdate IS NOT NULL;
    END IF;
END;
$$;

-- ── 3a. daily_score_summary — re-key day to COALESCE(published_at, ts) ────
DROP MATERIALIZED VIEW IF EXISTS critica.daily_score_summary CASCADE;
CREATE MATERIALIZED VIEW critica.daily_score_summary AS
SELECT
    date_trunc('day', COALESCE(published_at, ts))::date                      AS day,
    COALESCE(detected_language, 'unknown')                                   AS language,
    COUNT(*)                                                                 AS total_reviews,
    COUNT(*) FILTER (WHERE refusal_detected)                                 AS refusal_leak_count,
    COUNT(*) FILTER (WHERE grade = 'A')                                      AS a_count,
    COUNT(*) FILTER (WHERE grade = 'B')                                      AS b_count,
    COUNT(*) FILTER (WHERE grade = 'C')                                      AS c_count,
    COUNT(*) FILTER (WHERE grade = 'D')                                      AS d_count,
    COUNT(*) FILTER (WHERE grade = 'F')                                      AS f_count,
    ROUND(100.0 * COUNT(*) FILTER (WHERE grade IN ('A','B')) / NULLIF(COUNT(*), 0), 2)
                                                                             AS pct_passing,
    ROUND(100.0 * COUNT(*) FILTER (WHERE refusal_detected) / NULLIF(COUNT(*), 0), 2)
                                                                             AS pct_refusal_leak,
    ROUND(AVG(overall_score)::numeric, 2)                                    AS avg_score,
    ROUND(PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY overall_score)::numeric, 2) AS p50_score,
    ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY overall_score)::numeric, 2) AS p95_score,
    MIN(overall_score)                                                       AS min_score,
    MAX(overall_score)                                                       AS max_score,
    ROUND(AVG(duration_s)::numeric, 1)                                       AS avg_duration_s,
    ROUND(AVG(pacing_wpm_mean)::numeric, 1)                                  AS avg_wpm
FROM critica.episode_reviews
GROUP BY 1, 2;
ALTER MATERIALIZED VIEW critica.daily_score_summary OWNER TO analytics;
CREATE UNIQUE INDEX idx_critica_daily_score_summary
    ON critica.daily_score_summary (day, language);

-- ── 3b. dimension_summary — same re-key ───────────────────────────────────
DROP MATERIALIZED VIEW IF EXISTS critica.dimension_summary CASCADE;
CREATE MATERIALIZED VIEW critica.dimension_summary AS
SELECT
    date_trunc('day', COALESCE(e.published_at, e.ts))::date AS day,
    d.name                         AS dimension,
    COUNT(*)                       AS sample_count,
    ROUND(AVG(d.score)::numeric, 2) AS avg_score,
    ROUND(PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY d.score)::numeric, 2) AS p50_score,
    ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY d.score)::numeric, 2) AS p95_score,
    MIN(d.score)                   AS min_score,
    MAX(d.score)                   AS max_score
FROM critica.episode_reviews e,
     LATERAL jsonb_to_recordset(e.dimensions)
        AS d(name text, score numeric, rationale text)
WHERE e.dimensions IS NOT NULL
  AND jsonb_array_length(e.dimensions) > 0
GROUP BY 1, 2;
ALTER MATERIALIZED VIEW critica.dimension_summary OWNER TO analytics;
CREATE UNIQUE INDEX idx_critica_dimension_summary
    ON critica.dimension_summary (day, dimension);

-- ── 3c. recommendation_themes — same re-key ───────────────────────────────
DROP MATERIALIZED VIEW IF EXISTS critica.recommendation_themes CASCADE;
CREATE MATERIALIZED VIEW critica.recommendation_themes AS
WITH unrolled AS (
    SELECT
        date_trunc('day', COALESCE(e.published_at, e.ts))::date AS day,
        e.request_id,
        lower(rec_text)               AS rec
    FROM critica.episode_reviews e,
         LATERAL jsonb_array_elements_text(e.recommendations) AS rec_text
    WHERE e.recommendations IS NOT NULL
)
SELECT day, theme, COUNT(*) AS mention_count, COUNT(DISTINCT request_id) AS reviews_affected
FROM (
    SELECT day, request_id,
           CASE
             WHEN rec ~ 'cta|call to action|subscribe'             THEN 'CTA improvements'
             WHEN rec ~ 'hook|cold open|first 30|opening'          THEN 'Hook quality'
             WHEN rec ~ 'biograph|historical|context'              THEN 'Add biographical context'
             WHEN rec ~ 'pacing|pause|slow down|wpm'               THEN 'Pacing/pauses'
             WHEN rec ~ 'source|citation|verify|cite'              THEN 'Citation quality'
             WHEN rec ~ 'ai |llm|parallel|template|phrase'         THEN 'Reduce AI/LLM tells'
             WHEN rec ~ 'shorter|cut|trim|reduce length'           THEN 'Cut filler'
             WHEN rec ~ 'title|thesis|frame|narrative arc'         THEN 'Narrative framing'
             ELSE 'Other'
           END AS theme
    FROM unrolled
) tagged
GROUP BY day, theme;
ALTER MATERIALIZED VIEW critica.recommendation_themes OWNER TO analytics;
CREATE UNIQUE INDEX idx_critica_recommendation_themes
    ON critica.recommendation_themes (day, theme);

-- ── Re-grant reads dropped by DROP MATERIALIZED VIEW ──────────────────────
-- Superset connects as `analytics` (the owner). The /chat read-only role
-- gets its SELECT back explicitly.
GRANT SELECT ON critica.daily_score_summary,
                critica.dimension_summary,
                critica.recommendation_themes
    TO analytics_chat_ro;

COMMIT;
