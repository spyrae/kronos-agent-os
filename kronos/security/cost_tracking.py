"""Always-on LLM cost tracking.

A single LangChain callback, attached to every model the factory builds,
reads each LLM call's token usage in ``on_llm_end`` and records the cost in
two places:

* the shared swarm ledger (``swarm_costs``) — the daily budget is swarm-wide,
  so one of the six agents can't quietly burn the whole day's limit on its own
  while the other five each read a private per-process total;
* the per-process :class:`CostGuardian` session tally, so the per-conversation
  limit (previously dead — ``record_cost`` had no caller) actually bites.

Token counts come from the response when the provider reports them
(``usage_metadata`` / ``llm_output['token_usage']`` — OpenAI, DeepSeek). The
Codex CLI backend returns only text, so for it we fall back to a length-based
estimate captured at start (same ~3.5 chars/token heuristic as the audit log).

Pricing is per model and env-overridable
(``KAOS_MODEL_PRICE_<MODEL>_INPUT`` / ``_OUTPUT``, per 1M tokens). Codex runs
on a ChatGPT OAuth subscription — flat-rate, no per-token API charge — so its
marginal price defaults to zero. The budget then tracks real API spend
(DeepSeek), which is the money that actually accrues.

The callback never raises into the LLM call: any accounting failure is logged
at debug and swallowed, because a metrics glitch must not break a reply.
"""

from __future__ import annotations

import logging
import math
import os
import re
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from kronos.audit import get_tool_audit_context
from kronos.config import settings

log = logging.getLogger("kronos.security.cost")

# Per-1M-token prices as (input, output). Lower-cased model name is the key.
# Override any entry from the environment:
#   KAOS_MODEL_PRICE_DEEPSEEK_CHAT_INPUT=0.27  (dots/dashes → underscore, upper)
_MODEL_PRICES: dict[str, tuple[float, float]] = {
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    # Codex CLI uses ChatGPT OAuth (subscription, not per-token API billing),
    # so its marginal cost is zero. Override via env if billed per token.
    "gpt-5.5": (0.0, 0.0),
    "gpt-5": (0.0, 0.0),
}

# Unknown, API-billed model — a conservative non-zero estimate so an
# unpriced provider still counts against the budget rather than reading free.
_DEFAULT_PRICE = (0.50, 1.50)

_CHARS_PER_TOKEN = 3.5  # matches kronos.audit._estimate_tokens


