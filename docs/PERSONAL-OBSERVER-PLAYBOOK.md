# KAOS Personal Observer Playbook

Практическое руководство по фичам, собранным в Linear задачах RB-1276…
RB-1283 и связанных задачах этой сессии.

## Главная идея

Personal Observer / Capture Engine — это слой вокруг Telegram DM и локального
workspace, который:

1. отличает явные заметки/ссылки/voice capture от обычных вопросов агенту;
2. сохраняет explicit captures в локальный inbox;
3. читает личные Telegram dialogs read-only для digest/debt/scope задач;
4. не ставит Telegram read acknowledgements в фоновых scan/digest flows;
5. хранит состояние локально и не пишет полные переписки в background logs;
6. отдаёт ручное управление через `/observer ...` команды в DM.

Локальный workspace по умолчанию:

```text
workspaces/kronos/
```

Если задан `WORKSPACE_PATH`, все observer/capture артефакты будут в нём.

## Быстрый запуск

Из корня приложения:

```bash
cd "/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app"
kaos doctor
kaos connect telegram
```

Для Telegram userbot/scanner:

```bash
python scripts/auth-userbot.py
```

Запуск полного runtime с Telegram bridge и cron jobs:

```bash
AGENT_NAME=kronos python -m kronos
```

Cron-задачи работают только пока runtime живой.

## Нужные env-переменные

Минимум для Telegram/runtime:

```env
AGENT_NAME=kronos
TG_API_ID=
TG_API_HASH=
TG_BOT_TOKEN=
ALLOWED_USERS=
```

Минимум для LLM:

```env
OPENAI_API_KEY=
# или FIREWORKS_API_KEY / DEEPSEEK_API_KEY
```

Для Gmail expenses:

```env
NOTION_API_KEY=
NOTION_EXPENSES_DB_ID=
GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=
GMAIL_ACCOUNT=
EMAIL_EXPENSES_LOOKBACK_DAYS=2
EMAIL_EXPENSES_LIMIT=10
```

## Task map

| Linear | Что сделано | Commit |
| --- | --- | --- |
| RB-1276 | Observer safety contract, models, state store | `6f35107` |
| RB-1277 | Capture classifier + запись в `notes/inbox` | `7799154` |
| RB-1278 | Capture hook в Telegram DM flow до `_ask_agent()` | `e86a40d` |
| RB-1279 | Bookmark sink + optional Raindrop stub без hard dependency | `2c315c3` |
| RB-1280 | Read-only Telegram scanner без read ack | `e0f36e0` |
| RB-1281 | Reply-debt detector | `bfde136` |
| RB-1282 | Morning Observer Digest + cron | `5e023de` |
| RB-1283 | Daily Scope + cron | `aea0625` |
| RB-1187 | Notion Taste DB: Books view | Notion update |
| RB-1186 | Notion Taste DB: Films view | Notion update |
| RB-927 | Content digest skill | `3af653a` |
| RB-922 | Calories DB + health-lite / food-advisor skill | `209917f` |
| RB-907 | Gmail MCP-backed email expenses cron | `e2d5cd3` |

## RB-1276 — Observer safety contract, models, state store

Это фундамент безопасности Observer.

### Где хранится состояние

```text
<workspace>/ops/observer/state.json
<workspace>/ops/observer/runs.jsonl
```

Например:

```text
workspaces/kronos/ops/observer/state.json
workspaces/kronos/ops/observer/runs.jsonl
```

### Что хранится

- per-dialog cursors;
- last seen message ids;
- ignored peers;
- muted peers;
- sanitized ignore/mute reasons;
- last scan timestamps;
- last digest timestamps;
- sanitized run metadata.

### Что не должно храниться

- полные личные переписки из фоновых сканов;
- raw PII в логах;
- Telegram read acknowledgements из scanner/digest flows;
- outbound messages людям/группам без allowlist и явной команды.

## RB-1277 — Capture classifier + запись в `notes/inbox`

Capture classifier решает, нужно ли сохранить входящее DM-сообщение как знание
или отправить его обычному агенту.

### Текстовая заметка

В Telegram DM боту:

```text
запомни: мне нравится, когда агент отвечает коротко и без воды
```

Также поддерживаются префиксы:

```text
сохрани: ...
note: ...
capture: ...
```

Результат:

```text
Сохранил в inbox: заметку. ID: `knowledge-...`
```

Создаются файлы:

```text
<workspace>/notes/inbox/<task_id>.md
<workspace>/ops/queue/<task_id>.knowledge.json
```

### Link capture

Если отправить только ссылку:

```text
https://example.com/article
```

она сохраняется как `telegram_link`.

Если написать вопрос со ссылкой:

```text
посмотри https://example.com/article и перескажи
```

это считается обычным запросом агенту, а не capture.

### Voice capture

Голосовая заметка в DM считается explicit capture. Она сохраняется как
`telegram_voice_note`, если transcription pipeline вернул текст.

## RB-1278 — Capture hook в Telegram DM flow до `_ask_agent()`

Capture обрабатывается до LLM-вызова.

Практический эффект:

```text
capture: купить переходник USB-C
```

