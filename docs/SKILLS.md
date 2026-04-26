# Kronos II — Skills Reference

## How Skills Work

Skills use **Progressive Disclosure** to minimize token usage:

| Level | What | When Loaded | Token Cost |
|-------|------|-------------|------------|
| **L1 Catalog** | Name + description | Always in system prompt | ~50-100 per skill |
| **L2 Full Protocol** | Complete SKILL.md | `load_skill(name)` tool call | 500-3000 per skill |
| **L3 References** | Supporting data files | `load_skill_reference(name, ref)` tool call | Variable |

### Flow
1. System prompt contains L1 catalog of all 18 skills
2. User sends message matching a skill trigger
3. Supervisor or agent calls `load_skill("skill-name")` → gets full protocol
4. Agent follows the protocol step by step
5. If protocol needs reference data → `load_skill_reference("skill-name", "WATCHLIST")` etc.

### Storage
```
workspace/skills/
├── deep-research/
│   └── SKILL.md
├── news-monitor/
│   ├── SKILL.md
│   └── references/
│       └── WATCHLIST.md
├── expense-tracker/
│   ├── SKILL.md
│   └── references/
│       └── BUDGET.md
└── ...
```

## Full Skill List (18 skills)

### Research & Analysis

#### 1. deep-research
**Triggers:** "исследуй", "research", "проверь идею", "анализ рынка", "тренды"

5 research modes:
- **TOPIC RESEARCH** — deep dive into a topic (Brave + Exa + YouTube + synthesis)
- **IDEA VALIDATION** — validate before building (GitHub, HN, Product Hunt, npm/PyPI); outputs Reality Signal 0-100
- **MARKET RESEARCH** — pain points and opportunities (Reddit, Twitter, HN, reviews)
- **COMPETITIVE ANALYSIS** — deep competitor breakdown (product, tech, reputation, business model)
- **TREND ANALYSIS** — what's growing/dying (Exa, HN, GitHub stars, funding)

#### 2. web-research
**Triggers:** "найди информацию", "поищи в интернете", "загугли"

Systematic web research methodology with 5 strategies:
- Technology & Library Research
- Current Events & News
- Best Practices & Standards
- Problem-Solving (error messages, bugs)
- Person/Company Research

Includes source quality indicators and search pattern templates.

#### 3. new-project-research
**Triggers:** "new project research", "валидация идеи", "стоит ли запускать"

Full 5-direction research for new business ideas:
1. Technical landscape (existing solutions, recommended stack, MVP architecture)
2. Product strategy (differentiation, pricing tiers, competitive analysis)
3. Target audience (segmentation, Russian market specifics, first 50 customers)
4. Landing page (structure, copywriting, technical recommendations)
5. Unit economics (infrastructure costs, financial model, 3 scenarios)

Output: comprehensive RESEARCH.md with go/no-go recommendation.

#### 4. fact-checker
**Triggers:** "проверь факты", "fact check", "это правда?", "верифицируй"

Systematic fact verification:
- Extract all verifiable claims from text
- Prioritize: P0 (numbers, stats, tech claims), P1 (versions, dates, code), P2 (links), P3 (causal claims)
- Verify via Brave, Exa, Fetch, Content-core
- Classify: PASS / FAIL / OUTDATED / UNVERIFIED
- Special focus on typical AI errors (hallucinated libraries, wrong versions, fake URLs)

### Intelligence & Analysis

#### 5. osint
**Triggers:** "разведка", "osint", "досье", "пробей", "кто такой", "due diligence"

4 modes: person, domain, email, org.
- Person: 6-phase pipeline (quick search → deep dive → internal intel → cross-verification → psychoprofile → completeness assessment)
- Domain: whois, tech stack, security, site index
- Email: account discovery, domain analysis, internal history
- Org: Crunchbase, financials, team, funding, competitors

Each fact graded A-D. Depth Score 1-10.

#### 6. investment-analysis
**Triggers:** "инвестиционный обзор", "что на рынках?", "проанализируй AAPL"

4-step pipeline:
1. **Macro Scan** — S&P 500, NASDAQ, VIX, 10Y Treasury, Gold, Bitcoin
2. **Sector Screen** — 11 sector ETFs (XLK, XLF, XLE, etc.), sector rotation
3. **Company Dive** — prices, financials, earnings, recommendations, news
4. **Recommendations** — data-driven picks with entry/target/stop/risk

Uses Yahoo Finance MCP for real-time data. Full and Quick Check modes. [refs: WATCHLIST]

#### 7. vc-investor
**Triggers:** "оцени идею", "как инвестор", "бизнес-план", "pitch", "IC meeting"

Sequoia/YC/a16z mindset simulation:
- **Evaluation Mode** — Needle-Moving Check → Quick Kill → 5-dimension scoring (Team 35%, Market 25%, Product 20%, Traction 10%, Unit Economics 10%)
- **Business Plan Mode** — Sequoia-standard 10-15 slides with market data verification
- Pattern matching against similar startups (successes and failures)

### Decision Making

#### 8. decision-frameworks
**Triggers:** "помоги решить", "не могу выбрать", "фреймворк", "стоит ли"

Library of 405 decision protocols from Athena project. Categories: Decision (54), Safety (9), Psychology (40), Strategy (21), Business (28), Pattern Detection (16), Communication (18), Engineering (24), Architecture (63), and more.

Quick combinations for common scenarios:
- "Launch feature?" → Four Fits → Law of Ruin → Efficiency vs Robustness → Unit Economics
- "Stuck, unclear problem" → Frame vs Structural → Premise Validation → First Principles
- "Tempted but uncertain" → 3-Second Override → Base Rate Audit → Ergodicity Check

