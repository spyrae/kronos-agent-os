"""Web, blog, social, and press monitoring fetchers (Phase 2).

All fetchers return list[Change]. Uses Brave Search REST API
(kronos.tools.brave) — same pattern as news_monitor cron.
"""

import hashlib
import logging
import time

from langchain_core.messages import HumanMessage

from kronos.competitors.models import Change, ChangeType, CompetitorConfig, Severity
from kronos.competitors.store import CompetitorStore
from kronos.llm import ModelTier, get_model
from kronos.tools.brave import search as brave_search

log = logging.getLogger("kronos.competitors.web_fetchers")

# Relevant press/tech domains for filtering
RELEVANT_PRESS_DOMAINS = {
    "techcrunch.com", "skift.com", "phocuswire.com", "producthunt.com",
    "theverge.com", "thenextweb.com", "venturebeat.com", "wired.com",
    "engadget.com", "mashable.com", "travelweekly.com", "travelpulse.com",
    "arstechnica.com", "fastcompany.com", "bloomberg.com", "reuters.com",
}


# ---------------------------------------------------------------------------
# Website change detection
# ---------------------------------------------------------------------------

async def check_website(
    comp: CompetitorConfig,
    store: CompetitorStore,
) -> list[Change]:
    """Check landing + pricing pages for content changes."""
    if not comp.website:
        return []

    changes: list[Change] = []
    urls = _build_website_urls(comp.website)

    for url in urls:
        try:
            change = await _check_single_page(comp, store, url)
            if change:
                changes.append(change)
        except Exception as e:
            log.warning("Website check failed for %s %s: %s", comp.name, url, e)
        time.sleep(0.5)  # rate limit

    return changes


def _build_website_urls(base: str) -> list[str]:
    """Build list of URLs to monitor for a competitor."""
    base = base.rstrip("/")
    urls = [base]
    for path in ["/pricing", "/features"]:
        urls.append(base + path)
    return urls


async def _check_single_page(
    comp: CompetitorConfig,
    store: CompetitorStore,
    url: str,
) -> Change | None:
    """Scrape a page via Brave, compare hash, run LLM diff if changed."""
    # Use Brave Search to get a snippet of the page
    results = brave_search(f"site:{url}", count=1, freshness="pm")
    if not results:
        return None

    content = f"{results[0].title}\n{results[0].description}"
    content_hash = hashlib.md5(content.encode()).hexdigest()

    channel = f"website_{url}"
    prev = store.get_latest_snapshot(comp.id, channel)

    # Save current snapshot
    store.save_snapshot(comp.id, channel, {
        "content": content,
        "content_hash": content_hash,
        "url": url,
    })

    if prev is None:
        return None  # First check, baseline

    if prev.get("content_hash") == content_hash:
        return None  # No change

    # LLM diff — determine if the change is meaningful
    diff_summary = await _llm_diff(prev.get("content", ""), content, url)
    if not diff_summary:
        return None

    severity = Severity.IMPORTANT if "/pricing" in url else Severity.INFO
    return Change(
        competitor_id=comp.id,
        competitor_name=comp.name,
        channel=channel,
        change_type=ChangeType.WEBSITE_CHANGE,
        severity=severity,
        summary=f"{comp.name} changed {_page_label(url)}: {diff_summary}",
        details={"url": url, "diff": diff_summary},
    )


def _page_label(url: str) -> str:
    if "/pricing" in url:
        return "pricing page"
    if "/features" in url:
        return "features page"
    return "landing page"


async def _llm_diff(old_content: str, new_content: str, url: str) -> str | None:
    """Use LLM to determine if a web page change is meaningful."""
    prompt = (
        "Compare two versions of a web page. "
        "If changes are cosmetic (CSS, layout, minor rewording) — respond with exactly 'NO_CHANGE'. "
        "If text, messaging, pricing, or features changed — describe in 1 sentence.\n\n"
        f"URL: {url}\n\n"
        f"OLD:\n{old_content[:1500]}\n\n"
        f"NEW:\n{new_content[:1500]}"
    )

    model = get_model(ModelTier.LITE)
    response = model.invoke([HumanMessage(content=prompt)])
    result = response.content.strip() if isinstance(response.content, str) else str(response.content).strip()

    if "NO_CHANGE" in result.upper():
        return None
    return result[:200]


# ---------------------------------------------------------------------------
# Blog / Changelog monitoring
# ---------------------------------------------------------------------------

