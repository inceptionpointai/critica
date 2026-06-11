"""POST /api/v1/review + POST /api/v1/review/transcript"""
import logging
import statistics
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException

from .. import config, db
from ..auth import require_bearer
from ..llm import claude
from ..megaphone import MegaphoneNotFound
from ..megaphone import fetch_episode as megaphone_fetch_episode
from ..models import (DimensionScore, MegaphoneReviewRequest, RefusalCheck,
                      ReviewRequest, ReviewResponse, RubricResult,
                      TranscriptReviewRequest, UsageInfo)
from ..refusal import detect_refusal
from ..rubric import _bucket_wpm
from ..spreaker import SpreakerNotFound, fetch_episode
from ..tmpfiles import managed
from ..transcribe import fetch_audio_to_tmp, transcribe

router = APIRouter(prefix="/api/v1")
log = logging.getLogger("critica.review")


def _usage_info(usage: dict, elapsed_s: float) -> UsageInfo:
    """Project the llm.claude usage dict onto the public schema."""
    return UsageInfo(
        backend=usage.get("backend", "unknown"),
        model=usage.get("model", config.CLAUDE_MODEL),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        elapsed_s=elapsed_s,
    )


def _pacing_stats(word_timings: list) -> tuple[float | None, float | None]:
    """Mean + stdev of WPM across 20-second windows."""
    if not word_timings:
        return None, None
    wpms = _bucket_wpm(word_timings, window_s=20.0)
    wpms = [w for w in wpms if w > 0]
    if len(wpms) < 2:
        return (wpms[0] if wpms else None), 0.0
    return round(statistics.mean(wpms), 1), round(statistics.stdev(wpms), 1)


_NA_TOKENS = {"n/a", "na", "none", "null", "unavailable", "unknown", "-", "—"}


def _fmt_score(score: float | None) -> str:
    """Render a possibly-null score for the structured log line. Keep the
    field shape `score=<token>` stable so the v1.0.1 NTH#5 regex still
    matches when the score happens to be unavailable for an episode."""
    return f"{score:.1f}" if score is not None else "N/A"


def _safe_score(raw, *, field: str, context: str = "") -> float | None:
    """Coerce an LLM-supplied score to a float, or to None when the model
    declined to score the dimension.

    Bedrock occasionally returns "N/A" (or "", "n/a", null, an explicit
    "unavailable" sentinel) for a single dimension on episodes where
    the rubric anchor doesn't apply (e.g. pacing_variance on a 30s
    promo). Pre-fix, ``float("N/A")`` raised ValueError → HTTP 502 for
    the entire request, even when 9/10 dimensions scored fine. We treat
    these as a structured null instead and log so the operator can spot
    a model that's punting too often.
    """
    if raw is None:
        log.warning("rubric_score_unavailable field=%s reason=null context=%s",
                    field, context or "-")
        return None
    if isinstance(raw, bool):
        # bool is an int subclass — guard before the numeric branch so
        # True/False don't slip through as 1.0/0.0.
        log.warning("rubric_score_unavailable field=%s reason=bool value=%r context=%s",
                    field, raw, context or "-")
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped or stripped.lower() in _NA_TOKENS:
            log.warning("rubric_score_unavailable field=%s reason=na_token value=%r context=%s",
                        field, raw, context or "-")
            return None
        try:
            return float(stripped)
        except ValueError:
            log.warning("rubric_score_unavailable field=%s reason=non_numeric value=%r context=%s",
                        field, raw, context or "-")
            return None
    log.warning("rubric_score_unavailable field=%s reason=unexpected_type type=%s context=%s",
                field, type(raw).__name__, context or "-")
    return None


