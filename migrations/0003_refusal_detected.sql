-- Add deterministic refusal-leak signal alongside the LLM rubric score.
-- Forward-compatible: existing reviews get refusal_detected=NULL (treated
-- as "not yet checked"), new reviews always set the column.

BEGIN;

ALTER TABLE critica.episode_reviews
    ADD COLUMN IF NOT EXISTS refusal_detected BOOLEAN,
    ADD COLUMN IF NOT EXISTS refusal_severity TEXT,    -- 'none' | 'soft' | 'hard'
    ADD COLUMN IF NOT EXISTS refusal_labels   JSONB;

CREATE INDEX IF NOT EXISTS idx_critica_reviews_refusal
    ON critica.episode_reviews (refusal_detected, ts DESC)
    WHERE refusal_detected = TRUE;

-- Rebuild daily_score_summary to expose refusal counts.
DROP MATERIALIZED VIEW IF EXISTS critica.daily_score_summary CASCADE;
CREATE MATERIALIZED VIEW critica.daily_score_summary AS
SELECT
    date_trunc('day', published_at)::date                                    AS day,
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

-- Live VIEW for ops: every refusal-flagged review in the last 30 days.
DROP VIEW IF EXISTS critica.refusal_log CASCADE;
CREATE VIEW critica.refusal_log AS
SELECT
    e.ts,
    e.published_at,
    e.spreaker_episode_id,
    e.show_title,
    e.title,
    e.refusal_severity,
    e.refusal_labels,
    e.overall_score,
    e.grade,
    e.summary,
    'https://www.spreaker.com/episode/' || e.spreaker_episode_id AS spreaker_url
FROM critica.episode_reviews e
WHERE e.refusal_detected = TRUE
  AND (e.published_at >= NOW() - INTERVAL '30 days'
       OR e.ts          >= NOW() - INTERVAL '30 days');
ALTER VIEW critica.refusal_log OWNER TO analytics;

COMMIT;
