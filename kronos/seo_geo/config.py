"""SEO/GEO configuration: sites, keywords, GEO questions.

Sources synthesised from:
- Real Google Search Console top-impression queries (last 90d).
- Codex consultancy (gpt-5.2) on strategic gaps for AI-travel /
  AI-dev-blog niches.
- Competitor analysis (Wanderlog, Layla, Tripsy for travel; established
  AI/dev blogs for futurecraft).

Tiers:
- ``A``: defend / top-20 / brand / money pages — track WEEKLY.
- ``B``: climb / impressions but pos 20-80 — track MONTHLY.
- ``C``: blank-spot strategic topics — track QUARTERLY.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Site:
    id: str  # short id used in DB and reports
    url: str  # canonical URL
    gsc_property: str  # GSC site URL (sc-domain: prefix)
    locales: tuple[str, ...]
    description: str


SITES: tuple[Site, ...] = (
    Site(
        id="journeybay",
        url="https://journeybay.co",
        gsc_property="sc-domain:journeybay.co",
        locales=("en", "ru"),
        description="AI travel planning app (iOS/Android + web). Multi-day itineraries, POI, visa info.",
    ),
    Site(
        id="futurecraft",
        url="https://futurecraft.pro",
        gsc_property="sc-domain:futurecraft.pro",
        locales=("en", "ru"),
        description="Tech blog: Claude Code, LLM observability, prompt engineering, MCP, agent frameworks.",
    ),
)

SITE_BY_ID = {s.id: s for s in SITES}


# ── Keywords ────────────────────────────────────────────────────────────
# Each entry: (keyword, site_id, locale, tier, category)
# Tier A → weekly | Tier B → monthly | Tier C → quarterly

KEYWORDS: tuple[tuple[str, str, str, str, str], ...] = (
    # ─── journeybay.co — brand ──────────────────────────────────────────
    ("journeybay", "journeybay", "en", "A", "brand"),
    ("journey bay", "journeybay", "en", "A", "brand"),
    ("journeybay приложение", "journeybay", "ru", "A", "brand"),
    # ─── journeybay.co — category head ──────────────────────────────────
    ("ai travel planner", "journeybay", "en", "A", "category"),
    ("ai trip planner", "journeybay", "en", "A", "category"),
    ("ai itinerary generator", "journeybay", "en", "A", "category"),
    ("ai itinerary maker", "journeybay", "en", "A", "category"),
    ("ai travel assistant", "journeybay", "en", "A", "category"),
    ("ai vacation planner", "journeybay", "en", "B", "category"),
    ("best ai travel app", "journeybay", "en", "A", "category"),
    ("best ai travel app 2026", "journeybay", "en", "B", "category"),
    ("travel planning app with ai", "journeybay", "en", "B", "category"),
    ("automatic trip planner", "journeybay", "en", "B", "category"),
    ("ai tour planner", "journeybay", "en", "B", "category"),
    ("multi day trip planner ai", "journeybay", "en", "B", "category"),
    ("ai-powered travel app", "journeybay", "en", "B", "category"),
    # ─── journeybay.co — category RU ────────────────────────────────────
    ("ai планировщик путешествий", "journeybay", "ru", "A", "category"),
    ("приложение для планирования путешествий с ai", "journeybay", "ru", "A", "category"),
    ("приложение для путешествий с ии", "journeybay", "ru", "A", "category"),
    ("ai помощник для путешествий", "journeybay", "ru", "B", "category"),
    ("сгенерировать маршрут поездки", "journeybay", "ru", "B", "category"),
    ("умное приложение для поездок", "journeybay", "ru", "B", "category"),
    ("чат-бот для планирования путешествий", "journeybay", "ru", "B", "category"),
    ("ai планирование отпуска", "journeybay", "ru", "B", "category"),
    # ─── journeybay.co — competitor / alternative ───────────────────────
    ("alternative to wanderlog", "journeybay", "en", "A", "competitor"),
    ("alternative to layla ai", "journeybay", "en", "A", "competitor"),
    ("alternative to tripit", "journeybay", "en", "A", "competitor"),
    ("chatgpt travel planner alternative", "journeybay", "en", "B", "competitor"),
    ("альтернатива wanderlog", "journeybay", "ru", "A", "competitor"),
    # ─── journeybay.co — feature ────────────────────────────────────────
    ("visa information app", "journeybay", "en", "B", "feature"),
    ("travel visa requirements app", "journeybay", "en", "B", "feature"),
    ("travel planning chatbot", "journeybay", "en", "C", "feature"),
    ("ai poi recommendations", "journeybay", "en", "C", "feature"),
    # ─── journeybay.co — destination + duration templates (GSC pattern) ──
    # These are the queries that are ALREADY bringing impressions in GSC.
    ("3 days in antalya", "journeybay", "en", "B", "destination"),
    ("istanbul in winter", "journeybay", "en", "B", "destination"),
    ("3 day itinerary tokyo", "journeybay", "en", "B", "destination"),
    ("5 day dubai itinerary", "journeybay", "en", "B", "destination"),
    ("путеводитель по токио", "journeybay", "ru", "B", "destination"),
    ("стамбул в декабре", "journeybay", "ru", "B", "destination"),
    ("когда на пхукете сезон дождей", "journeybay", "ru", "B", "destination"),
    ("спланируй 3-дневный маршрут в оаэ", "journeybay", "ru", "B", "destination"),
    ("что посмотреть в риме за 3 дня", "journeybay", "ru", "B", "destination"),
    ("что посмотреть в тбилиси за 4 дня", "journeybay", "ru", "B", "destination"),
    ("приложение виза", "journeybay", "ru", "C", "destination"),
    # ─── futurecraft.pro — Tier A (defend top-20 / brand) ───────────────
    ("kronos agent", "futurecraft", "en", "A", "brand"),
    ("kronos agent os", "futurecraft", "en", "A", "brand"),
    ("futurecraft", "futurecraft", "en", "A", "brand"),
    ("futurecraft pro", "futurecraft", "en", "A", "brand"),
    ("roman belov futurecraft", "futurecraft", "en", "A", "brand"),
    ("ai code review checklist", "futurecraft", "en", "A", "money"),
    ("ai code review security checklist", "futurecraft", "en", "A", "money"),
    ("ai assisted development security vulnerabilities checklist", "futurecraft", "en", "A", "money"),
    ("trace.generation langfuse python", "futurecraft", "en", "A", "money"),
    ("langfuse python trace generation", "futurecraft", "en", "A", "money"),
    ("human-in-the-loop automation fallback mechanisms", "futurecraft", "en", "A", "money"),
    ("how to evaluate ai pricing based on business value", "futurecraft", "en", "A", "money"),
    # ─── futurecraft.pro — Tier B (climb 20-80) ─────────────────────────
    ("ai prompts for icp development", "futurecraft", "en", "B", "category"),
    ("ai icp targeting tool", "futurecraft", "en", "B", "category"),
    ("ai icp tool", "futurecraft", "en", "B", "category"),
    ("how ai helps build icp for b2b sales", "futurecraft", "en", "B", "category"),
    ("hyper-personalized cold outreach", "futurecraft", "en", "B", "category"),
    ("ai prompts for cold outreach", "futurecraft", "en", "B", "category"),
    ("ai prompts for sop creation", "futurecraft", "en", "B", "category"),
    ("a/b testing prompts", "futurecraft", "en", "B", "category"),
    ("best practices for a/b testing ai model prompts", "futurecraft", "en", "B", "category"),
    ("how to build mcp server", "futurecraft", "en", "B", "category"),
    ("claude code mcp", "futurecraft", "en", "B", "category"),
    ("claude code audit", "futurecraft", "en", "B", "category"),
    ("anthropic context management", "futurecraft", "en", "B", "category"),
    ("context engineering guide", "futurecraft", "en", "B", "category"),
    ("ai code audit", "futurecraft", "en", "B", "category"),
    ("adr template", "futurecraft", "en", "B", "category"),
    ("adr в разработке", "futurecraft", "ru", "B", "category"),
    ("human-in-the-loop это", "futurecraft", "ru", "B", "category"),
    # ─── futurecraft.pro — Tier C (blank spots / strategic) ─────────────
    ("llm observability", "futurecraft", "en", "C", "strategic"),
    ("langfuse vs langsmith", "futurecraft", "en", "C", "strategic"),
    ("langfuse openai integration", "futurecraft", "en", "C", "strategic"),
    ("langfuse tracing python", "futurecraft", "en", "C", "strategic"),
    ("обсервабилити llm langfuse", "futurecraft", "ru", "C", "strategic"),
    ("claude code best practices", "futurecraft", "en", "C", "strategic"),
    ("claude code hooks", "futurecraft", "en", "C", "strategic"),
    ("claude code subagents", "futurecraft", "en", "C", "strategic"),
    ("claude code security checklist", "futurecraft", "en", "C", "strategic"),
    ("mcp server tutorial", "futurecraft", "en", "C", "strategic"),
    ("mcp server security checklist", "futurecraft", "en", "C", "strategic"),
    ("как создать mcp сервер", "futurecraft", "ru", "C", "strategic"),
    ("multi-provider llm architecture", "futurecraft", "en", "C", "strategic"),
    ("llm provider fallback strategy", "futurecraft", "en", "C", "strategic"),
    ("ai agent framework python", "futurecraft", "en", "C", "strategic"),
    ("durable ai agents with memory", "futurecraft", "en", "C", "strategic"),
    ("cash flow forecasting with ai", "futurecraft", "en", "C", "strategic"),
    ("промпт инжиниринг для разработчиков", "futurecraft", "ru", "C", "strategic"),
    ("тестирование flutter maestro", "futurecraft", "ru", "C", "strategic"),
    ("adr для ai проектов", "futurecraft", "ru", "C", "strategic"),
)


# ── GEO Questions ───────────────────────────────────────────────────────
# Natural buyer-stage queries that we'd want LLMs (ChatGPT, Perplexity,
# Claude, Gemini, Kimi) to cite our sites in. Run weekly.

GEO_QUESTIONS: tuple[tuple[str, str, str], ...] = (
    # (question, site_id, locale)
    # journeybay.co — 15 questions
    ("Which AI travel planner can build a 3-day Tokyo itinerary with POI recommendations?", "journeybay", "en"),
    ("What is the best AI itinerary generator for a multi-city Europe trip?", "journeybay", "en"),
    ("Which app is a good Wanderlog alternative for AI-generated itineraries?", "journeybay", "en"),
    ("Is there an AI travel app that imports bookings and turns them into a day-by-day itinerary?", "journeybay", "en"),
    ("What are the best AI travel planning apps for 2026?", "journeybay", "en"),
    ("Which AI trip planner helps with visa requirements and destination research?", "journeybay", "en"),
    ("What app can automatically plan a 5-day Dubai itinerary?", "journeybay", "en"),
    ("Which AI travel assistant supports both English and Russian?", "journeybay", "en"),
    ("What are the best Layla AI alternatives for detailed travel itineraries?", "journeybay", "en"),
    ("Can ChatGPT plan trips well, or should I use a dedicated AI travel planner?", "journeybay", "en"),
    ("How do I generate an itinerary with attractions, restaurants, and travel times?", "journeybay", "en"),
    ("Какое AI-приложение поможет спланировать Стамбул на 3 дня?", "journeybay", "ru"),
    ("Чем заменить Wanderlog для планирования путешествий с ИИ?", "journeybay", "ru"),
    ("Есть ли приложение, которое импортирует брони и делает маршрут поездки?", "journeybay", "ru"),
    ("Как спланировать поездку в ОАЭ на 3 дня с помощью ИИ?", "journeybay", "ru"),
    # futurecraft.pro — 15 questions
    ("What is a practical AI code review checklist for production apps?", "futurecraft", "en"),
    ("How should I audit code generated by Claude Code before merging?", "futurecraft", "en"),
    ("How do I trace LLM calls in Python with Langfuse?", "futurecraft", "en"),
    ("What should a good Langfuse trace include for an AI agent?", "futurecraft", "en"),
    ("How do I build a secure MCP server for an internal tool?", "futurecraft", "en"),
    ("What are the main MCP server security risks?", "futurecraft", "en"),
    ("How should I design a multi-provider LLM architecture with fallbacks?", "futurecraft", "en"),
    ("What is context engineering and how is it different from prompt engineering?", "futurecraft", "en"),
    ("How can teams A/B test prompts for LLM features?", "futurecraft", "en"),
    ("What open-source agent framework supports durable sessions, memory, skills, and MCP tools?", "futurecraft", "en"),
    ("When should a team write ADRs for LLM architecture decisions?", "futurecraft", "en"),
    ("Как AI помогает PM сформулировать ICP и требования к продукту?", "futurecraft", "ru"),
    ("Как настроить observability для LLM-приложения на Python?", "futurecraft", "ru"),
    ("Как провести аудит проекта, написанного Claude Code?", "futurecraft", "ru"),
    ("Как использовать AI для cash flow forecasting без галлюцинаций?", "futurecraft", "ru"),
)


# Brand mention patterns — used by GEO citation parser to detect whether
# the LLM answer mentions our brands.
BRAND_PATTERNS = {
    "journeybay": ["journeybay", "journey bay", "journeybay.co"],
    "futurecraft": ["futurecraft", "futurecraft.pro", "kronos agent", "roman belov"],
}

# Direct competitors (so we can also count *their* citation rate as
# a useful comparison signal in the reports).
COMPETITOR_PATTERNS = {
    "journeybay": [
        "wanderlog",
        "layla",
        "tripit",
        "tripsy",
        "mindtrip",
        "roam around",
        "iplan.ai",
        "sygic",
        "lambus",
        "wonderplan",
    ],
    "futurecraft": ["langsmith", "humanloop", "weights and biases", "promptlayer"],
}
