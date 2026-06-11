"""Pydantic request/response schemas."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


# ── Request shapes ──────────────────────────────────────────────────────────

class ReviewRequest(BaseModel):
    """POST /api/v1/review — review a Spreaker episode end-to-end."""
    spreaker_episode_id: str = Field(..., description="Spreaker episode id (numeric or string)")
    show_id: Optional[str] = Field(None, description="Optional show context")
    context_ref: Optional[str] = Field(None, description="Free-form correlation tag")
    # Override hints — improve scoring quality
    subject_name: Optional[str] = Field(None, description="The subject of the episode (e.g. 'Willie Nelson')")
    subject_brand: Optional[str] = Field(None, description="Expected brand vibe (e.g. 'Outlaw country, relaxed storytelling')")


class TranscriptReviewRequest(BaseModel):
    """POST /api/v1/review/transcript — score a transcript directly,
    skipping Spreaker fetch + Whisper. Useful for offline replays."""
    transcript: str = Field(..., description="Full episode transcript")
    title: Optional[str] = None
    description: Optional[str] = None
    subject_name: Optional[str] = None
    subject_brand: Optional[str] = None
    duration_s: Optional[float] = None
    published_at: Optional[datetime] = Field(
        None, description="Episode publish timestamp (ISO-8601) — keys the daily analytics rollups")
    word_timings: Optional[List[dict]] = Field(None, description="Optional word-level timestamps from Whisper")
    context_ref: Optional[str] = None


class MegaphoneReviewRequest(BaseModel):
    """POST /api/v1/review/megaphone — review an episode published on Megaphone.

    All three IDs are required because Megaphone's API does not support
    bare-episode-id lookups (GET /networks/<n>/podcasts/<p>/episodes/<e>).
    The producer is expected to join arc.megaphone_episodes →
    arc.megaphone_podcasts to obtain them before POSTing here.
    """
    network_id: str = Field(..., description="Megaphone network (organization) id")
    podcast_id: str = Field(..., description="Megaphone podcast (show) id")
    episode_id: str = Field(..., description="Megaphone episode id")
    context_ref: Optional[str] = Field(None, description="Free-form correlation tag")
    subject_name: Optional[str] = None
    subject_brand: Optional[str] = None


# ── Rubric ─────────────────────────────────────────────────────────────────

class DimensionScore(BaseModel):
    name: str
    # Optional because Bedrock occasionally returns "N/A" / "" / null for
    # a dimension it can't anchor against (e.g. pacing on a 30s teaser).
    # The contract: a score is either a real number 0-10 or null.
    # Non-numeric strings ("N/A", "n/a", "") are normalized to null upstream
    # in routes/review._build_rubric_result via _safe_score.
    score: Optional[float] = Field(None, ge=0, le=10)
    rationale: str


class RubricResult(BaseModel):
    """The structured output of the LLM scoring pass."""
    # See DimensionScore.score for why overall_score is nullable too.
    # In practice the LLM almost always returns a real number here — only
    # an entirely-uncoverable transcript yields null — but the API contract
    # must not 502 on the edge case.
    overall_score: Optional[float] = Field(None, ge=0, le=10)
    grade: str = Field(..., description="A/B/C/D/F (or 'N/A' when overall_score is null)")
    dimensions: List[DimensionScore]
    summary: str = Field(..., description="One-paragraph executive summary")
    critique: str = Field(..., description="Long-form prose critique — the meat of the review")
    recommendations: List[str] = Field(default_factory=list)


# ── Response ────────────────────────────────────────────────────────────────

class RefusalCheck(BaseModel):
    """Output of the deterministic refusal-leak detector — a separate
    signal from the LLM rubric. When severity='hard', the review's
    overall_score is forced to 0.0 regardless of what the LLM scored."""
    severity: str = Field(..., description="'none' | 'soft' | 'hard'")
    matched_labels: List[str] = Field(default_factory=list)
    snippets: List[str] = Field(default_factory=list)


class UsageInfo(BaseModel):
    """LLM call accounting — surfaced so the runbook can assert the backend
    that actually served the request (vs. inferring it from log lines)."""
    backend: str = Field(..., description="'bedrock' | 'anthropic' | 'openrouter'")
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    elapsed_s: Optional[float] = None


class ReviewResponse(BaseModel):
    request_id: str
    spreaker_episode_id: Optional[str] = None
    megaphone_episode_id: Optional[str] = None
    title: Optional[str] = None
    duration_s: Optional[float] = None
    detected_language: Optional[str] = None
    pacing_wpm_mean: Optional[float] = None
    pacing_wpm_stdev: Optional[float] = None

    rubric: RubricResult
    refusal: RefusalCheck = Field(
        default_factory=lambda: RefusalCheck(severity="none"),
        description="Deterministic refusal-leak check — severity='hard' overrides rubric score to 0/F",
    )
    elapsed_s: float
    model: str
    usage: UsageInfo = Field(
        ..., description="LLM backend + token accounting — used by v1.0.1 runbook to assert Bedrock served the request"
    )
    context_ref: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    llm: str
