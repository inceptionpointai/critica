"""Unit tests for app/megaphone.py."""
import os

os.environ.setdefault("MEGAPHONE_API_TOKEN", "test-megaphone-token")

import pytest  # noqa: E402

from app import megaphone  # noqa: E402


# ── Fixture: a realistic Megaphone GET /episodes/<id> body ───────────────

EPISODE_JSON = {
    "id": "9b6f4c2a-1111-2222-3333-444455556666",
    "podcastId": "aaaaaaaa-1111-2222-3333-444455556666",
    "networkId": "bbbbbbbb-1111-2222-3333-444455556666",
    "guid": "https://api.spreaker.com/episode/12345678",
    "title": "Atwood at the Edge",
    "subtitle": "A reading from the new collection",
    "summary": "Margaret Atwood reads three new short pieces and discusses craft.",
    "author": "Margaret Atwood",
    "link": "https://example.com/atwood",
    "pubdate": "2026-06-01T15:00:00.000Z",
    "duration": "1834.512",
    "size": 29348203,
    "status": "ready",
    "audioFileStatus": "completed",
    "explicit": False,
    "downloadUrl": "https://cdn.megaphone.fm/podcasts/x/y/atwood.mp3",
    "audioFile":   "https://cdn.megaphone.fm/uploads/atwood-source.wav",
    "mediaFileUrl": "https://ingest.example.com/atwood-source.wav",
    "imageFile":   "https://cdn.megaphone.fm/images/atwood.jpg",
}


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ── Auth header ──────────────────────────────────────────────────────────

def test_headers_uses_rails_style_token_auth():
    h = megaphone._headers()
    # Critical: NOT 'Bearer <token>'. Megaphone rejects Bearer with 401.
    assert h["Authorization"] == 'Token token="test-megaphone-token"'


def test_headers_raises_when_token_missing(monkeypatch):
    monkeypatch.setattr(megaphone.config, "MEGAPHONE_API_TOKEN", "")
    with pytest.raises(RuntimeError, match="MEGAPHONE_API_TOKEN not set"):
        megaphone._headers()


# ── fetch_episode ────────────────────────────────────────────────────────

def test_fetch_episode_hits_three_id_url(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResp(200, EPISODE_JSON)

    monkeypatch.setattr(megaphone.requests, "get", fake_get)
    monkeypatch.setattr(megaphone.config, "MEGAPHONE_BASE_URL", "https://cms.megaphone.fm/api")

    out = megaphone.fetch_episode("net-1", "pod-9", "ep-7")
    assert captured["url"] == "https://cms.megaphone.fm/api/networks/net-1/podcasts/pod-9/episodes/ep-7"
    assert captured["headers"]["Authorization"].startswith('Token token=')
    assert captured["timeout"] == 30
    assert out["megaphone_episode_id"] == EPISODE_JSON["id"]


def test_fetch_episode_404_raises_typed_error(monkeypatch):
    monkeypatch.setattr(megaphone.requests, "get",
                        lambda *a, **kw: _FakeResp(404, {"error": "not found"}))
    with pytest.raises(megaphone.MegaphoneNotFound):
        megaphone.fetch_episode("n", "p", "e")


def test_fetch_episode_5xx_raises_runtime_error(monkeypatch):
    monkeypatch.setattr(megaphone.requests, "get",
                        lambda *a, **kw: _FakeResp(500, {}))
    # The route maps any non-404 failure to 502. RuntimeError is fine here.
    with pytest.raises(RuntimeError):
        megaphone.fetch_episode("n", "p", "e")


# ── _normalize_episode ───────────────────────────────────────────────────

def test_normalize_extracts_megaphone_ids_and_blanks_spreaker_ids():
    out = megaphone._normalize_episode(EPISODE_JSON)
    assert out["megaphone_episode_id"] == EPISODE_JSON["id"]
    assert out["megaphone_podcast_id"] == EPISODE_JSON["podcastId"]
    assert out["megaphone_network_id"] == EPISODE_JSON["networkId"]
    assert out["spreaker_episode_id"] == ""
    assert out["spreaker_show_id"]    == ""


def test_normalize_prefers_download_url_over_audio_file_and_media_url():
    out = megaphone._normalize_episode(EPISODE_JSON)
    assert out["audio_url"] == EPISODE_JSON["downloadUrl"]


def test_normalize_falls_back_to_audio_file_when_download_missing():
    ep = {**EPISODE_JSON, "downloadUrl": ""}
    assert megaphone._normalize_episode(ep)["audio_url"] == ep["audioFile"]


def test_normalize_falls_back_to_media_file_url_last():
    ep = {**EPISODE_JSON, "downloadUrl": "", "audioFile": ""}
    assert megaphone._normalize_episode(ep)["audio_url"] == ep["mediaFileUrl"]


def test_normalize_empty_audio_url_when_all_three_missing():
    ep = {**EPISODE_JSON, "downloadUrl": "", "audioFile": "", "mediaFileUrl": ""}
    assert megaphone._normalize_episode(ep)["audio_url"] == ""


def test_normalize_parses_duration_string_to_milliseconds():
    out = megaphone._normalize_episode(EPISODE_JSON)
    # "1834.512" → 1834512 ms
    assert out["duration_ms"] == 1834512


def test_normalize_handles_missing_duration():
    ep = {k: v for k, v in EPISODE_JSON.items() if k != "duration"}
    assert megaphone._normalize_episode(ep)["duration_ms"] == 0


def test_normalize_summary_falls_back_to_subtitle():
    ep = {**EPISODE_JSON, "summary": ""}
    assert megaphone._normalize_episode(ep)["description"] == ep["subtitle"]


def test_normalize_preserves_raw_payload():
    out = megaphone._normalize_episode(EPISODE_JSON)
    assert out["raw"] is EPISODE_JSON


# ── _parse_duration_seconds ──────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("0.0", 0.0),
    ("1234.5", 1234.5),
    (1234, 1234.0),
    (None, None),
    ("", None),
    ("not a number", None),
])
def test_parse_duration_seconds(raw, expected):
    assert megaphone._parse_duration_seconds(raw) == expected
