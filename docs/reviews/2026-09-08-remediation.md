# Исправление двух аудитов — общий реестр

Цель: устранить все ошибки, риски и недоработки из локального и production-аудитов,
не подменяя проверку на сервере локальными тестами. Полные production-доказательства
хранятся в приватном артефакте задачи, не в публичном репозитории.

## Правила завершения

- «Локально» означает только проверенный код, не закрытый production-пункт.
- Для изменения схемы нужна миграция и проверка на старой схеме/копии данных.
- Для production-пункта нужны фактическая конфигурация, контролируемый rollout и
  проверка соответствующего поведения. Зелёный health сам по себе недостаточен.
- Новые зависимости и изменения эксплуатационной конфигурации согласуются отдельно.
- Каждый логический фикс — атомарный коммит. Не удалять неизвестные данные и
  не повторять внешнюю операцию с неопределённым результатом.

## Локальный аудит

| ID | Требование / критерий приёмки | Код | Production |
|---|---|---|---|
| F01 | Public-web DNS/redirect/subrequest защита; live browser smoke | Перенесён, regression пройден | Код подтверждён хэшами в 10:37–10:44 UTC; live smoke ещё нужен |
| F02 | MCP writes/unknown требуют approval; проверки до эффекта | Перенесён, regression пройден | Код присутствует; approvals выключены во всех пяти initial env |
| F03 | Зарегистрированные capabilities достижимы через supervisor | Перенесён, regression пройден | Код присутствует; сквозная приёмка не выполнена |
| F04 | Ошибка extraction сохраняется для retry, не считается empty | Перенесён, regression пройден | Код присутствует; live приёмка и историческая сверка отдельно |
| F05 | Per-item ledger; partial/unknown outcomes не теряются и не дублируются | Перенесён, regression пройден | Код присутствует; таблицы email_expense_items в 10:44 ещё нет; нужны инициализация и сверка |
| F06 | Межпроцессная сериализация всех budget writers | Перенесён, regression пройден | Код присутствует; live приёмка не выполнена |
| F07 | Approval wait не завершает шаг; approve/reject/restart согласованы | Исправлено и проверено локально | Нужны миграции, rollout и Telegram smoke |
| F08 | Отмена/падение шага восстанавливаются без слепого повтора эффектов | В работе: claim/link recovery, live cancel/TTL и fenced cleanup проверены; operator reconciliation и гарантированное stop-уведомление ещё нужны | Ожидает |
| F09 | Generated/pending/delivered раздельны; сбой доставки повторяется | В работе: transactional outbox планов и восстановленных session turns проверен; обычные ответы, остальные producers и operator repair ещё нужны | Ожидает |
| F10 | Durable intent/idempotency/reconciliation; journal errors fail closed | В работе: intent/journal boundary проверен; полный перечень путей и reconciliation не закрыты | Ожидает |
| F11 | Один resume на turn; live registry и atomic ownership | Исправлено и проверено локально | Нужен согласованный rollout без старых исполнителей |
| F12 | Общая бюджетная проверка и рабочий downgrade всех model paths | В работе: factory, прямые ASO/GEO/Vision/scripts/Whisper и Mem0 boundary проверены с fake transports; реальный Mem0 1.0.7, durable session ledger, reservations и unknown outcomes ещё нужны | Ожидает |
| F13 | Честный scoped reset по всем слоям, включая background writers | Ожидает | Ожидает |
| F14 | Desired/effective runtime settings совпадают или restart явно указан | Ожидает | Ожидает |
| F15 | Model/memory I/O не блокирует event loop; responsiveness test | Ожидает | Ожидает |
| F16 | Timeout/cancel Codex CLI завершает процесс и потомков, очищает ресурсы | Основной llm_codex и отдельный Vision path используют общий проверенный process cleanup | Ожидает Linux/live Codex-приёмки и rollout |
| F17 | Необязательный Dashboard без доступного пароля не выключает bridge/cron; crash возвращает failure | Исправлено локально: explicit disabled outcome и проверенный service supervision | Ожидает rollout; текущие Dashboard работают |

## Production-аудит

| ID | Требование / критерий приёмки | Статус |
|---|---|---|
| PROD-01 | Уникальные Telegram session paths и аккаунты; нет новых locked errors | В снимке 10:37–10:44 UTC пути уникальны, новых locked после старта нет; account identity/Telegram smoke ещё нужны |
| PROD-02 | Раздельные session DB, cron-state/log paths; сохранена история | Runtime-пути DB/cron раздельны после старта 10:36; историческая сверка старой общей БД ещё нужна |
| PROD-03 | Непривилегированный пользователь не читает sessions/DB; безопасный umask | Запрошено согласование прав; shared ledger учесть отдельно |
| PROD-04 | Runtime отделён от admin/deploy; минимальные sudo/Docker/systemd права | Ожидает проекта и согласования |
| PROD-05 | Защитные контракты реально работают на сервере; effective approvals проверены | F01–F06 на диске подтверждены, expense schema/E2E ещё нет; F07–F11 не развёрнуты |
| PROD-06 | Запас диска ниже alert-порога; безопасная очистка и retention | Ожидает согласованного плана, данные не удалены |
| PROD-07 | Полный encrypted off-host backup и успешный isolated restore drill | Ожидает; не закрывать по одному успешному workspace job |
| PROD-08 | Readiness всего swarm и рабочая OnFailure/доставка alerts | Ожидает |
| PROD-09 | Согласованный ingress/key-only SSH; соседние сервисы не повреждены | Нужны отдельные согласования host/network изменений |
| PROD-10 | Согласованный release manifest и loaded build identity | F01–F06 и прежние production-only изменения вошли в main; manifest/loaded identity отсутствуют |
| PROD-11 | Основной агент выбирает собственный dotenv, не чужие memory/config paths | Новый пункт повторного аудита: неверный explicit env-source; ожидает согласования конфигурации |
| PROD-12 | Registry usernames соответствуют Telegram; @адресация проверена для каждого агента | Четыре startup-предупреждения registry mismatch в снимке 12:40 UTC; live routing smoke ещё нужен |

## Дополнительные пункты обоих аудитов — входят в цель

| ID | Требование / критерий приёмки | Статус |
|---|---|---|
| A01 | Discord owner allowlist, безопасные DM и границы контекста | Ожидает |
| A02 | User-scoped KG/session search без раскрытия чужой памяти | Ожидает |
| A03 | Browser screenshot доходит до модели полноценным изображением | Ожидает |
| A04 | Полное резервирование вместо одного workspace | См. PROD-07; не исключено из цели |
| A05 | FTS/shared memory доступна без обязательного DeepSeek key | Ожидает |
| A06 | Сквозные chat→tool→approval→effect→delivery и fault-injection gates | Ожидает |
| V01 | Актуальные CVE: installed Python, frontend и относящиеся к сервису OS packages | Передача inventory в OSV требует ранее запрошенного разрешения |
| V02 | Причина reboot-required/OS updates проверена; безопасное решение по reboot | Список содержит libc6/linux-base и более новые kernel packages; CVE/maintenance window ещё не определены, reboot не разрешён автоматически |
| V03 | Сверены старые approvals/исторические неопределённые расходы | Ожидает; не выполнять автоматически |

## Выполнено в объединённой ветке

### Дополнение production-аудита 12:33–12:40 UTC

- Все 383 проверенных хэша сервера совпали с 11:26; исправления отдельной
  remediation-ветки по-прежнему нельзя считать развёрнутыми.
- 2083 regression и 5 SIGKILL/restart тестов на неизменяемом main f31f283 прошли.
  Восемь отрицательных fault probes повторно подтвердили F07–F13/F16;
  дополнительные probes подтвердили A01, A03, блокировку event loop F15 и F17.
- F17: real main/run_dashboard с fake bridge/MCP завершают весь процесс без
  ошибки при отсутствии доступного Dashboard password. Это условный startup
  дефект; пять живых Dashboard в снимке работали.
- PROD-12: Kronos, Impulse, Lacuna, Resonant при текущем старте сообщили
  `Agent registry out of sync`. Не подменять проверку реальной адресации
  наличием startup-предупреждения или исправлением локального registry.
- Discord disabled у всех пяти по startup-журналам; A01 остаётся незакрытой
  границей для включённой интеграции, не доказанной текущей live-экспозицией.
- Диск 92%, свободно 6.23 GiB. Backup job успешен 12:02:34 UTC, но по-прежнему
  покрывает один workspace; полный restore не проверен. Три pending approvals
  подтверждены повторно; автоматическое исполнение/отмена не разрешены аудитом.
- Новые пункты входят в общую цель. Код и production этим аудитом не менялись;
  полный отчёт и доказательства остаются приватными артефактами задачи.

2026-09-08:

- Новый worktree от main `7964a8a`, существующие main и старая ветка сохранены.
- Сохранены три production-only изменения: конверсия расходов, расширенный weekly
  digest и внутренний source-quality report. Коммиты: `9d5508e`, `379f801`, `43b1244`.
- F01–F06 перенесены отдельными коммитами с исходной атрибуцией:
  `d64d7b6`, `a7b6bd9`, `67ad7bf`, `03c9eb7`, `e267f3f`, `52efbf1`.
