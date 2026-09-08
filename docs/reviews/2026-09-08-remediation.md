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
| F01 | Public-web DNS/redirect/subrequest защита; live browser smoke | Перенесён, regression пройден | Не развёрнуто |
| F02 | MCP writes/unknown требуют approval; проверки до эффекта | Перенесён, regression пройден | Не развёрнуто; проверить effective flags |
| F03 | Зарегистрированные capabilities достижимы через supervisor | Перенесён, regression пройден | Не развёрнуто |
| F04 | Ошибка extraction сохраняется для retry, не считается empty | Перенесён, regression пройден | Не развёрнуто; историческая сверка отдельно |
| F05 | Per-item ledger; partial/unknown outcomes не теряются и не дублируются | Перенесён, regression пройден | Нужна миграция и сверка |
| F06 | Межпроцессная сериализация всех budget writers | Перенесён, regression пройден | Не развёрнуто |
| F07 | Approval wait не завершает шаг; approve/reject/restart согласованы | Исправлено и проверено локально | Нужны миграции, rollout и Telegram smoke |
| F08 | Отмена/падение шага восстанавливаются без слепого повтора эффектов | В работе: подготовлена часть защиты F10; lease/recovery ещё нужны | Ожидает |
| F09 | Generated/pending/delivered раздельны; сбой доставки повторяется | Ожидает | Ожидает |
| F10 | Durable intent/idempotency/reconciliation; journal errors fail closed | В работе: intent/journal boundary проверен; полный перечень путей и reconciliation не закрыты | Ожидает |
| F11 | Один resume на turn; live registry и atomic ownership | Ожидает | Ожидает |
| F12 | Общая бюджетная проверка и рабочий downgrade всех model paths | Ожидает | Ожидает |
| F13 | Честный scoped reset по всем слоям, включая background writers | Ожидает | Ожидает |
| F14 | Desired/effective runtime settings совпадают или restart явно указан | Ожидает | Ожидает |
| F15 | Model/memory I/O не блокирует event loop; responsiveness test | Ожидает | Ожидает |
| F16 | Timeout/cancel Codex CLI завершает процесс и потомков, очищает ресурсы | Исправлено и проверено локально | Ожидает rollout |

## Production-аудит

| ID | Требование / критерий приёмки | Статус |
|---|---|---|
| PROD-01 | Уникальные Telegram session paths и аккаунты; нет новых locked errors | Перепроверено: не исправлено; запрошено согласование путей/restart |
| PROD-02 | Раздельные session DB, cron-state/log paths; сохранена история | Запрошено согласование конфигурации и защищённого снимка |
| PROD-03 | Непривилегированный пользователь не читает sessions/DB; безопасный umask | Запрошено согласование прав; shared ledger учесть отдельно |
| PROD-04 | Runtime отделён от admin/deploy; минимальные sudo/Docker/systemd права | Ожидает проекта и согласования |
| PROD-05 | F01–F06 реально работают на сервере; effective approvals проверены | Подготовлена объединённая ветка; rollout не выполнен |
| PROD-06 | Запас диска ниже alert-порога; безопасная очистка и retention | Ожидает согласованного плана, данные не удалены |
| PROD-07 | Полный encrypted off-host backup и успешный isolated restore drill | Ожидает; не закрывать по одному успешному workspace job |
| PROD-08 | Readiness всего swarm и рабочая OnFailure/доставка alerts | Ожидает |
| PROD-09 | Согласованный ingress/key-only SSH; соседние сервисы не повреждены | Нужны отдельные согласования host/network изменений |
| PROD-10 | Согласованный release manifest и loaded build identity | Production-only изменения перенесены; manifest/rollout ожидают |

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
| V02 | Причина reboot-required/OS updates проверена; безопасное решение по reboot | Ожидает, reboot не разрешён автоматически |
| V03 | Сверены старые approvals/исторические неопределённые расходы | Ожидает; не выполнять автоматически |

## Выполнено в объединённой ветке

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
