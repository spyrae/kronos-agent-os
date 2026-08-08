"""Plans: work that outlives a turn.

The durable machinery elsewhere in this codebase protects a **turn** — minutes,
at most twenty-five model steps, resumable after a crash. Some work does not fit
in that shape at all: *ask three landlords, wait for their answers, compare what
comes back*. That is days, mostly spent waiting, and the waiting is the point.

A plan is a goal plus steps. A step holds a prompt, what it depends on, and
optionally a condition it waits for. The poller (`kronos.cron.plans`) picks up
steps whose dependencies are settled and whose condition has fired, and runs each
one as an ordinary agent turn — so durable turns, the effects ledger, approvals
and the cost guardian all apply unchanged. Nothing here re-implements them.

Two decisions worth knowing:

* **A dependency is satisfied when it is finished, not when it succeeded.** Three
  landlords were asked; one never replies and that step fails. The comparison
  step still runs, with two answers and one stated absence. Cascading skips would
  turn one silent landlord into no answer at all.
* **A plan has an expiry.** A condition that never fires would otherwise leave a
  step in the poller forever, costing a little on every cycle and never
  admitting it failed. Expiry is what makes "still waiting" eventually become
  "this did not work".
"""

import json
import logging
import time

from kronos.db import get_db

log = logging.getLogger("kronos.plans")

PLAN_ACTIVE = "active"
PLAN_DONE = "done"
PLAN_FAILED = "failed"
PLAN_CANCELLED = "cancelled"
PLAN_TERMINAL = (PLAN_DONE, PLAN_FAILED, PLAN_CANCELLED)

STEP_PENDING = "pending"
STEP_WAITING = "waiting"
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_FAILED = "failed"
# Terminal for dependency purposes: a dependent step may proceed once every step
# it names has stopped moving, whatever the outcome.
STEP_TERMINAL = (STEP_DONE, STEP_FAILED)

# Bounds. Every one of these exists because its absence is a way for a plan to
# quietly cost money forever.
MAX_STEPS_PER_PLAN = 50
MAX_STEP_ATTEMPTS = 3
MAX_CONDITION_CHECKS = 5000
DEFAULT_TTL_SECONDS = 90 * 86400


class PlanError(Exception):
    """Raised when a plan or step is missing, or a bound was reached."""


