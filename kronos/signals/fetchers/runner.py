"""Run Signal Intelligence fetchers safely across source registries."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from kronos.signals.fetchers.base import (
    FetcherError,
    FetchErrorKind,
    FetchOptions,
    FetchResult,
    error_result,
)
from kronos.signals.fetchers.brave_search import fetch_search_source
from kronos.signals.fetchers.competitor import fetch_competitor_source
from kronos.signals.fetchers.reddit_search import fetch_reddit_source
from kronos.signals.fetchers.telegram_public import fetch_telegram_public_source
from kronos.signals.fetchers.telegram_telethon import fetch_telegram_telethon_source
from kronos.signals.fetchers.x_search import fetch_x_source
from kronos.signals.sources import SignalSource

Fetcher = Callable[[SignalSource, FetchOptions], Awaitable[FetchResult]]


async def fetch_source(
    source: SignalSource,
    *,
    options: FetchOptions | None = None,
    fetchers: dict[str, Fetcher] | None = None,
) -> FetchResult:
    """Fetch one source and convert failures into categorized errors."""
    opts = options or FetchOptions()
    started = time.monotonic()
    fetcher = (fetchers or _default_fetchers()).get(source.platform)
    if fetcher is None:
        return error_result(source, FetchErrorKind.UNSUPPORTED_PLATFORM, f"unsupported platform: {source.platform}")

    try:
        result = await fetcher(source, opts)
        return FetchResult(
            source=source,
            items=result.items,
            errors=result.errors,
            elapsed_ms=_elapsed_ms(started),
        )
    except FetcherError as exc:
        return error_result(source, exc.kind, exc.message, elapsed_ms=_elapsed_ms(started))
    except PermissionError as exc:
        return error_result(source, FetchErrorKind.AUTH_MISSING, str(exc), elapsed_ms=_elapsed_ms(started))
    except ConnectionError as exc:
        return error_result(source, FetchErrorKind.SOURCE_UNAVAILABLE, str(exc), elapsed_ms=_elapsed_ms(started))
    except Exception as exc:
        return error_result(source, FetchErrorKind.PARSER_FAILURE, str(exc), elapsed_ms=_elapsed_ms(started))


async def fetch_sources(
    sources: list[SignalSource] | tuple[SignalSource, ...],
    *,
    options: FetchOptions | None = None,
    fetchers: dict[str, Fetcher] | None = None,
) -> list[FetchResult]:
    """Fetch multiple sources; one failed source never fails the full run."""
    results: list[FetchResult] = []
    for source in sources:
        if not source.enabled or source.tier == "quarantine":
            continue
        results.append(await fetch_source(source, options=options, fetchers=fetchers))
    return results


def format_dry_run(results: list[FetchResult], *, sample_chars: int = 160) -> str:
    """Render a dry-run summary with counts and sample normalized items."""
    lines = ["Signal fetch dry-run"]
    for result in results:
        if result.errors:
            errors = ", ".join(f"{error.kind}: {error.message}" for error in result.errors)
            lines.append(f"- {result.source.id} [{result.source.platform}]: ERROR {errors}")
            continue

        sample = ""
        if result.items:
            first = result.items[0]
            sample = f" — {first.title or first.text}"
            if len(sample) > sample_chars:
                sample = sample[: sample_chars - 3].rstrip() + "..."
        lines.append(
            f"- {result.source.id} [{result.source.platform}]: {len(result.items)} items"
            f" in {result.elapsed_ms}ms{sample}"
        )
    return "\n".join(lines)


def _default_fetchers() -> dict[str, Fetcher]:
    async def _search(source: SignalSource, options: FetchOptions) -> FetchResult:
        return await fetch_search_source(source, options=options)

    async def _reddit(source: SignalSource, options: FetchOptions) -> FetchResult:
        return await fetch_reddit_source(source, options=options)

    async def _x(source: SignalSource, options: FetchOptions) -> FetchResult:
        return await fetch_x_source(source, options=options)

    async def _telegram(source: SignalSource, options: FetchOptions) -> FetchResult:
        mode = str(source.filters.get("mode") or "telethon")
        if mode == "public":
            return await fetch_telegram_public_source(source, options=options)
        return await fetch_telegram_telethon_source(source, options=options)

    async def _competitor(source: SignalSource, options: FetchOptions) -> FetchResult:
        return await fetch_competitor_source(source, options=options)

    return {
        "search": _search,
        "reddit": _reddit,
        "x": _x,
        "telegram": _telegram,
        "competitor": _competitor,
    }


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