- На объединённом коде: **2071 passed, 44 integration deselected**, 31.50 sec.
  Запуск с `KAOS_ENV_FILE=/dev/null`, импорт проверен из нового worktree, API не вызваны.
- Production повторно проверен: конфликт session paths остаётся. Никаких
  production-изменений/перезапусков этим этапом не сделано.

Исторические проверки F01–F06 подробно описаны в `2026-09-07-remediation.md`;
их прежние статусы относятся к старой локальной ветке, а не к rollout.

### F16 — владение процессом CLI

- Вызов запускается в отдельной POSIX process group. Timeout/cancel сначала
  отправляет TERM, затем KILL для оставшихся потомков, дожидается процесса и
  завершает чтение pipes перед удалением output-файла. Sync-путь также очищает дерево.
- Отмена во время запуска не теряет process handle; повторная отмена не обрывает
  cleanup. Даже уже вышедший leader не скрывает оставшихся потомков.
- 12 целевых тестов прошли, включая реальные локальные процессы: timeout,
  exited leader + живой child, повторная отмена, отмена во время spawn,
  ошибка spawn, успешный ответ, сохранность независимого процесса.
- Полный локальный набор: **2079 passed, 44 integration deselected**, 28.16 sec;
  Ruff, отдельный F821 и `git diff --check` без ошибок.
- Изменены `kronos/llm_codex.py`, `tests/test_llm_codex.py`, добавлен
  `tests/test_llm_codex_cleanup.py`. Codex и реальные провайдеры не запускались.
- Проверенная граница — POSIX process group (production Linux и локальный macOS),
  не sandbox против программы, намеренно отделяющейся через новую сессию. Windows
  имеет cleanup непосредственного процесса, но не проверялся как production target.

### F07 — этап 1: явный результат хода

- Добавлены immutable `InvocationOutcome`, `ainvoke_outcome` и чтение результата
  по `turn_id`. Старый `ainvoke` по-прежнему возвращает строку.
- Ошибка модели, circuit breaker и лимит итераций теперь имеют машинный статус
  ошибки, а не только непустой текст. Approval wait и input block также отличимы.
- Миграция сохраняет `final_content` атомарно с завершением хода. Старые строки
  без результата остаются unknown; чужая/сжатая история не используется как ответ.
- `on_turn_started` позволяет сохранить связь шага с ходом до model/tool calls;
  ошибка записи связи останавливает выполнение. Ожидающие approval ходы исключены
  из retention, чтобы не терять незавершённое состояние.
- 15 новых тестов: approve/reject, перезапуск SessionStore, конкурентные approvals,
  TTL, ошибки, ссылка до эффекта, сохранность результата после очистки истории,
  конкурентная идемпотентная миграция старой схемы. Внешние операции — mocks.
- Полный набор: **2094 passed, 44 integration deselected**, 28.00 sec; exit 0.
  Ruff, F821 и diff-check чистые. ADR-0005 фиксирует контракт и ограничения.
- **F07 ещё не закрыт:** poller пока использует строковый API. Следующий этап —
  миграция plan↔turn, ожидание/сверка approval и доставка кнопок владельцу.

### F07 — этап 2: планировщик и подтверждения

- `plan_steps` через миграцию получает `turn_id`, `approval_id` и подтверждение
  доставки. Claim атомарный; связь записывается до model/tool calls. Новый
  `awaiting_approval` не завершает шаг и блокирует другие ходы этого плана.
- Poller сверяет конкретный durable turn, включая окно падения между созданием
  approval и сохранением паузы. Approve ждёт завершения continuation; Reject/TTL
  завершают шаг ошибкой, не повторным запросом. Следующий approval уведомляет снова.
- Неотправленная approval-нотификация повторяется без повторения операции.
  Для userbot добавлены `/approve <id>` / `/reject <id>`, для bot — также кнопки.
  Проверяются явный owner allowlist и сохранённые chat/topic. ALLOW_ALL_USERS не
  даёт права подтверждения. Аргументы берутся из durable approval, не webhook.
- Отменённый/истёкший/несвязанный план не возобновляется через старый approval.
  Паузы исключены из ready queue, поэтому не отнимают слоты у других планов.
- Неопределённый результат, пустой ответ и ошибка после старта хода переводятся
  в `needs_review`, не в автоматический новый turn с новым idempotency scope.
- Конкурентный cold-start тест обнаружил прежнюю гонку SQLite WAL initialization:
  добавлена проверка режима и ограниченное ожидание только SQLITE_BUSY. 10 новых
  БД × 4 одновременно открываемых SessionStore сохраняют все ходы.
- **79 целевых тестов прошли. Полный набор: 2112 passed, 44 integration deselected,
  1 warning, 27.96 sec; exit 0.** Ruff, отдельный F821 и diff-check чистые.
  F07 добавил суммарно 33 тестовых случая относительно предыдущего baseline.
- Production не изменён; Telegram, модели и внешние эффекты в тестах заменены
  mocks. Реальная доставка и rollout остаются самостоятельными проверками.

Изменённые файлы F07:

- `kronos/engine.py`, `kronos/graph.py`, `kronos/outcomes.py`: явный execution API.
- `kronos/session.py`, `kronos/migrations/__init__.py`,
  `kronos/migrations/v001_turn_outcome.py`: долговечный результат и миграция.
- `kronos/plans.py`, `kronos/cron/plans.py`,
  `kronos/migrations/v002_plan_turns.py`: состояния/связи/сверка шагов.
- `kronos/bridge.py`, `kronos/bridge_plan_approval.py`: доставка и авторизация решений.
- `tests/test_invocation_outcomes.py`, `tests/test_plan_poller.py`: regression.
- `docs/decisions/ADR-0005-durable-invocation-outcomes.md`,
  `docs/decisions/ADR-0006-plan-approval-reconciliation.md`, `docs/decisions/README.md`
  и этот реестр: решения, границы и доказательства.

Проверка: из worktree с существующим venv выполнить
`KAOS_ENV_FILE=/dev/null PYTHONPATH="$PWD" ../app/.venv/bin/python -m pytest -m 'not integration' -q`.
Точечно — `tests/test_invocation_outcomes.py tests/test_plan_poller.py tests/test_plans.py`.

Оставшиеся границы **не исключаются из цели**:

- F08: lifecycle/ownership отменённого или упавшего live turn, legacy running без
  связи, workflow для needs_review, уведомление об остановке, согласованное
  завершение/retention истёкших pending approvals, корректность повторного park.
- F09: настоящий outbox summary/progress/resume; approval prompt допускает повтор
  при падении после фактической отправки, но до сохранения acknowledgement.
- F10/F11: intent/effect reconciliation и единственный владелец resume. Локальный
  статус completed означает завершение engine, а не доказательство всех заявлений
  модели или exactly-once внешней операции.

### F10 — этап 1: намерение до внешнего действия (зависимость F08)

- Миграция `v003_effect_intents` добавляет запись намерения: turn, tool, frozen args,
  fingerprint, call id, owner token. Она коммитится **до** вызова инструмента.
  Результат и закрытие намерения коммитятся вместе, только владельцем токена.
- Таймаут, отмена, исключение и ошибка записи после dispatch не разрешают повтор.
  Pending intent блокирует следующие мутации этого turn и ту же операцию в другом
  turn. Старые записанные результаты сохраняются; legacy writer не обходит token.
- Неопределённый intent нельзя удалить retention-ом или выдать за completed.
  Resume проверяет его до вызова модели, поэтому даже ответ-заглушка «всё отправлено»
  не превращает неизвестный исход в успех. CLI/API показывают pending отдельно.
- Ошибки message journal и tool cache больше не проглатываются. Это останавливает
  цепочку до следующих эффектов, сохраняя уже записанные результаты для сверки.
- Ledger подключён к прямому Approve и наследуется вложенными ReAct loops, включая
  custom signatures. Контекст очищается после tool call; durable errors не
  превращаются в обычную строку ответа делегата.
- Известные mutating built-ins защищаются и без side_effect metadata; выключенный
  флаг approval не выключает ledger. MCP использует классификацию F02. Ephemeral
  engine-путь без durable ledger не может выполнять мутацию.
- Evals по-прежнему используют stub tools/model, но теперь с временным SQLite
  ledger вместо обхода защитного контракта. Новых зависимостей/конфигурации нет.
- Одинаковые args с новым call id неоднозначны: это может быть второй настоящий
  расход, а не retry. Такой случай **не схлопывается молча** в один успешный вызов:
  нужна сверка намерения. Явный business idempotency key разрешает переиспользование
  результата при regenerated call id. Полноценная поддержка двух намеренных
  одинаковых действий остаётся обязательным продолжением F10, не закрытым пунктом.
- Проверено на финальном коде: **2131 passed, 45 integration deselected**, 24.44 sec.
  Отдельно **6 integration SIGKILL/restart тестов прошли**, 4.02 sec, включая новое
  окно «файл уже изменён, effect result ещё не закоммичен». Два новых процесса
  не повторили действие и не отправили ложное подтверждение. Оставшиеся 39
  integration cases не запускались; внешние API/production не вызывались.
- Ruff, F821, `git diff --check` — чистые. 19 новых unit cases и новый SIGKILL case.

Изменённые файлы этапа:

- `kronos/effect_state.py`, `kronos/migrations/v003_effect_intents.py`,
  `kronos/session.py`: intent protocol, fencing, inspection/retention guards.
