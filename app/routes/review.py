"""POST /api/v1/review + POST /api/v1/review/transcript"""
import logging
import statistics
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException

from .. import config, db
from ..auth import require_bearer
from ..llm import claude
from ..models import (DimensionScore, RefusalCheck, ReviewRequest,
                      ReviewResponse, RubricResult, TranscriptReviewRequest)
from ..refusal import detect_refusal
from ..rubric import _bucket_wpm
from ..spreaker import fetch_episode
from ..tmpfiles import managed
from ..transcribe import fetch_audio_to_tmp, transcribe

router = APIRouter(prefix="/api/v1")
log = logging.getLogger("critica.review")


def _pacing_stats(word_timings: list) -> tuple[float | None, float | None]:
    """Mean + stdev of WPM across 20-second windows."""
    if not word_timings:
        return None, None
    wpms = _bucket_wpm(word_timings, window_s=20.0)
    wpms = [w for w in wpms if w > 0]
    if len(wpms) < 2:
        return (wpms[0] if wpms else None), 0.0
    return round(statistics.mean(wpms), 1), round(statistics.stdev(wpms), 1)


def _build_rubric_result(parsed: dict) -> RubricResult:
    """Validate the LLM's JSON against our schema, with helpful errors
    if the model went off-script."""
    try:
        return RubricResult(
            overall_score=float(parsed["overall_score"]),
            grade=str(parsed["grade"]),
            dimensions=[
                DimensionScore(
                    name=d["name"],
                    score=float(d["score"]),
                    rationale=d["rationale"],
                )
                for d in parsed["dimensions"]
            ],
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
        "review done ep=%s score=%.1f grade=%s refusal=%s elapsed_s=%.2f req=%s",
        req.spreaker_episode_id, rubric.overall_score, rubric.grade,
        refusal.severity, elapsed, request_id,
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
        context_ref=req.context_ref,
    )
