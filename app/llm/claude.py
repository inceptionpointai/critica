"""Claude client for Critica's scoring pass.

Wraps the Anthropic SDK with: prompt-cache for the rubric block (it
doesn't change per request, so caching it saves ~80% of input tokens at
volume), retries on 5xx + transient errors, JSON-only output enforcement
via a regex post-parse.

Failures bubble up — unlike the analytics sink, the LLM call IS the
service contract. If Claude is down, /api/v1/review fails honestly.
"""
import json
import logging
import re
import time
from typing import Any

from anthropic import APIStatusError, Anthropic

from .. import config
from ..rubric import SYSTEM_PROMPT, build_prompt

log = logging.getLogger("critica.llm.claude")

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _client = Anthropic(
            api_key=config.ANTHROPIC_API_KEY,
            timeout=config.REQUEST_TIMEOUT_S,
        )
    return _client


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict:
    """The system prompt asks for raw JSON, but models occasionally wrap
    it in ```json fences or add a leading sentence. Strip and parse."""
    text = text.strip()
    if not text:
        raise ValueError("empty model response")
    # Try direct parse first.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the first {...} block.
    m = _JSON_RE.search(text)
    if not m:
        raise ValueError(f"no JSON object found in response: {text[:200]!r}")
    return json.loads(m.group(0))


def score_transcript(transcript: str,
                     metadata: dict,
                     word_timings: list | None = None) -> tuple[dict, dict]:
    """Send the rubric prompt to Claude and parse its JSON response.

    Returns (parsed_json, usage_dict). usage_dict contains
    input_tokens / output_tokens for billing observability.
    """
    system, user = build_prompt(transcript, metadata, word_timings)
    client = _get_client()

    last_err: Exception | None = None
    for attempt in range(config.CLAUDE_MAX_RETRIES):
        try:
            t0 = time.time()
            # Cache the rubric portion of the system prompt — it's static
            # and large; ~80% of input tokens become free on repeat calls
            # within the 5-minute cache window.
            resp = client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=config.CLAUDE_MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
                messages=[{"role": "user", "content": user}],
            )
            elapsed = time.time() - t0
            text_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
            text = "".join(text_parts)
            parsed = _extract_json(text)
            usage = {
                "input_tokens":  getattr(resp.usage, "input_tokens", 0),
                "output_tokens": getattr(resp.usage, "output_tokens", 0),
                "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
                "cache_read_input_tokens":     getattr(resp.usage, "cache_read_input_tokens", 0),
                "elapsed_s": round(elapsed, 3),
                "model":     config.CLAUDE_MODEL,
            }
            log.info(
                "claude scored: model=%s in_tok=%d out_tok=%d cache_read=%d elapsed_s=%.2f",
                config.CLAUDE_MODEL, usage["input_tokens"], usage["output_tokens"],
                usage["cache_read_input_tokens"], elapsed,
            )
            return parsed, usage
        except APIStatusError as e:
            last_err = e
            if 400 <= e.status_code < 500:
                # 4xx is on us — bad request, model unavailable, etc. Don't retry.
                raise
            log.warning("claude attempt %d failed (%s); retrying", attempt + 1, e)
        except Exception as e:
            last_err = e
            log.warning("claude attempt %d failed (%s); retrying", attempt + 1, type(e).__name__)
        if attempt < config.CLAUDE_MAX_RETRIES - 1:
            time.sleep(0.5 * (2 ** attempt))

    raise RuntimeError(f"claude scoring failed after {config.CLAUDE_MAX_RETRIES} attempts: {last_err}")
