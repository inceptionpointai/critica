"""Runtime configuration loaded from environment. Validates at startup."""
import os
import sys
from pathlib import Path


def _load_dotenv():
    p = Path(__file__).resolve().parent.parent / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _int_env(name: str, default: int, min_value: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"FATAL: env var {name}={raw!r} is not an integer", file=sys.stderr)
        sys.exit(1)
    if value < min_value:
        print(f"FATAL: env var {name}={value} is below minimum {min_value}", file=sys.stderr)
        sys.exit(1)
    return value


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    print(f"FATAL: env var {name}={raw!r} is not a valid boolean", file=sys.stderr)
    sys.exit(1)


# External-service credentials
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY     = os.environ.get("OPENAI_API_KEY", "")
SPREAKER_API_KEY   = os.environ.get("SPREAKER_API_KEY", "") or os.environ.get("SPREAKER_TOKEN", "")
SPREAKER_USER_ID   = os.environ.get("SPREAKER_USER_ID", "")  # for future list-by-user endpoints

# Auth
API_KEYS  = {k.strip() for k in os.environ.get("CRITICA_API_KEYS", "").split(",") if k.strip()}
DEV_MODE  = _bool_env("CRITICA_DEV_MODE", False)

# HTTP server
HOST              = os.environ.get("HOST", "0.0.0.0")
PORT              = _int_env("PORT", 8040)
MAX_AUDIO_MB      = _int_env("MAX_AUDIO_MB", 500)
REQUEST_TIMEOUT_S = _int_env("REQUEST_TIMEOUT_S", 600)

# LLM
# Four backends supported, selected by LLM_BACKEND (default 'bedrock'):
#   1. 'bedrock'    — AWS Bedrock via anthropic.AnthropicBedrock, auth by IRSA.
#                     Account-billed; the pre-GKE default. No API key needed.
#   2. 'vertex'     — GCP Vertex AI via anthropic.AnthropicVertex, auth by the
#                     pod's Workload Identity SA (ADC, roles/aiplatform.user on
#                     GOOGLE_CLOUD_PROJECT). No API key needed. Same Messages API
#                     shape as Bedrock; flip here per-env during the GKE cutover.
#   3. 'anthropic'  — Anthropic API direct via anthropic.Anthropic.
#                     Requires ANTHROPIC_API_KEY. Kept as break-glass for when
#                     the primary plane has a regional incident.
#   4. 'openrouter' — OpenAI-compatible router (anthropic/claude-opus-4.5 etc).
#                     Requires OPENROUTER_API_KEY. Currently unwired in cluster
#                     (no ExternalSecret entry) — local-dev / future-work only.
# Routing is EXPLICIT; setting an api key alone no longer flips backends.
# NOTE: media-gen (Nova/Titan/Stability) stays on Bedrock regardless — this
# switch governs the Claude scoring pass only.
LLM_BACKEND = os.environ.get("LLM_BACKEND", "bedrock").strip().lower()

# AWS Bedrock. Note: AWS_REGION fallback here is the seatbelt for when the
# ConfigMap forgets to set it — IRSA injects nothing region-related, so the
# boto chain would otherwise read $HOME/.aws/config (absent in-cluster).
AWS_REGION    = os.environ.get("AWS_REGION", "us-west-2")
# Cross-region inference profile id, NOT a bare foundation-model id. Claude
# Opus families on Bedrock require a profile (us.* or global.*) for
# InvokeModel; the bare anthropic.claude-opus-4-* id 400s with
# "on-demand throughput isn't supported."
BEDROCK_MODEL = os.environ.get(
    "BEDROCK_MODEL",
    "us.anthropic.claude-opus-4-5-20251101-v1:0",
)

# GCP Vertex AI (Claude via publishers/anthropic). Auth is Application Default
# Credentials — in-cluster this is the pod's Workload Identity SA (granted
# roles/aiplatform.user on GOOGLE_CLOUD_PROJECT), the GKE analogue of Bedrock's
# IRSA. No API key. GOOGLE_CLOUD_PROJECT is required when LLM_BACKEND=vertex.
# Claude on Vertex lives in us-east5 (see the region availability matrix).
GOOGLE_CLOUD_PROJECT    = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
ANTHROPIC_VERTEX_REGION = os.environ.get("ANTHROPIC_VERTEX_REGION", "us-east5")

# Bedrock model id -> Vertex model id. Vertex uses the bare first-party id with
# an '@'-separated snapshot date (publishers/anthropic), dropping the Bedrock
# cross-region 'us.'/'anthropic.' prefix, the '-'-joined date, and the '-v1:0'
# suffix (e.g. us.anthropic.claude-opus-4-5-20251101-v1:0 -> claude-opus-4-5@20251101).
# Keep in sync when BEDROCK_MODEL changes.
BEDROCK_TO_VERTEX_MODEL = {
    "us.anthropic.claude-opus-4-5-20251101-v1:0":   "claude-opus-4-5@20251101",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": "claude-sonnet-4-5@20250929",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0":  "claude-haiku-4-5@20251001",
}
# Vertex model id. Defaults to the Vertex equivalent of BEDROCK_MODEL so the
# app's model choice is preserved across the backend switch; overridable.
VERTEX_MODEL = os.environ.get("VERTEX_MODEL", "").strip() or \
    BEDROCK_TO_VERTEX_MODEL.get(BEDROCK_MODEL, "claude-opus-4-5@20251101")

# OpenRouter (break-glass / local dev)
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# Model id format differs by backend:
#   bedrock:          'us.anthropic.claude-opus-4-5-20251101-v1:0' (BEDROCK_MODEL)
#   vertex:           'claude-opus-4-5@20251101'                   (VERTEX_MODEL)
#   anthropic direct: 'claude-opus-4-5'
#   openrouter:       'anthropic/claude-opus-4.5'
CLAUDE_MODEL       = os.environ.get("CLAUDE_MODEL", "claude-opus-4-5")
OPENROUTER_MODEL   = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-opus-4.5")
CLAUDE_MAX_TOKENS  = _int_env("CLAUDE_MAX_TOKENS", 4096, min_value=512)
CLAUDE_MAX_RETRIES = _int_env("CLAUDE_MAX_RETRIES", 3)

# Whisper
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-1")

# Spreaker
SPREAKER_BASE_URL = os.environ.get("SPREAKER_BASE_URL", "https://api.spreaker.com/v2")

# Megaphone. Published episodes after the 2026-Q2 cutover land here instead of
# Spreaker. Auth scheme differs: Megaphone uses Rails-style 'Token token="..."',
# not Bearer. The token is shared org-wide (SSM /prod/shared/megaphone-api-token).
MEGAPHONE_API_TOKEN = os.environ.get("MEGAPHONE_API_TOKEN", "")
MEGAPHONE_BASE_URL  = os.environ.get("MEGAPHONE_BASE_URL", "https://cms.megaphone.fm/api")

# Analytics sink
ANALYTICS_DB_URL = os.environ.get("ANALYTICS_DB_URL", "")

# Temp files
TMP_DIR  = Path(os.environ.get("CRITICA_TMP_DIR", "/tmp/critica"))
TMP_DIR.mkdir(parents=True, exist_ok=True)
TMP_TTL_S = _int_env("TMP_TTL_S", 3600)
