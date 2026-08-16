"""What a subsystem can do right now, and who needs to hear that it changed.

Two subsystems here fail the same way: they degrade quietly, keep every sign of
being configured, and surface weeks later as a task that returned nothing. The
fetch tiers lose a backend to a host rebuild; the sandbox passes its readiness
check while every run inside it fails. So both are probed the same way and
reported by the same rules, because the rules are the hard part and a second
copy of them is a second thing to get wrong.

**Three statuses, and the third is the one that earns its keep.** `off` means a
capability this deployment never installed — a documented choice, not a fault.
Collapsing it into `broken` would alert about a browser or a container runtime
somebody deliberately never wanted, and an alert that is usually wrong gets
muted, taking the real ones with it.

**Silence is the normal outcome.** A daily "everything is fine" is a message
that teaches its reader to skip it, and then the one morning it says something
else gets skipped too — which is worse than having no check at all, because it
comes with the feeling of being covered.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("kronos.health")

STATUS_OK = "ok"
STATUS_BROKEN = "broken"
STATUS_OFF = "off"


@dataclass(frozen=True)
class HealthCheck:
    """One thing that either works, doesn't, or isn't installed here."""

    name: str
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


def load_previous(state_path: Path) -> dict[str, str]:
    """Last run's statuses, or nothing when there is no usable record."""
    if not state_path.exists():
        return {}
    try:
        loaded = json.loads(state_path.read_text())
    except Exception as e:
        # A corrupt state file must not stop the probe: the check is the point,
        # remembering it is the optimisation. Worst case is one extra message.
        log.warning("Could not read %s (%s); treating this as a first run", state_path.name, e)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save(state_path: Path, current: dict[str, str]) -> None:
    """Record this run's statuses, atomically."""
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(current, indent=2))
        tmp.replace(state_path)
    except Exception as e:
        log.warning("Could not persist %s: %s", state_path.name, e)


def describe(name: str, before: str | None, after: str, detail: str) -> str:
    if before is None:
        return f"{name}: {after} — {detail}"
    return f"{name}: {before} → {after} — {detail}"


def report_changes(
    *,
    subject: str,
    checks: list[HealthCheck],
    state_path: Path,
    title: str,
    consequence: str = "",
) -> bool:
    """Compare with last time, speak only about what moved. Returns whether it spoke.

    A check missing from `checks` is neither compared nor remembered. That is how
    a probe reports "I could not determine this" without inventing a status for
    it: when the sandbox cannot run code at all, its containment guarantees are
    simply not in the list, and they come back — reported if broken — as soon as
    it can run again.
    """
    current = {check.name: check.status for check in checks}
    previous = load_previous(state_path)

    log.info("%s: %s", subject, ", ".join(f"{c.name}={c.status}" for c in checks) or "nothing probed")

    notable: list[HealthCheck] = []
    lines: list[str] = []
    for check in checks:
        before = previous.get(check.name)
        if before == check.status:
            continue
        # A first run has nothing to compare against, so only a fault is worth
        # saying. Announcing "off" for a backend this host never wanted would be
        # the checker's first message and its least useful one.
        if before is None and check.status != STATUS_BROKEN:
            continue
        notable.append(check)
        lines.append(describe(check.name, before, check.status, check.detail))

    save(state_path, current)

    if not notable:
        return False

    # Bad news is anything being reported that is not working *now* — a capability
    # lost since yesterday, or one found broken on a first run, where there is no
    # yesterday to have lost it from. Good news is a report made only of
    # recoveries, and it should neither wake anyone nor carry a warning about a
    # problem that has just ended.
    bad = [c for c in notable if c.status != STATUS_OK]
    healthy = [c.name for c in checks if c.status == STATUS_OK]

    body = (
        f"{subject} changed:\n"
        + "\n".join(f"• {line}" for line in lines)
        + f"\n\nStill working: {', '.join(healthy) or 'none'}"
    )
    if bad and consequence:
        body += f"\n\n{consequence}"

    from kronos.cron.notify import send_ntfy, send_webhook

    send_webhook(body)
    send_ntfy(
        body,
        title=title,
        priority="high" if bad else "default",
        tags="warning" if bad else "white_check_mark",
    )
    return True