сохранится в inbox и не пойдёт в `_ask_agent()`.

А обычный вопрос:

```text
Что думаешь про идею сделать KAOS для personal ops?
```

пойдёт в обычный агентский flow.

В группах capture отключён: group message не должен сохраняться в личный inbox.

## RB-1279 — Bookmark sink + optional Raindrop stub

Link capture нормализует URL и записывает bookmark metadata в knowledge task.

Пример DM:

```text
https://github.com/spyrae/kronos-agent-os
```

Сейчас Raindrop — intentionally non-networked stub:

- без `RAINDROP_API_TOKEN` локальный capture продолжает работать;
- с `RAINDROP_API_TOKEN` direct API persistence всё равно не включён;
- токен не обязателен и не должен логироваться.

То есть это контракт под будущий remote bookmark sink, а не готовый Raindrop
sync.

## RB-1280 — Read-only Telegram scanner без read ack

Scanner читает private Telegram dialogs для Observer jobs.

### Что он делает

- проходит по private dialogs;
- пропускает группы, каналы, ботов и self;
- читает последние сообщения через `iter_messages`;
- строит compact `DialogSnapshot`;
- сохраняет только summary/excerpt/metadata;
- не вызывает `send_read_acknowledge`.

### Где используется

- Morning Observer Digest;
- `/observer debts`;
- Daily Scope.

## RB-1281 — Reply-debt detector

Reply-debt detector ищет диалоги, где последнее значимое сообщение входящее и
пользователь, вероятно, должен ответить.

### Ручная команда

В Telegram DM:

```text
/observer debts
```

### Threshold

По умолчанию:

```env
OBSERVER_REPLY_THRESHOLD_HOURS=8
```

### Severity

| Severity | Условие |
| --- | --- |
| `medium` | старше threshold |
| `high` | старше 24 часов |
| `critical` | старше 72 часов |

Шумы вроде `ок`, `👍`, `+`, `...` игнорируются.

## RB-1282 — Morning Observer Digest + cron

Утренний digest лички.

### Расписание

```text
daily 23:00 UTC = 07:00 WITA / Asia-Makassar
```

Работает только при:

```env
AGENT_NAME=kronos
```

### Что присылает

Пример структуры:

```text
🌅 Утренний обзор лички

Непрочитанное:
1. Ivan — 3 сообщ., главное: ...

Ждут ответа:
• Sasha — 1д, high; последний входящий: ...
```

### Безопасный ручной тест

В Telegram DM:

```text
/observer digest dry-run
```

Dry-run ничего не отправляет по расписанию и не обновляет scanner cursors.

## RB-1283 — Daily Scope + cron

Вечерняя карта дня по private dialogs.

### Расписание

```text
daily 14:00 UTC = 22:00 WITA / Asia-Makassar
```

Работает только при:

```env
AGENT_NAME=kronos
```

### Что делает

- читает private dialog snapshots за текущий локальный день UTC+8;
- собирает per-contact summaries;
- ищет маркеры договорённостей;
- помечает risk, если последнее сообщение входящее;
- отправляет Telegram digest;
- сохраняет Markdown-файл.

### Agreement markers

```text
договорились
жду
скинь
напомни
давай
сделаю
```

### Где сохраняется

```text
<workspace>/notes/user/daily-scope/YYYY-MM-DD.md
```

Ручной Telegram-команды для Daily Scope пока нет: сейчас это cron-only flow.

## Observer manual controls

Команды доступны только в Telegram DM. Group commands игнорируются.

```text
/observer status
/observer ignore <peer> [reason]
/observer unignore <peer>
/observer mute <peer> [reason]
/observer unmute <peer>
/observer debts
/observer digest dry-run
```

### Разница `ignore` и `mute`

- `ignore` — privacy/no-scan use case;
- `mute` — noisy peer, которого не нужно поднимать в digests/debts.

`<peer>` — это peer id/token из status/debt outputs.

## RB-1187 / RB-1186 — Taste DB: Books + Films

Notion Taste DB:

```text
https://app.notion.com/p/352f0ab7397780b480fbd4241bf69f98
```

Views:

- `All`
- `Books`
- `Films`

### Поля

- `Title`
- `Type`: `music`, `film`, `book`, `podcast`, `game`
- `Reaction`: `🔥`, `⭐`, `👌`, `❌`, `🚫`
- `Vibes`
- `Contexts`
- `Why not`
- `Creator`
- `Year`
- `Link`
- `Replayed`
- `Date`

### Пример книги

```text
Title: Solaris
Type: book
Reaction: 🔥
Vibes: philosophical, cosmic, slow
Contexts: night reading, deep thinking
Creator: Stanisław Lem
```

### Пример фильма

```text
Title: Blade Runner 2049
Type: film
Reaction: 🔥
Vibes: neon, melancholic, slow sci-fi
Contexts: evening, visual inspiration
```

### Как просить рекомендации

```text
На основе моего Taste DB подбери 7 книг, похожих на мои 🔥 книги.
```

```text
Подбери фильмы на вечер по моему Taste DB, но без слишком тупого action.
```

Ограничение: текущий Notion access лучше работает с доступными страницами и
поиском, а не как полноценный SQL/BI engine.

