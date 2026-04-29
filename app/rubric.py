"""The Critica scoring rubric — 10 dimensions, anchored, strictly typed.

Critica's value proposition is the *contract* between an episode and a
score. The prompt below is that contract. Don't soften the anchors; don't
add dimensions without a corresponding column in critica.episode_reviews.
"""

# Dimension definitions — names match qc_events column ergonomics.
# Each dimension has anchored examples at 0, 5, 10 so the LLM doesn't
# silently drift over time as we tune things.
DIMENSIONS = [
    {
        "name": "subject_tone_alignment",
        "title": "Subject-Tone Alignment",
        "what": "Does the delivery vibe match the subject's brand register?",
        "anchors": {
            0: "A polished commercial-news cadence covering an outlaw country legend known for relaxed campfire storytelling.",
            5: "Generic upbeat narration covering a topic where vibe is irrelevant (e.g. weather, sports scores).",
            10: "Delivery register matches the subject's known brand — e.g. measured + literary for a Pulitzer biographical sketch; energetic + irreverent for an influencer profile.",
        },
    },
    {
        "name": "narrative_arc",
        "title": "Narrative Arc",
        "what": "Is there a thesis or arc, or is the episode a list of events?",
        "anchors": {
            0: "Chronological list of news clippings with no through-line.",
            5: "Loosely organized around a topic but no argument; reader cannot summarize the show's point.",
            10: "Clear cold-open hook → thesis → escalating evidence → satisfying close that earns the CTA.",
        },
    },
    {
        "name": "original_synthesis",
        "title": "Original Synthesis",
        "what": "Does the show offer perspective, or is it summary-of-summaries?",
        "anchors": {
            0: "Every paragraph cites a source; show offers zero original analysis.",
            5: "Some original framing but the substantive claims all come from cited outlets.",
            10: "Episode synthesizes reporting into a take that doesn't exist in any single source.",
        },
    },
    {
        "name": "citation_quality",
        "title": "Citation Quality",
        "what": "Are sources credible, weighted appropriately, and distinguished?",
        "anchors": {
            0: "Cites unverified rumors as fact; treats blog aggregators as primary sources; presents 'X echoes Y' as corroboration.",
            5: "Mostly credible sources but no weighting; college papers and tabloids cited beside major outlets.",
            10: "Sources are weighted; unverified claims are flagged; corroboration is real, not repeated aggregation.",
        },
    },
    {
        "name": "tonal_coherence",
        "title": "Tonal Coherence",
        "what": "Does the show sustain a register, or whiplash between heavy and light?",
        "anchors": {
            0: "'On a lighter note' pivot from labor-abuse allegations to dating tips.",
            5: "Register drifts but no jarring transitions; minor pacing inconsistency.",
            10: "Stable register throughout; deliberate slow-downs and quickenings serve the narrative.",
        },
    },
    {
        "name": "ai_tell_score",
        "title": "AI Tell Score (lower = better)",
        "what": "How visible is LLM-generated phrasing, redundancies, broken negations? Lower score = MORE AI-tells = WORSE. (We invert this column on the score side: 10 means 'no AI tells', 0 means 'obviously generated'.)",
        "anchors": {
            0: "Multiple LLM idioms ('weighting this as', 'flashpoint', stacked 'in addition to'); redundancies like 'Artificial Intelligence AI'; broken negations slip through.",
            5: "Occasional LLM-stilted phrase but mostly plausible as written by a human.",
            10: "Reads like human-written copy — no telltale generation artifacts, no copy-paste errors.",
        },
    },
    {
        "name": "pacing_variance",
        "title": "Pacing Variance",
        "what": "Words-per-minute range across the episode (high variance = good — the show slows for weight, quickens for action).",
        "anchors": {
            0: "Monotone — same WPM across emotional and factual segments alike.",
            5: "Some variance, but doesn't track topic gravity (slows on filler, rushes on the substance).",
            10: "Pacing maps to content — slow on heavy claims, brisk on lighter beats, deliberate pauses on landings.",
        },
    },
    {
        "name": "hook_quality",
        "title": "Hook Quality",
        "what": "Does the cold open earn attention?",
        "anchors": {
            0: "Generic intro music + 'Today on the show...' summary of the description.",
            5: "Topic preview with mild stakes ('a feud that's getting attention').",
            10: "Specific, visceral, and stakes-laden in the first 15 seconds; gives the listener a reason to stay.",
        },
    },
    {
        "name": "cta_effectiveness",
        "title": "CTA Effectiveness",
        "what": "Is the closer specific, earned, and actionable?",
        "anchors": {
            0: "'Subscribe and search the term <show name> for more great <shows>'.",
            5: "Specific ask but disconnected from the episode's content.",
            10: "Specific, content-anchored ask that converts attention into participation ('Send us a voice memo with your favorite Willie track').",
        },
    },
    {
        "name": "subject_body_match",
        "title": "Subject-Body Match",
        "what": "Does the content match the title and brand promise?",
        "anchors": {
            0: "'Biography Flash' branding on an episode that's entirely current-week news with zero biographical content.",
            5: "Body partially delivers on the title's promise but with significant filler.",
            10: "Body exceeds the title — the listener gets more than the framing implied.",
        },
    },
]