- `kronos/engine.py`, `kronos/graph.py`, `kronos/agents/supervisor.py`,
  `kronos/security/effects.py`: execution boundary, approval/nested propagation.
- `kronos/cli.py`, `kronos/evals/runner.py`: статусы и hermetic eval compatibility.
- `tests/test_effect_intents.py`, `tests/test_external_effects.py`,
  `tests/test_durable_turns.py`, `tests/test_durable_kill.py`,
  `tests/helpers/durable_crash.py`, `tests/test_engine.py`,
  `tests/test_engine_parallel.py`, `tests/test_subagent_approval.py`,
  `tests/test_turns_cli.py`: fault injection и обновлённый безопасный контракт.
- ADR-0007, индекс ADR и этот реестр: решение и ограничения.

Проверка: команды полного regression выше; отдельно
`KAOS_ENV_FILE=/dev/null PYTHONPATH="$PWD" ../app/.venv/bin/python -m pytest tests/test_durable_kill.py -q`.

**F08/F10/F11 ещё не закрыты. До rollout обязательны:**

1. Lease/heartbeat и fencing живого turn, не только записи effect result;
   восстановление plan step и обработка CancelledError/legacy orphans.
2. Operator reconciliation с доказательством остановки исполнителя, upstream
   idempotency там, где API её поддерживает, и distinct logical operation ids.
   В том числе не делать автоматический replay неоднозначных одинаковых действий.
3. Инвентаризация путей **в обход engine**: custom pipelines, прямые `.ainvoke()`
   инструментов, внутренние API/cron writers. Например, knowledge_pipeline пишет
   файлы/память напрямую; новая защита не покрывает это автоматически.
4. Проверить retention глобальных business keys: старый finished-turn prune
   удаляет recorded effects; нельзя обещать бессрочную дедупликацию такого ключа.
5. F09 delivery/outbox, production-права/backup/migrations и live verification.
   Этот этап не разрешает деплой и не доказывает exactly-once у внешнего провайдера.

### F11 — единственный исполнитель беседы и live resume

- Общая POSIX-блокировка удерживается от загрузки истории до окончания durable
  invocation, approval continuation или resume. Область — canonical SQLite path
  + conversation id: это защищает также новый вопрос от перезаписи истории старого.
  Блокировка не истекает по таймеру и не зависит от heartbeat/event loop.
- Новый `claim_turn_for_resume` работает для `running` и `resuming` под ownership
  и SQL-транзакцией. Проверяет актуальные thread/input, pending approval, newer turn
  и attempts cap. Снимок, присланный caller, больше не даёт права исполнения.
  Старый batch claim без живого владельца удалён, все callers переведены.
- Startup report и first-invocation recovery не трогают занятые беседы. Повторный
  report не дублирует историю; падение во время resume не оставляет навечно занятый
  `resuming`. Ожидающий approval не обходится даже при ошибочном running-флаге.
- Dashboard использует agent конкретного приложения с его живыми tools/MCP;
  отдельный fallback agent больше не создаётся. Без runtime — 503, при занятом
  исполнителе — 409. API/UI различают completed, waiting_approval и другие outcomes.
  CLI открывает/закрывает managed MCP registry и корректно сообщает отказ busy.
- Отмена освобождает execution ownership, но **не** снимает F10 pending intent.
  Неизвестный исход операции по-прежнему блокирует resume до model/tool calls.
  `resume_abandoned_turns` больше не считает непустой текст ошибки успехом.
- Sidecar-файлы имеют хешированные имена без текста беседы, создаются с 0600
  в директории 0700 и не удаляются после unlock. Нельзя удалять/пересоздавать
  эту директорию при живых исполнителях; это заменило бы inode блокировки.
- Решение и альтернативы описаны в ADR-0008. TTL lease отвергнут: его истечение
  не доказывает остановку внешнего запроса старого исполнителя. Схема БД,
  зависимости и эксплуатационная конфигурация этим этапом не менялись.

Проверка на финальном коде:

- **2149 passed, 47 integration deselected, 1 warning; 32.78 sec**, exit 0.
- Отдельно **8 integration SIGKILL/restart passed; 7.78 sec**, exit 0. Два новых
  сценария держат реальный процесс с намеренно заблокированным event loop в
  `running` и `resuming`: другие процессы не присваивают и не переписывают ход.
  После SIGKILL тот же ход завершается с правильным attempts count.
- 18 новых unit/ASGI/CLI cases: две независимые SessionStore, гонка resume с
  live invocation/approval/новым вопросом, отмена owner/waiter, неподходящий и
  освобождённый ownership, path alias, lock I/O failure, stale caller snapshot,
  dashboard с реальным mock tool registry, отсутствие fallback, MCP cleanup.
- Ruff, отдельный F821, `git diff --check` — чистые. TypeScript `--noEmit` пройден
  для app и Vite configs на временной копии UI с уже установленными зависимостями
  из основного checkout, без установки пакетов и изменения исходного checkout.
- Первый запуск общего набора в sandbox: 15 отказов на ограничения локальных
  socket/process операций, 2116 passed. Приведённый выше итог — повторный полный
  запуск с разрешением этих локальных тестов, не игнорирование проваленных cases.
- Оставшиеся **39 integration cases не запускались**; настоящие API, Telegram,
  MCP-серверы и production не вызывались. Production не менялся.

Изменённые файлы F11:

- `kronos/turn_ownership.py`, `kronos/session.py`, `kronos/graph.py`: ownership,
  единый guarded execution API, atomic single-turn claim и безопасный report.
- `dashboard/server.py`, `dashboard/api/turns.py`,
  `dashboard-ui/src/pages/TurnsPage.tsx`, `kronos/cli.py`: live registry и outcomes.
- `tests/test_turn_ownership.py`, `tests/test_durable_resume.py`,
  `tests/test_dashboard_turns.py`, `tests/test_turns_cli.py`,
  `tests/test_durable_kill.py`, `tests/helpers/durable_crash.py`: regression gates.
- `docs/decisions/ADR-0008-conversation-execution-ownership.md`, индекс и реестр.

Проверить локально: команды полного pytest/Ruff выше; отдельно
`KAOS_ENV_FILE=/dev/null PYTHONPATH="$PWD" ../app/.venv/bin/python -m pytest tests/test_turn_ownership.py tests/test_durable_kill.py -q`.

Границы не исключаются из цели:

- F11 локально закрывает кооперативное однохостовое владение через agent API, не
  multi-host/NFS/Windows или произвольный код с прямым доступом к SQLite. Rollout
  требует остановить старые исполнители, которые ещё не используют этот протокол.
- F08 всё ещё требует lifecycle/recovery плановых шагов, legacy orphan handling,
  park/release, cancel/expiry notifications и operator workflow. При восстановлении
  также нужно проверить валидность journal→model сообщений с tool-call без ответа:
  scripted model в regression не доказывает принятие такой истории провайдером.
- F10: distinct logical operation ids, reconciliation неизвестных эффектов,
  provider idempotency, прямые custom/cron writers, retention business keys остаются.
- F09: transport readiness и durable outbox; F13: scope reset/background writers.
  Сериализация выполнения не доказывает exactly-once внешней операции или доставки.

### F08/F10 — восстановление исходного tool batch и граница legacy-данных

- Resume больше не отправляет модели незавершённый assistant→tool протокол.
  Сначала восстанавливает только недостающие ответы последнего batch, с исходными
  call id/аргументами. Уже записанные sibling results не повторяются, исходный
  AIMessage не дублируется. Replay идёт последовательно и не расходует лишний
  model-call iteration.
- Результат берётся из tool cache либо recorded effect, совпадающего по turn,
  call id, tool и frozen args. Это работает даже после удаления tool из registry.
  Для legacy сохраняется чтение по известному idempotency key. Положительный
  результат восстанавливается до approval, поэтому выполненная операция не
  запрашивает повторного подтверждения. Untrusted framing сохраняется.
- Новый вызов проходит прежние approval/intent guards. Pending intent по-прежнему
  запрещает resume до модели. Незавершённые delegates/custom pipelines без
  доказанного replay-контракта не перезапускаются целиком: нужна работа по
  child journal/reconciliation. Готовый cached parent result восстановить можно.
- Миграция `v004_effect_protocol` оставляет старым ходам версию 0; `begin_turn`
  нового runtime атомарно записывает 1. Отсутствие intent у старого хода **не**
  даёт права на новую мутацию. Legacy recorded results и read-only вызовы доступны.
  API/CLI показывают версию, чтобы оператор видел эту границу.
- Повреждённые journal rows больше не пропускаются. Непарные/повторные tool results,
  duplicate call ids, смена аргументов под прежним id, не-JSON args и незавершённый
  batch перед следующим сообщением останавливают продолжение. Исходный journal
  остаётся для проверки; ошибка не становится успешным результатом.
- Report-only recovery закрывает протокольные слоты в conversation history
  явным `NO VERIFIED RESULT`, не выдуманным success/failure. Такой placeholder
  не записывается в execution journal или tool cache и не разрешает replay.
  При повреждённом journal прежняя история не перезаписывается.
- Обрезка completed history по MAX_HISTORY больше не оставляет в начале хвост
  ToolMessage без исходного запроса. Старый обрезанный prefix пропускается при
  чтении без изменения его SQLite-источника. Активный journal не обрезается и
  не «чинится» пропуском неоднозначных записей.

