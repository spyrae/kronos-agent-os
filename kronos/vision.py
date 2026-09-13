"""Vision input support via OpenAI Responses API."""

from __future__ import annotations

import base64
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any

from kronos.config import settings
from kronos.llm_codex import run_codex_command
from kronos.security.direct_model import admit_fixed_model, record_direct_response

SUPPORTED_IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
DEFAULT_VISION_PROMPT = (
    "Проанализируй изображение для персонального агента. "
    "Если на изображении есть текст, извлеки его на языке оригинала. "
    "Опиши важные визуальные детали, определи тип документа/скриншота, "
    "и выдели задачи, даты, суммы, контакты или action items, если они есть."
)


@dataclass(frozen=True)
class VisionResult:
    """Text extracted from a vision-capable model."""

    text: str
    model: str
    mime_type: str


def is_supported_image_mime(mime_type: str) -> bool:
    return mime_type.lower().split(";", 1)[0].strip() in SUPPORTED_IMAGE_MIME_TYPES


def is_vision_configured() -> bool:
    provider = _vision_provider()
    if provider == "codex-cli":
        return bool(shutil.which(settings.kaos_codex_command))
    if provider == "openai-api":
        return bool(settings.openai_api_key and settings.kaos_vision_model)
    return False


async def analyze_image_bytes(
    image_bytes: bytes,
    *,
    mime_type: str = "image/jpeg",
    prompt: str = "",
    context: str = "",
    detail: str = "auto",
) -> VisionResult:
    """Analyze one image with the configured vision backend."""
    normalized_mime = mime_type.lower().split(";", 1)[0].strip() or "image/jpeg"
    if not is_supported_image_mime(normalized_mime):
        supported = ", ".join(sorted(SUPPORTED_IMAGE_MIME_TYPES))
        raise ValueError(f"Unsupported image type '{mime_type}'. Supported: {supported}")
    if not image_bytes:
        raise ValueError("Image is empty")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image is too large ({len(image_bytes)} bytes, max {MAX_IMAGE_BYTES})")

    provider = _vision_provider()
    if provider == "codex-cli":
        return await _analyze_with_codex_cli(
            image_bytes,
            mime_type=normalized_mime,
            prompt=prompt,
            context=context,
        )
    if provider == "openai-api":
        return await _analyze_with_openai_api(
            image_bytes,
            mime_type=normalized_mime,
            prompt=prompt,
            context=context,
            detail=detail,
        )
    raise RuntimeError(f"Unsupported vision provider '{settings.kaos_vision_provider}'")


async def _analyze_with_openai_api(
    image_bytes: bytes,
    *,
    mime_type: str,
    prompt: str,
    context: str,
    detail: str,
) -> VisionResult:
    if not settings.openai_api_key or not settings.kaos_vision_model:
        raise RuntimeError("OpenAI API vision is not configured. Set OPENAI_API_KEY and KAOS_VISION_MODEL.")
    from openai import AsyncOpenAI

    admit_fixed_model("Vision")
    instruction = _build_prompt(prompt=prompt, context=context)
    data_url = _to_data_url(image_bytes, mime_type)
    async with AsyncOpenAI(api_key=settings.openai_api_key) as client:
        response = await client.responses.create(
            model=settings.kaos_vision_model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": instruction},
                        {"type": "input_image", "image_url": data_url, "detail": detail},
                    ],
                }
            ],
        )
        text = _extract_response_text(response)
        record_direct_response(
            model=settings.kaos_vision_model,
            response=response,
            input_content=instruction,
            output_content=text,
        )
    if not text.strip():
        raise RuntimeError("Vision model returned no text")
    return VisionResult(
        text=text,
        model=settings.kaos_vision_model,
        mime_type=mime_type,
    )


async def _analyze_with_codex_cli(
    image_bytes: bytes,
    *,
    mime_type: str,
    prompt: str,
    context: str,
) -> VisionResult:
    command = settings.kaos_codex_command
    if not shutil.which(command):
        raise RuntimeError(
            f"Codex CLI vision is not configured. Install/login Codex CLI or set KAOS_VISION_PROVIDER=openai-api. "
            f"Command not found: {command}"
        )
    admit_fixed_model("Codex Vision", lite_compatible=True)

    instruction = _build_prompt(prompt=prompt, context=context)
    suffix = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(mime_type, ".img")

    image_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as image_file:
            image_file.write(image_bytes)
            image_path = image_file.name

        def make_args(output_path: str) -> list[str]:
            args = [
                command,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--output-last-message",
                output_path,
                "--images",
                image_path,
            ]
            if settings.kaos_vision_model:
                args.extend(["-m", settings.kaos_vision_model])
            args.append(instruction)
            return args

        text = await run_codex_command(make_args, timeout_seconds=settings.kaos_vision_timeout_seconds)
        record_direct_response(
            model=settings.kaos_vision_model or "codex-cli-default",
            response=None,
            input_content=instruction,
            output_content=text,
            billing="subscription",
        )
        return VisionResult(
            text=text,
            model=settings.kaos_vision_model or "codex-cli-default",
            mime_type=mime_type,
        )
    finally:
        if image_path and os.path.exists(image_path):
            os.unlink(image_path)


def _build_prompt(*, prompt: str, context: str) -> str:
    parts = [prompt.strip() or DEFAULT_VISION_PROMPT]
    if context.strip():
        parts.append(f"Контекст сообщения пользователя/caption:\n{context.strip()}")
    return "\n\n".join(parts)


def _to_data_url(image_bytes: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _vision_provider() -> str:
    return settings.kaos_vision_provider.strip().lower().replace("_", "-")


def _extract_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", "")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", "")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
    return "\n".join(chunks).strip()
