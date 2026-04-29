"""Pydantic request/response schemas."""
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
    word_timings: Optional[List[dict]] = Field(None, description="Optional word-level timestamps from Whisper")
    context_ref: Optional[str] = None


# ── Rubric ─────────────────────────────────────────────────────────────────

class DimensionScore(BaseModel):
    name: str
    score: float = Field(..., ge=0, le=10)
    rationale: str


class RubricResult(BaseModel):
    """The structured output of the LLM scoring pass."""
    overall_score: float = Field(..., ge=0, le=10)
    grade: str = Field(..., description="A/B/C/D/F")
    dimensions: List[DimensionScore]
    summary: str = Field(..., description="One-paragraph executive summary")
    critique: str = Field(..., description="Long-form prose critique — the meat of the review")
    recommendations: List[str] = Field(default_factory=list)


# ── Response ────────────────────────────────────────────────────────────────

class ReviewResponse(BaseModel):
    request_id: str
    spreaker_episode_id: Optional[str] = None
    title: Optional[str] = None
    duration_s: Optional[float] = None
    detected_language: Optional[str] = None
    pacing_wpm_mean: Optional[float] = None
    pacing_wpm_stdev: Optional[float] = None

    rubric: RubricResult
    elapsed_s: float
    model: str
    context_ref: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    llm: str
