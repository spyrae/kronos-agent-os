# Signal Intelligence Scoring

## Source tiers

- `core`: high value source, fetched regularly, eligible for digest headlines.
- `candidate`: useful but unproven/noisy source, fetched with stricter filters.
- `quarantine`: retained for periodic audits, excluded from active digests.

## Trust levels

- `official`: company/project/account speaking for itself.
- `expert`: consistently high-signal individual or specialist publication.
- `community_high`: strong community with moderation or meaningful voting.
- `community_low`: open discussion, useful but noisy.
- `noisy`: memes, rumors, ragebait, or duplicated commentary.

## Trend claim guardrail

Do not claim that "the market is shifting" from a single post or one Telegram
thread. Treat it as a trend only when at least two of these are true:

1. 3+ independent sources mention the same pattern in the same window.
2. Evidence spans at least 2 platforms or source types.
3. One source is official or expert-tier.
4. Quantitative signal exists: votes, views, job posts, releases, changelog,
   App Store reviews, SEO/GEO metrics, or competitor activity.

If evidence is weaker, phrase as:

- "single-source observation"
- "early weak signal"
- "discussion worth watching"

## Suggested item score

Start at `0` and add:

- `+35` official launch/changelog/pricing/regulatory/source-of-record update
- `+25` expert source with concrete details or primary evidence
- `+20` high community engagement relative to source baseline
- `+15` repeats across independent sources/platforms
- `+10` clear JourneyBay strategic relevance
- `+10` actionable job/product/business signal
- `-20` single-source speculation
- `-20` repost/duplicate without new evidence
- `-30` meme/ragebait/uncited claim

Routing guideline:

- `70+`: headline
- `45-69`: notable item
- `25-44`: watchlist/weak signal
- `<25`: archive only