def _to_float(value: Any, default: float) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _price_for(model: str) -> tuple[float, float]:
    """Resolve (input, output) per-1M price for a model, env-overridable."""
    key = (model or "").strip().lower()
    base = _MODEL_PRICES.get(key, _DEFAULT_PRICE)
    env_key = re.sub(r"[^A-Z0-9]+", "_", key.upper()).strip("_")
    if not env_key:
        return base
    in_price = _to_float(os.environ.get(f"KAOS_MODEL_PRICE_{env_key}_INPUT"), base[0])
    out_price = _to_float(os.environ.get(f"KAOS_MODEL_PRICE_{env_key}_OUTPUT"), base[1])
    return in_price, out_price


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD cost for a call given its model and token counts."""
    in_price, out_price = _price_for(model)
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return str(content)


def _extract_usage(response: Any) -> tuple[str, int, int]:
    """Return (model, input_tokens, output_tokens) from an LLMResult.

    Prefers per-message ``usage_metadata`` (provider-agnostic), falling back
    to ``llm_output['token_usage']`` (OpenAI shape). Zeros mean the provider
    reported nothing — the caller then estimates from text length.
    """
    model = ""
    input_tokens = 0
    output_tokens = 0

    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, dict):
        model = str(llm_output.get("model_name") or llm_output.get("model") or "")
        usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
        if isinstance(usage, dict):
            input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)

    for batch in getattr(response, "generations", None) or []:
        for generation in batch:
            message = getattr(generation, "message", None)
            if message is None:
                continue
            usage_metadata = getattr(message, "usage_metadata", None)
            if isinstance(usage_metadata, dict) and (
                usage_metadata.get("input_tokens") or usage_metadata.get("output_tokens")
            ):
                input_tokens = int(usage_metadata.get("input_tokens") or 0)
                output_tokens = int(usage_metadata.get("output_tokens") or 0)
            metadata = getattr(message, "response_metadata", None)
            if not model and isinstance(metadata, dict):
                model = str(metadata.get("model_name") or metadata.get("model") or "")

    return model, input_tokens, output_tokens


def _response_text_len(response: Any) -> int:
    """Total characters of generated text, for the estimate fallback."""
    total = 0
    for batch in getattr(response, "generations", None) or []:
        for generation in batch:
            text = getattr(generation, "text", "") or ""
            if not text:
                message = getattr(generation, "message", None)
                text = _content_text(getattr(message, "content", "")) if message else ""
            total += len(text)
    return total


def record_llm_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
) -> None:
    """Record one call's cost to the swarm ledger and the session guardian.

    Agent and session come from the per-request audit context (set around the
    model loop in ``graph.ainvoke``); the swarm write always has the agent
    (falls back to ``settings.agent_name``), the session tally only fires when
    a ``session_id`` is present. Both writes are best-effort.
    """
    context = get_tool_audit_context()
    agent = context.get("agent") or settings.agent_name
    session_id = context.get("session_id", "")

    try:
        from kronos.swarm_store import get_swarm

        get_swarm().add_cost(
            agent=agent,
            cost_usd=cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    except Exception as e:  # pragma: no cover - defensive
        log.debug("Swarm cost ledger write failed: %s", e)

    if session_id:
        try:
            from kronos.security.cost_guardian import get_guardian

            get_guardian().record_cost(session_id, cost_usd)
        except Exception as e:  # pragma: no cover - defensive
            log.debug("Guardian session cost record failed: %s", e)


class CostTrackingCallbackHandler(BaseCallbackHandler):
    """Record every LLM call's cost to the swarm ledger + session guardian."""

    raise_error = False  # a metrics failure must never abort an LLM call

    def __init__(self) -> None:
        super().__init__()
        # run_id -> input char count, kept only for the Codex estimate fallback.
        self._pending_input_chars: dict[str, int] = {}

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        chars = 0
        for batch in messages or []:
            for message in batch:
                chars += len(_content_text(getattr(message, "content", "")))
        if run_id is not None:
            self._pending_input_chars[str(run_id)] = chars

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        chars = sum(len(prompt) for prompt in prompts or [])
        if run_id is not None:
            self._pending_input_chars[str(run_id)] = chars

    def on_llm_end(self, response: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        try:
            model, input_tokens, output_tokens = _extract_usage(response)
            if input_tokens == 0 and output_tokens == 0:
                # Provider reported no usage (Codex CLI). Estimate from lengths.
                input_chars = self._pending_input_chars.get(str(run_id), 0)
                output_chars = _response_text_len(response)
                input_tokens = math.ceil(input_chars / _CHARS_PER_TOKEN)
                output_tokens = math.ceil(output_chars / _CHARS_PER_TOKEN)
            cost = estimate_cost_usd(model, input_tokens, output_tokens)
            record_llm_cost(model, input_tokens, output_tokens, cost)
        except Exception as e:  # pragma: no cover - defensive
            log.debug("Cost tracking failed: %s", e)
        finally:
            if run_id is not None:
                self._pending_input_chars.pop(str(run_id), None)

    def on_llm_error(self, error: BaseException, *, run_id: Any = None, **kwargs: Any) -> None:
        if run_id is not None:
            self._pending_input_chars.pop(str(run_id), None)


_handler: CostTrackingCallbackHandler | None = None


def get_cost_callbacks() -> list[BaseCallbackHandler]:
    """The always-on cost callback (cached singleton), for model construction."""
    global _handler
    if _handler is None:
        _handler = CostTrackingCallbackHandler()
    return [_handler]
