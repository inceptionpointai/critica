"""Route-level tests for /api/v1/review and /api/v1/review/transcript.

These tests exercise the v1.0.1 blocker fixes:
  - BLOCKER #2: ReviewResponse.usage.backend surfaces the LLM backend.
  - BLOCKER #3: Spreaker 404 → HTTP 404 (not 502).
  - NTH #4:    empty transcript → HTTP 422 before Bedrock is hit.
  - NTH #5:    structured "review complete backend=… ep=… req=…" log line.
  - DB sink defense: non-prod ANALYTICS_DB_URL host refuses to open pool.

They do NOT call Bedrock, Whisper, or Spreaker — every external dependency
is monkeypatched at the route layer.
"""
import logging
import os
import re
from unittest.mock import MagicMock

# Auth is gated by CRITICA_API_KEYS — set it before importing the app.
os.environ.setdefault("CRITICA_API_KEYS", "test-key")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db as critica_db  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import review as review_route  # noqa: E402
from app.spreaker import SpreakerNotFound  # noqa: E402

# The `critica` parent logger pins propagate=False in app/__init__ so the
# default uvicorn pipeline doesn't double-print. That blocks pytest's caplog
# (which attaches at the root). Re-enable propagation for the duration of
# this test module so the existing log records reach caplog.
logging.getLogger("critica").propagate = True


AUTH = {"Authorization": "Bearer test-key"}


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def fake_rubric_parsed():
    """A minimal-but-valid rubric JSON shape that matches RubricResult."""
    return {
        "overall_score": 7.5,
        "grade": "B",
        "dimensions": [
            {"name": "subject_tone_alignment", "score": 7.0, "rationale": "ok"},
            {"name": "narrative_arc",          "score": 8.0, "rationale": "ok"},
            {"name": "original_synthesis",     "score": 7.0, "rationale": "ok"},
            {"name": "citation_quality",       "score": 6.0, "rationale": "ok"},
            {"name": "tonal_coherence",        "score": 8.0, "rationale": "ok"},
            {"name": "ai_tell_score",          "score": 9.0, "rationale": "ok"},
            {"name": "pacing_variance",        "score": 7.0, "rationale": "ok"},
            {"name": "hook_quality",           "score": 7.0, "rationale": "ok"},
            {"name": "cta_effectiveness",      "score": 6.0, "rationale": "ok"},
            {"name": "subject_body_match",     "score": 8.0, "rationale": "ok"},
        ],
        "summary": "fine episode",
        "critique": "well-paced; sources sparse",
        "recommendations": ["tighten the open"],
    }


@pytest.fixture
def fake_usage_bedrock():
    return {
        "input_tokens":  1234,
        "output_tokens":  567,
        "cache_read_input_tokens":     800,
        "cache_creation_input_tokens": 200,
        "elapsed_s": 4.2,
        "model":     "us.anthropic.claude-opus-4-5-20251101-v1:0",
        "backend":   "bedrock",
    }


@pytest.fixture(autouse=True)
def silence_db_sink(monkeypatch):
    """Tests never write to a real analytics-db. record_review is a no-op."""
    monkeypatch.setattr(critica_db, "record_review", lambda **kwargs: None)


# ── BLOCKER #3 + #2 — /api/v1/review ────────────────────────────────────────

def test_review_returns_404_when_spreaker_unknown(client, monkeypatch):
    """Spreaker 404 must map to HTTP 404, not 502 (BLOCKER #3)."""
    def _raise_not_found(_id):
        raise SpreakerNotFound("spreaker episode 00000000 not found")

    monkeypatch.setattr(review_route, "fetch_episode", _raise_not_found)

    resp = client.post(
        "/api/v1/review",
        json={"spreaker_episode_id": "00000000"},
        headers=AUTH,
    )
    assert resp.status_code == 404, resp.text
    assert "not found" in resp.json()["detail"]