def check_blog_rss(
    comp: CompetitorConfig,
    store: CompetitorStore,
) -> list[Change]:
    """Check RSS feed for new blog posts."""
    if not comp.blog_rss:
        return []

    try:
        import feedparser
    except ImportError:
        log.warning("feedparser not installed, skipping RSS check")
        return []

    try:
        feed = feedparser.parse(comp.blog_rss)
    except Exception as e:
        log.warning("RSS parse failed for %s: %s", comp.name, e)
        return []

    if not feed.entries:
        return []

    # Get last known entry
    prev = store.get_latest_snapshot(comp.id, "blog_rss")
    known_urls: set[str] = set()
    if prev:
        known_urls = set(prev.get("known_urls", []))

    changes: list[Change] = []
    new_urls: list[str] = []

    for entry in feed.entries[:10]:  # cap at 10 entries
        entry_url = entry.get("link", "")
        if not entry_url or entry_url in known_urls:
            continue

        new_urls.append(entry_url)
        title = entry.get("title", "Untitled")
        summary = entry.get("summary", "")[:300]

        changes.append(Change(
            competitor_id=comp.id,
            competitor_name=comp.name,
            channel="blog_rss",
            change_type=ChangeType.BLOG_POST,
            severity=Severity.IMPORTANT,
            summary=f'{comp.name} published: "{title}"',
            details={"url": entry_url, "summary": summary},
        ))

    # Update known URLs (keep last 100)
    all_urls = list(known_urls | set(new_urls))[-100:]
    store.save_snapshot(comp.id, "blog_rss", {"known_urls": all_urls})

    return changes


def check_blog_search(
    comp: CompetitorConfig,
    store: CompetitorStore,
) -> list[Change]:
    """Fallback blog check via Brave Search when no RSS available."""
    if comp.blog_rss:  # RSS takes priority
        return []
    if not comp.website:
        return []

    results = brave_search(f"site:{comp.website}/blog", count=5, freshness="pw")
    if not results:
        return []

    prev = store.get_latest_snapshot(comp.id, "blog_search")
    known_urls: set[str] = set()
    if prev:
        known_urls = set(prev.get("known_urls", []))

    changes: list[Change] = []
    new_urls: list[str] = []

    for r in results:
        if r.url in known_urls:
            continue
        new_urls.append(r.url)
        changes.append(Change(
            competitor_id=comp.id,
            competitor_name=comp.name,
            channel="blog_search",
            change_type=ChangeType.BLOG_POST,
            severity=Severity.IMPORTANT,
            summary=f'{comp.name} published: "{r.title}"',
            details={"url": r.url, "summary": r.description[:300]},
        ))

    all_urls = list(known_urls | set(new_urls))[-100:]
    store.save_snapshot(comp.id, "blog_search", {"known_urls": all_urls})

    return changes


# ---------------------------------------------------------------------------
# Social media monitoring (Twitter)
# ---------------------------------------------------------------------------

def check_twitter(
    comp: CompetitorConfig,
    store: CompetitorStore,
) -> list[Change]:
    """Check for recent tweets from competitor's account."""
    if not comp.twitter:
        return []

    # Search specifically on twitter.com/x.com for this account
    results = brave_search(
        f"site:twitter.com OR site:x.com @{comp.twitter}",
        count=5,
        freshness="pw",
    )
    if not results:
        return []

    prev = store.get_latest_snapshot(comp.id, "twitter")
    known_urls: set[str] = set()
    if prev:
        known_urls = set(prev.get("known_urls", []))

    changes: list[Change] = []
    new_urls: list[str] = []

    for r in results:
        if r.url in known_urls:
            continue
        # Only accept actual Twitter/X URLs
        if not _is_twitter_url(r.url):
            continue
        # Filter out replies, likes, retweets — only original posts
        if "/status/" not in r.url:
            continue
        # Relevance check: title/description should mention the competitor or travel
        if not _is_relevant_tweet(r, comp):
            continue

        new_urls.append(r.url)
        changes.append(Change(
            competitor_id=comp.id,
            competitor_name=comp.name,
            channel="twitter",
            change_type=ChangeType.SOCIAL_POST,
            severity=Severity.INFO,
            summary=f"{comp.name} (@{comp.twitter}): {r.title[:120]}",
            details={"url": r.url},
        ))

    all_urls = list(known_urls | set(new_urls))[-50:]
    store.save_snapshot(comp.id, "twitter", {"known_urls": all_urls})

    return changes


def _is_twitter_url(url: str) -> bool:
    """Check if URL is from Twitter/X."""
    return any(domain in url for domain in ["twitter.com/", "x.com/"])


def _is_relevant_tweet(result, comp: CompetitorConfig) -> bool:
    """Filter out irrelevant tweets (wrong account, unrelated content)."""
    text = (result.title + " " + result.description).lower()
    handle = comp.twitter.lower()

    # Must mention the account handle or company name
    if handle not in text and comp.name.lower() not in text:
        return False

    # Exclude common noise patterns
    noise_patterns = [
        "limbus", "limbuscompany",  # r/limbuscompany noise for "lambus"
        "tv series", "tv show", "season", "episode", "trailer",
        "from season", "from series",
    ]
    if any(p in text for p in noise_patterns):
        return False

    return True


