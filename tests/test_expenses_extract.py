from dataclasses import dataclass

from kronos.cron.expenses.extract import (
    ExtractedExpense,
    audit_expense,
    extract_expenses,
)
from kronos.cron.expenses.gmail import EmailMessage


@dataclass
class _Resp:
    content: str


class FakeModel:
    """Minimal chat model stub: returns a fixed content string on invoke."""

    def __init__(self, content: str):
        self._content = content
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append(messages[0].content)
        return _Resp(self._content)


def _email(text="dummy"):
    return EmailMessage(message_id="m1", text=text)


def test_extract_parses_expense():
    model = FakeModel(
        '{"expenses":[{"description":"GrabFood Warung","amount":"41,500",'
        '"currency":"IDR","category":"Food","category_confidence":0.9,'
        '"date":"2026-07-05","merchant":"Warung Bu Made"}]}'
    )
    out = extract_expenses(_email("Grab receipt 41,500 IDR"), model=model)

    assert len(out) == 1
    exp = out[0]
    assert exp.amount == 41500.0
    assert exp.currency == "IDR"
    assert exp.category == "Food"
    assert exp.confidence == 0.9
    assert exp.expense_date == "2026-07-05"


def test_extract_skips_non_expense_empty():
    model = FakeModel('{"expenses":[]}')
    assert extract_expenses(_email("Top-up successful"), model=model) == []


def test_extract_drops_item_without_amount():
    model = FakeModel('{"expenses":[{"description":"?","currency":"IDR","category":"Food"}]}')
    assert extract_expenses(_email(), model=model) == []


def test_extract_unmapped_category_forces_low_confidence():
    model = FakeModel(
        '{"expenses":[{"description":"ATM Bali","amount":500000,"currency":"IDR",'
        '"category":"Cash","category_confidence":0.8}]}'
    )
    out = extract_expenses(_email(), model=model)
    assert len(out) == 1
    assert out[0].category is None
    assert out[0].confidence == 0.0  # unmapped → uncertain → pending downstream


def test_extract_maps_category_alias():
    model = FakeModel(
        '{"expenses":[{"description":"Hosting","amount":9.99,"currency":"USD",'
        '"category":"Subscription","category_confidence":0.95}]}'
    )
    out = extract_expenses(_email(), model=model)
    assert out[0].category == "Subscriptions"


def test_extract_handles_non_json():
    model = FakeModel("sorry, I could not parse this email")
    assert extract_expenses(_email(), model=model) == []


def test_audit_ok_verdict():
    model = FakeModel(
        '{"ok":true,"is_expense":true,"amount_matches":true,"category":"Food","confidence":0.9,"issues":""}'
    )
    exp = ExtractedExpense("GrabFood", 41500, "IDR", "Food", 0.9, "2026-07-05")
    verdict = audit_expense("Grab receipt 41,500 IDR", exp, model=model)

    assert verdict.ok is True
    assert verdict.is_expense is True
    assert verdict.amount_matches is True
    assert verdict.category == "Food"


def test_audit_amount_mismatch_not_ok():
    model = FakeModel(
        '{"ok":false,"is_expense":true,"amount_matches":false,"category":"Food",'
        '"confidence":0.2,"issues":"amount 41500 not found in email"}'
    )
    exp = ExtractedExpense("GrabFood", 999999, "IDR", "Food", 0.9, "2026-07-05")
    verdict = audit_expense("Grab receipt 41,500 IDR", exp, model=model)

    assert verdict.ok is False
    assert verdict.amount_matches is False
    assert "not found" in verdict.issues


def test_audit_fails_closed_without_json():
    model = FakeModel("hmm")
    exp = ExtractedExpense("X", 100, "IDR", "Food", 0.9, "2026-07-05")
    verdict = audit_expense("email", exp, model=model)

    assert verdict.ok is False
    assert verdict.issues == "audit unavailable"