def test_review_response_includes_usage_backend(
    client, monkeypatch, fake_rubric_parsed, fake_usage_bedrock,
):
    """ReviewResponse.usage.backend must surface the LLM backend (BLOCKER #2)."""
    fake_episode = {
        "spreaker_episode_id": "59595876",
        "spreaker_show_id":    "0000",
        "title":         "JAKE TALKS WITH WALT",
        "description":   "",
        "audio_url":     "https://example.com/audio.mp3",
        "duration_ms":   600_000,
        "published_at":  None,
        "site_url":      "",
        "explicit":      False,
        "image_url":     "",
        "raw":           {},
    }
    monkeypatch.setattr(review_route, "fetch_episode", lambda _id: fake_episode)
    monkeypatch.setattr(
        review_route, "fetch_audio_to_tmp", lambda _url: "/tmp/critica/fake.mp3"
    )
    monkeypatch.setattr(
        review_route, "transcribe",
        lambda _p: {"transcript": "hello world", "words": [], "duration_s": 600.0, "language": "en"},
    )
    monkeypatch.setattr(
        review_route.claude, "score_transcript",
        lambda _t, _m, _w: (fake_rubric_parsed, fake_usage_bedrock),
    )

    resp = client.post(
        "/api/v1/review",
        json={"spreaker_episode_id": "59595876", "context_ref": "v101-smoke"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["usage"]["backend"] == "bedrock"
    assert body["usage"]["model"] == "us.anthropic.claude-opus-4-5-20251101-v1:0"
    assert isinstance(body["usage"]["input_tokens"], int)
    assert body["usage"]["input_tokens"] >= 0
    assert body["usage"]["output_tokens"] >= 0
    assert body["usage"]["cache_read_input_tokens"] >= 0


# ── NTH #4 — /api/v1/review/transcript empty guard ──────────────────────────

@pytest.mark.parametrize("payload", ["", "   ", "\n\t  \n"])
def test_review_transcript_empty_returns_422(client, monkeypatch, payload):
    """Empty / whitespace-only transcript must 422 before claude is called."""
    score_mock = MagicMock()
    monkeypatch.setattr(review_route.claude, "score_transcript", score_mock)

    resp = client.post(
        "/api/v1/review/transcript",
        json={"transcript": payload},
        headers=AUTH,
    )
    assert resp.status_code == 422, resp.text
    assert score_mock.call_count == 0, "score_transcript must not be called on empty input"


# ── NTH #5 — structured "review complete" log line ──────────────────────────

_REVIEW_COMPLETE_RE = re.compile(
    r"review complete backend=\S+ model=\S+ in_tok=\d+ out_tok=\d+ "
    r"cache_read=\d+ elapsed_s=[\d.]+ score=[\d.]+ grade=[A-F] "
    r"refusal=\w+ ep=\S+ req=\S+"
)


def test_review_complete_log_is_structured(
    client, monkeypatch, caplog, fake_rubric_parsed, fake_usage_bedrock,
):
    """A single structured line "review complete backend=…" must be emitted."""
    monkeypatch.setattr(
        review_route.claude, "score_transcript",
        lambda _t, _m, _w: (fake_rubric_parsed, fake_usage_bedrock),
    )

    # Ensure the route logger propagates and is visible at INFO level.
    logging.getLogger("critica.review").setLevel(logging.INFO)
    with caplog.at_level(logging.INFO):
        resp = client.post(
            "/api/v1/review/transcript",
            json={"transcript": "a fine podcast about software"},
            headers=AUTH,
        )
    assert resp.status_code == 200, resp.text

    matched = [r for r in caplog.records if _REVIEW_COMPLETE_RE.search(r.getMessage())]
    assert matched, (
        "no structured 'review complete' log line found; got:\n"
        + "\n".join(r.getMessage() for r in caplog.records)
    )


# ── DB sink defense — non-prod host refuses to open pool ────────────────────

def test_db_sink_refuses_non_prod_host(monkeypatch, caplog):
    """The fail-open sink in db.py must refuse to dial any host other than
    the prod CNPG service, even if ANALYTICS_DB_URL is somehow injected."""
    # Reset module-level pool state between tests.
    monkeypatch.setattr(critica_db, "_pool", None, raising=False)
    monkeypatch.setattr(critica_db, "_pool_disabled", False, raising=False)
    monkeypatch.setattr(
        critica_db.config, "ANALYTICS_DB_URL",
        "postgresql://x@some-other-host:5432/y",
    )

    logging.getLogger("critica.db").setLevel(logging.WARNING)
    with caplog.at_level(logging.WARNING):
        pool = critica_db._get_pool()
    assert pool is None
    assert any(
        "host is not analytics-db-rw.prod.svc.cluster.local" in r.getMessage()
        for r in caplog.records
    ), caplog.records
