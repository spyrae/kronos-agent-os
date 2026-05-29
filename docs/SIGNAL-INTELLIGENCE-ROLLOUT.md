# Signal Intelligence Rollout Checklist

Use this checklist before changing source tiers or enabling new signal jobs.

## Verification commands

```bash
pytest -q -m "not integration"
kaos signals dry-run news --source-limit 5 --output /tmp/signal-news.json
kaos signals dry-run jobs --source-limit 5 --output /tmp/signal-jobs.json
kaos signals dry-run ideas --source-limit 5 --output /tmp/signal-ideas.md --format md
kaos signals dry-run travel_insights --source-limit 5 --output /tmp/signal-travel.md --format md
```

Dry-run artifacts include:

- source item counts;
- source error counts;
- saved item count;
- cluster count;
- evidence-level counts;
- rendered Telegram body.

Dry-runs set `send=False` and `dry_run=True`, so they do not send Telegram
messages. Stored dry-run digests are marked with `[dry-run]`.

## Rollout phases

1. Keep existing cron jobs running unchanged.
2. Enable topic routing fallback only.
3. Enable source registry and store in dry-run.
4. Enable `Digest: News`.
5. Enable `Digest: Jobs`, `Digest: Product/Business Ideas`, and
   `JB: Travel Insights`.
6. Run one-day dry-run backfill and manually inspect wording.
7. Review biweekly source-quality audit before promoting/demoting sources.

## Manual QA guardrails

- No single post/comment/message may be described as a market trend.
- Trend/demand language requires `emerging_signal`, `trend`, or `confirmed`.
- Anecdotes must use wording like “one signal”, “single discussion”, or
  “worth watching”.
- Product and travel insights must separate evidence from speculation:
  product angle/JourneyBay implication is a hypothesis, not validated demand.
- Source changes should be based on source-quality stats, not gut feel.

## One-day backfill procedure

The default fetch freshness is past day (`pd`) where adapters support it:

```bash
kaos signals dry-run news --fetch-limit 12 --output /tmp/news-backfill.md --format md
kaos signals dry-run jobs --fetch-limit 12 --output /tmp/jobs-backfill.md --format md
kaos signals dry-run ideas --fetch-limit 12 --output /tmp/ideas-backfill.md --format md
kaos signals dry-run travel_insights --fetch-limit 12 --output /tmp/travel-backfill.md --format md
```

Inspect the rendered bodies before enabling or changing production schedules.
