"""Cheap LLM client for ASO pipeline.

Primary: DeepSeek-V3 (~$0.27/M input, $1.10/M output).
Fallback: GLM-4-Flash (free tier), Kimi (Moonshot).

All providers use OpenAI-compatible API format.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum

import httpx

log = logging.getLogger("aso.llm")


class Model(str, Enum):
    """Available models by cost tier."""

    FAST = "deepseek-chat"  # DeepSeek-V3, cheapest
    REASON = "deepseek-reasoner"  # DeepSeek-R1, for complex analysis
    FREE = "glm-4-flash"  # Zhipu free tier


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key_env: str
    default_model: str

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


PROVIDERS = [
    Provider("deepseek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-chat"),
    Provider("zhipu", "https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY", "glm-4-flash"),
    Provider("moonshot", "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY", "moonshot-v1-8k"),
]


async def ask(
    prompt: str,
    *,
    system: str = "",
    model: str | Model = Model.FAST,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float = 120,
) -> str:
    """Send prompt to LLM with automatic fallback chain.

    Tries providers in order until one succeeds.
    Returns response text.
    Raises RuntimeError if all providers fail.
    """
    model_str = model.value if isinstance(model, Model) else model

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    errors: list[str] = []

    for provider in PROVIDERS:
        if not provider.api_key:
            continue

        # Use provider's default model if the requested model doesn't match this provider
        use_model = model_str
        if provider.name == "zhipu" and not model_str.startswith("glm"):
            use_model = provider.default_model
        elif provider.name == "moonshot" and not model_str.startswith("moonshot"):
            use_model = provider.default_model

        try:
            result = await _call_openai_compatible(
                base_url=provider.base_url,
                api_key=provider.api_key,
                model=use_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            log.info("LLM OK via %s (%s): %d chars", provider.name, use_model, len(result))
            return result

        except Exception as e:
            error_msg = f"{provider.name}: {e}"
            errors.append(error_msg)
            log.warning("LLM failed via %s: %s", provider.name, e)

    raise RuntimeError(f"All LLM providers failed: {'; '.join(errors)}")


async def _call_openai_compatible(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> str:
    """Call any OpenAI-compatible chat completions endpoint."""
    url = f"{base_url}/chat/completions"

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError(f"Empty choices from {model}: {data}")

    return choices[0]["message"]["content"].strip()
