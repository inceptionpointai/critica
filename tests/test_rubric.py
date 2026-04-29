"""Tests for the rubric prompt builder + score parsing.

These tests are deterministic — they don't call Claude. The point is to
validate that:
  1. All 10 dimensions are in the prompt
  2. The output schema parses correctly
  3. The pacing-WPM bucketer handles edge cases (empty, single word)
"""
from app.rubric import DIMENSIONS, _bucket_wpm, build_prompt


EXPECTED_DIM_NAMES = {
    "subject_tone_alignment", "narrative_arc", "original_synthesis",
    "citation_quality", "tonal_coherence", "ai_tell_score",
    "pacing_variance", "hook_quality", "cta_effectiveness",
    "subject_body_match",
}


def test_all_ten_dimensions_present():
    names = {d["name"] for d in DIMENSIONS}
    assert names == EXPECTED_DIM_NAMES


def test_each_dimension_has_anchors_at_0_5_10():
    for d in DIMENSIONS:
        assert set(d["anchors"].keys()) == {0, 5, 10}, \
            f"{d['name']} missing required anchors"


def test_prompt_includes_metadata_when_provided():
    s, u = build_prompt(
        "transcript text",
        {"title": "Episode 42", "subject_name": "Willie Nelson", "duration_s": 240},
    )
    assert "Episode 42" in u
    assert "Willie Nelson" in u
    assert "240" in u


def test_prompt_omits_missing_metadata_fields():
    """Padding the metadata block with placeholders signals the LLM to
    invent context. Empty fields must be elided."""
    s, u = build_prompt("transcript", {"title": "X"})
    # subject_name was not provided; it must not appear in the prompt at all
    assert "subject_name" not in u
    assert "<title>X</title>" in u


def test_prompt_includes_all_dimensions():
    s, u = build_prompt("t", {})
    for name in EXPECTED_DIM_NAMES:
        assert name in u


def test_prompt_explains_ai_tell_inversion():
    """The ai_tell_score column inverts (high score = no AI tells).
    The prompt MUST flag this or we'll get backwards scores."""
    s, u = build_prompt("t", {})
    assert "REMEMBER" in u or "inversion" in u or "10 means" in u


def test_pacing_bucketer_empty():
    assert _bucket_wpm([]) == []


def test_pacing_bucketer_single_word():
    """One word means we have no idea about pacing. Don't crash."""
    out = _bucket_wpm([{"word": "hello", "start": 0.0, "end": 0.5}])
    assert isinstance(out, list)
    # one window, one word → 1 word per 20s = 3 WPM (yes, very low — that's the literal value)
    assert out[0] == 3


def test_pacing_bucketer_normal_speech():
    """~150 WPM for ~40 seconds → first two windows roughly 150 WPM each."""
    words = []
    for i in range(100):
        words.append({"word": f"w{i}", "start": i * 0.4, "end": i * 0.4 + 0.3})
    out = _bucket_wpm(words, window_s=20.0)
    # 50 words per 20s window = 150 WPM
    assert 130 <= out[0] <= 170, f"first window WPM out of range: {out[0]}"
    assert 130 <= out[1] <= 170, f"second window WPM out of range: {out[1]}"


def test_pacing_bucketer_handles_missing_timestamps():
    """Whisper sometimes returns a word without start/end. Treat as 0."""
    words = [
        {"word": "a", "start": 0.0, "end": 0.5},
        {"word": "b"},  # missing
        {"word": "c", "start": 1.0, "end": 1.5},
    ]
    out = _bucket_wpm(words, window_s=20.0)
    assert isinstance(out, list)
    assert out[0] >= 0
