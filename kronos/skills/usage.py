"""Which skills actually get used (moat 12.5).

Twenty installed skills and no idea which three carry the work is the state this
answers. The counter is local, per agent, and increments where a skill is really
loaded — nothing is inferred from a catalog listing, because being *offered* to the
model is not being used.

Deliberately narrow: `calls` and `last_used_at`, nothing about outcomes. Nothing in
the runtime links a turn's result back to the skills that turn loaded, so an
`ok_rate` column would be a number with no derivation behind it. The signal that
does exist for "does this skill work" is its scenario verdict from 12.3, and
`kaos skills stats` shows that next to the usage instead of inventing a rate.

Sharing is off by default and stays off unless `registry.telemetry: share` is
written in the policy. The exported aggregate carries no skill content, no ids and
no counts — only bucketed call volume — and a test asserts that nothing is
assembled at all in any other mode.
"""

import logging
import time

from kronos.db import get_db

log = logging.getLogger("kronos.skills.usage")

CALL_BUCKETS = ((0, "unused"), (1, "1-9"), (10, "10-99"), (100, "100+"))


def _init_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS skill_usage (
            skill        TEXT PRIMARY KEY,
            calls        INTEGER NOT NULL DEFAULT 0,
            last_used_at REAL
        );
        """
    )


def _db():
    db = get_db("skill_usage")
    db.init_schema(_init_schema)
    return db


def record_call(skill: str) -> None:
    """Count one real load. Never raises — telemetry must not break a turn."""
    if not skill:
        return
    try:
        _db().write(
            """
            INSERT INTO skill_usage (skill, calls, last_used_at) VALUES (?, 1, ?)
            ON CONFLICT(skill) DO UPDATE SET calls = calls + 1, last_used_at = excluded.last_used_at
            """,
            (skill, time.time()),
        )
    except Exception as e:  # pragma: no cover - defensive
        log.debug("Could not record usage for skill '%s': %s", skill, e)


def usage() -> dict[str, dict]:
    """Per-skill counters, keyed by skill name."""
    try:
        rows = _db().read("SELECT skill, calls, last_used_at FROM skill_usage")
    except Exception as e:  # pragma: no cover - defensive
        log.debug("Could not read skill usage: %s", e)
        return {}
    return {row["skill"]: {"calls": int(row["calls"]), "last_used_at": row["last_used_at"]} for row in rows}


def call_bucket(calls: int) -> str:
    """Coarse volume label — the only usage figure that may ever be shared."""
    label = CALL_BUCKETS[0][1]
    for threshold, name in CALL_BUCKETS:
        if calls >= threshold:
            label = name
    return label


def local_report(store=None) -> list[dict]:
    """Usage joined with what is known about each skill's provenance."""
    from kronos.skills.store import SkillStore

    store = store or SkillStore()
    counters = usage()
    rows = []
    for skill in store.list_skills():
        counter = counters.get(skill.name, {})
        rows.append(
            {
                "skill": skill.name,
                "version": skill.version,
                "status": skill.status,
                "calls": int(counter.get("calls", 0)),
                "last_used_at": counter.get("last_used_at"),
                "verified": bool(skill.checksum),
                "signed": bool(skill.signature),
                "eval_status": skill.eval_status or "none",
            }
        )
    return sorted(rows, key=lambda row: (-row["calls"], row["skill"]))


def telemetry_mode() -> str:
    try:
        from kronos.policy import get_policy

        return get_policy().registry.telemetry
    except Exception as e:  # pragma: no cover - policy is optional
        log.debug("Could not read registry.telemetry: %s", e)
        return "off"


def shareable_aggregate(store=None) -> list[dict]:
    """The anonymous payload — only when the policy says `share`.

    No skill content, no timestamps, no exact counts, no agent or user identity:
    a skill name, its version, a volume bucket and its own scenario verdict. That
    is enough for "is this skill used and does it pass its check" and not enough to
    describe anyone's workflow.
    """
    if telemetry_mode() != "share":
        return []
    return [
        {
            "skill": row["skill"],
            "version": row["version"],
            "calls_bucket": call_bucket(row["calls"]),
            "eval_status": row["eval_status"],
            "verified": row["verified"],
        }
        for row in local_report(store)
    ]
