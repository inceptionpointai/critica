"""Deterministic refusal-leak detector.

Runs before the LLM scoring pass. If the transcript contains hard refusal
text — i.e., the script-generation LLM literally said "I cannot make this"
and Hume TTS read it aloud — we force the review's score and flag it as
a `refusal_leak` finding.

This catches the production failure that surfaced in the 7-day audit:
~52 of 314 reviewed Biography Flash episodes had unedited LLM refusal
messages published as podcast episodes.

Pattern tiers
-------------
HARD: a single match is sufficient. Each pattern is unambiguously a
      script-generation refusal in audio context.

SOFT: a single match is suggestive but ambiguous (could be content). Two
      or more SOFT matches in the same transcript triggers a leak flag.

We deliberately do NOT include patterns that fire on intentional
disclosures ("I am an AI host") or idiomatic English ("I cannot stress
this enough"). See app/scan.py in qualitas for the audit notes.
"""
import re
from dataclasses import dataclass


_FLAGS = re.IGNORECASE


# Each pattern is a (regex, label, tier) tuple. Tier ∈ {'hard', 'soft'}.
_PATTERNS: list[tuple[str, str, str]] = [
    # ── HARD ────────────────────────────────────────────────────────────────
    (r"\bI\s+cannot\s+ethically\b",                  "I_cannot_ethically",        "hard"),
    (r"\bI\s+cannot\s+responsibly\b",                "I_cannot_responsibly",      "hard"),
    (r"\bI\s+cannot\s+fulfill\s+your\s+request\b",   "I_cannot_fulfill",          "hard"),
    (r"\bwould\s+require\s+me\s+to\s+(fabricate|speculate)\b",
                                                     "would_require_fabricate",   "hard"),
    (r"\bI'?d\s+recommend\s+conducting\s+a?\s*(fresh|new|more|additional)\s*search\b",
                                                     "recommend_fresh_search",    "hard"),
    (r"\bsearch\s+results\s+(don'?t|do\s+not)\s+contain\b",
                                                     "search_results_dont_contain","hard"),
    (r"\bsearch\s+results\s+lack\s+(substantive|verified|specific|sufficient)\b",
                                                     "search_results_lack",       "hard"),
    (r"\bviolate\s+my\s+(commitment|core\s+commitment)\s+to\s+(accuracy|integrity)\b",
                                                     "violate_commitment",        "hard"),
    (r"\bverified\s+information\s+from\s+reliable\s+sources\s+about\b",
                                                     "verified_info_about",       "hard"),
    (r"\bonce\s+you\s+have\s+(relevant|verified|current|specific)\s+search\s+results\b",
                                                     "once_you_have_results",     "hard"),
    (r"\bwithout\s+access\s+to\s+(substantive|verified|current|relevant|specific)\s+(information|sources|search\s+results)\b",
                                                     "without_access_to",         "hard"),
    (r"\bfabricate\s+(news|stories|details|biographical)\b",
                                                     "fabricate_news",            "hard"),
    (r"\bI'?m\s+happy\s+to\s+help\s+(craft|write|produce)\s+(that|the)\s+(episode|script|podcast)\b",
                                                     "happy_to_help_craft",       "hard"),
    (r"\bI\s+(can|could)\s+offer\s+instead\b",       "can_offer_instead",         "hard"),
    (r"\bcannot\s+ethically\s+(create|provide|construct)\b",
                                                     "cannot_ethically_create",   "hard"),
    (r"\bI\s+cannot\s+(provide|create|construct)\s+the\b",
                                                     "I_cannot_provide_the",      "hard"),
    (r"\bdoing\s+so\s+would\s+require\s+me\s+to\b", "doing_so_would_require",    "hard"),
    (r"\b(this|the)\s+would\s+violate\s+(journalistic|my)\s+(integrity|commitment)\b",
                                                     "would_violate_integrity",   "hard"),
    (r"\bdocumented\s+social\s+media\s+(activity|posts)\s+or\s+(public|recent)\s+(appearances|statements)\b",
                                                     "documented_social_media",   "hard"),

    # ── SOFT (need 2+ to flag; each common alone in legit content) ──────────
    (r"\bknowledge\s+cutoff\b",                      "knowledge_cutoff",          "soft"),
    (r"\btraining\s+data\b",                         "training_data",             "soft"),
    (r"\bas\s+of\s+my\s+(last\s+)?(update|knowledge)\b",
                                                     "as_of_my_update",           "soft"),
    (r"\bunable\s+to\s+(verify|confirm|find\s+specific)\b",
                                                     "unable_to_verify",          "soft"),
    (r"\bno\s+(verified|reliable|sufficient)\s+(information|sources)\b",
                                                     "no_verified_info",          "soft"),
    (r"\bI'?d\s+be\s+happy\s+to\b",                  "id_be_happy_to",            "soft"),
    (r"\binsufficient\s+(information|sources|context|details)\b",
                                                     "insufficient_info",         "soft"),
    (r"\boutdated\s+or\s+potentially\s+(inaccurate|incorrect)\b",
                                                     "outdated_or_inaccurate",    "soft"),
    (r"\bI\s+would\s+need\s+(search\s+results|additional|more|verified)\b",
                                                     "I_would_need",              "soft"),
    (r"\bspeculate\s+or\s+fabricate\b",              "speculate_or_fabricate",    "soft"),
]

# Compile once
_COMPILED = [(re.compile(p, _FLAGS), label, tier) for p, label, tier in _PATTERNS]


@dataclass
class RefusalResult:
    """Output of detect_refusal(). `severity` is the public signal."""
    severity: str            # 'none' | 'soft' | 'hard'
    matched_labels: list[str]   # canonical labels of every pattern that fired
    snippets: list[str]      # short context excerpts around each match (≤200 chars)
    soft_match_count: int    # how many soft patterns fired
    hard_match_count: int    # how many hard patterns fired


def _snippet(text: str, m: re.Match, before: int = 80, after: int = 120) -> str:
    s = text[max(0, m.start() - before): m.end() + after]
    return re.sub(r"\s+", " ", s).strip()


def detect_refusal(transcript: str) -> RefusalResult:
    """Run the deterministic refusal-leak detector.

    Returns:
      severity = 'hard' if any HARD pattern matches OR ≥2 SOFT patterns
                 'soft' if exactly 1 SOFT pattern matches
                 'none' otherwise
    """
    if not transcript:
        return RefusalResult("none", [], [], 0, 0)
    matched_labels: list[str] = []
    snippets: list[str] = []
    soft = hard = 0
    for compiled, label, tier in _COMPILED:
        m = compiled.search(transcript)
        if not m:
            continue
        matched_labels.append(label)
        snippets.append(_snippet(transcript, m))
        if tier == "hard":
            hard += 1
        else:
            soft += 1
    if hard >= 1 or soft >= 2:
        sev = "hard"
    elif soft >= 1:
        sev = "soft"
    else:
        sev = "none"
    return RefusalResult(sev, matched_labels, snippets, soft, hard)
