"""LLM client for Critica's scoring pass.

Three backends, switched explicitly by config.LLM_BACKEND:
  - 'bedrock'    → anthropic.AnthropicBedrock (AWS Bedrock, IRSA-auth, default).
  - 'anthropic'  → anthropic.Anthropic (direct API, requires ANTHROPIC_API_KEY).
  - 'openrouter' → openai.OpenAI against OpenRouter (requires OPENROUTER_API_KEY).

Bedrock and Anthropic-direct share the exact same messages.create(...) shape
including the cache_control ephemeral system block, so a single helper drives
both.

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
_bedrock_client: Any = None
_openrouter_client: Any = None


def _get_bedrock_client():
    """Lazy-build AnthropicBedrock. boto3 picks up IRSA-injected
    AWS_ROLE_ARN + AWS_WEB_IDENTITY_TOKEN_FILE via the default credential
    chain — no explicit AWS keys needed."""
    global _bedrock_client
    if _bedrock_client is None:
        from anthropic import AnthropicBedrock
        _bedrock_client = AnthropicBedrock(
            aws_region=config.AWS_REGION,
            timeout=config.REQUEST_TIMEOUT_S,
        )
    return _bedrock_client


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
    """Explicit dispatch via LLM_BACKEND. Implicit fallbacks on api-key
    presence were removed: with three backends the env-var-presence dance
    is a footgun."""
    b = (config.LLM_BACKEND or "").strip().lower()
    if b in ("bedrock", "anthropic", "openrouter"):
        return b
    raise RuntimeError(f"invalid LLM_BACKEND={b!r}; "
                       f"expected one of: bedrock, anthropic, openrouter")


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


def _score_via_messages_api(client, model_id: str, backend_name: str,
                            system: str, user: str) -> tuple[dict, dict]:
    """Anthropic Messages API call. Drives both AnthropicBedrock and
    Anthropic-direct — the signature is byte-identical, including the
    ephemeral cache_control block (the rubric system prompt is static, so
    repeat calls within 5min get ~80% of input tokens free)."""
    last_err: Exception | None = None
    for attempt in range(config.CLAUDE_MAX_RETRIES):
        try:
            t0 = time.time()
            resp = client.messages.create(
                model=model_id,
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
                "model":     model_id,
                "backend":   backend_name,
            }
            log.info("%s ok: in_tok=%d out_tok=%d cache_read=%d elapsed_s=%.2f",
                     backend_name, usage["input_tokens"], usage["output_tokens"],
                     usage["cache_read_input_tokens"], elapsed)
            return _extract_json(text), usage
        except Exception as e:
            last_err = e
            from anthropic import APIStatusError
            if isinstance(e, APIStatusError) and 400 <= e.status_code < 500:
                raise
            log.warning("%s attempt %d failed (%s); retrying",
                        backend_name, attempt + 1, type(e).__name__)
            if attempt < config.CLAUDE_MAX_RETRIES - 1:
                time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"{backend_name} scoring failed after "
                       f"{config.CLAUDE_MAX_RETRIES} attempts: {last_err}")


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

    Returns (parsed_json, usage_dict). Backend chosen by config.LLM_BACKEND.
    """
    system, user = build_prompt(transcript, metadata, word_timings)
    b = _backend()
    if b == "bedrock":
        return _score_via_messages_api(
            _get_bedrock_client(), config.BEDROCK_MODEL, "bedrock",
            system, user,
        )
    if b == "anthropic":
        return _score_via_messages_api(
            _get_anthropic_client(), config.CLAUDE_MODEL, "anthropic",
            system, user,
        )
    return _score_via_openrouter(system, user)