def _init_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS plans (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name  TEXT    NOT NULL,
            goal        TEXT    NOT NULL,
            state       TEXT    NOT NULL DEFAULT 'active',
            chat_id     INTEGER NOT NULL DEFAULT 0,
            topic_id    INTEGER,
            thread_id   TEXT    NOT NULL DEFAULT '',
            summary     TEXT    NOT NULL DEFAULT '',
            created_at  REAL    NOT NULL,
            updated_at  REAL    NOT NULL,
            expires_at  REAL    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plan_steps (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id     INTEGER NOT NULL,
            seq         INTEGER NOT NULL,
            title       TEXT    NOT NULL DEFAULT '',
            prompt      TEXT    NOT NULL,
            state       TEXT    NOT NULL DEFAULT 'pending',
            -- Comma-separated step ids. Several, because "wait for all three
            -- landlords" is the shape this exists for.
            depends_on  TEXT    NOT NULL DEFAULT '',
            wait_json   TEXT    NOT NULL DEFAULT '',
            wake_at     REAL    NOT NULL DEFAULT 0,
            parked_at   REAL    NOT NULL DEFAULT 0,
            checks      INTEGER NOT NULL DEFAULT 0,
            attempts    INTEGER NOT NULL DEFAULT 0,
            notify      INTEGER NOT NULL DEFAULT 0,
            result      TEXT    NOT NULL DEFAULT '',
            created_at  REAL    NOT NULL,
            updated_at  REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_plan_steps_plan ON plan_steps(plan_id, seq);
        CREATE INDEX IF NOT EXISTS idx_plan_steps_open ON plan_steps(state, wake_at);
        CREATE INDEX IF NOT EXISTS idx_plans_open ON plans(agent_name, state);
        """
    )


def _db():
    db = get_db("plans")
    db.init_schema(_init_schema)
    return db


def _now(now: float | None = None) -> float:
    return time.time() if now is None else now


def _row(row) -> dict:
    return dict(row) if row is not None else {}


def dependency_ids(step: dict) -> list[int]:
    return [int(part) for part in str(step.get("depends_on") or "").split(",") if part.strip()]


def wait_spec(step: dict) -> dict:
    """The step's parked condition, or {} when it simply waits its turn."""
    raw = step.get("wait_json") or ""
    if not raw:
        return {}
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Step %s has an unreadable condition: %r", step.get("id"), raw)
        return {}
    return spec if isinstance(spec, dict) else {}


# --- creating -----------------------------------------------------------------


def create_plan(
    *,
    agent_name: str,
    goal: str,
    chat_id: int = 0,
    topic_id: int | None = None,
    thread_id: str = "",
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> int:
    goal = goal.strip()
    if not goal:
        raise PlanError("a plan needs a goal")
    stamp = _now(now)
    cursor = _db().write(
        """
        INSERT INTO plans (agent_name, goal, state, chat_id, topic_id, thread_id,
                           created_at, updated_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (agent_name, goal, PLAN_ACTIVE, chat_id, topic_id, thread_id, stamp, stamp, stamp + ttl_seconds),
    )
    plan_id = int(cursor.lastrowid)
    log.info("Plan #%s created: %s", plan_id, goal[:80])
    return plan_id


def add_step(
    plan_id: int,
    prompt: str,
    *,
    title: str = "",
    depends_on: list[int] | None = None,
    wait: dict | None = None,
    notify: bool = False,
    now: float | None = None,
) -> int:
    """Append a step. ``wait`` parks it until a condition fires.

    The condition is stored opaquely — this module does not know what
    ``page_number`` means, only when to hand the step to whoever does.
    """
    plan = get_plan(plan_id)
    if not plan:
        raise PlanError(f"no plan #{plan_id}")
    if plan["state"] != PLAN_ACTIVE:
        raise PlanError(f"plan #{plan_id} is {plan['state']}, not active")

    existing = steps_of(plan_id)
    if len(existing) >= MAX_STEPS_PER_PLAN:
        raise PlanError(f"plan #{plan_id} already has {MAX_STEPS_PER_PLAN} steps")

    known = {step["id"] for step in existing}
    deps = [int(d) for d in (depends_on or [])]
    unknown = [d for d in deps if d not in known]
    if unknown:
        raise PlanError(f"step depends on {unknown}, which is not part of plan #{plan_id}")

    stamp = _now(now)
    state = STEP_WAITING if wait else STEP_PENDING
    cursor = _db().write(
        """
        INSERT INTO plan_steps (plan_id, seq, title, prompt, state, depends_on, wait_json,
                                wake_at, parked_at, notify, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan_id,
            len(existing) + 1,
            title.strip(),
            prompt.strip(),
            state,
            ",".join(str(d) for d in deps),
            json.dumps(wait, ensure_ascii=False) if wait else "",
            0.0,
            stamp if wait else 0.0,
            1 if notify else 0,
            stamp,
            stamp,
        ),
    )
    _touch_plan(plan_id, stamp)
    return int(cursor.lastrowid)


# --- reading ------------------------------------------------------------------


def get_plan(plan_id: int) -> dict:
    return _row(_db().read_one("SELECT * FROM plans WHERE id = ?", (plan_id,)))


def get_step(step_id: int) -> dict:
    return _row(_db().read_one("SELECT * FROM plan_steps WHERE id = ?", (step_id,)))


def steps_of(plan_id: int) -> list[dict]:
    return [dict(row) for row in _db().read("SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY seq", (plan_id,))]


def list_plans(agent_name: str, *, state: str = "", limit: int = 50) -> list[dict]:
    """Plans newest first. Empty ``state`` means the open ones."""
    if state:
        rows = _db().read(
            "SELECT * FROM plans WHERE agent_name = ? AND state = ? ORDER BY id DESC LIMIT ?",
            (agent_name, state, limit),
        )
    else:
        rows = _db().read(
            "SELECT * FROM plans WHERE agent_name = ? AND state = ? ORDER BY id DESC LIMIT ?",
            (agent_name, PLAN_ACTIVE, limit),
        )
    return [dict(row) for row in rows]


def dependency_results(step: dict) -> list[dict]:
    """What the steps this one waited for actually produced."""
    ids = dependency_ids(step)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = _db().read(f"SELECT * FROM plan_steps WHERE id IN ({placeholders}) ORDER BY seq", tuple(ids))
    return [dict(row) for row in rows]


def open_steps(agent_name: str) -> list[dict]:
    """Every step of an active plan that has not finished, soonest wake first."""
    rows = _db().read(
        """
        SELECT s.* FROM plan_steps s
        JOIN plans p ON p.id = s.plan_id
        WHERE p.agent_name = ? AND p.state = ? AND s.state IN (?, ?)
        ORDER BY s.wake_at, s.seq
        """,
        (agent_name, PLAN_ACTIVE, STEP_PENDING, STEP_WAITING),
    )
    return [dict(row) for row in rows]


def ready_steps(agent_name: str, now: float | None = None) -> list[dict]:
    """Steps whose dependencies are settled and whose wake time has come.

    A parked step is returned so its condition can be evaluated — whether it may
    actually run is that condition's answer, not this function's.
    """
    stamp = _now(now)
    ready = []
    for step in open_steps(agent_name):
        if step["wake_at"] and step["wake_at"] > stamp:
            continue
        if not _dependencies_settled(step):
            continue
        ready.append(step)
    return ready


def _dependencies_settled(step: dict) -> bool:
    return all(dep.get("state") in STEP_TERMINAL for dep in dependency_results(step))


# --- moving a step along ------------------------------------------------------


def mark_running(step_id: int, now: float | None = None) -> None:
    stamp = _now(now)
    _db().write(
        "UPDATE plan_steps SET state = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
        (STEP_RUNNING, stamp, step_id),
    )


def finish_step(step_id: int, result: str, now: float | None = None) -> None:
    stamp = _now(now)
    _db().write(
        "UPDATE plan_steps SET state = ?, result = ?, updated_at = ? WHERE id = ?",
        (STEP_DONE, result, stamp, step_id),
    )
    step = get_step(step_id)
    if step:
        _touch_plan(step["plan_id"], stamp)


def fail_step(step_id: int, error: str, now: float | None = None) -> bool:
    """Record a failed attempt. Returns whether the step is now given up on.

    Below the attempt limit the step goes back to pending and the next cycle
    retries it — a transient network failure should not end a week-long plan.
    """
    stamp = _now(now)
    step = get_step(step_id)
    if not step:
        raise PlanError(f"no step #{step_id}")

    if step["attempts"] >= MAX_STEP_ATTEMPTS:
        _db().write(
            "UPDATE plan_steps SET state = ?, result = ?, updated_at = ? WHERE id = ?",
            (STEP_FAILED, f"failed after {step['attempts']} attempts: {error}", stamp, step_id),
        )
        _touch_plan(step["plan_id"], stamp)
        return True

    _db().write(
        "UPDATE plan_steps SET state = ?, result = ?, wake_at = ?, updated_at = ? WHERE id = ?",
        (STEP_PENDING, f"attempt {step['attempts']} failed: {error}", stamp + 60, stamp, step_id),
    )
    _touch_plan(step["plan_id"], stamp)
    return False


def park_step(step_id: int, wait: dict, *, wake_at: float = 0.0, now: float | None = None) -> None:
    """Put a step back to waiting on a condition."""
    stamp = _now(now)
    _db().write(
        """
        UPDATE plan_steps
        SET state = ?, wait_json = ?, wake_at = ?, parked_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (STEP_WAITING, json.dumps(wait, ensure_ascii=False), wake_at, stamp, stamp, step_id),
    )


def note_check(step_id: int, next_check_at: float, now: float | None = None) -> bool:
    """A condition was evaluated and had not fired. Returns whether to give up.

    Counting checks bounds a condition that can never fire — a page that stopped
    existing, a threshold nothing will reach.
    """
    stamp = _now(now)
    step = get_step(step_id)
    if not step:
        raise PlanError(f"no step #{step_id}")
    checks = step["checks"] + 1
    if checks >= MAX_CONDITION_CHECKS:
        _db().write(
            "UPDATE plan_steps SET state = ?, checks = ?, result = ?, updated_at = ? WHERE id = ?",
            (STEP_FAILED, checks, f"condition never fired in {checks} checks", stamp, step_id),
        )
        _touch_plan(step["plan_id"], stamp)
        return True
    _db().write(
        "UPDATE plan_steps SET checks = ?, wake_at = ?, state = ?, updated_at = ? WHERE id = ?",
        (checks, next_check_at, STEP_WAITING, stamp, step_id),
    )
    return False


def release_step(step_id: int, now: float | None = None) -> None:
    """The condition fired: the step is now merely pending."""
    stamp = _now(now)
    _db().write(
        "UPDATE plan_steps SET state = ?, wait_json = '', wake_at = 0, updated_at = ? WHERE id = ?",
        (STEP_PENDING, stamp, step_id),
    )


# --- moving a plan along ------------------------------------------------------


def _touch_plan(plan_id: int, stamp: float) -> None:
    _db().write("UPDATE plans SET updated_at = ? WHERE id = ?", (stamp, plan_id))


def set_summary(plan_id: int, summary: str, now: float | None = None) -> None:
    _db().write(
        "UPDATE plans SET summary = ?, updated_at = ? WHERE id = ?",
        (summary.strip(), _now(now), plan_id),
    )


def settle_plan(plan_id: int, now: float | None = None) -> str:
    """Close a plan whose steps have all stopped moving. Returns its state.

    A plan with no step that reached ``done`` is a failure; anything else is a
    plan that got somewhere, and its summary says what did not work.
    """
    steps = steps_of(plan_id)
    if not steps or any(step["state"] not in STEP_TERMINAL for step in steps):
        return PLAN_ACTIVE
    state = PLAN_DONE if any(step["state"] == STEP_DONE for step in steps) else PLAN_FAILED
    _db().write("UPDATE plans SET state = ?, updated_at = ? WHERE id = ?", (state, _now(now), plan_id))
    log.info("Plan #%s %s", plan_id, state)
    return state


def cancel_plan(plan_id: int, agent_name: str, now: float | None = None) -> bool:
    cursor = _db().write(
        "UPDATE plans SET state = ?, updated_at = ? WHERE id = ? AND agent_name = ? AND state = ?",
        (PLAN_CANCELLED, _now(now), plan_id, agent_name, PLAN_ACTIVE),
    )
    return cursor.rowcount > 0


def plans_awaiting_summary(agent_name: str, *, limit: int = 5) -> list[dict]:
    """Finished plans that have not told the owner how it went yet.

    The marker is the state plus an empty summary, not a variable in the poller's
    loop — so a crash between finishing and reporting loses nothing, and a plan
    stays owed its summary until it has one. Cancelled plans are not owed one:
    the owner did that on purpose.
    """
    rows = _db().read(
        """
        SELECT * FROM plans
        WHERE agent_name = ? AND state IN (?, ?) AND summary = ''
        ORDER BY updated_at LIMIT ?
        """,
        (agent_name, PLAN_DONE, PLAN_FAILED, limit),
    )
    return [dict(row) for row in rows]


def expired_plans(agent_name: str, now: float | None = None) -> list[dict]:
    rows = _db().read(
        "SELECT * FROM plans WHERE agent_name = ? AND state = ? AND expires_at <= ?",
        (agent_name, PLAN_ACTIVE, _now(now)),
    )
    return [dict(row) for row in rows]


def expire_plan(plan_id: int, now: float | None = None) -> None:
    """Give up on a plan that ran out of time, saying so rather than vanishing."""
    stamp = _now(now)
    _db().write_many(
        [
            (
                "UPDATE plan_steps SET state = ?, result = ?, updated_at = ? WHERE plan_id = ? AND state IN (?, ?)",
                (STEP_FAILED, "plan expired before this step could run", stamp, plan_id, STEP_PENDING, STEP_WAITING),
            ),
            ("UPDATE plans SET state = ?, updated_at = ? WHERE id = ?", (PLAN_FAILED, stamp, plan_id)),
        ]
    )
    log.info("Plan #%s expired", plan_id)