Проверки финального кода:

- **2184 passed, 49 integration deselected, 1 warning; 31.10 sec**, exit 0.
- Отдельно **10 integration SIGKILL/restart passed; 7.72 sec**, exit 0. Два новых
  сценария убивают процесс после записи tool request до dispatch и после effect
  result commit до tool cache. Новый процесс завершает исходный batch и оставляет
  ровно одну реальную строку side effect. Mock model в crash helper теперь строго
  отвергает неполную историю: старые положительные тесты тоже стали сильнее.
- **35 новых unit/adapter cases**: partial batch, cache/ledger, удалённый tool,
  повторный approval, legacy protocol, concurrent migration, malformed journal,
  changed call identity, approval/deferred batch continuation, untrusted framing,
  отказ blind custom replay, границы trimming и report-only без ложных результатов.
- **ChatOpenAI и ChatDeepSeek** запускались с их настоящей сериализацией и
  `httpx.MockTransport`: проверяется JSON HTTP request с полными tool-call/result
  парами. Это проверка адаптеров, **не** вызов настоящего провайдера.
- Ruff, отдельный F821 и `git diff --check` чистые. Зависимости и эксплуатационная
  конфигурация не менялись. Остальные **39 integration cases не запускались**.
- Первый общий sandbox-запуск выявил 15 ограничений socket/process тестов и 5
  fixtures, вручную создававших legacy rows для проверки новых эффектов. Эти
  fixtures явно переведены на новый protocol; запрет legacy replay покрыт
  отдельными новыми тестами. Финальный полный набор запущен с разрешением
  локальных socket/process проверок — ошибки не скрыты исключением тестов.

Изменённые файлы этапа:

- `kronos/tool_history.py`, `kronos/engine.py`, `kronos/graph.py`: pairing validation,
  guarded replay исходного batch и восстановление recorded results.
- `kronos/session.py`, `kronos/migrations/v004_effect_protocol.py`: строгий журнал,
  protocol migration, result lookup, report/history boundaries.
- `kronos/cli.py`: отображение legacy protocol.
- `tests/test_durable_tool_replay.py`, `tests/test_external_effects.py`,
  `tests/test_turn_ownership.py`, `tests/test_durable_kill.py`,
  `tests/helpers/durable_crash.py`: новые/усиленные regression gates.
- ADR-0009, индекс ADR и этот реестр: контракт, альтернативы и границы.

Повторная проверка из worktree:

```sh
KAOS_ENV_FILE=/dev/null PYTHONPATH="$PWD" ../app/.venv/bin/python -m pytest -m 'not integration' -q
KAOS_ENV_FILE=/dev/null PYTHONPATH="$PWD" ../app/.venv/bin/python -m pytest tests/test_durable_kill.py -q
../app/.venv/bin/ruff check kronos/ dashboard/ aso/ tests/
```

**F08/F10 остаются открытыми**, production не изменён. Обязательные продолжения:

1. Lifecycle plan step: claim/link crash window, cancellation/expiry, park/release,
   orphan recovery и уведомление владельца. Сейчас poller ещё может оставить
   running step без turn или бесконечно наблюдать остановившийся running turn.
2. Полный operator workflow, включая legacy и неоднозначные действия; поддержка
   child-level continuation для custom/delegated pipelines, не вечный запрет replay.
3. Не удалять evidence до завершения review: текущий finished-turn retention
   защищает pending intents, но ещё не все failed/corrupt reviews и глобальные
   business keys. Этот риск остаётся частью F08/F10/V03.
4. Инвентаризация прямых/custom writers вне engine, distinct logical operation ids,
   provider idempotency, F09 transport readiness/outbox и live rollout verification.

### F08 — plan-owned recovery and park/release lifecycle

- Conversation ownership is acquired before claiming a step and lent to the
  runtime as a task-bound capability. A live or lock-waiting executor cannot be
  mistaken for an abandoned step. Cancelled tasks propagate cancellation and
  leave recovery to a later lock owner.
- Migration v005 correlates a plan execution with turn creation across the two
  SQLite databases. The unique caller key survives pre-turn retries, including
  a delayed commit. Missing reverse links are repaired; legacy unlinked work
  requires review. Caller-owned identities are protected from generic retention.
- Generic startup recovery leaves caller-owned/plan turns to the poller. Report
  mode exposes an interrupted step for explicit same-turn continuation; resume
  mode uses the existing intent/journal protocol. Recovery and fresh execution
  share the cycle budget; a plan cannot take two execution slots in that cycle.
- Park is a persisted request while execution is live. Completion commits the
  result and the waiting transition atomically, preserving the prior turn id.
  Stale polling/condition results cannot reclassify or repark completed/released
  work. Legacy linked waits survive approval/interruption. Corrupt conditions
  require review. API/CLI report only actual successful releases.
- Plans UI exposes current/last turn, repark intent and explicit continuation of
  an interrupted turn. It never turns a failed resume response into a success.

Changed files:

- `kronos/plans.py`, `kronos/cron/plans.py`: claim ownership, repair, policy, cycle
  quotas and race-safe waiting lifecycle.
- `kronos/graph.py`, `kronos/session.py`,
  `kronos/migrations/v005_plan_execution.py`: borrowed ownership, durable caller
  identity, generic-recovery separation and retention guard.
- `dashboard/api/plans.py`, `kronos/cli.py`,
  `dashboard-ui/src/pages/PlansPage.tsx`: truthful releases and visible same-turn
  recovery, without new dependencies or configuration changes.
- `tests/test_plan_execution_recovery.py`, `tests/test_plan_poller.py`,
  `tests/test_dashboard_plans.py`: lifecycle, concurrency, migration and UI/API
  contract regressions using temporary databases and mock model/tools.
- `tests/helpers/plan_crash.py`, `tests/test_plan_kill.py`: four real SIGKILL windows,
  fresh-process recovery, one turn identity, repeat recovery and SQLite checks.
- `docs/decisions/ADR-0010-plan-execution-recovery.md`, ADR index and this tracker:
  architecture, alternatives, evidence and remaining scope.

Still required for F08: live cancel/expiry fencing between model/tool boundaries,
owner notifications, safe expiry/approval cleanup, operator effect reconciliation,
and bounded archive/tombstone retention. A started external request cannot be
promised undone by cancellation. F09 delivery outbox and F10 child/direct pipeline
coverage remain open. Production has not received this code or its migrations.

Verification on final code:

- `KAOS_ENV_FILE=/dev/null PYTHONPATH="$PWD" ../app/.venv/bin/python -m pytest -m 'not integration' -q --disable-warnings`
  — **2212 passed, 53 deselected, 1 warning**, 28.36 s, exit 0.
- `KAOS_ENV_FILE=/dev/null PYTHONPATH="$PWD" ../app/.venv/bin/python -m pytest tests/test_plan_kill.py tests/test_durable_kill.py -q --disable-warnings`
  — **14 passed**, 13.47 s, exit 0. The other 39 integration tests (external services)
  were not run. No production APIs, Telegram or real model providers were invoked.
- Ruff for Python, separate F821 and `git diff --check`: clean. TypeScript build
  checks and PlansPage ESLint passed in a temporary copy with the existing main
  node_modules; no install, dependency or configuration change.
- Local Python is 3.13; production Python 3.12 has not been used for these tests.
  Four process-loss cases and 28 unit/regression cases were added versus the
  previous committed baseline. These do not close full end-to-end acceptance.

### Production — повторное подтверждение 10:37–10:44 UTC

- Пять служб запущены в 10:36:17 UTC не этим аудитом. Уникальны Telegram sessions,
  исправлены фактические session DB и cron-state Lacuna/Resonant. Старый общий
  transcript не удалён; доступность прежнего контекста каждому агенту не подтверждена.
- F01–F06 теперь совпадают с main `5cca79a` по хэшам. Таблица `email_expense_items`
  ещё отсутствует: инициализация ленивой миграции и pipeline smoke не проверены.
- Сохраняются права чтения runtime DB/session от nobody, sudo/Docker privileges,
  диск 92%, неполный backup, неверный env-source Kronos и отсутствие build identity.
- 50/50 SQLite quick_check ok; 10 health 200, 15 protected GET 401. Это не E2E.
  Полный immutable main suite: 2071 passed; 5 SIGKILL tests passed; static/UI checks ok.
  Повторные fault-probes подтверждают оставшиеся F07–F13/F16, не исправность сценариев.
- Полный экспорт production-исходников заблокирован авто-проверкой. Хэши имеющихся
  локальных файлов дали 382 совпадения; полный diff оставшегося swarm_config.py
  требует ранее запрошенного отдельного разрешения. Экспорт не обходился.
- Приватный отчёт задачи: `PRODUCTION-AUDIT-2026-09-08-1044.md`, evidence JSONL,
  source provenance и логи тестов. Этот проход не менял production/main/remediation.

### F08 — live cancel/TTL и безопасное завершение остановки

- `stop_reason` и `stop_reconciled` добавлены миграцией v006: запрос остановки
  отделён от освобождения живого исполнителя и сверки его результата. Поздний TTL
  обычного failed-плана не превращает его в новую операцию остановки.
