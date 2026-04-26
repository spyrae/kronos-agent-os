"""Telegram public channel parser via t.me/s/ web preview.

Zero config — no API keys, no MTProto, no bot tokens.
Parses HTML from public web preview pages.

Inspired by artwist-polyakov/telegram-channel-parser (bash/awk),
rewritten as async Python for LangGraph integration.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import unescape

import aiohttp

log = logging.getLogger("kronos.tools.telegram_channels")

TG_BASE_URL = "https://t.me/s"
REQUEST_DELAY = 1.5  # seconds between requests to t.me
REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TgPost:
    id: int
    date: str  # ISO 8601
    views: str
    reactions: int
    fwd_from: str
    fwd_link: str
    text: str
    media_url: str

    @property
    def views_numeric(self) -> int:
        """Parse views string like '12.5K' or '1.2M' into int."""
        v = self.views.strip()
        if not v:
            return 0
        v_upper = v.upper()
        try:
            if v_upper.endswith("K"):
                return int(float(v_upper[:-1]) * 1_000)
            if v_upper.endswith("M"):
                return int(float(v_upper[:-1]) * 1_000_000)
            return int(re.sub(r"[^0-9]", "", v))
        except (ValueError, TypeError):
            return 0


@dataclass
class TgChannelInfo:
    username: str
    title: str = ""
    description: str = ""
    subscribers: str = ""


@dataclass
class DigestEntry:
    channel: str
    posts: list[TgPost] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Channel normalization
# ---------------------------------------------------------------------------


def normalize_channel(raw: str) -> str:
    """Accept any format: @user, https://t.me/user, t.me/s/user, etc."""
    s = raw.strip()
    s = s.lstrip("@")
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^t\.me/s/", "", s)
    s = re.sub(r"^t\.me/", "", s)
    s = s.split("?")[0].rstrip("/")
    return s


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


async def _fetch_page(
    session: aiohttp.ClientSession,
    channel: str,
    before: int | None = None,
) -> str:
    """Fetch one page of t.me/s/ HTML."""
    url = f"{TG_BASE_URL}/{channel}"
    if before:
        url += f"?before={before}"

    await asyncio.sleep(REQUEST_DELAY)

    async with session.get(
        url,
        headers={
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
            "Accept": "text/html",
            "User-Agent": USER_AGENT,
        },
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
    ) as resp:
        resp.raise_for_status()
        return await resp.text()


# ---------------------------------------------------------------------------
# HTML parsing (regex-based, matching t.me/s/ structure)
# ---------------------------------------------------------------------------

# Each post is a <div class="tgme_widget_message_wrap ..."> with data-post="channel/ID"
_RE_POST_BLOCK = re.compile(
    r'<div[^>]*class="tgme_widget_message_wrap[^"]*"[^>]*>'
    r'.*?'
    r'(?=<div[^>]*class="tgme_widget_message_wrap|$)',
    re.DOTALL,
)
_RE_DATA_POST = re.compile(r'data-post="[^"]*?/(\d+)"')
_RE_DATETIME = re.compile(r'datetime="([^"]+)"')
_RE_VIEWS = re.compile(
    r'class="tgme_widget_message_views"[^>]*>([\d.]+[KkMm]?)\s*<'
)
_RE_TEXT = re.compile(
    r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL,
)
_RE_FWD_NAME = re.compile(
    r'class="tgme_widget_message_forwarded_from_name"[^>]*>'
    r'(?:<a[^>]*href="([^"]*)"[^>]*>)?'
    r'(?:<span[^>]*>)?([^<]+)',
    re.DOTALL,
)
_RE_REACTIONS = re.compile(r'</i>\s*(\d+)')
_RE_MEDIA_BG = re.compile(r"background-image:url\('([^']+)'\)")
_RE_MEDIA_PHOTO = re.compile(
    r'class="tgme_widget_message_photo_wrap[^"]*"[^>]*style="[^"]*'
    r"background-image:url\('([^']+)'\)",
)
_RE_MEDIA_VIDEO = re.compile(
    r'class="tgme_widget_message_video_thumb[^"]*"[^>]*style="[^"]*'
    r"background-image:url\('([^']+)'\)",
)

