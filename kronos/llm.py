"""LLM factory with multi-provider fallback chain and cooldown tracking.

Resolution order per tier:
  standard: Kimi K2.5 → DeepSeek V3 (fallback)
  lite:     DeepSeek V3 → Kimi K2.5 (fallback)

On error, automatically tries next provider in chain.
Failed providers enter cooldown (5 min) before being retried.
"""

import logging
import time
from enum import Enum

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage

from kronos.config import settings

log = logging.getLogger("kronos.llm")

COOLDOWN_SECONDS = 300  # 5 minutes


class ModelTier(str, Enum):
    LITE = "lite"
    STANDARD = "standard"


class _ProviderState:
    """Tracks per-provider health and cooldown."""

    def __init__(self):
        self._cooldowns: dict[str, float] = {}  # provider -> cooldown_until timestamp
        self._models: dict[str, BaseChatModel] = {}  # provider -> cached instance

    def is_available(self, provider: str) -> bool:
        cooldown_until = self._cooldowns.get(provider, 0)
        if time.time() < cooldown_until:
            return False
        return True

    def mark_failed(self, provider: str) -> None:
        self._cooldowns[provider] = time.time() + COOLDOWN_SECONDS
        log.warning("Provider '%s' entered cooldown for %ds", provider, COOLDOWN_SECONDS)

    def mark_success(self, provider: str) -> None:
        self._cooldowns.pop(provider, None)

    def get_or_create(self, provider: str) -> BaseChatModel | None:
        if provider not in self._models:
            model = _create_model(provider)
            if model:
                self._models[provider] = model
        return self._models.get(provider)

    def reset_cooldown(self, provider: str) -> None:
        self._cooldowns.pop(provider, None)


_state = _ProviderState()

# Provider chains per tier
_STANDARD_CHAIN = ["kimi", "deepseek"]
_LITE_CHAIN = ["deepseek", "kimi"]


def get_model(tier: ModelTier = ModelTier.STANDARD) -> BaseChatModel:
    """Get primary LLM for the given tier.

    Returns the first available provider in the chain.
    Skips providers in cooldown or without API keys.
    """
    chain = _STANDARD_CHAIN if tier == ModelTier.STANDARD else _LITE_CHAIN

    for provider in chain:
        if not _has_key(provider):
            continue
        if not _state.is_available(provider):
            continue
        model = _state.get_or_create(provider)
        if model:
            return model

    # Fallback: ignore cooldowns, try anything
    for provider in chain:
        if not _has_key(provider):
            continue
        model = _state.get_or_create(provider)
        if model:
            log.warning("All providers in cooldown, using '%s' anyway", provider)
            return model

    raise RuntimeError(f"No API keys configured for {tier.value} tier")


def get_fallback_model() -> BaseChatModel:
    """Get a fallback model (used by graph.py on primary failure)."""
    for provider in ["deepseek", "kimi"]:
        if not _has_key(provider):
            continue
        if not _state.is_available(provider):
            continue
        model = _state.get_or_create(provider)
        if model:
            return model

    raise RuntimeError("No fallback API keys configured")


def invoke_with_fallback(
    messages: list[BaseMessage],
    tier: ModelTier = ModelTier.STANDARD,
    tools: list | None = None,
) -> BaseMessage:
    """Invoke LLM with automatic fallback chain.

    Tries each provider in the chain. On failure, marks provider
    for cooldown and tries the next one.

    Returns the AI response message.
    """
    chain = _STANDARD_CHAIN if tier == ModelTier.STANDARD else _LITE_CHAIN
    last_error = None

    for provider in chain:
        if not _has_key(provider):
            continue
        if not _state.is_available(provider):
            log.debug("Skipping '%s' (cooldown)", provider)
            continue

        model = _state.get_or_create(provider)
        if not model:
            continue

        try:
            if tools:
                model = model.bind_tools(tools)
            response = model.invoke(messages)
            _state.mark_success(provider)
            return response
        except Exception as e:
            last_error = e
            _state.mark_failed(provider)
            log.error("Provider '%s' failed: %s", provider, e)
            continue

    # Last resort: retry first available ignoring cooldowns
    for provider in chain:
        if not _has_key(provider):
            continue
        model = _state.get_or_create(provider)
        if not model:
            continue
        try:
            if tools:
                model = model.bind_tools(tools)
            response = model.invoke(messages)
            _state.mark_success(provider)
            return response
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(f"All providers failed. Last error: {last_error}")


def _has_key(provider: str) -> bool:
    if provider == "kimi":
        return bool(settings.fireworks_api_key)
    if provider == "deepseek":
        return bool(settings.deepseek_api_key)
    return False


def _create_model(provider: str) -> BaseChatModel | None:
    try:
        if provider == "kimi":
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(
                model="accounts/fireworks/routers/kimi-k2p5-turbo",
                base_url="https://api.fireworks.ai/inference/v1",
                api_key=settings.fireworks_api_key,
                max_tokens=8192,
                temperature=0.5,
            )
        elif provider == "deepseek":
            from langchain_deepseek import ChatDeepSeek
            return ChatDeepSeek(
                model="deepseek-chat",
                api_key=settings.deepseek_api_key,
                max_tokens=4096,
                temperature=0.5,
            )
    except Exception as e:
        log.error("Failed to create '%s' model: %s", provider, e)
    return None