- Fresh invocation, resume и прямой/delegated approval получают revocable scope.
  Он наследуется вложенными ReAct calls, provider fallback и custom pipelines;
  проверки выполняются перед model/tool вызовом и внутри отложенного tool task.
  `plan:` без durable linkage больше не запускает даже memory/embedding retrieval.
- Подтверждённый результат уже начатого эффекта коммитится независимо от stop;
  следующая операция не начинается после обнаружения отзыва. Неопределённый
  эффект/legacy execution/неизвестный статус остаются needs_review без replay.
  Pending intent до dispatch — консервативная неопределённость, не доказательство
  фактически совершённой внешней операции.
- Stop cleanup получает ту же conversation ownership. Не переписывает live step,
  не закрывает его approval, не считает worker остановленным по CancelledError.
  Plan memory retrieval/persistence/compaction выполняются в worker с копией
  scope; awaiter держит ownership до его выхода, включая повторную отмену.
- Cleanup восстанавливает claim/link crash window по исходному caller key,
  закрывает старые approvals, сохраняет journals/cache/effects. Completed result
  сохраняется только при доказанном завершении без refusal/inconsistent approval.
  Existing needs_review не затирается. Uncertainty записана также в turn DB:
  падение до plan acknowledgement не теряет основание для ручной сверки.
- API/Plans UI/CLI/tool различают «запрошена остановка», незавершённую cleanup и
  review. Stop не обещает rollback. UI оставляет отменённый план видимым и даёт
  обновить статус. Summary истёкшего плана ждёт cleanup и не вызывает модель;
  обычная summary использует отдельный ephemeral `plan-summary:` thread.
- 31 новый unit case и 3 SIGKILL case: cancel/TTL во время model/effect,
  approval claim race, pending intent до dispatch, worker cancellation,
  claim/link gaps, unknown/legacy/refusal/finished states, потеря plan commit,
  вложенные scopes, fallback, custom tools, миграция и UI/CLI contract.
- **2243 passed, 56 integration deselected, 1 warning** — полный локальный набор.
  Отдельно **17 SIGKILL/restart integration passed**. Остальные 39 integration
  cases с внешними сервисами не запускались; реальные API/MCP/production не вызваны.
  Ruff, отдельный F821, TypeScript `tsc -b`, PlansPage ESLint и diff-check чистые.
  UI проверен во временной копии с существующими node_modules, без установки deps.
- Проверенная Python среда локально — 3.13; production Python 3.12 и полный
  браузерный/Telegram E2E этого изменения пока не проверены.

Файлы этапа:

- `kronos/execution_control.py`, `kronos/engine.py`, `kronos/llm.py`,
  `kronos/graph.py`: scope, boundaries, provider/nested propagation, worker ownership.
- `kronos/plans.py`, `kronos/session.py`, `kronos/migrations/v006_plan_stop.py`,
  `kronos/cron/plans.py`: stop identity, migration, approval/effect reconciliation.
- `kronos/memory/nodes.py`, `kronos/agents/deep_research/graph.py`,
  `kronos/agents/topic_research/graph.py`,
  `kronos/agents/topic_research/nodes/discover.py`,
  `kronos/agents/knowledge_pipeline/nodes.py`,
  `kronos/agents/knowledge_pipeline/queue.py`: checks перед следующими действиями.
- `dashboard/api/plans.py`, `dashboard-ui/src/pages/PlansPage.tsx`,
  `kronos/cli.py`, `kronos/tools/plans_tools.py`: честные статусы остановки.
- `tests/test_plan_stop.py`, `tests/test_execution_control.py`,
  `tests/test_invocation_outcomes.py`, `tests/test_plans_cli.py`,
  `tests/test_plans_tools.py`, `tests/test_plan_kill.py`,
  `tests/helpers/plan_crash.py`: regression/fault tests и generic thread fixtures.
- ADR-0011, индекс ADR и этот реестр: решение, ограничения, evidence.

Проверка: `KAOS_ENV_FILE=/dev/null PYTHONPATH="$PWD" ../app/.venv/bin/python -m pytest
-m 'not integration' -q`; отдельно `tests/test_plan_kill.py tests/test_durable_kill.py`.
Логи проверок задачи: `/tmp/kaos-plan-stop-full-final.txt`,
`/tmp/kaos-plan-stop-kill-final.txt`; UI scratch — `/tmp/kaos-stop-ui-path.txt`.

**F08/F09/F10 и общая цель ещё не закрыты:**

1. Гарантированное уведомление об окончании остановки/доставке — следующий F09
   durable outbox; отменённые планы пока не имеют надёжной final stop-нотификации.
2. Operator workflow для needs_review, upstream idempotency, distinct logical
   operations, bounded retention/tombstones и полный перечень non-engine writers.
3. Scope — кооперативная граница, не sandbox/компенсация. Уже dispatch-нутый SDK
   может закончиться позднее; намеренно отделившийся plugin не контролируется.
   Stuck sync SDK удерживает ownership до своего timeout/выхода процесса;
   общий F15 async/ responsiveness не закрыт.
4. Перед release обязательно согласовать новые изменения main (`da351a3`,
   `f31f283` и незавершённые правки) с этой веткой, чтобы не откатить уже исправленную
   production-изоляцию. Ветка не объявлена готовым deploy candidate.
5. Production approvals/config/rights/backup, миграции на копии, rollout и E2E
   остаются обязательными. Этот этап ничего не разворачивает на сервере.


### F09 — этап 1: transactional outbox для планов

- Миграция v007 создаёт `delivery_outbox` в **той же** SQLite-БД, что и план.
  Итог/результат выполненного шага и обязанность отправки коммитятся вместе;
  ошибка enqueue откатывает изменение producer. Перезапуск не требует повторной
  модели или выполнения шага. Summary — first-writer-wins.
- Очередь хранит неизменяемые chat/topic, plain-text chunks, Telegram random_id,
  sender binding, receipts и retry metadata. UTF-16 chunk limit учитывает emoji;
  перед записью применяется redaction. False/None/пустой/несвязанный receipt не
  считается доставкой. Delivered — принят Telegram, не прочитан владельцем.
- Отдельный application worker работает независимо от новых шагов/моделей,
  ждёт готовности Telegram и обрабатывает ограниченный объём очереди. Poller
  только сохраняет обязанности и не ждёт transport, даже если отправка зависла. Backoff,
  FloodWait и timeout не удаляют обязанность. После частичной отправки продолжается
  первый неподтверждённый chunk с тем же random_id, не новая операция.
- Kernel ownership удерживается до записи receipt **или** retry metadata;
  параллельный worker не крадёт живую отправку по TTL. Смена аккаунта и
  некоррелируемый RANDOM_ID_DUPLICATE требуют review. Bot API fallback и платные
  флаги не включаются. Не заявляется безусловное exactly-once внешнего Telegram.
- Новая отмена плана теперь создаёт детерминированный финальный итог только после
  fenced cleanup, включая честный needs_review. Исторические cancelled планы не
  рассылаются при upgrade; старые сохранённые summary имеют legacy_unknown, не
  искусственный delivered. API/UI/CLI/plan tools показывают доставку отдельно.
- **2275 passed, 61 integration deselected, 1 warning**, 34.61 sec на локальном
  Python 3.13. **22 SIGKILL/restart tests passed** (17 прежних + 5 новых), включая
  producer rollback, committed queue, send-before-ack, partial ack и final ack.
  32 новых unit cases и 5 новых crash cases относительно предыдущего этапа.
  39 внешних integration cases не запускались. Реальные Telegram/MCP/provider
  writes и production не вызывались. Ruff, отдельный F821, diff-check чистые;
  Dashboard `tsc -b` и PlansPage ESLint прошли во временной копии со старыми deps.
- Первый полный прогон выявил 8 ошибок из-за глобального FakeClient, оставленного
  прежними bridge-тестами; плановые fixtures теперь явно изолируют readiness.
  Исправление не ослабляет production transport contract.

Изменённые файлы этапа:

- `kronos/migrations/v007_delivery_outbox.py`, `kronos/delivery.py`,
  `kronos/telegram_delivery.py`: схема, atomic enqueue, ownership, chunk receipts,
  retry и корреляция MTProto.
- `kronos/plans.py`, `kronos/cron/plans.py`, `kronos/cron/delivery.py`,
  `kronos/app.py`, `kronos/bridge.py`, `kronos/db.py`: producer transactions,
  stop notification, независимый worker, readiness и database identity.
- `dashboard/api/plans.py`, `dashboard-ui/src/pages/PlansPage.tsx`,
  `kronos/cli.py`, `kronos/tools/plans_tools.py`: generated/pending/delivered/review.
- `tests/test_delivery.py`, `tests/test_telegram_delivery.py`,
  `tests/test_delivery_kill.py`, `tests/helpers/delivery_crash.py`: новые проверки;
  связанные plan/outcome fixtures обновлены на новый транспортный контракт.
- `docs/decisions/ADR-0012-transactional-plan-delivery.md`, индекс ADR и этот реестр.

Проверка: `KAOS_ENV_FILE=/dev/null PYTHONPATH="$PWD" ../app/.venv/bin/python -m pytest
-m 'not integration' -q`; отдельный crash suite — `tests/test_delivery_kill.py
 tests/test_plan_kill.py tests/test_durable_kill.py`.

