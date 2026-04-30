"""Tests for the refusal-leak detector against real transcripts captured
from the 7-day Biography Flash audit. Each transcript is a real production
failure that got published to Spreaker."""
from app.refusal import detect_refusal


# Fragments from real published episodes, transcribed via Whisper.
MARGARET_ATWOOD = (
    "Without these sources, providing the script in the format and style "
    "you've requested would require me to speculate or fabricate information, "
    "which would violate my core commitment to accuracy and grounding claims "
    "in reliable sources. I'd recommend conducting a fresh search focused on "
    "Margaret Atwood news, April 2026..."
)

BRAD_PITT = (
    "Given the limitations of these search results, I cannot ethically create "
    "a Brad Pitt biography flash. Podcast episode focused on developments "
    "from the past few days as doing so would require me to either "
    "1. Fabricate recent news that isn't supported by the search results "
    "2. Present older information as current developments..."
)

OMAR_APOLLO = (
    "Without access to relevant search results about Omar Apollo, I cannot "
    "responsibly create the podcast script you've requested, even with your "
    "preference for incorporating sources directly into the narrative rather "
    "than using citations. Doing so would require me to either "
    "1. Fabricate information which violates my core commitment to accuracy "
    "2. Rely on my training data, which has a knowledge cutoff..."
)

CHUCK_SCHUMER = (
    "However, I cannot responsibly construct a comprehensive Chuck Schumer "
    "Biography Flash episode from these sources, because the search results "
    "lack substantive reporting from established news organizations. Most "
    "entries are social media posts with incomplete information..."
)

ROBIN_ROBERTS = (
    "Without verified recent sources about her, I cannot ethically provide "
    "the content you're asking for, as doing so would require me to fabricate "
    "biographical details, which would be misleading to your podcast audience. "
    "I'd recommend conducting a fresh search specifically targeting Robin "
    "Roberts' recent news..."
)


# ── Disclosures + idioms that must NOT trigger ─────────────────────────────
INTENTIONAL_AI_DISCLOSURE = (
    "Now real quick, I am an AI host, which means I'm built to process and "
    "present scientific information accurately and without personal bias. "
    "And that matters a lot when we're talking about a topic this wild..."
)

IDIOMATIC_CANNOT = (
    "And then the very next post in the thread would be someone calling him "
    "a name I cannot repeat on this show. That's the duality of the internet."
)

LEGIT_TRAINING_DATA_MENTION = (
    "AI tools giving facial ratings are not objective arbiters of beauty. "
    "They are mirrors reflecting the biases of their training data. When an "
    "AI tells a young man he is a four out of ten, it is not delivering truth..."
)


def test_margaret_atwood_hard():
    r = detect_refusal(MARGARET_ATWOOD)
    assert r.severity == "hard", r
    assert r.hard_match_count >= 1
    assert "would_require_fabricate" in r.matched_labels or "doing_so_would_require" in r.matched_labels


def test_brad_pitt_hard():
    r = detect_refusal(BRAD_PITT)
    assert r.severity == "hard", r
    assert "I_cannot_ethically" in r.matched_labels or "cannot_ethically_create" in r.matched_labels


def test_omar_apollo_hard():
    r = detect_refusal(OMAR_APOLLO)
    assert r.severity == "hard", r
    assert "I_cannot_responsibly" in r.matched_labels


def test_chuck_schumer_hard():
    r = detect_refusal(CHUCK_SCHUMER)
    assert r.severity == "hard", r
    assert any(l in r.matched_labels for l in
               ("I_cannot_responsibly", "search_results_lack"))


def test_robin_roberts_hard():
    r = detect_refusal(ROBIN_ROBERTS)
    assert r.severity == "hard", r
    assert any(l in r.matched_labels for l in
               ("I_cannot_ethically", "fabricate_news", "recommend_fresh_search"))


def test_intentional_disclosure_clean():
    """'I am an AI host' is intentional disclosure — must not trigger."""
    r = detect_refusal(INTENTIONAL_AI_DISCLOSURE)
    assert r.severity == "none", r


def test_idiomatic_cannot_clean():
    """'I cannot repeat on this show' is idiomatic — must not trigger."""
    r = detect_refusal(IDIOMATIC_CANNOT)
    assert r.severity == "none", r


def test_legit_training_data_clean():
    """A SINGLE soft pattern alone shouldn't escalate to hard.
    Discussing AI training data in an episode about AI bias is legit content."""
    r = detect_refusal(LEGIT_TRAINING_DATA_MENTION)
    # exactly one soft hit ('training data'), no hards — should be 'soft'
    assert r.severity in ("soft", "none"), r
    assert r.hard_match_count == 0


def test_two_soft_escalates_to_hard():
    """≥2 soft patterns in same transcript should escalate to 'hard' since
    it's a strong tell that the LLM is hedging out of the request."""
    text = (
        "Based on my training data, which has a knowledge cutoff in early 2024, "
        "I would need additional verified information to write this episode."
    )
    r = detect_refusal(text)
    # has training data + knowledge cutoff + I would need = 3 soft hits
    assert r.severity == "hard", r
    assert r.soft_match_count >= 2


def test_empty_transcript():
    r = detect_refusal("")
    assert r.severity == "none"
    assert r.matched_labels == []


def test_returns_snippets_for_review():
    r = detect_refusal(MARGARET_ATWOOD)
    assert r.snippets, "should return at least one snippet"
    assert all(len(s) <= 250 for s in r.snippets)
