"""Whisper transcription — verbose_json for word timestamps.

We always pull a fresh Whisper transcript rather than reusing Spreaker's
auto-transcript: Spreaker's transcripts don't include word-level timing,
which is what powers the pacing-variance dimension of the rubric.
"""
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from . import config

log = logging.getLogger("critica.transcribe")


_OPENAI_SUPPORTED_EXTS = {"flac", "m4a", "mp3", "mp4", "mpeg", "mpga", "oga", "ogg", "wav", "webm"}


def _ext_from_url(url: str) -> str:
    """Whisper rejects files without a recognizable audio extension; we
    can't write `.bin`. Spreaker download URLs end in `.mp3`; for other
    sources we pull the suffix off the URL path. Falls back to `mp3`."""
    from urllib.parse import urlparse
    path = urlparse(url).path
    if "." in path:
        ext = path.rsplit(".", 1)[-1].lower()
        if ext in _OPENAI_SUPPORTED_EXTS:
            return ext
    return "mp3"


def fetch_audio_to_tmp(audio_url: str) -> str:
    """Stream the audio to a temp file with a hard size cap. Returns the
    local path. Raises if oversized or unreachable."""
    max_bytes = config.MAX_AUDIO_MB * 1024 * 1024
    ext = _ext_from_url(audio_url)
    tmp = config.TMP_DIR / f"c_{uuid.uuid4().hex[:12]}.{ext}"
    total = 0
    try:
        r = requests.get(audio_url, stream=True, timeout=config.REQUEST_TIMEOUT_S)
        r.raise_for_status()
        cl = r.headers.get("Content-Length")
        if cl and cl.isdigit() and int(cl) > max_bytes:
            r.close()
            raise RuntimeError(
                f"audio exceeds {config.MAX_AUDIO_MB}MB "
                f"(server reports {int(cl) // (1024 * 1024)}MB)"
            )
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError(f"audio exceeds {config.MAX_AUDIO_MB}MB during download")
                f.write(chunk)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return str(tmp)


def transcribe(audio_path: str, language: str | None = None) -> dict:
    """Returns {transcript, words, language, duration_s}.

    Uses Whisper-1 via the OpenAI API in `verbose_json` mode so we get
    word-level timestamps. Raises on failure — the caller is responsible
    for translating into an HTTP error.
    """
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError("openai SDK not installed") from e
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    t0 = time.time()
    with open(audio_path, "rb") as f:
        result = client.audio.transcriptions.create(
            model=config.WHISPER_MODEL,
            file=f,
            language=language or None,
            response_format="verbose_json",
            timestamp_granularities=["word"],
        )
    elapsed = time.time() - t0

    transcript = getattr(result, "text", "") or ""
    words_raw  = getattr(result, "words", None) or []
    duration   = getattr(result, "duration", 0) or 0
    detected   = getattr(result, "language", language or "")

    words = []
    for w in words_raw:
        if hasattr(w, "model_dump"):
            words.append(w.model_dump())
        elif isinstance(w, dict):
            words.append(w)

    log.info(
        "whisper: %d words, duration=%.1fs, detected_lang=%s, elapsed_s=%.2f",
        len(words), duration, detected, elapsed,
    )
    return {
        "transcript": transcript,
        "words":      words,
        "language":   detected,
        "duration_s": float(duration),
        "elapsed_s":  round(elapsed, 3),
    }
