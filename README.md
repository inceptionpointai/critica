# Critica

Editorial-quality review service for already-published podcast episodes.

Where Qualitas measures **mechanical** quality (does the audio match the script — fidelity, silence, garble), Critica measures **editorial** quality of the published product:

- Does the delivery vibe match the subject's brand?
- Is there a thesis, or is it a list of news clippings?
- Does the show offer original synthesis or summary-of-summaries?
- Is there tonal whiplash between heavy and light registers?
- Does the title/branding match what's actually in the body?

## Pipeline

```
Spreaker episode_id
   ↓
Spreaker v2 API   ── episode metadata + audio URL
   ↓
Whisper (large-v3)   ── transcript with word timestamps
   ↓
Critica scoring pass ── 10-dim rubric via Claude
   ↓
analytics-db.critica.episode_reviews  ── structured score JSONB + prose blob
   ↓
Superset "Critica Reviews" dashboard
```

## Scoring rubric

Ten dimensions, 0–10 each, plus a free-form prose critique:

| # | Dimension |
|---|---|
| 1 | Subject-Tone Alignment |
| 2 | Narrative Arc |
| 3 | Original Synthesis |
| 4 | Citation Quality |
| 5 | Tonal Coherence |
| 6 | AI Tell Score (lower better) |
| 7 | Pacing Variance |
| 8 | Hook Quality |
| 9 | CTA Effectiveness |
| 10 | Subject-Body Match |

See `app/rubric.py` for the full definitions and the LLM prompt.

## Endpoints (v1)

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/health` | Service status |
| `POST` | `/api/v1/review` | JSON: `{spreaker_episode_id}` → full structured review |
| `POST` | `/api/v1/review/transcript` | JSON: `{transcript, manuscript?, metadata?}` — bypass Spreaker fetch + Whisper, score a transcript directly |

## Running locally

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY + SPREAKER_API_KEY + CRITICA_API_KEYS
./run.sh               # binds 0.0.0.0:8040
```

## Deployment

Same EKS cluster as Qualitas / Veritas. Image `673066883217.dkr.ecr.us-west-2.amazonaws.com/ipoint-critica-prod`. See `k8s/` for kustomize overlays.

## Convention notes

This repo follows the same patterns as Qualitas:

- Tag-driven prod deploys (`deploy-production.yml` on `v*`)
- ExternalSecret syncing from SSM `/prod/critica/*`
- Fail-open analytics sink — review-write failures never block the request
- Logging configured at module-import time (uvicorn workers don't run `main()`)
