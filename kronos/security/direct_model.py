"""Admission and usage accounting for model calls outside LangChain."""

import json
import math
import urllib.request
from collections.abc import Mapping
from typing import Any

from kronos.execution_control import check_execution
from kronos.security.cost_tracking import BillingKind, estimate_audio_cost_usd, estimate_cost_usd, record_llm_cost
from kronos.security.model_budget import ModelBudgetError, admit_model_call


class ModelUsageUnknownError(RuntimeError):
    """A dispatched request has no usable usage; reconciliation is required."""


def admit_fixed_model(label: str, *, lite_compatible: bool = False) -> None:
    """Never silently relabel a measurement or replace a required modality."""
    if admit_model_call() and not lite_compatible:
        raise ModelBudgetError(f"{label} paused by budget downgrade; no compatible lite replacement configured")


def _field(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)


def chat_response_text(response: Any) -> str:
    """Extract text without embedding a raw provider response into an error."""
    choices = _field(response, "choices") or []
    if not choices:
        return ""
    content = _field(_field(choices[0], "message"), "content")
    return content if isinstance(content, str) else ""


def _tokens(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def record_direct_response(
    *,
    model: str,
    response: Any,
    input_content: Any,
    output_content: Any,
    billing: BillingKind = "api",
) -> None:
    """Account one response; absent usage is an estimate, never a free API call.

    Estimation shares the existing callback pricing contract. It is not a
    provider invoice or an upper bound for images, retries and unknown outcomes.
    The durable reservation/reconciliation stage must retain that distinction.
    """
    usage = _field(response, "usage")
    input_tokens = _tokens(_field(usage, "input_tokens"))
    if input_tokens is None:
        input_tokens = _tokens(_field(usage, "prompt_tokens"))
    output_tokens = _tokens(_field(usage, "output_tokens"))
    if output_tokens is None:
        output_tokens = _tokens(_field(usage, "completion_tokens"))
    if input_tokens is None:
        input_tokens = max(1, math.ceil(len(json.dumps(input_content, ensure_ascii=False, default=str)) / 3.5))
    if output_tokens is None:
        output_tokens = math.ceil(len(json.dumps(output_content, ensure_ascii=False, default=str)) / 3.5)
    cost = estimate_cost_usd(model, input_tokens, output_tokens, billing=billing)
    record_llm_cost(model, input_tokens, output_tokens, cost)
    check_execution()


def record_direct_audio_response(*, model: str, response: Any) -> None:
    """Record returned audio duration before cleanup; never invent text usage.

    Missing/invalid duration is explicitly unknown, not a zero-cost success.
    Durable unknown-outcome recording remains part of the next ledger stage.
    """
    try:
        cost = estimate_audio_cost_usd(model, _field(response, "duration"))
    except (ValueError, OverflowError) as error:
        raise ModelUsageUnknownError("Invalid audio duration or price; cost is unknown and needs reconciliation") from error
    record_llm_cost(model, 0, 0, cost)
    check_execution()


def ask_script_model(prompt: str, *, base_url: str, api_key: str, model: str, max_tokens: int, timeout: float) -> str:
    """Keep standalone script text calls on the runtime's budget boundary."""
    messages = [{"role": "user", "content": prompt}]
    if admit_model_call():
        from langchain_core.messages import HumanMessage

        from kronos.llm import ModelTier, get_model

        response = get_model(ModelTier.LITE).invoke(
            [HumanMessage(content=prompt)], max_tokens=max_tokens, timeout=timeout
        )
        if not isinstance(response.content, str) or not response.content.strip():
            raise RuntimeError("Lite model returned no text")
        return response.content

    body = json.dumps({"model": model, "messages": messages, "max_tokens": max_tokens}).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read())
        text = chat_response_text(data)
        record_direct_response(model=model, response=data, input_content=messages, output_content=text)
    if not text.strip():
        raise RuntimeError("Script model returned no text")
    return text
