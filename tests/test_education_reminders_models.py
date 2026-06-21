from datetime import UTC, datetime

from kronos.education_reminders.models import (
    AnnouncementPreview,
    QuietHours,
    ScheduledAnnouncement,
    compute_idempotency_key,
    evaluate_send_gates,
)

NOW = datetime(2026, 6, 19, 10, 0, tzinfo=UTC)


def test_scheduled_announcement_derives_stable_idempotency_key_and_roundtrips():
    announcement = ScheduledAnnouncement.create(
        target_group_id=-100123,
        topic_id=42,
        message_template="Сегодня в {lesson_time} занятие.",
        schedule="FREQ=WEEKLY;BYDAY=MO;BYHOUR=19;BYMINUTE=0",
        timezone="Europe/Moscow",
    )

    expected = compute_idempotency_key(
        target_group_id=-100123,
        topic_id=42,
        schedule="FREQ=WEEKLY;BYDAY=MO;BYHOUR=19;BYMINUTE=0",
        message_template="Сегодня в {lesson_time} занятие.",
    )

    assert announcement.idempotency_key == expected
    assert announcement.enabled is False
    assert announcement.preview_required is True
    assert ScheduledAnnouncement.from_dict(announcement.to_dict()) == announcement


def test_preview_renders_template_without_llm_and_preserves_missing_tokens():
    announcement = ScheduledAnnouncement.create(
        target_group_id=-100123,
        message_template="Сегодня в {lesson_time}. Дедлайн: {deadline}.",
        schedule="daily:19:00",
    )

    preview = AnnouncementPreview.from_announcement(
        announcement,
        context={"lesson_time": "19:00"},
    )

    assert preview.dry_run is True
    assert preview.rendered_text == "Сегодня в 19:00. Дедлайн: {deadline}."
    assert preview.idempotency_key == announcement.idempotency_key


def test_send_gates_block_dry_run_disabled_unapproved_and_not_allowlisted():
    announcement = ScheduledAnnouncement.create(
        target_group_id=-100123,
        message_template="Reminder",
        schedule="daily:19:00",
        enabled=False,
    )

    assert evaluate_send_gates(
        announcement,
        allowed_group_ids={-100123},
        sent_idempotency_keys=set(),
        preview_approved=True,
        dry_run=True,
        now=NOW,
    ).reason == "dry_run"

    assert evaluate_send_gates(
        announcement,
        allowed_group_ids={-100123},
        sent_idempotency_keys=set(),
        preview_approved=True,
        dry_run=False,
        now=NOW,
    ).reason == "announcement_disabled"

    enabled = ScheduledAnnouncement.create(
        target_group_id=-100123,
        message_template="Reminder",
        schedule="daily:19:00",
        enabled=True,
    )
    assert evaluate_send_gates(
        enabled,
        allowed_group_ids={-999},
        sent_idempotency_keys=set(),
        preview_approved=True,
        dry_run=False,
        now=NOW,
    ).reason == "target_group_not_allowlisted"

    assert evaluate_send_gates(
        enabled,
        allowed_group_ids={-100123},
        sent_idempotency_keys=set(),
        preview_approved=False,
        dry_run=False,
        now=NOW,
    ).reason == "preview_required"


def test_send_gates_block_duplicate_and_quiet_hours_then_allow_send():
    announcement = ScheduledAnnouncement.create(
        target_group_id=-100123,
        message_template="Reminder",
        schedule="daily:19:00",
        enabled=True,
        quiet_hours=QuietHours(start_hour=22, end_hour=8),
    )

    assert evaluate_send_gates(
        announcement,
        allowed_group_ids={-100123},
        sent_idempotency_keys={announcement.idempotency_key},
        preview_approved=True,
        dry_run=False,
        now=NOW,
    ).reason == "idempotency_key_already_sent"

    quiet = datetime(2026, 6, 19, 23, 0, tzinfo=UTC)
    assert evaluate_send_gates(
        announcement,
        allowed_group_ids={-100123},
        sent_idempotency_keys=set(),
        preview_approved=True,
        dry_run=False,
        now=quiet,
    ).reason == "quiet_hours"

    decision = evaluate_send_gates(
        announcement,
        allowed_group_ids={-100123},
        sent_idempotency_keys=set(),
        preview_approved=True,
        dry_run=False,
        now=NOW,
    )
    assert decision.can_send is True
    assert decision.reason == "ok"