Three depth levels: QUICK (1-2 protocols), STANDARD (3-5), DEEP (6+).

#### 9. ensemble
**Triggers:** "стоит ли", "как лучше", "что выбрать", "консилиум", "мультиперспектива"

4-5 independent viewpoints analyzing one question:
- Default: Strategist, Pragmatist, Skeptic, User, Economist
- Domain-specific sets for investments, product, life decisions, career
- Perspectives must CONFLICT — if all agree, it's done wrong
- Synthesis: consensus, key conflict, specific recommendation

#### 10. simulate
**Triggers:** "что будет если", "simulate", "симулируй", "стоит ли рискнуть"

Mental Loop Simulator:
- 3 scenarios: Optimistic (15-25%), Realistic (50-60%), Pessimistic (15-25%)
- Temporal dynamics: T+1 week → T+1 month → T+3 months → T+6 months
- Decision framework: Expected Value, Worst-case tolerance, Optionality, Regret minimization

### Personal & Lifestyle

#### 11. life-design-coach
**Triggers:** "лайф дизайн", "выход из застоя", "vision", "прокрастинация", "коучинг"

Dan Koe Life Design Protocol adaptation. 4 modes:
- **FULL PROTOCOL** — full-day transformation (morning excavation → afternoon autopilot interruption → evening synthesis → gamification)
- **EXPRESS SESSION** — specific problem solving (2-3 questions → hidden goal → anti-vision → one action)
- **CHECKUP** — progress review via Notion goals
- **DECOMPOSITION** — goal breakdown with gamification table (Anti-Vision, Vision, 1Y Goal, 1M Project, Daily Levers, Constraints)

#### 12. food-advisor
**Triggers:** "что поесть", "оцени продукты", "чем заменить", "диета"

EAS diet-based nutrition advisor:
- "What to eat?" — 3-5 specific meals from recommended products
- "Rate products" — [OK] / [MODERATE] / [REPLACE] with alternatives
- "Replace X?" — 2-3 alternatives from recommended category
- Full database: fats, meats, dairy, fish, fruits/vegetables, grains, baked goods, beverages, sweets

### Monitoring & Automation

#### 13. news-monitor
**Triggers:** Automatic (daily cron), "что нового", "дайджест", "news digest"

Daily news digest:
1. Parse WATCHLIST.md for Reddit subreddits and Twitter accounts
2. Brave Search for each topic (last 24h)
3. LLM synthesis → structured digest grouped by topic
4. Send to Telegram News topic (HTML format)

[refs: WATCHLIST]

#### 14. heartbeat
**Triggers:** Internal reference for system operations

System operations reference:
- Active automated tasks (watchlist monitor, expense scanner, health check, daily status, workspace backup)
- Inactive tasks (investment review, Notion review, morning briefing, expense processing)

#### 15. people-scout
**Triggers:** Automatic (weekly cron), "найди людей", "people scout", "networking"

Weekly LinkedIn profile discovery:
- Focus rotation: US founders → EU founders → AI engineers → Indie hackers
- 8-12 search queries via Brave/Exa, rotated weekly
- Scoring 1-10 by role match, domain, activity, connect potential, location
- Top 10 profiles with mini-profiles
- Deduplication via SEEN.md

[refs: CRITERIA, SEEN]

#### 16. group-digest
**Triggers:** Automatic (daily cron), "дайджест групп", "что в группах"

Daily Telegram group digest:
- Read group history via Telethon (last 24h)
- Filter by reactions and views (engagement scoring)
- LLM synthesis → structured summary per group with insights

[refs: GROUPS]

#### 17. expense-tracker
**Triggers:** "что по расходам", "обработай расходы", expense scanner notification

Expense tracking with PermataBank (IDR) and Maybank (MYR):
- Gmail scanning for bank transaction emails
- Interactive categorization (Food, Transport, Shopping, etc.)
- FIFO budget management (tranches in RUB at exchange rates)
- Split tracking (shared expenses with partner)
- Notion Expenses DB integration

[refs: BUDGET]

#### 18. tool-radar
**Triggers:** "tool radar", "триаж тулов", "разбери инструменты", "разбери inbox"

Incoming tool/content triage:
- 7 content types: Tool/Repo, Article/Guide, News digest, Tech note, Channel list, Personal, Model release
- 4 priority categories: Skip (60%), Someday (25%), Soon (10%), Important (5%)
- Research for tools (GitHub stats, maturity, alternatives)
- Save to Notion "See Later" database with tags

## Creating a New Skill

### 1. Create directory structure
```
workspace/skills/{skill-name}/
├── SKILL.md
└── references/           # optional
    ├── WATCHLIST.md
    └── CRITERIA.md
```

### 2. Write SKILL.md with frontmatter
```markdown
---
name: my-skill
description: >
  One-line description for L1 catalog.
  Include trigger keywords for the supervisor to match.
---

# My Skill — Skill Protocol

## Триггер
[When to activate]

## Pipeline
[Step-by-step instructions]

## Формат отчёта
[Output template]

## Правила
[Constraints and style]
```

### 3. Restart agent
SkillStore scans `workspace/skills/` on startup. New skill appears in L1 catalog automatically.

### 4. Optional: Add references
Files in `references/*.md` become available via `load_skill_reference("my-skill", "FILENAME")`.

### Key Principles
- Frontmatter `description` is critical — it's what the supervisor sees for routing
- Include trigger phrases in Russian and English
- Keep L1 description under 200 chars
- Full protocol can be any length (loaded on demand)
- Include MCP tool examples (`brave-search`, `exa`, etc.) in pipeline steps
- Add `## Стиль INTJ` section for persona consistency
