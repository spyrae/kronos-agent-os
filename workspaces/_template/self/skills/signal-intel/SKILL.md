# Signal Intelligence

Use this skill to collect, score, cluster, and route high-signal updates from
X, Reddit, Telegram, search, and JourneyBay-owned data sources.

## Source registry

Primary source config lives in `references/SOURCES.yaml`.

Each source declares:

- `id`: stable snake_case identifier
- `platform`: `reddit`, `x`, `telegram`, `search`, `competitor`, `app_store`,
  `play_store`, `analytics`, `seo`, or `rss`
- one locator: `handle`, `url`, or `query`
- `categories`: one or more of `news`, `jobs`, `ideas`, `travel_insights`,
  `jb_competitors`, `jb_system`
- `tier`: `core`, `candidate`, or `quarantine`
- `trust`: `official`, `expert`, `community_high`, `community_low`, or `noisy`
- `filters`: platform-specific thresholds and include/exclude rules

Default behavior:

- disabled sources are never fetched
- quarantine sources are kept for audits but excluded from active digests
- trend claims require corroboration; see `references/SCORING.md`
- rendering and summarization prompts live in `references/PROMPTS.md`

## Output destinations

- `news` → `Digest: News`
- `jobs` → `Digest: Jobs`
- `ideas` → `Digest: Product/Business Ideas`
- `travel_insights` → `JB: Travel Insights`
- `jb_competitors` → `JB: Competitors Status`
- `jb_system` → `JB: System Status`

When unsure whether something is a trend, downgrade it to an observation and
include the evidence count/source spread instead of overstating the conclusion.
