"""LLM client for Critica's scoring pass.

Two backends, switched by env:
  - OPENROUTER_API_KEY set → OpenRouter (OpenAI-compatible, can route to
    Anthropic/Bedrock/etc). Used when set even if Anthropic direct is
    also configured, so we can fall back when an Anthropic org is
    suspended without a code change.
  - Otherwise → Anthropic SDK direct.

Failures bubble up. Unlike the analytics-db sink, the LLM IS the service
contract — if scoring fails, the request fails.
"""
import json
import logging
import re
import time
from typing import Any

from .. import config
from ..rubric import SYSTEM_PROMPT, build_prompt

log = logging.getLogger("critica.llm.claude")

_anthropic_client: Any = None
_openrouter_client: Any = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _anthropic_client = Anthropic(api_key=config.ANTHROPIC_API_KEY,
                                       timeout=config.REQUEST_TIMEOUT_S)
    return _anthropic_client


def _get_openrouter_client():
    global _openrouter_client
    if _openrouter_client is None:
        from openai import OpenAI
        if not config.OPENROUTER_API_KEY:
            raise RuntimeError("OPENROUTER_API_KEY not set")
        _openrouter_client = OpenAI(
            api_key=config.OPENROUTER_API_KEY,
            base_url=config.OPENROUTER_BASE_URL,
            timeout=config.REQUEST_TIMEOUT_S,
        )
    return _openrouter_client


def _backend() -> str:
    """Returns 'openrouter' or 'anthropic' based on which env var is set."""
    if config.OPENROUTER_API_KEY:
        return "openrouter"
    return "anthropic"


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty model response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_RE.search(text)
    if not m:
        raise ValueError(f"no JSON object found in response: {text[:200]!r}")
    return json.loads(m.group(0))


def _score_via_anthropic(system: str, user: str) -> tuple[dict, dict]:
    """Anthropic SDK direct. Uses ephemeral prompt caching on the system
    block (the rubric is static) so repeat calls within 5min get ~80% of
    input tokens free."""
    client = _get_anthropic_client()
    last_err: Exception | None = None
    for attempt in range(config.CLAUDE_MAX_RETRIES):
        try:
            t0 = time.time()
            resp = client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=config.CLAUDE_MAX_TOKENS,
                system=[{
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user}],
            )
            elapsed = time.time() - t0
            text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
            usage = {
                "input_tokens":  getattr(resp.usage, "input_tokens", 0),
                "output_tokens": getattr(resp.usage, "output_tokens", 0),
                "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
                "cache_read_input_tokens":     getattr(resp.usage, "cache_read_input_tokens", 0),
                "elapsed_s": round(elapsed, 3),
                "model":     config.CLAUDE_MODEL,
                "backend":   "anthropic",
            }
            log.info("anthropic ok: in_tok=%d out_tok=%d cache_read=%d elapsed_s=%.2f",
                     usage["input_tokens"], usage["output_tokens"],
                     usage["cache_read_input_tokens"], elapsed)
            return _extract_json(text), usage
        except Exception as e:
            last_err = e
            from anthropic import APIStatusError
            if isinstance(e, APIStatusError) and 400 <= e.status_code < 500:
                raise
            log.warning("anthropic attempt %d failed (%s); retrying", attempt + 1, type(e).__name__)
            if attempt < config.CLAUDE_MAX_RETRIES - 1:
                time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"anthropic scoring failed after {config.CLAUDE_MAX_RETRIES} attempts: {last_err}")


def _score_via_openrouter(system: str, user: str) -> tuple[dict, dict]:
    """OpenAI-compatible API. No prompt caching for now (semantics vary by
    upstream provider on OpenRouter; can be added once we settle on one)."""
    client = _get_openrouter_client()
    last_err: Exception | None = None
    for attempt in range(config.CLAUDE_MAX_RETRIES):
        try:
            t0 = time.time()
            resp = client.chat.completions.create(
                model=config.OPENROUTER_MODEL,
                max_tokens=config.CLAUDE_MAX_TOKENS,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
            )
            elapsed = time.time() - t0
            text = resp.choices[0].message.content if resp.choices else ""
            usage_obj = getattr(resp, "usage", None)
            in_tok  = getattr(usage_obj, "prompt_tokens", 0) if usage_obj else 0
            out_tok = getattr(usage_obj, "completion_tokens", 0) if usage_obj else 0
            usage = {
                "input_tokens":  in_tok,
                "output_tokens": out_tok,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens":     0,
                "elapsed_s": round(elapsed, 3),
                "model":     config.OPENROUTER_MODEL,
                "backend":   "openrouter",
            }
            log.info("openrouter ok: in_tok=%d out_tok=%d elapsed_s=%.2f",
                     in_tok, out_tok, elapsed)
            return _extract_json(text), usage
        except Exception as e:
            last_err = e
            log.warning("openrouter attempt %d failed (%s); retrying", attempt + 1, type(e).__name__)
            if attempt < config.CLAUDE_MAX_RETRIES - 1:
                time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"openrouter scoring failed after {config.CLAUDE_MAX_RETRIES} attempts: {last_err}")


def score_transcript(transcript: str,
                     metadata: dict,
                     word_timings: list | None = None) -> tuple[dict, dict]:
    """Send the rubric prompt and parse the JSON response.

    Returns (parsed_json, usage_dict). Backend chosen by env vars.
    """
    system, user = build_prompt(transcript, metadata, word_timings)
    if _backend() == "openrouter":
        return _score_via_openrouter(system, user)
    return _score_via_anthropic(system, user)
