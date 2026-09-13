# Объединение main и исправлений аудита — 2026-09-13

## Объём

- Main до объединения: `42bcd1e`.
- Ветка аудита до объединения: `ff39885`.
- Оба рабочих дерева до начала чистые; незакоммиченные изменения не переносились.
- История обеих веток сохранена обычным merge, без squash/rebase и повторного
  применения F01–F06. Деревья `52efbf1` и squash `5cca79a` совпадают.
- Production, зависимости и private configuration этим шагом не менялись.
  Push/deploy не выполнялись.

## Разрешение пересечений

Единственный файл с текстовым конфликтом — `dashboard/server.py`.
Сохранены оба контракта:

1. `run_dashboard()` возвращает False только при явном disabled outcome;
   app supervision сохраняет bridge/cron/delivery живыми.
2. Отмена включённого Dashboard проходит через `serve_until_cancelled()`:
   uvicorn получает `should_exit`, завершает lifespan, затем отмена передаётся
   вызывающему коду. Нормальное окончание enabled server возвращает True.
3. Неожиданная остановка/исключение сервиса остаётся ошибкой после cleanup;
   явный SIGTERM остаётся штатным завершением приложения.

Дополнительно проверены автоматически объединённые app/bridge/supervisor/engine.
Storage validation вызывается до создания хранилищ; startup recovery больше
не блокирует запуск транспорта; plan/recovery approvals и delivery worker
не вытеснили registry diagnostics и обработку TOPIC_GENERAL.

Без изменения относительно исходного main сохранены:

- `kronos/config.py`, `kronos/swarm_config.py`: storage resolution и registry overlay;
- `kronos/cron/notify.py`, `kronos/cron/scheduler.py`: уведомления и регистрация jobs;
- `scripts/deploy.sh`, `scripts/health-check.sh`: clean-checkout gate, worktree
  `.git` exclude, сохранение private overlay и видимость ошибок доставки;
- env examples, public registry, `.gitignore` и `.dockerignore`.

## Интеграционные тесты

В `tests/test_app_supervision.py` добавлены два реальных локальных uvicorn
сценария: SIGTERM и отмена main. Они проверяют закрытие lifespan, завершение
других сервисов/MCP и отсутствие оставшихся asyncio tasks. Provider, bridge,
scheduler и delivery transport в этих тестах заменены локальными doubles.

Полный прогон выявил зависимость от порядка тестов: вызов swarm demo в том же
Python-процессе оставлял `settings.db_dir` указывающим на удалённый временный
каталог. Новый storage guard корректно отказывал в последующих startup tests.

В `tests/test_cli_swarm.py` добавлена изоляция изменяемых demo settings и
singleton stores. Guard приложения и assertions не ослаблены. До исправления
пара `test_cli_swarm.py` + `test_recovery_startup.py` давала 3 failures / 8 passed;
после исправления — **11 passed**.

## Результаты на объединённом коде

| Проверка | Результат |
|---|---|
| Полный pytest `not integration` | **2526 passed**, 58 deselected, 39.15 s |
| SIGKILL/restart: durable turns, plans, plan delivery, turn delivery | **27 passed**, 26.70 s |
| Фокус: startup/dashboard/config/registry/notify/deploy/graph contracts | **125 passed**, 6.51 s |
| Ruff + отдельный F821 | Без ошибок |
| compileall: kronos/dashboard/aso | Без ошибок |
| Frontend TypeScript / ESLint / Vite build | Пройдены; output только в /tmp |
| git diff --check | Без ошибок |

27 crash tests входят в 58 deselected основного набора и выполнены отдельно;
оставшиеся 31 integration case не запускались. Реальные LLM/MCP/Telegram,
production migrations и Linux/live Codex acceptance этим прогоном не проверены.
Локальные Mem0/Discord/Playwright adapters не установлены — новые dependencies
ради интеграции не добавлялись.

Предупреждения: Starlette/httpx deprecation и frontend CodeMirror chunk >500 kB.
Это не ошибки сборки/тестов. Первый sandbox-прогон без разрешённых loopback
сокетов не прошёл; окончательные network-local checks выполнены с разрешением.

## Повторить проверку

Из корня checkout:

```bash
KAOS_ENV_FILE=/dev/null PYTHONPATH="$PWD" .venv/bin/python -m pytest -m 'not integration' -q
KAOS_ENV_FILE=/dev/null PYTHONPATH="$PWD" .venv/bin/python -m pytest -q \
  tests/test_durable_kill.py tests/test_plan_kill.py \
  tests/test_delivery_kill.py tests/test_turn_delivery_kill.py
.venv/bin/ruff check .
.venv/bin/ruff check --select F821 .
.venv/bin/python -m compileall -q kronos dashboard aso
cd dashboard-ui
./node_modules/.bin/tsc -b --pretty false
./node_modules/.bin/eslint .
./node_modules/.bin/vite build --outDir /tmp/kaos-integration-ui
```

В worktree без собственного venv использовать существующий соседний
`../app/.venv/bin/python`, оставляя PYTHONPATH текущего checkout.

## Дальнейшая работа

Следующий локальный блок — A02: доверенная user scope для session search/KG,
миграции и отрицательные cross-user tests. Legacy-данные нельзя автоматически
приписывать пользователю, который первым обратился после обновления.

F13 reset/background writers, полная durable budget accounting, reconciliation
и прочие незавершённые пункты остаются в общем реестре. Production permissions,
runtime identity, env-source, approval enablement и rollout согласуются
отдельно; объединение веток само по себе их не исправляет.
