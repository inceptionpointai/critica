"""Health endpoints — liveness, readiness, top-level."""
import os

from fastapi import APIRouter

from .. import __version__, config
from ..models import HealthResponse

router = APIRouter()


def _llm_label() -> str:
    """Best-effort 'what model are we calling' string for /health."""
    b = (config.LLM_BACKEND or "").strip().lower()
    if b == "bedrock":
        return config.BEDROCK_MODEL
    if b == "anthropic" and config.ANTHROPIC_API_KEY:
        return config.CLAUDE_MODEL
    if b == "openrouter" and config.OPENROUTER_API_KEY:
        return config.OPENROUTER_MODEL
    return "none"


@router.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(
        status="ok",
        service="critica",
        version=__version__,
        llm=_llm_label(),
    )


@router.get("/health/live")
def liveness():
    return {"status": "alive"}


@router.get("/health/ready")
def readiness():
    """Ready iff the selected LLM backend has credentials in place.

    For 'bedrock' we check that IRSA actually injected a web-identity
    token file — two os.path checks, no STS round trip. Catches the
    'SA missing eks.amazonaws.com/role-arn annotation' failure mode at
    probe time instead of at first /api/v1/review.

    For 'anthropic'/'openrouter' the API key must be set.
    """
    b = (config.LLM_BACKEND or "").strip().lower()
    if b == "bedrock":
        token_path = os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE", "")
        if not token_path or not os.path.exists(token_path):
            return {"status": "not_ready",
                    "reason": "IRSA web-identity token not mounted "
                              "(check serviceAccountName + role annotation)"}
        return {"status": "ready", "backend": "bedrock",
                "region": config.AWS_REGION}
    if b == "anthropic":
        if not config.ANTHROPIC_API_KEY:
            return {"status": "not_ready", "reason": "ANTHROPIC_API_KEY unset"}
        return {"status": "ready", "backend": "anthropic"}
    if b == "openrouter":
        if not config.OPENROUTER_API_KEY:
            return {"status": "not_ready", "reason": "OPENROUTER_API_KEY unset"}
        return {"status": "ready", "backend": "openrouter"}
    return {"status": "not_ready", "reason": f"invalid LLM_BACKEND={b!r}"}