**F09 не закрыт. Следующий этап обязателен:** durable delivery восстановленных
session turns с закрытием окна finalize→enqueue; запуск resume/bridge без потери
уведомлений; approval prompts/continuations, обычные ответы и остальные cron
producers. Нужны также operator reconciliation/repair без слепого resend,
согласованный retention/scoped reset очереди, live userbot/bot/topic smoke и rollout.
Все остальные F/A/PROD/V пункты остаются в общей цели, без исключения инфраструктуры.

Свежий read-only production-аудит 11:26 UTC подтвердил прежние риски прав/admin,
92% диска, неполный backup и отсутствие позднего remediation rollout. Локальная
ротация cron history `f31f283` ещё не соответствует production-файлу scheduler.
Перед будущим релизом по-прежнему обязательна безопасная сверка с изменяемым main;
этот worktree нельзя автоматически деплоить поверх более новых runtime исправлений.

### F09 — этап 2: transactional delivery восстановленных session turns

- Миграция v008 добавляет outbox в **ту же** БД, что и SessionStore, frozen
  transport provenance и признак обязанности доставки. Исходный Telegram turn
  сохраняет проверенные chat/topic/account до вызова модели; user-facing resume
  атомарно записывает обязанность вместе с claim. Числовой thread ID сам по себе
  не считается разрешением отправить сообщение. Плановые turns не дублируют
  очередь планов; старые записи не принимаются в доставку автоматически.
- History, terminal outcome и сообщение коммитятся одной транзакцией.
  Неизвестный/повреждённый маршрут и отсутствующий результат видны как отдельная
  проблема, не fabricated delivered. Повторная финализация не заменяет результат;
  поздний fail не превращает завершённый turn в ошибку. Report-mode recovery
  завершает уже запрошенную обязанность честным уведомлением о прерывании.
- Approval wait создаёт уведомление вместе с waiting state, а не готовый ответ.
  Решение/завершение помечает старое уведомление obsolete, без поддельного receipt.
  TTL обрабатывается ограниченным проходом под conversation ownership; старые
  non-recovery approvals не исполняются и не отменяются автоматически.
- `/approve`, `/reject` и callbacks проверяют явный owner allowlist, исходные
  chat/topic/account. Callback использует фактический topic события. Результат
  continuation отправляет только queue worker, без второго прямого ответа.
  Обязательный dissent не обходится: соответствующий результат остаётся needs_review.
- Bridge и recovery запускаются одновременно после установки shutdown handlers;
  завершение одноразового resume не завершает процесс. Delivery имеет независимые
  циклы для планов и session store. Ошибка одного producer и transport-originated
  CancelledError не удаляют его цикл; настоящая отмена корректно завершает worker.
- API, CLI и TurnsPage разделяют outcome и transport acceptance, показывают
  сохранённый итог и состояние очереди. Delivered означает принятие Telegram,
  не прочтение пользователем. Старый backend без metadata не отображается как
  успешно доставивший сообщение.

Проверки на локальном Python 3.13:

- **2314 passed, 66 integration deselected, 1 warning**, 31.87 sec. Относительно
  этапа 1 добавлены 39 unit cases. Первый финальный прогон в sandbox дал
  2299 passed и 15 PermissionError (loopback/ps); повтор с разрешённым доступом
  прошёл полностью. Защитные проверки не отключались.
- **27 SIGKILL/restart tests passed**, 29.53 sec: 22 прежних + 5 новых. Проверены
  rollback producer transaction, committed queue, send-before-ack, partial/final
  receipts. После committed result модель не вызывается повторно; неподтверждённые
  chunks используют прежние random IDs. SQLite quick_check остаётся ok.
- Ruff, отдельный F821, diff-check, TypeScript app/node и полный UI ESLint — PASS.
  UI проверен во временной копии с существующими зависимостями; tsbuildinfo не
  записывался в исходный node_modules. Конфигурация/dependencies не менялись.
- 39 внешних integration cases, реальные Telegram/MCP/model writes и production
  не запускались. Эти результаты не заменяют Linux/Telegram/rollout-приёмку.

Изменённые файлы этапа:

- `kronos/migrations/v008_turn_delivery.py`, `v007_delivery_outbox.py`,
  `kronos/turn_delivery.py`, `delivery.py`, `session.py`, `plans.py`, `db.py`:
  миграции, единый enqueue-контракт, producer transactions и lifecycle соединения.
- `kronos/bridge_recovery.py`, `bridge.py`, `graph.py`, `app.py`,
  `cron/delivery.py`: доверенные маршруты, ownership, approvals и независимый запуск.
- `dashboard/api/turns.py`, `dashboard-ui/src/pages/TurnsPage.tsx`, `kronos/cli.py`:
  наблюдаемость результата и доставки.
- `tests/test_turn_delivery.py`, `test_bridge_recovery.py`,
  `test_recovery_startup.py`, `test_turn_delivery_kill.py`,
  `helpers/turn_delivery_crash.py`: новые проверки. Существующие delivery,
  durable resume/kill/helper и graph-contract tests обновлены на queue API.
- ADR-0013, индекс архитектурных решений и этот реестр.

Повторить: полный pytest как выше; crash suite —
`tests/test_turn_delivery_kill.py tests/test_delivery_kill.py tests/test_plan_kill.py
tests/test_durable_kill.py`. Локальные логи: `/tmp/kaos-turn-delivery-full-approved.txt`,
`/tmp/kaos-turn-delivery-kill-final.txt`; UI scratch указан в `/tmp/kaos-turn-ui-path.txt`.

**F09 всё ещё открыт:** обычные ответы, исходные non-recovery/plan approvals,
остальные cron producers, operator adoption/reconciliation/review resolution,
retention и scoped reset очереди. Requested turns временно не удаляются prune:
это сохранение доказательств, а не завершённая политика хранения. Sync SafeDB I/O
остаётся в F15. F17 optional-dashboard startup — отдельный следующий фикс.
Ни production-конфигурация, ни main с незакоммиченными изменениями не тронуты.

### F17 — optional Dashboard не останавливает агента

- Причина: no-password ветка `run_dashboard` возвращала управление, а
  `FIRST_COMPLETED` считал это окончанием всей службы и выключал bridge/cron.
  Без исключения `main` возвращал успех. Безопасный отказ открыть HTTP тем самым
  превращался в незаметное завершение полезных сервисов.
- `run_dashboard` теперь явно возвращает False только для disabled startup.
  Application wrapper удерживает такую необязательную службу до отмены; он не
  превращает произвольный возврат или ошибку активного Dashboard в disabled.
  HTTP без пароля по-прежнему не открывается. Standalone CLI получает exit 1,
  а не ложный успех или бесконечное ожидание без сервера.
- Любая неожиданно завершившаяся/самоотменённая служба теперь даёт failure после
  отмены и join остальных служб. Настоящее исключение сохраняется. Штатный сигнал
  и чистое завершение одновременно остаются success; MCP context и signal
  handlers освобождаются.
- **2334 passed, 66 integration deselected, 1 warning**, 36.22 sec; **27 crash
  tests passed**, 29.02 sec. Добавлены 20 unit cases: реальные main/run_dashboard
  с fake transports/MCP, disabled startup, signal/caller cancel, return/error/
  self-cancel каждой службы, standalone CLI и enabled server contract.
  Ruff/F821 и diff-check — PASS. Внешние 39 integration cases и production не
  запускались; в этом шаге UI не менялся, проверки предыдущего этапа применимы.

Файлы: `dashboard/server.py`, `kronos/app.py`, `kronos/cli.py`,
`tests/test_app_supervision.py`, ADR-0014, индекс ADR и этот реестр.
Проверить: `KAOS_ENV_FILE=/dev/null PYTHONPATH="$PWD" ../app/.venv/bin/python
-m pytest tests/test_app_supervision.py tests/test_recovery_startup.py -q`, затем
полная regression/crash suite выше. Логи: `/tmp/kaos-supervision-full.txt`,
`/tmp/kaos-supervision-kill.txt`.

**F17 закрыт только локально.** Автоматический restart ранее включённого Dashboard,
degraded readiness и оповещение об отключённом интерфейсе — не часть этого фикса;
PROD-08 остаётся открытым. Конфигурация, зависимости и systemd не менялись.

### F12 — этап 1: admission перед каждым factory-backed model call

- Устранён Telegram-only guard: `get_model`, `get_orchestrator_model` и
  `get_fallback_model` возвращают wrapper, проверяющий текущий расход перед
  invoke/ainvoke. Кэшированный supervisor/specialist больше не обходит проверку.
  Каждая явная fallback-попытка проверяется отдельно; budget refusal не считается
  отказом провайдера и не запускает более дорогой fallback.
- Lite выбирается во время вызова, а не только построения агента. ContextVar
  переносит force_tier через supervisor/delegation, изолирует параллельные задачи
  и не позволяет вложенному standard отменить внешний lite. Tool bindings и
  options сохраняются. Строка `get_model("lite")` нормализуется в ModelTier.
- Ошибка/повреждённое значение daily ledger не трактуется как нулевой расход.
  NaN/Inf/отрицательные значения стоимости не принимаются callback recorder.
  Writer и обе daily-read функции используют UTC, как обещано пользователю.
  При отсутствии session_id и recorder, и admission используют thread_id.
