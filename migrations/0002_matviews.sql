-- Critica analytics — matviews + live failure view + refresh helper.
-- Idempotent. Apply after 0001_init.sql.
--
-- Lessons baked in from qualitas:
--   - All matviews owned by `analytics` role (matches arc/spreaker/qualitas convention)
--   - Each matview has a UNIQUE INDEX so REFRESH MATERIALIZED VIEW CONCURRENTLY works
--   - Refresh runs in <1s at current volumes (~80 reviews/day); non-concurrent is fine

BEGIN;

-- ── Daily summary ─────────────────────────────────────────────────────────
DROP MATERIALIZED VIEW IF EXISTS critica.daily_score_summary CASCADE;
CREATE MATERIALIZED VIEW critica.daily_score_summary AS
SELECT
    date_trunc('day', ts)::date                                              AS day,
    COALESCE(detected_language, 'unknown')                                   AS language,
    COUNT(*)                                                                 AS total_reviews,
    COUNT(*) FILTER (WHERE grade = 'A')                                      AS a_count,
    COUNT(*) FILTER (WHERE grade = 'B')                                      AS b_count,
    COUNT(*) FILTER (WHERE grade = 'C')                                      AS c_count,
    COUNT(*) FILTER (WHERE grade = 'D')                                      AS d_count,
    COUNT(*) FILTER (WHERE grade = 'F')                                      AS f_count,
    ROUND(100.0 * COUNT(*) FILTER (WHERE grade IN ('A','B')) / NULLIF(COUNT(*), 0), 2)
                                                                             AS pct_passing,
    ROUND(AVG(overall_score)::numeric, 2)                                    AS avg_score,
    ROUND(PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY overall_score)::numeric, 2)
                                                                             AS p50_score,
    ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY overall_score)::numeric, 2)
                                                                             AS p95_score,
    MIN(overall_score)                                                       AS min_score,
    MAX(overall_score)                                                       AS max_score,
    ROUND(AVG(duration_s)::numeric, 1)                                       AS avg_duration_s,
    ROUND(AVG(pacing_wpm_mean)::numeric, 1)                                  AS avg_wpm
FROM critica.episode_reviews
GROUP BY 1, 2;
ALTER MATERIALIZED VIEW critica.daily_score_summary OWNER TO analytics;
CREATE UNIQUE INDEX idx_critica_daily_score_summary
    ON critica.daily_score_summary (day, language);

-- ── Per-dimension daily means ─────────────────────────────────────────────
-- Explodes the JSONB `dimensions` array so each dimension becomes its own
-- row. Lets the dashboard chart "which dimension is failing most".
DROP MATERIALIZED VIEW IF EXISTS critica.dimension_summary CASCADE;
CREATE MATERIALIZED VIEW critica.dimension_summary AS
SELECT
    date_trunc('day', e.ts)::date  AS day,
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

-- ── Per-show 30-day rollup ────────────────────────────────────────────────
DROP MATERIALIZED VIEW IF EXISTS critica.show_summary_30d CASCADE;
CREATE MATERIALIZED VIEW critica.show_summary_30d AS
SELECT
    COALESCE(spreaker_show_id, '(none)')                                     AS spreaker_show_id,
    COALESCE(show_title, '(unknown show)')                                   AS show_title,
    COUNT(*)                                                                 AS total_reviews,
    COUNT(*) FILTER (WHERE grade IN ('A','B'))                               AS passing_count,
    ROUND(100.0 * COUNT(*) FILTER (WHERE grade IN ('A','B')) / NULLIF(COUNT(*), 0), 2)
                                                                             AS pct_passing,
    ROUND(AVG(overall_score)::numeric, 2)                                    AS avg_score,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY overall_score)::numeric, 2)
                                                                             AS p50_score,
    MIN(overall_score)                                                       AS min_score,
    MAX(overall_score)                                                       AS max_score,
    MAX(ts)                                                                  AS last_reviewed_at
FROM critica.episode_reviews
WHERE ts >= NOW() - INTERVAL '30 days'
GROUP BY 1, 2;
ALTER MATERIALIZED VIEW critica.show_summary_30d OWNER TO analytics;
CREATE UNIQUE INDEX idx_critica_show_summary_30d
    ON critica.show_summary_30d (spreaker_show_id);

-- ── Recommendation themes (per-day rollup) ────────────────────────────────
-- Free-form recommendation strings keyword-bucketed into editorial themes.
-- Same approach as the on-the-fly analysis we did during pilot — codify it
-- so the dashboard can chart "which producer behaviors are most flagged".
DROP MATERIALIZED VIEW IF EXISTS critica.recommendation_themes CASCADE;
CREATE MATERIALIZED VIEW critica.recommendation_themes AS
WITH unrolled AS (
    SELECT
        date_trunc('day', e.ts)::date AS day,
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

-- ── Recent failures (live VIEW, not materialized) ─────────────────────────
DROP VIEW IF EXISTS critica.recent_failures CASCADE;
CREATE VIEW critica.recent_failures AS
SELECT
    e.ts,
    e.request_id,
    e.spreaker_episode_id,
    e.show_title,
    e.title,
    e.duration_s,
    e.overall_score,
    e.grade,
    e.summary,
    e.model
FROM critica.episode_reviews e
WHERE e.ts >= NOW() - INTERVAL '30 days'
  AND e.grade IN ('D', 'F');
ALTER VIEW critica.recent_failures OWNER TO analytics;

-- ── Refresh helper ────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION critica.refresh_all_views()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    REFRESH MATERIALIZED VIEW critica.daily_score_summary;
    REFRESH MATERIALIZED VIEW critica.dimension_summary;
    REFRESH MATERIALIZED VIEW critica.show_summary_30d;
    REFRESH MATERIALIZED VIEW critica.recommendation_themes;
END;
$$;
ALTER FUNCTION critica.refresh_all_views() OWNER TO analytics;

COMMIT;