def _build_rubric_result(parsed: dict) -> RubricResult:
    """Validate the LLM's JSON against our schema, with helpful errors
    if the model went off-script.

    Scores ("overall_score" and per-dimension "score") are run through
    _safe_score so an "N/A"/null/non-numeric value yields a null score
    rather than a 502. Structural problems (missing keys, dimensions not
    a list, missing summary/critique/grade) still raise 502 — those are
    contract violations, not score-availability gaps.
    """
    try:
        dimensions = []
        for d in parsed["dimensions"]:
            dimensions.append(DimensionScore(
                name=d["name"],
                score=_safe_score(d.get("score"), field=f"dimensions.{d.get('name', '?')}"),
                rationale=d["rationale"],
            ))
        return RubricResult(
            overall_score=_safe_score(parsed.get("overall_score"), field="overall_score"),
            grade=str(parsed["grade"]),
            dimensions=dimensions,
            summary=parsed["summary"],
            critique=parsed["critique"],
            recommendations=list(parsed.get("recommendations") or []),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(502, f"LLM returned malformed rubric result: {type(e).__name__}: {e}")


def _apply_refusal_override(rubric: RubricResult, refusal) -> RubricResult:
    """When the deterministic detector flags a hard refusal, force the
    headline score to 0.0/F regardless of what the LLM said. The LLM's
    dimensional rationale + critique prose stay intact — they're useful
    context for the operator — but the overall score reflects the
    deterministic truth: this is a refusal leak, not just bad content."""
    if refusal.severity != "hard":
        return rubric
    note = (
        f"[REFUSAL LEAK DETECTED: {len(refusal.matched_labels)} pattern(s) matched. "
        "This episode contains unedited LLM refusal text — pull from distribution.] "
    )
    return RubricResult(
        overall_score=0.0,
        grade="F",
        dimensions=rubric.dimensions,
        summary=note + rubric.summary,
        critique=rubric.critique,
        recommendations=["Pull this episode from distribution — refusal text detected"]
                        + list(rubric.recommendations),
    )


@router.post("/review", response_model=ReviewResponse)
def review(req: ReviewRequest, api_key: str = Depends(require_bearer)):
    """Pull a Spreaker episode, transcribe it, score against the rubric.

    All-or-nothing: a failure at any step returns the error to the caller
    rather than partial results. Analytics-db write is best-effort.
    """
    request_id = uuid.uuid4()
    t_start = time.time()

    log.info("review starting ep=%s ctx=%s req=%s",
             req.spreaker_episode_id, req.context_ref or "-", request_id)

    # 1. Fetch episode metadata
    try:
        episode = fetch_episode(req.spreaker_episode_id)
    except SpreakerNotFound as e:
        log.info("spreaker 404: ep=%s", req.spreaker_episode_id)
        raise HTTPException(404, str(e))
    except Exception as e:
        log.warning("spreaker fetch failed: %s", e)
        raise HTTPException(502, f"spreaker fetch failed: {e}")

    if not episode["audio_url"]:
        raise HTTPException(404, f"episode {req.spreaker_episode_id} has no audio URL")

    # 2. Download audio + transcribe
    try:
        with managed(fetch_audio_to_tmp(episode["audio_url"])) as audio_path:
            stt = transcribe(audio_path)
    except Exception as e:
        log.warning("transcription failed: %s", e)
        raise HTTPException(502, f"transcription failed: {e}")

    transcript = stt["transcript"]
    words      = stt["words"]
    duration_s = stt["duration_s"] or (episode["duration_ms"] / 1000.0 if episode["duration_ms"] else None)
    detected   = stt["language"]

    if not transcript:
        raise HTTPException(422, "Whisper returned empty transcript")

    # 3. Deterministic refusal-leak check (runs BEFORE the LLM so the
    #    rubric prompt can be informed of the result). Hard severity
    #    will force overall_score=0/F after the LLM returns.
    refusal_result = detect_refusal(transcript)
    refusal = RefusalCheck(
        severity=refusal_result.severity,
        matched_labels=refusal_result.matched_labels,
        snippets=refusal_result.snippets,
    )
    if refusal.severity == "hard":
        log.warning("refusal_leak hard: ep=%s labels=%s", req.spreaker_episode_id,
                    refusal.matched_labels[:5])

    # 4. Score against the rubric
    metadata = {
        "title":         episode.get("title"),
        "description":   episode.get("description"),
        "subject_name":  req.subject_name,
        "subject_brand": req.subject_brand,
        "duration_s":    duration_s,
        "refusal_hint":  (f"DETERMINISTIC DETECTOR FLAG: refusal severity={refusal.severity}, "
                          f"matched={refusal.matched_labels[:3]}. The transcript likely "
                          "contains unedited LLM refusal text. Score subject_body_match=0, "
                          "narrative_arc=0, original_synthesis=0 accordingly.")
                         if refusal.severity == "hard" else None,
    }
    try:
        parsed, usage = claude.score_transcript(transcript, metadata, words)
    except Exception as e:
        log.warning("claude scoring failed: %s", e)
        raise HTTPException(502, f"scoring failed: {e}")

    rubric = _apply_refusal_override(_build_rubric_result(parsed), refusal)

    # 5. Pacing stats (cheap; deterministic from word_timings)
    wpm_mean, wpm_stdev = _pacing_stats(words)

    elapsed = round(time.time() - t_start, 3)
    log.info(
        "review complete backend=%s model=%s in_tok=%d out_tok=%d cache_read=%d "
        "elapsed_s=%.2f score=%s grade=%s refusal=%s ep=%s req=%s",
        usage.get("backend", "unknown"),
        usage.get("model", config.CLAUDE_MODEL),
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        int(usage.get("cache_read_input_tokens") or 0),
        elapsed,
        _fmt_score(rubric.overall_score), rubric.grade, refusal.severity,
        req.spreaker_episode_id, request_id,
    )

    # 6. Best-effort analytics write — overwrite the rubric_result dict
    # with the post-override values so the DB matches what we returned.
    parsed_override = {
        **parsed,
        "overall_score": rubric.overall_score,
        "grade": rubric.grade,
        "summary": rubric.summary,
        "recommendations": rubric.recommendations,
    }
    db.record_review(
        request_id=request_id,
        spreaker_episode_id=episode["spreaker_episode_id"],
        spreaker_show_id=episode["spreaker_show_id"] or req.show_id,
        title=episode.get("title"),
        duration_s=duration_s,
        detected_language=detected,
        published_at=episode.get("published_at"),
        pacing_wpm_mean=wpm_mean,
        pacing_wpm_stdev=wpm_stdev,
        rubric_result=parsed_override,
        transcript=transcript,
        word_timings=words,
        raw_metadata=episode.get("raw") or {},
        usage=usage,
        elapsed_s=elapsed,
        caller_fingerprint=db.fingerprint_key(api_key),
        context_ref=req.context_ref,
        refusal_severity=refusal.severity,
        refusal_labels=refusal.matched_labels,
    )

    return ReviewResponse(
        request_id=str(request_id),
        spreaker_episode_id=episode["spreaker_episode_id"],
        title=episode.get("title"),
        duration_s=duration_s,
        detected_language=detected,
        pacing_wpm_mean=wpm_mean,
        pacing_wpm_stdev=wpm_stdev,
        rubric=rubric,
        refusal=refusal,
        elapsed_s=elapsed,
        model=usage.get("model", config.CLAUDE_MODEL),
        usage=_usage_info(usage, elapsed),
        context_ref=req.context_ref,
    )


@router.post("/review/megaphone", response_model=ReviewResponse)
def review_megaphone(req: MegaphoneReviewRequest, api_key: str = Depends(require_bearer)):
    """Pull a Megaphone episode, transcribe it, score against the rubric.

    Mirrors /review but sources the episode from Megaphone instead of
    Spreaker. All three Megaphone IDs are required because the API does
    not support bare-episode lookups.
    """
    request_id = uuid.uuid4()
    t_start = time.time()

    log.info("review starting ep=mp:%s pod=%s net=%s ctx=%s req=%s",
             req.episode_id, req.podcast_id, req.network_id,
             req.context_ref or "-", request_id)

    try:
        episode = megaphone_fetch_episode(req.network_id, req.podcast_id, req.episode_id)
    except MegaphoneNotFound as e:
        log.info("megaphone 404: ep=%s pod=%s net=%s",
                 req.episode_id, req.podcast_id, req.network_id)
        raise HTTPException(404, str(e))
    except Exception as e:
        log.warning("megaphone fetch failed: %s", e)
        raise HTTPException(502, f"megaphone fetch failed: {e}")

    if not episode["audio_url"]:
        raise HTTPException(404, f"megaphone episode {req.episode_id} has no audio URL")

    try:
        with managed(fetch_audio_to_tmp(episode["audio_url"])) as audio_path:
            stt = transcribe(audio_path)
    except Exception as e:
        log.warning("transcription failed: %s", e)
        raise HTTPException(502, f"transcription failed: {e}")

    transcript = stt["transcript"]
    words      = stt["words"]
    duration_s = stt["duration_s"] or (episode["duration_ms"] / 1000.0 if episode["duration_ms"] else None)
    detected   = stt["language"]

    if not transcript:
        raise HTTPException(422, "Whisper returned empty transcript")

    refusal_result = detect_refusal(transcript)
    refusal = RefusalCheck(
        severity=refusal_result.severity,
        matched_labels=refusal_result.matched_labels,
        snippets=refusal_result.snippets,
    )
    if refusal.severity == "hard":
        log.warning("refusal_leak hard: ep=mp:%s labels=%s",
                    req.episode_id, refusal.matched_labels[:5])

    metadata = {
        "title":         episode.get("title"),
        "description":   episode.get("description"),
        "subject_name":  req.subject_name,
        "subject_brand": req.subject_brand,
        "duration_s":    duration_s,
        "refusal_hint":  (f"DETERMINISTIC DETECTOR FLAG: refusal severity={refusal.severity}, "
                          f"matched={refusal.matched_labels[:3]}. The transcript likely "
                          "contains unedited LLM refusal text. Score subject_body_match=0, "
                          "narrative_arc=0, original_synthesis=0 accordingly.")
                         if refusal.severity == "hard" else None,
    }
    try:
        parsed, usage = claude.score_transcript(transcript, metadata, words)
    except Exception as e:
        log.warning("claude scoring failed: %s", e)
        raise HTTPException(502, f"scoring failed: {e}")

    rubric = _apply_refusal_override(_build_rubric_result(parsed), refusal)
    wpm_mean, wpm_stdev = _pacing_stats(words)
    elapsed = round(time.time() - t_start, 3)
    log.info(
        "review complete backend=%s model=%s in_tok=%d out_tok=%d cache_read=%d "
        "elapsed_s=%.2f score=%s grade=%s refusal=%s ep=mp:%s req=%s",
        usage.get("backend", "unknown"),
        usage.get("model", config.CLAUDE_MODEL),
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        int(usage.get("cache_read_input_tokens") or 0),
        elapsed,
        _fmt_score(rubric.overall_score), rubric.grade, refusal.severity,
        req.episode_id, request_id,
    )

    parsed_override = {
        **parsed,
        "overall_score": rubric.overall_score,
        "grade": rubric.grade,
        "summary": rubric.summary,
        "recommendations": rubric.recommendations,
    }
    # Megaphone ids land in raw_metadata.megaphone.* until the schema gets
    # dedicated columns (deferred — see project memory).
    raw_md = {**(episode.get("raw") or {}),
              "megaphone_episode_id": episode["megaphone_episode_id"],
              "megaphone_podcast_id": episode["megaphone_podcast_id"],
              "megaphone_network_id": episode["megaphone_network_id"]}
    db.record_review(
        request_id=request_id,
        spreaker_episode_id=None,
        spreaker_show_id=None,
        title=episode.get("title"),
        duration_s=duration_s,
        detected_language=detected,
        published_at=episode.get("published_at"),
        pacing_wpm_mean=wpm_mean,
        pacing_wpm_stdev=wpm_stdev,
        rubric_result=parsed_override,
        transcript=transcript,
        word_timings=words,
        raw_metadata=raw_md,
        usage=usage,
        elapsed_s=elapsed,
        caller_fingerprint=db.fingerprint_key(api_key),
        context_ref=req.context_ref,
        refusal_severity=refusal.severity,
        refusal_labels=refusal.matched_labels,
    )

    return ReviewResponse(
        request_id=str(request_id),
        spreaker_episode_id=None,
        megaphone_episode_id=episode["megaphone_episode_id"] or req.episode_id,
        title=episode.get("title"),
        duration_s=duration_s,
        detected_language=detected,
        pacing_wpm_mean=wpm_mean,
        pacing_wpm_stdev=wpm_stdev,
        rubric=rubric,
        refusal=refusal,
        elapsed_s=elapsed,
        model=usage.get("model", config.CLAUDE_MODEL),
        usage=_usage_info(usage, elapsed),
        context_ref=req.context_ref,
    )


@router.post("/review/transcript", response_model=ReviewResponse)
def review_transcript(req: TranscriptReviewRequest, api_key: str = Depends(require_bearer)):
    """Score a transcript directly — bypass Spreaker + Whisper.

    Useful for offline replays, A/B testing different rubric prompts on
    the same transcript, and scoring text we already have on hand.
    """
    request_id = uuid.uuid4()
    t_start = time.time()

    if not req.transcript or not req.transcript.strip():
        log.info("transcript review rejected: empty transcript req=%s", request_id)
        raise HTTPException(422, "transcript is empty")

    refusal_result = detect_refusal(req.transcript)
    refusal = RefusalCheck(
        severity=refusal_result.severity,
        matched_labels=refusal_result.matched_labels,
        snippets=refusal_result.snippets,
    )

    metadata = {
        "title":         req.title,
        "description":   req.description,
        "subject_name":  req.subject_name,
        "subject_brand": req.subject_brand,
        "duration_s":    req.duration_s,
        "refusal_hint":  (f"DETERMINISTIC DETECTOR FLAG: refusal severity={refusal.severity}, "
                          f"matched={refusal.matched_labels[:3]}. Score subject_body_match=0, "
                          "narrative_arc=0, original_synthesis=0 accordingly.")
                         if refusal.severity == "hard" else None,
    }
    try:
        parsed, usage = claude.score_transcript(req.transcript, metadata, req.word_timings)
    except Exception as e:
        log.warning("claude scoring failed: %s", e)
        raise HTTPException(502, f"scoring failed: {e}")

    rubric = _apply_refusal_override(_build_rubric_result(parsed), refusal)
    wpm_mean, wpm_stdev = _pacing_stats(req.word_timings or [])
    elapsed = round(time.time() - t_start, 3)
    log.info(
        "review complete backend=%s model=%s in_tok=%d out_tok=%d cache_read=%d "
        "elapsed_s=%.2f score=%s grade=%s refusal=%s ep=%s req=%s",
        usage.get("backend", "unknown"),
        usage.get("model", config.CLAUDE_MODEL),
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        int(usage.get("cache_read_input_tokens") or 0),
        elapsed,
        _fmt_score(rubric.overall_score), rubric.grade, refusal.severity,
        "transcript", request_id,
    )

    parsed_override = {
        **parsed,
        "overall_score": rubric.overall_score,
        "grade": rubric.grade,
        "summary": rubric.summary,
        "recommendations": rubric.recommendations,
    }

    db.record_review(
        request_id=request_id,
        spreaker_episode_id=None,
        spreaker_show_id=None,
        title=req.title,
        duration_s=req.duration_s,
        detected_language=None,
        published_at=req.published_at,
        pacing_wpm_mean=wpm_mean,
        pacing_wpm_stdev=wpm_stdev,
        rubric_result=parsed_override,
        transcript=req.transcript,
        word_timings=req.word_timings or [],
        raw_metadata={"title": req.title, "description": req.description},
        usage=usage,
        elapsed_s=elapsed,
        caller_fingerprint=db.fingerprint_key(api_key),
        context_ref=req.context_ref,
        refusal_severity=refusal.severity,
        refusal_labels=refusal.matched_labels,
    )

    return ReviewResponse(
        request_id=str(request_id),
        spreaker_episode_id=None,
        title=req.title,
        duration_s=req.duration_s,
        detected_language=None,
        pacing_wpm_mean=wpm_mean,
        pacing_wpm_stdev=wpm_stdev,
        rubric=rubric,
        refusal=refusal,
        elapsed_s=elapsed,
        model=usage.get("model", config.CLAUDE_MODEL),
        usage=_usage_info(usage, elapsed),
        context_ref=req.context_ref,
    )