- Engine возвращает явный `budget_blocked`, не completed и не совет «попробуй
  ещё раз» из generic model_error. Существующая execution cancellation boundary
  сохраняется и для моделей, созданных до начала execution scope.
- **2370 passed, 66 integration deselected, 1 warning**, 32.75 sec; **27 crash
  tests passed**, 29.32 sec. 36 новых unit cases; focused suite — 78 passed.
  Ruff/F821/diff-check — PASS. Внешние 39 integration cases не запускались.
  UI не менялся; production, реальные API, зависимости и конфигурация не тронуты.

Файлы: `kronos/security/model_budget.py`, `cost_guardian.py`, `cost_tracking.py`,
`kronos/llm.py`, `graph.py`, `engine.py`, `swarm_store.py`,
`tests/test_model_budget.py`, `tests/test_llm_providers.py`, ADR-0015, индекс ADR
и этот реестр. Старые factory tests теперь проверяют adapter внутри budget proxy,
а не требуют необёрнутый SDK instance. Защитные проверки не отключались.

Проверить: `KAOS_ENV_FILE=/dev/null PYTHONPATH="$PWD" ../app/.venv/bin/python
-m pytest tests/test_model_budget.py tests/test_cost_tracking.py tests/test_cost_stats.py
tests/test_llm_providers.py tests/test_cassettes_llm.py -q`; полная regression и
crash suite — как выше. Логи: `/tmp/kaos-budget-full-final.txt`,
`/tmp/kaos-budget-kill-final.txt`, `/tmp/kaos-budget-focused-final.txt`.

**F12 не закрыт. Обязательное продолжение, выявленное по текущему коду:**

| Поверхность / требование | Текущее доказательство и следующий шаг |
|---|---|
| Factory-backed graph/supervisor/specialists, cron, analytics, compaction, group routing | Все проходят общую factory; normal invoke/resume/ReAct/fallback и cached model проверены с fake providers. Live E2E всех callers ещё нет |
| Vision | `kronos/vision.py`: прямой OpenAI Responses SDK и отдельный Codex subprocess; нет общего budget admission/accounting. Нельзя подменять vision произвольной текстовой lite-моделью |
| Mem0 | `kronos/memory/store.py`: Memory.from_config создаёт собственный LLM вне factory. В локальном тестовом окружении mem0 не установлен; настоящая adapter-приёмка не выполнена |
| ASO | `aso/llm.py`: собственная HTTP completion/fallback цепочка; требует общей admission/accounting boundary |
| GEO measurement | `kronos/seo_geo/trackers/llm.py`: прямой LiteLLM HTTP. При downgrade нельзя незаметно заменить измеряемую модель и записать результат под прежним engine |
| Отдельные scripts | `scripts/contact-profiler.py`, `scripts/recall.py`: прямые DeepSeek HTTP calls, не покрытые runtime factory |
| Session budget | Текущий tally остаётся in-memory. Требуются durable totals и неизменяемая связь scope с исходным turn для resume/approval; fallback на thread_id не решает это полностью |
| Жёсткий денежный лимит | Нужна атомарная межпроцессная reservation до dispatch, стоимость всех retries, unknown outcomes/crash и reconciliation. Preflight против уже записанного spend не ограничивает совокупность параллельных in-flight запросов |
| Полнота учёта | Callback writes всё ещё best-effort; SDK-internal retries/неизвестный usage и актуальные provider-specific цены не приняты. Модельное имя само по себе не доказывает нулевую цену API |
| История/производительность | Старые local-day buckets не переписывались. Нужны историческая сверка для non-UTC hosts и F15 для sync accounting reads |

При этой инвентаризации дополнен **F16**: `_analyze_with_codex_cli` в Vision
при timeout не создаёт owned process group и не вызывает terminate/kill/wait.
Изолированный fake-process probe дал `new_session=False`, `timeout_raised=True`,
`process_signals=[]`, `waited=False`. Это доказывает пропущенный cleanup path,
не утверждает наличие конкретного живого orphan в production. Основной
`llm_codex`-фикс не закрывает эту отдельную реализацию.

### F16 — дополнение: Vision process lifecycle

- Отдельный Vision subprocess path переведён на `run_codex_command` — извлечённую
  без изменения аргументов общую async-реализацию из `llm_codex`. Helper держит
  spawn/communication tasks, isolated process group и output file. Vision держит
  image file до окончания cleanup, в том числе при повторной отмене и отмене
  во время запуска. Empty response теперь не считается успешным OCR.
- Восемь новых тестов используют настоящие локальные Python subprocesses вместо
  Codex: timeout, уже завершившийся leader с живым child, повторная отмена,
  отмена до получения process handle, missing executable, nonzero/empty/success
  и удаление обоих временных файлов. Посторонний test process остаётся жив.
- **2378 passed, 66 integration deselected, 1 warning**, 39.27 sec;
  **27 crash tests passed**, 29.10 sec; focused Codex/Vision — **25 passed**.
  Ruff/F821/diff-check — PASS. 39 внешних integration cases, настоящие Codex/API
  и production не запускались. Конфигурация, зависимости и UI не менялись.

Файлы: `kronos/llm_codex.py`, `kronos/vision.py`, `tests/test_vision.py`,
`tests/test_vision_cleanup.py`, ADR-0016, индекс ADR и этот реестр.
Проверить: `KAOS_ENV_FILE=/dev/null PYTHONPATH="$PWD" ../app/.venv/bin/python
-m pytest tests/test_vision.py tests/test_vision_cleanup.py tests/test_llm_codex.py
tests/test_llm_codex_cleanup.py -q`; полный и crash прогоны — как выше.
Логи: `/tmp/kaos-vision-cleanup-full.txt`, `/tmp/kaos-vision-cleanup-kill.txt`,
`/tmp/kaos-vision-cleanup-focused.txt`.

F16 закрыт **локально для этих двух реализаций**, не в production. Граница —
owned POSIX process group, не произвольный escaped daemon/новый OS sandbox.
Linux/live Codex приёмка остаётся обязательной. Budget admission Vision — F12,
этот cleanup-фикс его не подменяет.

### F12 — этап 2: прямые ASO/GEO/Vision/scripts и тип оплаты

- Общая admission/accounting boundary добавлена для прямых ASO HTTP calls,
  GEO LiteLLM measurements, OpenAI Vision, Codex Vision и DeepSeek-вызовов
  `recall.py`/`contact-profiler.py`. Повторная явная ASO fallback-попытка снова
  проверяет бюджет; budget refusal не запускает следующий провайдер.
- ASO/scripts при soft downgrade используют настроенную factory lite. GEO не
  подменяет измеряемый engine: возвращает явную ошибку. API Vision при отсутствии
  совместимой lite-модели также отказывает. Subscription Codex Vision допускает
  soft downgrade, но не обходит hard admission refusal.
- Usage из dict/SDK response записывается внутри context manager до закрытия
  клиента. При отсутствии usage используются оценки, а не бесплатный API-вызов.
  Regression на ошибку client close подтверждает, что полученный usage сохранён.
  Пустой OpenAI Vision output учитывается, затем отклоняется как ошибка; два
  отрицательных теста сначала воспроизвели прежний ложный success.
- Billing identity привязана к adapter, не имени модели. API-вызов `gpt-5.5`
  больше не получает нулевую цену только из-за совпадения имени с Codex. Factory
  callbacks сохраняют известную модель как fallback при отсутствии metadata;
  настоящая reported model имеет приоритет. Price overrides не менялись.
- Media audit scope связывает pre-agent Vision с тем же chat/session budget,
  который проверяет последующий agent turn, и восстанавливает прежний context.
  В bridge budget refusal для изображения больше не маскируется под ошибку
  конфигурации. Такой же scope вокруг voice подготовлен, но **сам Whisper пока
  не проверяет бюджет и не пишет стоимость**.
- **2409 passed, 66 integration deselected, 1 warning**, 43.54 sec;
  **27 crash tests passed**, 31.84 sec; focused suite — **102 passed**.
  31 новый direct-model test case относительно предыдущего baseline, включая
  пять client-close failures. Ruff/F821/diff-check — PASS. 39 внешних integration
  cases, реальные модели/Codex, production и UI не запускались/не менялись.

Файлы: `kronos/security/direct_model.py`, `cost_tracking.py`, `kronos/llm.py`,
`aso/llm.py`, `kronos/seo_geo/trackers/llm.py`, `kronos/vision.py`, `bridge.py`,
`bridge_media.py`, два script-файла выше, `tests/test_direct_model_budget.py`,
`tests/test_cost_tracking.py`, `tests/test_vision.py`, ADR-0017, индекс ADR и
этот реестр. Конфигурация, зависимости и DB schema не менялись. Незакоммиченные
пользовательские изменения в main f31f283 не затронуты.

Проверить: `KAOS_ENV_FILE=/dev/null PYTHONPATH="$PWD" ../app/.venv/bin/python
-m pytest tests/test_direct_model_budget.py tests/test_model_budget.py
tests/test_cost_tracking.py tests/test_vision.py tests/test_llm_providers.py
tests/test_observer_capture.py -q`; полный набор `-m 'not integration'` и crash
suite из четырёх `test_*_kill.py` — как выше. Логи:
`/tmp/kaos-direct-budget-focused-final.txt`, `/tmp/kaos-direct-budget-full-final.txt`,
`/tmp/kaos-direct-budget-kill-final.txt`; red probe — `/tmp/kaos-direct-budget-red.txt`.