# ---------------------------------------------------------------------------
# Press / news mentions
# ---------------------------------------------------------------------------

def check_press(
    comp: CompetitorConfig,
    store: CompetitorStore,
) -> list[Change]:
    """Check for press mentions in tech/travel media."""
    query = f'"{comp.name}" travel app'
    results = brave_search(query, count=10, freshness="pw")
    if not results:
        return []

    prev = store.get_latest_snapshot(comp.id, "press")
    known_urls: set[str] = set()
    if prev:
        known_urls = set(prev.get("known_urls", []))

    changes: list[Change] = []
    new_urls: list[str] = []

    for r in results:
        if r.url in known_urls:
            continue
        # Filter: only relevant press domains
        if not any(domain in r.url for domain in RELEVANT_PRESS_DOMAINS):
            continue

        new_urls.append(r.url)
        changes.append(Change(
            competitor_id=comp.id,
            competitor_name=comp.name,
            channel="press",
            change_type=ChangeType.PRESS_MENTION,
            severity=Severity.IMPORTANT,
            summary=f"{comp.name} in press: {r.title[:120]}",
            details={"url": r.url, "source": r.description[:200]},
        ))

    all_urls = list(known_urls | set(new_urls))[-50:]
    store.save_snapshot(comp.id, "press", {"known_urls": all_urls})

    return changes


# ---------------------------------------------------------------------------
# ProductHunt launches
# ---------------------------------------------------------------------------

def check_producthunt(
    comp: CompetitorConfig,
    store: CompetitorStore,
) -> list[Change]:
    """Check for ProductHunt launches."""
    results = brave_search(
        f'site:producthunt.com "{comp.name}"',
        count=3,
        freshness="pw",
    )
    if not results:
        return []

    prev = store.get_latest_snapshot(comp.id, "producthunt")
    known_urls: set[str] = set()
    if prev:
        known_urls = set(prev.get("known_urls", []))

    changes: list[Change] = []

    for r in results:
        if r.url in known_urls:
            continue
        if "producthunt.com/posts/" not in r.url:
            continue

        changes.append(Change(
            competitor_id=comp.id,
            competitor_name=comp.name,
            channel="producthunt",
            change_type=ChangeType.PRODUCTHUNT_LAUNCH,
            severity=Severity.CRITICAL,
            summary=f"{comp.name} launched on ProductHunt: {r.title[:120]}",
            details={"url": r.url},
        ))

    all_urls = list(known_urls | {r.url for r in results})[-20:]
    store.save_snapshot(comp.id, "producthunt", {"known_urls": all_urls})

    return changes


# ---------------------------------------------------------------------------
# Job postings (signal: what they're building)
# ---------------------------------------------------------------------------

def check_jobs(
    comp: CompetitorConfig,
    store: CompetitorStore,
) -> list[Change]:
    """Check for job postings (hiring = strategic signal)."""
    results = brave_search(
        f'"{comp.name}" (hiring OR "open positions" OR careers OR jobs)',
        count=5,
        freshness="pw",
    )
    if not results:
        return []

    prev = store.get_latest_snapshot(comp.id, "jobs")
    known_urls: set[str] = set()
    if prev:
        known_urls = set(prev.get("known_urls", []))

    changes: list[Change] = []

    for r in results:
        if r.url in known_urls:
            continue
        # Filter out generic job board noise
        if any(skip in r.url for skip in ["indeed.com/cmp", "glassdoor.com/Reviews"]):
            continue

        changes.append(Change(
            competitor_id=comp.id,
            competitor_name=comp.name,
            channel="jobs",
            change_type=ChangeType.JOB_POSTING,
            severity=Severity.INFO,
            summary=f"{comp.name} hiring: {r.title[:120]}",
            details={"url": r.url, "description": r.description[:200]},
        ))

    all_urls = list(known_urls | {r.url for r in results})[-30:]
    store.save_snapshot(comp.id, "jobs", {"known_urls": all_urls})

    return changes


# ---------------------------------------------------------------------------
# Aggregate all web channels
# ---------------------------------------------------------------------------

async def check_all_web_channels(
    comp: CompetitorConfig,
    store: CompetitorStore,
) -> list[Change]:
    """Run all Phase 2 web/social checks for a competitor.

    Returns combined list of changes across all channels.
    """
    all_changes: list[Change] = []

    # Website (async — uses LLM diff)
    changes = await check_website(comp, store)
    all_changes.extend(changes)

    # Blog — RSS first (no API call), Brave Search fallback
    all_changes.extend(check_blog_rss(comp, store))
    all_changes.extend(check_blog_search(comp, store))

    # Social — Brave Search calls below, throttled in brave.py
    all_changes.extend(check_twitter(comp, store))
    all_changes.extend(check_press(comp, store))
    all_changes.extend(check_producthunt(comp, store))
    all_changes.extend(check_jobs(comp, store))

    return all_changes
