"""Bearer-token auth. Tokens come from CRITICA_API_KEYS.

Fails closed by default. To run without auth (local dev only), set
CRITICA_API_KEYS="" AND CRITICA_DEV_MODE=true — requiring the explicit
flag prevents accidental open deployments when the secret fails to sync.
"""
from fastapi import Header, HTTPException, status

from . import config


async def require_bearer(authorization: str = Header(None)) -> str:
    if not config.API_KEYS:
        if config.DEV_MODE:
            return "dev"
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "No API keys configured. Set CRITICA_API_KEYS, or CRITICA_DEV_MODE=true for local dev.",
        )
    if not authorization:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing Authorization header")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Expected: Authorization: Bearer <token>")
    token = parts[1].strip()
    if token not in config.API_KEYS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid token")
    return token