# Channel info
_RE_CH_TITLE = re.compile(
    r'class="tgme_channel_info_header_title"[^>]*>.*?<span[^>]*>([^<]+)</span>',
    re.DOTALL,
)
_RE_CH_DESC = re.compile(
    r'class="tgme_channel_info_description"[^>]*>(.*?)</div>',
    re.DOTALL,
)
_RE_CH_SUBS = re.compile(
    r'class="tgme_channel_info_counter"[^>]*>.*?'
    r'class="counter_value"[^>]*>([^<]+)<',
    re.DOTALL,
)


def _strip_html(html: str) -> str:
    """Remove HTML tags, decode entities, normalize whitespace."""
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _parse_posts(html: str) -> list[TgPost]:
    """Parse posts from a single HTML page."""
    posts: list[TgPost] = []

    for block_match in _RE_POST_BLOCK.finditer(html):
        block = block_match.group(0)

        m_id = _RE_DATA_POST.search(block)
        if not m_id:
            continue
        post_id = int(m_id.group(1))

        m_date = _RE_DATETIME.search(block)
        date = m_date.group(1) if m_date else ""

        m_views = _RE_VIEWS.search(block)
        views = m_views.group(1) if m_views else ""

        m_text = _RE_TEXT.search(block)
        text = _strip_html(m_text.group(1)) if m_text else ""

        # Reactions — sum all emoji counts
        reactions = sum(int(m.group(1)) for m in _RE_REACTIONS.finditer(block))

        # Forwarded from
        fwd_from = ""
        fwd_link = ""
        m_fwd = _RE_FWD_NAME.search(block)
        if m_fwd:
            fwd_link = m_fwd.group(1) or ""
            fwd_from = m_fwd.group(2).strip()

        # Media URL (first photo or video thumb)
        media_url = ""
        m_photo = _RE_MEDIA_PHOTO.search(block)
        if m_photo:
            media_url = m_photo.group(1)
        else:
            m_video = _RE_MEDIA_VIDEO.search(block)
            if m_video:
                media_url = m_video.group(1)

        posts.append(TgPost(
            id=post_id,
            date=date,
            views=views,
            reactions=reactions,
            fwd_from=fwd_from,
            fwd_link=fwd_link,
            text=text,
            media_url=media_url,
        ))

    return posts


def _parse_channel_info(html: str, username: str) -> TgChannelInfo:
    """Extract channel metadata from HTML."""
    info = TgChannelInfo(username=username)

    m = _RE_CH_TITLE.search(html)
    if m:
        info.title = unescape(m.group(1).strip())

    m = _RE_CH_DESC.search(html)
    if m:
        info.description = _strip_html(m.group(1))

    m = _RE_CH_SUBS.search(html)
    if m:
        info.subscribers = m.group(1).strip()

    return info


# ---------------------------------------------------------------------------
# Public API (async)
# ---------------------------------------------------------------------------


async def fetch_posts(
    channel: str,
    limit: int = 20,
    after_date: str | None = None,
) -> list[TgPost]:
    """Fetch posts from a public Telegram channel.

    Args:
        channel: Channel username (any format accepted).
        limit: Max number of posts to return.
        after_date: Only return posts on or after this date (YYYY-MM-DD).

    Returns:
        List of TgPost, newest first.
    """
    channel = normalize_channel(channel)
    collected: list[TgPost] = []
    before: int | None = None
    cutoff = None
    if after_date:
        cutoff = datetime.strptime(after_date, "%Y-%m-%d").replace(tzinfo=datetime.UTC)

    async with aiohttp.ClientSession() as session:
        while len(collected) < limit:
            html = await _fetch_page(session, channel, before)
            if not html:
                break

            page_posts = _parse_posts(html)
            if not page_posts:
                break

            for post in page_posts:
                if cutoff and post.date:
                    try:
                        post_dt = datetime.fromisoformat(post.date)
                        if post_dt.tzinfo is None:
                            post_dt = post_dt.replace(tzinfo=datetime.UTC)
                        if post_dt < cutoff:
                            return collected[:limit]
                    except ValueError:
                        pass
                collected.append(post)
                if len(collected) >= limit:
                    break

            before = page_posts[-1].id

    return collected[:limit]


