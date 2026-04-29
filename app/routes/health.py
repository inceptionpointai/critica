"""Health endpoints — liveness, readiness, top-level."""
from fastapi import APIRouter

from .. import __version__, config
from ..models import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health():
    llm = config.CLAUDE_MODEL if config.ANTHROPIC_API_KEY else "none"
    return HealthResponse(
        status="ok",
        service="critica",
        version=__version__,
        llm=llm,
    )


@router.get("/health/live")
def liveness():
    return {"status": "alive"}


@router.get("/health/ready")
def readiness():
    """Ready iff Anthropic key is set. We tolerate Spreaker/OpenAI being
    misconfigured at boot — those failures are visible to the caller via
    explicit error responses and don't justify failing readiness."""
    if not config.ANTHROPIC_API_KEY:
        return {"status": "not_ready", "reason": "ANTHROPIC_API_KEY unset"}
    return {"status": "ready"}
