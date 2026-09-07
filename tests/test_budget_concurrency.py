"""Budget read/modify/write must be one transaction, including across processes."""

import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

from kronos.tools import budget_lock, expense

BUDGET = """# Budget

## Активные транши

| # | Дата | Сумма IDR | Остаток IDR | Курс (IDR/RUB) | Курс (IDR/USD) | Заметка |
|---|------|-----------|-------------|-----------------|-----------------|---------|
| 1 | 01.09.2026 | 1,000 | 1,000 | 200 | 16000 | Initial |

## История
"""


def spend(amount):
    return expense.add_expense.invoke({"description": "Test", "amount": amount, "currency": "IDR", "category": "Food"})


@pytest.fixture
def budget(tmp_path, monkeypatch):
    path = tmp_path / "BUDGET.md"
    path.write_text(BUDGET)
    monkeypatch.setattr(expense, "_budget_path", lambda: str(path))
    monkeypatch.setattr(expense, "_schedule_duplicate_cleanup", lambda **kw: None)
    monkeypatch.setattr(expense, "_notion_create_page", lambda _: {"id": "test-page"})
    return path


def remaining(path):
    return sum(row["remaining"] for row in expense._parse_tranches(path.read_text()))


def test_two_expenses_cannot_read_the_same_uncommitted_balance(budget, monkeypatch):
    entered, release, second_post, second_started = (threading.Event() for _ in range(4))
    calls = []

    def notion(properties):
        calls.append(properties)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        else:
            second_post.set()
        return {"id": "test-page"}

    def second():
        second_started.set()
        return spend(200)

    monkeypatch.setattr(expense, "_notion_create_page", notion)
    with ThreadPoolExecutor(2) as pool:
        one = pool.submit(spend, 100)
        try:
            assert entered.wait(5)
            two = pool.submit(second)
            assert second_started.wait(5)
            assert not second_post.wait(0.15), "the second POST must wait for the first budget commit"
        finally:
            release.set()
        assert one.result(5).startswith("✅")
        assert two.result(5).startswith("✅")
    assert remaining(budget) == 700
    assert len(calls) == 2


@pytest.mark.parametrize("operation", ["add", "replace"])
def test_tranche_changes_wait_for_inflight_expense(budget, monkeypatch, operation):
    entered, release, started = (threading.Event() for _ in range(3))

    def notion(_):
        entered.set()
        assert release.wait(5)
        return {"id": "test-page"}

    def change():
        started.set()
        if operation == "add":
            return expense.add_tranche.invoke({"amount_idr": 500, "rate": 250, "rate_usd": 16500})
        return expense.replace_tranche.invoke({"tranche_num": 1, "new_rate": 250})

    monkeypatch.setattr(expense, "_notion_create_page", notion)
    with ThreadPoolExecutor(2) as pool:
        one = pool.submit(spend, 100)
        try:
            assert entered.wait(5)
            two = pool.submit(change)
            assert started.wait(5)
            with pytest.raises(TimeoutError):
                two.result(timeout=0.15)
            assert budget.read_text() == BUDGET
        finally:
            release.set()
        assert one.result(5).startswith("✅")
        assert two.result(5).startswith("OK")
    assert remaining(budget) == (1400 if operation == "add" else 900)
    if operation == "replace":
        assert expense._parse_tranches(budget.read_text())[0]["rate"] == 250


def test_lock_failure_stops_before_notion_write(budget, monkeypatch):
    import fcntl

    post = Mock()
    monkeypatch.setattr(expense, "_notion_create_page", post)
    monkeypatch.setattr(budget_lock, "LOCK_TIMEOUT_SECONDS", 0.02)
    with budget.with_name("BUDGET.md.lock").open("a") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        try:
            assert spend(100).startswith("[ERROR] budget is busy")
        finally:
            fcntl.flock(other, fcntl.LOCK_UN)
    post.assert_not_called()
    assert budget.read_text() == BUDGET


def test_failure_releases_lock_and_keeps_old_budget(budget, monkeypatch):
    post = Mock(side_effect=[RuntimeError("rejected"), {"id": "test-page"}])
    monkeypatch.setattr(expense, "_notion_create_page", post)
    assert spend(100).startswith("[ERROR]")
    assert budget.read_text() == BUDGET
    assert spend(200).startswith("✅")
    assert remaining(budget) == 800


@pytest.mark.parametrize("operation", ["add", "replace"])
def test_tranche_write_failure_never_truncates_budget(budget, monkeypatch, operation):
    def fail_replace(*args):
        raise OSError("replace refused")

    monkeypatch.setattr(expense.os, "replace", fail_replace)
    with pytest.raises(OSError):
        if operation == "add":
            expense.add_tranche.invoke({"amount_idr": 500, "rate": 200})
        else:
            expense.replace_tranche.invoke({"tranche_num": 1, "new_rate": 250})
    assert budget.read_text() == BUDGET
    assert not list(budget.parent.glob(".tmp-budget-*"))


def _process_spend(path, amount, barrier, results):
    """Spawn entry point: a real independent process, with no external API calls."""
    expense._budget_path = lambda: path
    expense._schedule_duplicate_cleanup = lambda **kw: None

    def notion(_):
        time.sleep(0.1)
        return {"id": "test-page"}

    expense._notion_create_page = notion
    try:
        barrier.wait(timeout=10)
        results.put(spend(amount))
    except Exception as exc:
        results.put(type(exc).__name__)


def test_processes_and_symlink_aliases_share_the_same_lock(budget, monkeypatch):
    monkeypatch.setenv("KAOS_ENV_FILE", "/dev/null")
    alias = budget.parent / "alias.md"
    alias.symlink_to(budget)
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(2), context.Queue()
    children = [
        context.Process(target=_process_spend, args=(str(path), amount, barrier, results))
        for path, amount in [(budget, 100), (alias, 200)]
    ]
    try:
        for child in children:
            child.start()
        replies = [results.get(timeout=20) for _ in children]
        for child in children:
            child.join(timeout=10)
            assert child.exitcode == 0
        assert all(reply.startswith("✅") for reply in replies)
        assert remaining(budget) == 700
        assert alias.is_symlink(), "atomic writes must target the canonical file, not replace the alias"
        assert not Path(str(alias) + ".lock").exists()
        assert Path(str(budget) + ".lock").exists()
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
            child.join(timeout=5)
        results.close()
        results.join_thread()


def test_atomic_budget_replacement_does_not_replace_lock_inode(budget):
    assert spend(100).startswith("✅")
    lock = Path(str(budget) + ".lock")
    inode = lock.stat().st_ino
    assert spend(200).startswith("✅")
    assert lock.stat().st_ino == inode