async def get_channel_info(channel: str) -> TgChannelInfo:
    """Get channel title, description, subscriber count."""
    channel = normalize_channel(channel)

    async with aiohttp.ClientSession() as session:
        html = await _fetch_page(session, channel)
        return _parse_channel_info(html, channel)


async def search_posts(
    channel: str,
    query: str,
    limit: int = 50,
) -> list[TgPost]:
    """Fetch posts and filter by text query (case-insensitive)."""
    posts = await fetch_posts(channel, limit=limit)
    q_lower = query.lower()
    return [p for p in posts if q_lower in p.text.lower()]


async def top_posts(
    channel: str,
    limit: int = 50,
    sort_by: str = "views",
    top_n: int = 10,
) -> list[TgPost]:
    """Fetch posts and return top N by views or reactions."""
    posts = await fetch_posts(channel, limit=limit)
    if sort_by == "reactions":
        posts.sort(key=lambda p: p.reactions, reverse=True)
    else:
        posts.sort(key=lambda p: p.views_numeric, reverse=True)
    return posts[:top_n]


async def digest(
    channels: list[str],
    period: str = "today",
    limit_per_channel: int = 50,
) -> list[DigestEntry]:
    """Collect posts from multiple channels for a period.

    Args:
        channels: List of channel usernames.
        period: 'today', 'yesterday', 'week', or N (days as string).

    Returns:
        List of DigestEntry per channel.
    """
    after_date = _period_to_date(period)

    entries: list[DigestEntry] = []
    for ch in channels:
        ch = normalize_channel(ch)
        try:
            posts = await fetch_posts(ch, limit=limit_per_channel, after_date=after_date)
            entries.append(DigestEntry(channel=ch, posts=posts))
            log.info("Channel @%s: %d posts since %s", ch, len(posts), after_date)
        except Exception as e:
            log.warning("Failed to fetch @%s: %s", ch, e)
            entries.append(DigestEntry(channel=ch, posts=[]))

    return entries


async def compare_channels(
    channels: list[str],
    limit: int = 30,
) -> list[dict]:
    """Compare channels by engagement metrics.

    Returns list of dicts with: username, title, subscribers,
    avg_views, avg_reactions, post_count, posts_per_week.
    """
    results = []
    for ch in channels:
        ch = normalize_channel(ch)
        try:
            info = await get_channel_info(ch)
            posts = await fetch_posts(ch, limit=limit)

            avg_views = 0
            avg_reactions = 0
            posts_per_week = 0.0

            if posts:
                avg_views = sum(p.views_numeric for p in posts) // len(posts)
                avg_reactions = sum(p.reactions for p in posts) // len(posts)

                # Estimate posts/week from date range
                dates = []
                for p in posts:
                    if p.date:
                        try:
                            dates.append(datetime.fromisoformat(p.date))
                        except ValueError:
                            pass
                if len(dates) >= 2:
                    span = (max(dates) - min(dates)).total_seconds() / 86400
                    if span > 0:
                        posts_per_week = len(posts) / span * 7

            results.append({
                "username": ch,
                "title": info.title,
                "subscribers": info.subscribers,
                "avg_views": avg_views,
                "avg_reactions": avg_reactions,
                "post_count": len(posts),
                "posts_per_week": round(posts_per_week, 1),
            })
        except Exception as e:
            log.warning("Failed to compare @%s: %s", ch, e)

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _period_to_date(period: str) -> str | None:
    """Convert period string to YYYY-MM-DD date."""
    now = datetime.now(datetime.UTC)
    if period == "today":
        return now.strftime("%Y-%m-%d")
    if period == "yesterday":
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if period == "week":
        return (now - timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        days = int(period)
        return (now - timedelta(days=days)).strftime("%Y-%m-%d")
    except ValueError:
        return None
