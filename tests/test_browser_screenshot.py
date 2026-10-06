"""A03: a screenshot reaches a model as a whole image, or the tool says it did not."""

from unittest.mock import AsyncMock

import pytest

from kronos import vision
from kronos.security.model_budget import ModelBudgetError
from kronos.tools.browser import engine
from kronos.tools.browser.tools import browser_screenshot
from kronos.vision import VisionResult

PNG = b"\x89PNG\r\n\x1a\n" + b"pixel-data" * 500


@pytest.fixture
def seen(monkeypatch):
    calls = {}

    async def analyze(image_bytes, *, mime_type, prompt, context, detail="auto"):
        calls.update(image=image_bytes, mime_type=mime_type, prompt=prompt, context=context)
        return VisionResult(text="A pricing table; the Pro plan costs $20.", model="vision-test", mime_type=mime_type)

    monkeypatch.setattr(vision, "is_vision_configured", lambda: True)
    monkeypatch.setattr(vision, "analyze_image_bytes", analyze)
    monkeypatch.setattr(engine, "screenshot", AsyncMock(return_value=PNG))
    return calls


async def test_the_vision_model_receives_every_byte_of_the_screenshot(seen):
    reply = await browser_screenshot.ainvoke({"question": "what does the Pro plan cost?"})

    assert seen["image"] == PNG and seen["mime_type"] == "image/png"
    assert seen["prompt"] == "what does the Pro plan cost?"
    assert "$20" in reply and "vision-test" in reply


async def test_the_reply_is_a_description_not_a_base64_fragment(seen):
    import base64

    reply = await browser_screenshot.ainvoke({})

    assert base64.b64encode(PNG).decode()[:60] not in reply
    assert "base64" not in reply
    assert seen["prompt"], "a default question is asked when none is given"


async def test_page_content_is_framed_as_untrusted_for_the_vision_model(seen):
    await browser_screenshot.ainvoke({})
    assert "untrusted" in seen["context"]


async def test_without_a_vision_model_the_tool_says_so_and_takes_no_screenshot(monkeypatch):
    shot = AsyncMock(return_value=PNG)
    monkeypatch.setattr(vision, "is_vision_configured", lambda: False)
    monkeypatch.setattr(engine, "screenshot", shot)

    reply = await browser_screenshot.ainvoke({})

    assert reply.startswith("[ERROR]") and "browser_snapshot" in reply
    shot.assert_not_awaited()


@pytest.mark.parametrize(
    "image, expected",
    [(b"", "empty screenshot"), (b"x" * (vision.MAX_IMAGE_BYTES + 1), "too large")],
)
async def test_an_unreadable_image_is_an_error_not_a_success(monkeypatch, image, expected):
    analyze = AsyncMock()
    monkeypatch.setattr(vision, "is_vision_configured", lambda: True)
    monkeypatch.setattr(vision, "analyze_image_bytes", analyze)
    monkeypatch.setattr(engine, "screenshot", AsyncMock(return_value=image))

    reply = await browser_screenshot.ainvoke({})

    assert reply.startswith("[ERROR]") and expected in reply
    analyze.assert_not_awaited()


@pytest.mark.parametrize(
    "failure, expected",
    [
        (ModelBudgetError("Daily cost limit reached"), "Daily cost limit reached"),
        (RuntimeError("provider said: sk-secret-abcdef123456"), "could not be read"),
    ],
)
async def test_a_vision_failure_is_reported_without_leaking_its_text(monkeypatch, failure, expected):
    monkeypatch.setattr(vision, "is_vision_configured", lambda: True)
    monkeypatch.setattr(vision, "analyze_image_bytes", AsyncMock(side_effect=failure))
    monkeypatch.setattr(engine, "screenshot", AsyncMock(return_value=PNG))

    reply = await browser_screenshot.ainvoke({})

    assert reply.startswith("[ERROR]") and expected in reply
    assert "sk-secret-abcdef123456" not in reply


def test_the_tool_stays_a_read_whose_output_is_untrusted():
    from kronos.security.effects import SIDE_EFFECT_METADATA_KEY
    from kronos.security.untrusted import UNTRUSTED_METADATA_KEY

    assert browser_screenshot.metadata[SIDE_EFFECT_METADATA_KEY] is False
    assert browser_screenshot.metadata[UNTRUSTED_METADATA_KEY] is True
