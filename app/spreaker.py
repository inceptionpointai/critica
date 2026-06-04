"""Spreaker v2 API client — pulls episode metadata + audio URL.

Critica only ever does GETs against Spreaker; no draft writes, no auth
escalation. The API key is bearer-passed via the Authorization header.
"""
import logging
from typing import Any

import requests

from . import config

log = logging.getLogger("critica.spreaker")


class SpreakerNotFound(RuntimeError):
    """Spreaker returned 404 for the requested episode id."""


def _headers() -> dict:
    if not config.SPREAKER_API_KEY:
        raise RuntimeError("SPREAKER_API_KEY not set")
    return {"Authorization": f"Bearer {config.SPREAKER_API_KEY}"}


def fetch_episode(episode_id: str) -> dict:
    """GET /v2/episodes/<id> → normalized dict.

    Raises SpreakerNotFound on HTTP 404 so the route can map it to a
    user-facing 404. Other Spreaker failures raise RuntimeError and the
    route maps them to 502.
    """
    url = f"{config.SPREAKER_BASE_URL}/episodes/{episode_id}"
    r = requests.get(url, headers=_headers(), timeout=30)
    if r.status_code == 404:
        raise SpreakerNotFound(f"spreaker episode {episode_id} not found")
    r.raise_for_status()
    body = r.json()
    ep = body.get("response", {}).get("episode") or body.get("episode") or body
    return _normalize_episode(ep)


def _normalize_episode(ep: dict) -> dict:
    """Reduce Spreaker's verbose response to the fields Critica needs.

    Field names match what we'll write to critica.episode_reviews so the
    DB schema and the Python types stay aligned.
    """
    return {
        "spreaker_episode_id": str(ep.get("episode_id") or ep.get("id") or ""),
        "spreaker_show_id":    str(ep.get("show_id") or ""),
        "title":         ep.get("title", ""),
        "description":   _strip_html(ep.get("description", "")),
        "audio_url":     ep.get("download_url") or ep.get("playback_url") or "",
        "duration_ms":   int(ep.get("duration") or 0),
        "published_at":  ep.get("published_at"),
        "site_url":      ep.get("site_url", ""),
        "explicit":      bool(ep.get("explicit")),
        "image_url":     ep.get("image_url", ""),
        "raw":           ep,
    }


def _strip_html(text: str) -> str:
    """Tiny HTML stripper — Spreaker descriptions occasionally include
    <p>, <br>, <a>. We only need the plain text for prompting."""
    if not text:
        return ""
    import re as _re
    text = _re.sub(r"<br\s*/?>", "\n", text, flags=_re.I)
    text = _re.sub(r"<[^>]+>", "", text)
    return text.strip()
