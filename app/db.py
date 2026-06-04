"""Best-effort write of episode reviews to analytics-db.critica.*

Same fail-open pattern as qualitas.app.sink: failures here NEVER affect
the /review response. If ANALYTICS_DB_URL is unset, the connection times
out, or the role is wrong, we log and move on. Analytics is observability.
"""
import hashlib
import json
import logging
import threading
import uuid
from typing import Any, Optional

from . import config

log = logging.getLogger("critica.db")

_pool: Any = None
_pool_lock = threading.Lock()
_pool_disabled = False


def _get_pool():
    global _pool, _pool_disabled
    if _pool is not None or _pool_disabled:
        return _pool
    with _pool_lock:
        if _pool is not None or _pool_disabled:
            return _pool
        url = config.ANALYTICS_DB_URL.strip()
        if not url:
            log.info("sink disabled — ANALYTICS_DB_URL not set")
            _pool_disabled = True
            return None
        # Belt-and-suspenders: refuse to write anywhere other than the
        # prod analytics-db. Staging has no critica schema, and writing
        # cross-env would corrupt prod with staging smoke data.
        _ALLOWED_HOST = "analytics-db-rw.prod.svc.cluster.local"
        if _ALLOWED_HOST not in url:
            log.warning(
                "sink disabled — ANALYTICS_DB_URL host is not %s (got opaque url); "
                "refusing to write", _ALLOWED_HOST,
            )
            _pool_disabled = True
            return None
        try:
            from psycopg_pool import ConnectionPool
            _pool = ConnectionPool(
                conninfo=config.ANALYTICS_DB_URL,
                min_size=1,
                max_size=4,
                timeout=2.0,
                kwargs={
                    "connect_timeout": 2,
                    "options": "-c statement_timeout=2000",
                },
                open=True,
            )
            log.info("sink ready")
        except Exception as e:
            log.warning("sink init failed (%s); disabled for this process", e)
            _pool_disabled = True
            _pool = None
        return _pool


def fingerprint_key(api_key: str) -> str:
    if not api_key or api_key == "dev":
        return ""
    return hashlib.sha256(api_key.encode()).hexdigest()[:12]


_INSERT_REVIEW = """
INSERT INTO critica.episode_reviews (
    request_id, spreaker_episode_id, spreaker_show_id, show_title, title,
    duration_s, detected_language,
    pacing_wpm_mean, pacing_wpm_stdev,
    overall_score, grade,
    dimensions, summary, critique, recommendations,
    transcript_sha256, model,
    input_tokens, output_tokens, cache_read_input_tokens,
    elapsed_s, caller_fingerprint, context_ref,
    refusal_detected, refusal_severity, refusal_labels
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s,
    %s, %s,
    %s, %s,
    %s::jsonb, %s, %s, %s::jsonb,
    %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s::jsonb
)
ON CONFLICT (request_id) DO NOTHING
"""

_INSERT_TRANSCRIPT = """
INSERT INTO critica.episode_transcripts (
    request_id, transcript_full, word_timings, raw_metadata
) VALUES (%s, %s, %s::jsonb, %s::jsonb)
ON CONFLICT (request_id) DO NOTHING
"""


def record_review(
    *,
    request_id: uuid.UUID,
    spreaker_episode_id: Optional[str],
    spreaker_show_id: Optional[str],
    title: Optional[str],
    duration_s: Optional[float],
    detected_language: Optional[str],
    pacing_wpm_mean: Optional[float],
    pacing_wpm_stdev: Optional[float],
    rubric_result: dict,
    transcript: str,
    word_timings: list,
    raw_metadata: dict,
    usage: dict,
    elapsed_s: float,
    caller_fingerprint: str,
    context_ref: Optional[str],
    show_title: Optional[str] = None,
    refusal_severity: str = "none",
    refusal_labels: Optional[list] = None,
) -> None:
    pool = _get_pool()
    if pool is None:
        return
    try:
        transcript_sha = hashlib.sha256(transcript.encode("utf-8")).hexdigest() if transcript else None
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_INSERT_REVIEW, (
                    str(request_id),
                    spreaker_episode_id,
                    spreaker_show_id,
                    show_title,
                    title,
                    duration_s,
                    detected_language,
                    pacing_wpm_mean,
                    pacing_wpm_stdev,
                    rubric_result.get("overall_score"),
                    rubric_result.get("grade"),
                    json.dumps(rubric_result.get("dimensions") or []),
                    rubric_result.get("summary"),
                    rubric_result.get("critique"),
                    json.dumps(rubric_result.get("recommendations") or []),
                    transcript_sha,
                    usage.get("model"),
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                    usage.get("cache_read_input_tokens"),
                    elapsed_s,
                    caller_fingerprint or None,
                    context_ref or None,
                    refusal_severity == "hard",  # boolean: refusal_detected
                    refusal_severity,
                    json.dumps(refusal_labels or []),
                ))
                cur.execute(_INSERT_TRANSCRIPT, (
                    str(request_id),
                    transcript or None,
                    json.dumps(word_timings) if word_timings else None,
                    json.dumps(raw_metadata) if raw_metadata else None,
                ))
            conn.commit()
    except Exception as e:
        log.warning(
            "review-write failed for ctx=%s req=%s: %s",
            context_ref or "-", request_id, e,
        )