def _format_anchors(anchors: dict) -> str:
    return "\n".join(f"      <anchor score=\"{k}\">{v}</anchor>" for k, v in sorted(anchors.items()))


def _format_dimensions() -> str:
    parts = []
    for d in DIMENSIONS:
        parts.append(
            f"  <dimension>\n"
            f"    <name>{d['name']}</name>\n"
            f"    <title>{d['title']}</title>\n"
            f"    <what>{d['what']}</what>\n"
            f"    <anchors>\n{_format_anchors(d['anchors'])}\n"
            f"    </anchors>\n"
            f"  </dimension>"
        )
    return "\n".join(parts)


SYSTEM_PROMPT = """\
You are a senior editorial critic for an audio publishing house. You grade
podcast episodes against a fixed 10-dimension rubric. Your tone is direct
and specific — the publisher pays you to find the things their producers
missed, not to be encouraging. You quote evidence from the transcript when
making a point. You never invent facts not in the transcript.

You output strictly-typed JSON matching the schema you are given. No
markdown fences, no preamble, no apology. Just the JSON object.
"""


def build_prompt(transcript: str,
                 metadata: dict,
                 word_timings: list | None = None) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt) for the LLM call.

    metadata may contain: title, description, subject_name, subject_brand,
    duration_s. Anything missing is omitted from the prompt — don't pad
    with placeholders, the LLM treats that as a signal to invent context.
    """
    md_parts = []
    for k in ("title", "description", "subject_name", "subject_brand", "duration_s"):
        v = metadata.get(k)
        if v:
            md_parts.append(f"    <{k}>{v}</{k}>")
    metadata_block = "\n".join(md_parts) if md_parts else "    <none/>"

    pacing_block = ""
    if word_timings:
        # Compute a rough WPM-over-time signal so the LLM can comment on
        # pacing variance without having to count words itself.
        windows = _bucket_wpm(word_timings, window_s=20.0)
        if windows:
            pacing_block = (
                "\n  <pacing_wpm_per_20s_window>"
                + ", ".join(f"{w:.0f}" for w in windows)
                + "</pacing_wpm_per_20s_window>"
            )

    user = f"""\
<episode>
  <metadata>
{metadata_block}
  </metadata>{pacing_block}
  <transcript>
{transcript}
  </transcript>
</episode>

<rubric>
{_format_dimensions()}
</rubric>

<task>
Score each dimension 0–10 using the anchors. For ai_tell_score, REMEMBER
the inversion: 10 means "no AI tells", 0 means "obviously generated".

For each dimension, provide a one-sentence rationale that quotes or
references specific evidence from the transcript. Do not write generic
advice in the rationale field — that goes in the recommendations array.

Then write a 200-400 word prose critique that a producer could read in 90
seconds and act on. Be specific. Quote phrases. Name the failure modes.

Finally, list 3-7 concrete, actionable recommendations.
</task>

<output_schema>
Return a single JSON object with exactly these keys:
{{
  "overall_score": <number 0-10, weighted average>,
  "grade": <"A" | "B" | "C" | "D" | "F">,
  "dimensions": [
    {{"name": "<dimension_name>", "score": <0-10>, "rationale": "<one sentence>"}}
    ... one entry per dimension, in the order listed in the rubric
  ],
  "summary": "<one-paragraph executive summary, 1-3 sentences>",
  "critique": "<200-400 word prose critique>",
  "recommendations": ["<concrete action 1>", "<concrete action 2>", ...]
}}

Grade mapping: 9-10=A, 7.5-8.9=B, 6-7.4=C, 4-5.9=D, 0-3.9=F.
</output_schema>"""
    return SYSTEM_PROMPT, user


def _bucket_wpm(word_timings: list, window_s: float = 20.0) -> list[float]:
    """Rough WPM per fixed-width time window. word_timings is a list of
    {word, start, end} dicts (Whisper verbose_json shape)."""
    if not word_timings:
        return []
    last = max((w.get("end") or 0) for w in word_timings)
    if last <= 0:
        return []
    n_buckets = int(last // window_s) + 1
    counts = [0] * n_buckets
    for w in word_timings:
        s = w.get("start") or 0
        b = int(s // window_s)
        if 0 <= b < n_buckets:
            counts[b] += 1
    return [c * (60.0 / window_s) for c in counts]
