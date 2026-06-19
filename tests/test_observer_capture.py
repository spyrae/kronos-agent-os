from kronos.observer.capture import (
    classify_capture,
    extract_urls,
    is_forced_capture,
    record_capture,
    strip_forced_capture_prefix,
)
from kronos.observer.models import ObserverSourceKind
from kronos.workspace import Workspace


def test_extract_urls_preserves_order_and_strips_trailing_punctuation():
    assert extract_urls("Read https://example.com/a, then https://x.com/post?x=1.") == [
        "https://example.com/a",
        "https://x.com/post?x=1",
    ]


def test_forced_capture_prefixes_strip_note_body():
    assert is_forced_capture("  Запомни: купить кофе")
    assert is_forced_capture("capture: ship Kronos")
    assert strip_forced_capture_prefix("  note: keep this") == "keep this"

    decision = classify_capture("сохрани: идея про Observer", is_voice=False, is_dm=True)

    assert decision.should_capture
    assert decision.source_kind == ObserverSourceKind.TELEGRAM_TEXT_CAPTURE
    assert decision.content == "идея про Observer"
    assert decision.original_modality == "text"


def test_standalone_url_in_dm_is_link_capture():
    decision = classify_capture("https://example.com/article", is_voice=False, is_dm=True)

    assert decision.should_capture
    assert decision.source_kind == ObserverSourceKind.TELEGRAM_LINK
    assert decision.content == "https://example.com/article"
    assert decision.urls == ("https://example.com/article",)
    assert decision.original_modality == "link"


def test_voice_note_in_dm_is_capture():
    decision = classify_capture("Идея для launch copy", is_voice=True, is_dm=True)

    assert decision.should_capture
    assert decision.source_kind == ObserverSourceKind.TELEGRAM_VOICE_NOTE
    assert decision.content == "Идея для launch copy"
    assert decision.original_modality == "voice"


def test_url_with_agent_question_is_not_capture():
    decision = classify_capture(
        "Что думаешь про https://example.com/pricing?",
        is_voice=False,
        is_dm=True,
    )

    assert not decision.should_capture
    assert decision.reason == "url_agent_request"
    assert decision.urls == ("https://example.com/pricing",)


def test_capture_classifier_ignores_images_and_non_dm_messages():
    assert not classify_capture("https://example.com", is_voice=False, is_dm=True, has_image=True).should_capture
    assert not classify_capture("запомни: group note", is_voice=False, is_dm=False).should_capture


def test_record_capture_writes_inbox_and_task_with_metadata(tmp_path):
    workspace = Workspace(tmp_path)

    task = record_capture(
        "capture: demo@example.com should be masked in metadata only",
        is_voice=False,
        is_dm=True,
        chat_id=42,
        user_id=7,
        message_id=100,
        timestamp="2026-06-19T08:00:00Z",
        workspace=workspace,
        extra_metadata={"email": "demo@example.com"},
    )

    assert task is not None
    assert task["source"]["kind"] == "telegram_text_capture"
    assert task["source"]["metadata"]["chat_id"] == 42
    assert task["source"]["metadata"]["user_id"] == 7
    assert task["source"]["metadata"]["message_id"] == 100
    assert task["source"]["metadata"]["timestamp"] == "2026-06-19T08:00:00Z"
    assert task["source"]["metadata"]["source_kind"] == "telegram_text_capture"
    assert task["source"]["metadata"]["original_modality"] == "text"
    assert task["source"]["metadata"]["email"] == "***@***.com"

    inbox = workspace.root / task["inbox_path"]
    task_path = workspace.queue_dir / f"{task['task_id']}.knowledge.json"

    assert inbox.exists()
    assert task_path.exists()
    assert "demo@example.com should be masked in metadata only" in inbox.read_text(encoding="utf-8")


def test_record_capture_returns_none_for_regular_chat(tmp_path):
    assert record_capture("привет, как дела?", is_voice=False, is_dm=True, workspace=Workspace(tmp_path)) is None
