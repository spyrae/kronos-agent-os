"""Hiring signal detection for Digest: Jobs."""

from __future__ import annotations

from urllib.parse import urlparse

from kronos.signals.models import SignalItem

STRONG_JOB_DOMAINS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workable.com",
    "wellfound.com",
    "linkedin.com",
    "jobs.ashbyhq.com",
    "ycombinator.com",
)
ROLE_KEYWORDS = (
    "hiring",
    "we're hiring",
    "we are hiring",
    "ищем",
    "вакансия",
    "job",
    "role",
    "engineer",
    "developer",
    "founding",
    "remote",
    "ml engineer",
    "ai engineer",
    "agent engineer",
)
NOISE_KEYWORDS = (
    "top companies hiring",
    "best companies hiring",
    "companies are hiring",
    "listicle",
    "seo",
    " подборка вакансий без ссылок",
)


def job_signal_score(item: SignalItem) -> float:
    """Return deterministic confidence that an item is an actionable job signal."""
    text = f"{item.title} {item.text} {item.normalized_text}".lower()
    url = (item.url or item.source_url).lower()
    score = 0.0

    if any(noise in text for noise in NOISE_KEYWORDS):
        score -= 50
    if _domain_matches(url, STRONG_JOB_DOMAINS):
        score += 60
    if any(keyword in text for keyword in ROLE_KEYWORDS):
        score += 35
    if any(term in text for term in ("salary", "compensation", "senior", "lead", "founding")):
        score += 10
    if any(term in text for term in ("http://", "https://", "careers", "apply")):
        score += 10

    return max(0.0, min(100.0, score))


def is_job_signal(item: SignalItem, *, min_score: float = 25.0) -> bool:
    """Return True when the item should be allowed into Digest: Jobs."""
    return job_signal_score(item) >= min_score


def _domain_matches(url: str, domains: tuple[str, ...]) -> bool:
    if not url:
        return False
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)
