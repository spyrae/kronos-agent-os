"""Hiring signal detection for Digest: Jobs."""

from __future__ import annotations

import re
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
    "geekjob.ru",
    "remocate.app",
    "vseti.app",
)
ROLE_PHRASES = (
    "hiring",
    "we're hiring",
    "we are hiring",
    "ищем",
    "вакансия",
    "вакансии",
    "требуется",
    "приглашаем",
    "отклик",
    "job description",
    "open role",
    "job opening",
)
ROLE_WORDS = (
    "role",
    "engineer",
    "developer",
    "designer",
    "analyst",
    "consultant",
    "manager",
    "owner",
    "founding",
    "ml engineer",
    "ai engineer",
    "agent engineer",
    "product manager",
    "product owner",
)
NOISE_KEYWORDS = (
    "top companies hiring",
    "best companies hiring",
    "companies are hiring",
    "listicle",
    "seo",
    " подборка вакансий без ссылок",
    "как подготовиться к собеседованию",
    "подготовиться к собеседованию",
    "советы для собеседования",
    "инсайтов",
)
CAREER_URL_TERMS = (
    "/jobs/",
    "/job/",
    "/careers/",
    "/career/",
    "/vacancies/",
    "/vacancy/",
    "/vakansii/",
    "apply",
)


def job_signal_score(item: SignalItem) -> float:
    """Return deterministic confidence that an item is an actionable job signal."""
    text = f"{item.title} {item.text} {item.normalized_text}".lower()
    urls = _extract_urls(f"{item.url} {item.source_url} {item.text} {item.normalized_text}")
    score = 0.0

    if any(noise in text for noise in NOISE_KEYWORDS):
        score -= 50

    if any(_is_strong_job_url(url) for url in urls):
        score += 60
    elif any(_looks_like_career_url(url) for url in urls):
        score += 20

    if _contains_role_signal(text):
        score += 35
    if any(term in text for term in ("salary", "compensation", "senior", "lead", "founding")):
        score += 10
    if any(term in text for term in ("зарплата", "релокация", "удалённо", "удалёнка", "гибрид")):
        score += 10
    if any(_looks_like_career_url(url) for url in urls):
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


def _extract_urls(text: str) -> tuple[str, ...]:
    urls = re.findall(r"https?://[^\s\])>\"']+", text or "", flags=re.IGNORECASE)
    return tuple(url.rstrip(".,;:") for url in urls)


def _is_strong_job_url(url: str) -> bool:
    if not _domain_matches(url, STRONG_JOB_DOMAINS):
        return False
    parsed = urlparse(url.lower())
    if parsed.netloc.lower().removeprefix("www.").endswith("linkedin.com"):
        return "/jobs/" in parsed.path
    return True


def _looks_like_career_url(url: str) -> bool:
    parsed = urlparse(url.lower())
    combined = f"{parsed.netloc}{parsed.path}{parsed.query}"
    return any(term in combined for term in CAREER_URL_TERMS)


def _contains_role_signal(text: str) -> bool:
    if any(phrase in text for phrase in ROLE_PHRASES):
        return True
    for word in ROLE_WORDS:
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text):
            return True
    return False
