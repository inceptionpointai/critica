"""Megaphone CMS API client — fetch episode metadata + audio URL.

After the 2026-Q2 publishing cutover, new episodes land on Megaphone
instead of Spreaker. Critica only ever does GETs; no draft writes,
no auth escalation.

The Megaphone API requires (network, podcast, episode) — all three —
to look up a single episode. Callers supply them; Critica does not
store any topology mapping. The (network, podcast) pair is normally
joined from arc.megaphone_episodes → arc.megaphone_podcasts in the
analytics DB by whoever is calling /review/megaphone.
"""
import logging
from typing import Any, Optional

import requests

from . import config

log = logging.getLogger("critica.megaphone")


class MegaphoneNotFound(RuntimeError):
    """Megaphone returned 404 for the requested episode id."""


def _headers() -> dict:
    if not config.MEGAPHONE_API_TOKEN:
        raise RuntimeError("MEGAPHONE_API_TOKEN not set")
    # Rails-style token auth — NOT plain Bearer. Documented at
    # https://developers.megaphone.fm/.
    return {"Authorization": f'Token token="{config.MEGAPHONE_API_TOKEN}"'}


def fetch_episode(network_id: str, podcast_id: str, episode_id: str) -> dict:
    """GET /networks/<n>/podcasts/<p>/episodes/<e> → normalized dict.

    Raises MegaphoneNotFound on HTTP 404 so the route can map it to a
    user-facing 404. Other Megaphone failures raise RuntimeError and the
    route maps them to 502.
    """
    url = (f"{config.MEGAPHONE_BASE_URL}/networks/{network_id}"
           f"/podcasts/{podcast_id}/episodes/{episode_id}")
    r = requests.get(url, headers=_headers(), timeout=30)
    if r.status_code == 404:
        raise MegaphoneNotFound(f"megaphone episode {episode_id} not found")
    r.raise_for_status()
    return _normalize_episode(r.json())


def _normalize_episode(ep: dict) -> dict:
    """Reduce Megaphone's response to the fields Critica needs.

    Field names match the Spreaker normalizer where they have the same
    semantic role, so route code stays symmetric. Spreaker-side id
    columns are blank; Megaphone-side ids are surfaced alongside.
    """
    duration_s = _parse_duration_seconds(ep.get("duration"))
    return {
        "spreaker_episode_id":  "",
        "spreaker_show_id":     "",
        "megaphone_episode_id": str(ep.get("id") or ""),
        "megaphone_podcast_id": str(ep.get("podcastId") or ""),
        "megaphone_network_id": str(ep.get("networkId") or ""),
        "title":         ep.get("title", ""),
        "description":   ep.get("summary") or ep.get("subtitle") or "",
        "audio_url":     ep.get("downloadUrl") or ep.get("audioFile") or ep.get("mediaFileUrl") or "",
        "duration_ms":   int(duration_s * 1000) if duration_s else 0,
        "published_at":  ep.get("pubdate"),
        "site_url":      ep.get("link", ""),
        "explicit":      bool(ep.get("explicit")),
        "image_url":     ep.get("imageFile") or ep.get("backgroundImageFileUrl", ""),
        "raw":           ep,
    }


def _parse_duration_seconds(d: Any) -> Optional[float]:
    """Megaphone returns duration as a JSON-Number-encoded string
    (e.g. "0.0", "1234.5"). May be absent on draft episodes."""
    if d is None or d == "":
        return None
    try:
        return float(d)
    except (TypeError, ValueError):
        return None
