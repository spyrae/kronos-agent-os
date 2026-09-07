# Исправления ревью от 2026-09-07

Работа ведётся локально, по одному логическому исправлению на коммит. Деплой,
изменение секретов и конфигурации в этот этап не входят.

| ID | Проблема | Статус |
|---|---|---|
| F01 | SSRF: DNS, редиректы, браузерные подзапросы, устаревшая страница | Исправлено локально |
| F02 | Обход подтверждения для MCP/Notion-записей | Исправлено локально |
| F03 | Инструменты недоступны через supervisor | Исправлено локально |
| F04 | Сбой извлечения расходов превращается в пустой результат | Исправлено локально |
| F05 | Частичный успех обработки письма скрывает ошибки | Ожидает |
| F06 | Конкурентные записи теряют изменения BUDGET.md | Ожидает |
| F07 | Ожидание подтверждения ошибочно завершает шаг плана | Ожидает |
| F08 | Прерванный шаг навсегда остаётся running | Ожидает |
| F09 | Результат считается доставленным до успешной отправки | Ожидает |
| F10 | Журнал внешних эффектов допускает повторные операции | Ожидает |
| F11 | Конкурентный resume и агент без живых MCP-инструментов | Ожидает |
| F12 | Обход бюджетного ограничения и потеря force_tier | Ожидает |
| F13 | Сброс памяти не очищает все хранилища | Ожидает |
| F14 | Изменения dashboard не применяются к живому runtime | Ожидает |
| F15 | Синхронные операции блокируют asyncio | Ожидает |
| F16 | Таймаут Codex CLI оставляет дочерний процесс | Ожидает |

## F01 — public-web egress

- Общий HTTP/CONNECT-прокси проверяет все DNS-адреса и соединяется с проверенным
  числовым IP; закрыты обходы через редиректы и подзапросы браузеров.
- Ошибка безопасности не запускает следующий fetch-backend. При неуспешной
  навигации HTML предыдущей страницы не возвращается.
- Поддерживается документированный stealth-адаптер; произвольные команды без
  гарантированного подключения к защите пропускаются.
- Архитектура и ограничения: `docs/decisions/ADR-0002-public-web-egress.md`.
- Проверено: 2001 локальный тест (`pytest -m 'not integration'`), включая 44
  проверки новой границы; Ruff. Для loopback-тестов нужны локальные сокеты.
- Не проверено: живые Chromium/CloakBrowser (не установлены в тестовой среде),
  production. Проверки запуска браузеров используют заглушки.

## F02 — подтверждения MCP-записей

- Названия API-post/patch/put/delete распознаются независимо от разделителей.
- Startup manager, gateway и hot reload назначают локальные признаки
  подтверждения и побочного эффекта. Неизвестные операции требуют подтверждения;
  одной подсказки сервера readOnlyHint недостаточно для обхода.
- Известные имена чтения остаются без лишнего подтверждения. Это классификация
  контрактов доверенных оператору MCP-серверов, а не песочница для вредоносного
  сервера, который намеренно выдаёт запись за get/read.
- Проверено: 98 целевых тестов, включая остановку Notion-записи до выполнения;
  Ruff и отдельная проверка F821. Внешние записи не выполнялись.

## F03 — доступность зарегистрированных инструментов

- Supervisor получает зарегистрированные локальные инструменты без второго
  устаревающего списка имён. MCP-каталоги остаются у специализированных агентов.
- Устранены повторяющиеся имена: зарегистрированный объект сохраняет собственные
  callbacks/metadata вместо подмены встроенным экземпляром.
- Для ставших доступными browser_click/type/evaluate явно требуется подтверждение;
  они исключены из параллельных вызовов как операции с побочным эффектом.
- Проверено: 26 целевых тестов и общий набор — 2036 тестов; Ruff. Тест проходит
  через реальный react_loop и проверяет привязку инструмента к модели и вызов.

## F04 — ошибка извлечения не означает отсутствие расходов

- Только валидный `expenses: []` означает отсутствие расходов. Таймаут, неверный
  JSON, неправильная структура, неполный элемент и NaN/Infinity дают ошибку.
- Ошибка оставляет письмо в нетерминальном состоянии, не архивирует его и не
  останавливает обработку следующих писем. Отчёт показывает необходимость повтора.
- Ошибочные письма выбираются также из журнала, независимо от скользящего окна
  Gmail. Dry-run ничего не записывает. Тексты исключений LLM не попадают в лог.
- Проверено: 61 целевой тест; общий набор — 2047 тестов, 44 integration-теста
  исключены (требуют отдельной среды/сервисов); Ruff. Production не проверялся.

## Изменённые файлы

### F01

- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/kronos/security/public_web.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/kronos/tools/acquire.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/kronos/tools/browser/engine.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/kronos/tools/browser/security.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/scripts/stealth_fetch.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/tests/test_public_web.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/tests/test_acquire.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/tests/test_browser_snapshot.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/tests/test_stealth_fetch.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/docs/decisions/ADR-0002-public-web-egress.md`

### F02

- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/kronos/security/mcp_tools.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/kronos/engine.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/kronos/tools/manager.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/kronos/tools/gateway.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/tests/test_mcp_approvals.py`

### F03

- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/kronos/agents/supervisor.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/kronos/tools/browser/tools.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/tests/test_supervisor_tool_surface.py`

### F04

- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/kronos/cron/expenses/extract.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/kronos/cron/expenses/processor.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/kronos/cron/expenses/ledger.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/tests/test_expenses_extract.py`
- `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/tests/test_expenses_processor.py`

Журнал исправлений: `/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app/docs/reviews/2026-09-07-remediation.md`.

## Как проверить

```sh
cd "/Users/romanbelov/Documents/Projects/Projects/Kronos Agent OS/app"
.venv/bin/python -m pytest -m 'not integration' -q --disable-warnings
.venv/bin/python -m ruff check kronos/ dashboard/ aso/ tests/
```

Тестам public-web нужны временные сокеты на 127.0.0.1; в ограниченной песочнице
для этого необходимо разрешение. Они не обращаются к внешним сайтам.
Следующий пункт — F05: учёт частичного результата письма по отдельным расходам,
чтобы повтор не терял неуспешные позиции и не записывал успешные ещё раз.