## RB-927 — Content digest skill

Commit: `3af653a`.

Скилл: `content-digest`.

### Установка

```bash
kaos skills install-pack content --agent kronos --dry-run
kaos skills install-pack content --agent kronos --force
```

### Использование через CLI

```bash
kaos chat --tools --prompt "Сделай выжимку из этого текста: ..."
```

### Использование через Telegram

```text
Сделай выжимку из этой статьи: <url>
```

Для YouTube/video:

```text
Вот transcript видео. Сделай TL;DR, key points и action items:
...
```

Если transcript недоступен, агент должен попросить transcript и не должен
притворяться, что посмотрел видео.

### Ожидаемый формат

- TL;DR;
- main points;
- useful details;
- actions/questions;
- caveats / missing context.

## RB-922 — Calories DB + health-lite / food-advisor

Commit: `209917f`.

Notion Calories DB:

```text
https://app.notion.com/p/8d068ec14f524b97acfa3b57b6d5b498
```

Скилл: `food-advisor` из pack `health-lite`.

### Установка

```bash
kaos skills install-pack health-lite --agent kronos --dry-run
kaos skills install-pack health-lite --agent kronos --force
```

### Использование

```text
Оцени калории: 2 яйца, авокадо, тост, кофе с молоком.
```

```text
Залогируй завтрак: nasi goreng, курица, iced latte.
```

### Ожидаемый вывод

- kcal estimate;
- protein/carbs/fat;
- assumptions;
- confidence;
- compact log row для Notion/manual entry.

Это informational tracker, не medical advice.

## RB-907 — Email expenses cron

Commit: `e2d5cd3`.

Gmail MCP-backed cron для автоматического извлечения расходов из чеков и
инвойсов.

### Расписание

```text
daily 00:00 UTC = 08:00 WITA / Asia-Makassar
```

Работает только при:

```env
AGENT_NAME=kronos
```

### Что делает

1. Ищет receipts/invoices в Gmail через Google Workspace MCP.
2. Извлекает expense data через LITE LLM.
3. Создаёт расходы через canonical `add_expense`.
4. Сохраняет RUB/IDR/FIFO invariants.
5. Если созданы расходы, отправляет Telegram notification.

### Gmail query

```text
newer_than:2d (receipt OR invoice OR "payment confirmation" OR "tax invoice" OR "order receipt")
```

`2d` берётся из:

```env
EMAIL_EXPENSES_LOOKBACK_DAYS=2
```

### Поддерживаемые валюты

Live creation сейчас поддерживает:

- `IDR`
- `RUB`

Остальные валюты пропускаются.

### Notification

```text
📧 Email Expenses: N новых расходов из почты
```

### Важное ограничение

Сейчас это live-запись в Notion. Отдельного dry-run режима для email expenses
пока нет. Перед включением на реальной почте лучше ограничить:

```env
EMAIL_EXPENSES_LOOKBACK_DAYS=1
EMAIL_EXPENSES_LIMIT=3
```

## Ежедневный сценарий использования

### Утро

1. Получить scheduled Morning Observer Digest.
2. При необходимости вручную проверить:

```text
/observer debts
```

3. Замьютить шумные peer:

```text
/observer mute <peer> noisy
```

### В течение дня

Сохранять заметки:

```text
capture: мысль для KAOS — сделать inbox review
```

Сохранять ссылки:

```text
https://example.com/useful-article
```

Просить выжимку:

```text
Сделай выжимку из этой статьи: <url>
```

Логировать еду:

```text
Оцени и залогируй: rice bowl, chicken, iced latte
```

### Вечер

Получить scheduled Daily Scope и посмотреть:

- с кем были договорённости;
- где последнее сообщение входящее;
- что стоит продолжить завтра.

## Smoke test checklist

В Telegram DM:

```text
capture: проверить, как работает Observer capture
```

Ожидаемо:

```text
Сохранил в inbox: заметку. ID: ...
```

Потом:

```text
https://github.com/spyrae/kronos-agent-os
```

Ожидаемо: link capture.

Потом:

```text
/observer status
/observer debts
/observer digest dry-run
```

Если всё отвечает без ошибок, базовая цепочка RB-1276…RB-1283 живая.

## Где смотреть артефакты

Observer state:

```text
<workspace>/ops/observer/state.json
<workspace>/ops/observer/runs.jsonl
```

Captured inbox:

```text
<workspace>/notes/inbox/
```

Knowledge queue:

```text
<workspace>/ops/queue/
```

Daily Scope:

```text
<workspace>/notes/user/daily-scope/
```

OSINT dossiers, если используется `/osint person`:

```text
<workspace>/notes/world/contacts/
```

## Честные ограничения

- Raindrop sink пока stub, без реального remote save.
- Daily Scope пока cron-only, без ручной Telegram-команды.
- Email expenses пока без dry-run и пишет реальные Notion expenses.
- Content digest по видео требует transcript; без transcript агент не должен
  выдумывать содержание.
- Food advisor не является medical advice.
- Notion Taste DB работает как structured human-readable память, но не как
  полноценный SQL/BI слой.