**F12 остаётся открытым.** Таблица этапа 1 фиксирует историческое состояние;
актуальный остаток после этапа 2:

| Поверхность / требование | Остаток и граница доказательства |
|---|---|
| Mem0 | Собственный LLM из `Memory.from_config` вне runtime factory; mem0 локально не установлен, совместимый adapter и реальная интеграционная приёмка ещё нужны |
| Whisper voice | Прямой Groq HTTP в `bridge_media._transcribe_voice` остаётся без admission/accounting; voice audit scope не закрывает этот обход. Нужен учёт длительности, а не выдуманные text tokens |
| Durable session scope | Tally пока in-memory; original-turn scope для resume/approval и межпроцессные durable totals ещё не реализованы |
| Жёсткий денежный лимит | Нет атомарных reservations для конкурентных in-flight calls, полного учёта retries и crash/timeout/unknown outcomes с reconciliation |
| Полнота и точность учёта | Recorder остаётся best-effort. Отсутствующий usage — оценка длины текста; image tokens достоверно так не вычисляются. Provider-specific prices/defaults не являются invoice или upper bound |
| Приёмка и rollout | Новые пути проверены с fake transports и настоящим изолированным ledger; live E2E, Linux/Codex и controlled rollout ещё нужны. Main isolation/cron-rotation changes надо согласованно совместить с веткой перед rollout |

Нельзя объявлять закрытым F12 или весь аудит по этим локальным тестам.

### F12 — этап 3: Whisper, длительность аудио и voice cleanup

- `_transcribe_voice` теперь проверяет daily/session/execution boundary до открытия
  файла и HTTP. При soft downgrade остаётся уже выбранный turbo ASR, без подмены
  текстовой моделью; hard refusal предотвращает отправку.
- Запрашивается `verbose_json`. Валидная положительная длительность считается
  по опубликованному тарифу turbo с минимальным billed duration; входные/выходные
  text tokens не выдумываются. Расход записывается в общем media chat/session scope
  до проверки текста и закрытия клиента. Основание расчёта: [Groq speech-to-text
  pricing](https://console.groq.com/docs/speech-to-text), проверено 2026-09-08.
- Невалидный/пустой transcript не считается успешным, но полученный usage не
  теряется. Отсутствующая/невалидная длительность даёт `ModelUsageUnknownError`,
  а не фиктивный бесплатный success. Никакого автоматического retry не добавлено.
- HTTP provider body больше не вкладывается в исключение. Bridge логирует только
  тип ошибки голоса и показывает фиксированное сообщение, отдельно сообщает
  budget refusal. Temp audio удаляется в finally, включая CancelledError и сбой
  отправки уведомления. HTTP contexts и file handle освобождаются при отмене.
- 31 новый case: 30 сначала воспроизвели прежние дефекты, один подтвердил уже
  работавшее закрытие file/client при отмене внутри HTTP. Проверены минимальное,
  дробное и длинное audio duration, daily/session/unavailable ledger, soft limit,
  невалидный usage/text, stop до/после response, cleanup error, HTTP 401/429/500,
  отмена HTTP и реальные temp files зарегистрированного bridge handler.
- **2440 passed, 66 integration deselected, 1 warning**, 41.67 sec;
  **27 crash/restart tests passed**, 29.08 sec; focused suite — **66 passed**.
  Ruff, F821, compileall и diff-check — PASS. Daily reporting округляет итог,
  поэтому отдельная проверка читает raw SQLite и подтверждает, что сам расход
  хранится без такого округления. Защитные проверки в тестах не отключались.

Файлы: `kronos/bridge.py`, `kronos/bridge_media.py`,
`kronos/security/cost_tracking.py`, `kronos/security/direct_model.py`,
`tests/test_voice_budget.py`, ADR-0018, индекс ADR и этот реестр.
Проверить: `KAOS_ENV_FILE=/dev/null PYTHONPATH="$PWD" ../app/.venv/bin/python
-m pytest tests/test_voice_budget.py tests/test_observer_bridge_capture.py
tests/test_direct_model_budget.py -q`; полный набор и четыре crash files — как
выше. Логи: `/tmp/kaos-voice-budget-red.txt`, `/tmp/kaos-voice-budget-focused.txt`,
`/tmp/kaos-voice-budget-full.txt`, `/tmp/kaos-voice-budget-kill.txt`.

**Границы:** цена — опубликованная оценка, не invoice. Whisper timeout/cancel до
получения usage, invalid duration и сбой recorder пока не создают durable unknown
receipt. Это обязательный остаток F12, а не «нулевой расход»; новый error лишь
прекращает конкретную обработку. Mem0 по-прежнему вне общей boundary. Durable
session scope, межпроцессные reservations, retries/reconciliation и live provider
приёмка остаются открытыми. Stage 2 table выше — историческое состояние до этого
фикса. Все остальные F/A/PROD/V пункты сохраняются в полной цели.

39 внешних integration cases, реальный Groq/Codex и production не запускались.
Конфигурация, зависимости, DB schema и UI не менялись; 12 dirty main-файлов
не затронуты. До rollout по-прежнему нужно согласованно объединить ветку с
main isolation/cron-rotation изменениями, не потеряв пользовательские правки.

### F12 — этап 4: Mem0 completion boundary и полный invocation context

- Новый `BudgetedMemory` оборачивает используемые add/search/get_all. Для каждой
  операции создаётся shallow copy адаптера с отдельным model wrapper; storage
  resources остаются прежними. Общий singleton не получает изменяемый контекст
  чужого чата. Неизвестные методы, graph/reranker и неподдержанный SDK shape
  отклоняются, а не проксируются без проверки.
- Проверка стоит непосредственно перед `chat.completions.create`, включая каждый
  новый extraction/update pass. Исходный SDK response учитывается до того, как
  parser Mem0 отбросит usage или упадёт. При downgrade используются factory lite,
  исходные messages/JSON options/tools; результат адаптирован к parser contract,
  повторная запись стоимости поверх factory callback не добавляется.
- Каждый внутренний generate call входит в собственную копию Context, поэтому
  raw ThreadPoolExecutor Mem0 не теряет audit/session/force-lite/execution scope.
  Проверены одновременно два caller scope и несколько workers одной операции.
- Контекст охватывает весь invocation: retrieval, основную модель, background
  storage и compaction. Внешний executor получает `copy_context().run`. Ранее
  retrieval/compaction были снаружи audit scope, а background не переносил его.
- Без DeepSeek key Mem0 не создаёт неявный default API provider. A05 этим **не
  закрыт**: graph-level memory gate ещё требует исправления. FTS fallback внутри
  search_memories сохраняется.
- **2459 passed, 66 integration deselected, 1 warning**, 36.39 sec;
  **27 crash tests passed**, 22.61 sec; focused локальный набор — **87 passed**.
  19 новых test cases. Ruff/F821/compileall/diff-check — PASS. Один тест использует
  настоящий BaseChatModel с callback, остальные transports — fake, ledger реальный
  изолированный SQLite. Production, зависимости и конфигурация не менялись.

Файлы: `kronos/memory/model_boundary.py`, `kronos/memory/store.py`,
`kronos/graph.py`, `tests/test_mem0_budget.py`, ADR-0019, индекс ADR и этот реестр.
Проверить: `KAOS_ENV_FILE=/dev/null PYTHONPATH="$PWD" ../app/.venv/bin/python
-m pytest tests/test_mem0_budget.py tests/test_model_budget.py tests/test_memory.py
tests/test_plan_stop.py -q`; full `-m 'not integration'` и crash suite — как выше.
Логи: `/tmp/kaos-mem0-budget-red.txt`, `/tmp/kaos-mem0-budget-focused-local.txt`,
`/tmp/kaos-mem0-budget-full.txt`, `/tmp/kaos-mem0-budget-kill.txt`.

**Отдельный verification gap:** диагностический запуск прежнего
`tests/test_graph_contract.py` дал 6 failed / 2 passed из-за отсутствия provider
configuration при построении модели перед замоканным react_loop. Такой же результат
подтверждён с `kronos/graph.py` из HEAD 530717b до этих правок, без реальных моделей.
Логи: `/tmp/kaos-mem0-budget-focused.txt`, `/tmp/kaos-graph-contract-baseline.txt`.
Это не зелёная интеграционная приёмка. Нужен самостоятельный фикс test fixture,
чтобы эти восемь локальных контрактов не исключались из обычного regression.

**Обязательный остаток:** Mem0 локально отсутствует; в приватном inventory
production указан **mem0ai 1.0.7**. Запрошено разрешение установить только эту уже
объявленную optional-зависимость во временное окружение для реальной package-level
проверки с fake network. Разрешение/установка/проверка пока не выполнены. Чтение
upstream source и fake Mem0 tests этого не заменяют. Также остаются durable session
ledger, reservation, unknown outcomes/SDK retries и reconciliation; F12 не закрыт.
F13 background writer/reset ownership, F15 event-loop responsiveness, A02 scoping,
A05 keyless memory и остальные F/A/PROD/V пункты не исключены из цели.
