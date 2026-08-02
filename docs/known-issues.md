# Известные Проблемы И Риски

Статусы:

- **Подтверждено**: следует непосредственно из текущего кода или безопасной статической проверки.
- **Условный риск**: код допускает проблему, но impact зависит от deployment/data/timing.
- **Неподтверждено**: требуется runtime/browser/production information; не утверждается как факт.

## Критические И Высокие

### KI-001: три rule endpoint падают с `NameError`

**Подтверждено.** `app/web.py` использует, но не импортирует `get_rule_source_ids` и `already_sent_exists` из `web_contexts.py`.

Затронуты:

- `GET /rules/{rule_id}/queue` (`web.py:715`, `735`);
- `POST /channels/{channel_id}/rules/import` (`web.py:1519`);
- `POST /rules/{rule_id}/files/{file_id}/post-now` (`web.py:1651`).

Import route уже делает `flush` до ошибки, но request session закрывается без commit; явного rollback нет. Queue и direct selected-file publication фактически не работают.

### KI-002: active content отдается inline с origin панели

**Подтверждено.** `GET /files/{id}/original` использует guessed MIME и `Content-Disposition: inline` (`web.py:554-567`). Documents включают неизвестные extensions, в том числе HTML/SVG/XML. При открытии недоверенный файл может исполняться same-origin с authenticated panel. CSP отсутствует.

Требуется force-download/neutral MIME для active types либо отдельный untrusted origin.

### KI-003: удаление source может удалить multi-source rule и history

**Подтверждено.** `ContentSource.rules` имеет `cascade="all, delete-orphan"` через legacy primary `PostingRule.source_id` (`models.py:122-126`). Primary source назначается первым selected source. `DELETE source` вызывает ORM delete без guard (`web.py:1233-1245`). Rule delete затем удаляет history.

### KI-004: нет блокировки публикации и идемпотентности

**Подтвержденный риск.** File выбирается до внешнего Telegram call, а history фиксируется после ответа. Manual/scheduled runs и несколько processes могут одновременно отправить один candidate. `max_instances=1` действует только на local scheduler job.

### KI-005: неожиданные ошибки публикации обходят history/scheduling

**Подтверждено.** Callers ловят только `TelegramPublishError`, но caption formatting, file open и HTTPX могут выбросить другие exceptions. Rule может остаться due и повторяться каждый tick; request может вернуть 500. Token находится в URL и потенциально может попасть в exception logs.

### KI-006: default credentials предсказуемы

**Подтверждено.** Новый admin создается как `admin/admin`; Compose публикует host port. UI предупреждает, но не требует смены. Минимальная новая длина password всего 4 символа.

## Средние

### KI-007: `APP_TIMEZONE` не управляет schedule calculations

**Подтверждено.** Variable передается APScheduler, но due checks, allowed windows и next runs используют naive `datetime.now()`. Compose не задает `TZ`. Окна могут работать по UTC/system timezone вместо configured timezone.

### KI-008: блокирующая работа выполняется в async event loop

**Подтверждено.** Scheduler coroutines синхронно обходят filesystem, коммитят DB, запускают Pillow/ffmpeg и pre-publication scans. Большие источники могут задержать web requests и другие rules.

### KI-009: stale `scan_in_progress` блокирует scans после crash

**Подтвержденный риск.** Persisted flag проверяется manual routes и automatic scheduler, но startup recovery отсутствует. Process termination в scan оставляет source заблокированным.

### KI-010: scan coordination локальна и неполна

**Подтверждено.** Process-local lock покрывает только manual thread jobs. Automatic и pre-publish scans не устанавливают flag атомарно. Возможны параллельные scans одного source.

### KI-011: failed scan может commit partial mutations

**Подтверждено.** Progress callback коммитит session каждые 25 files. Exception handler не делает rollback и коммитит error status. Full scan не является atomic.

### KI-012: SQLite foreign keys не включены

**Подтверждено.** Engine не выполняет `PRAGMA foreign_keys=ON`, хотя models используют `CASCADE`/`SET NULL`. ORM cascades покрывают не все сценарии; orphan references возможны.

### KI-013: migrations невeрсионированы и SQLite-specific

**Подтверждено.** `create_all` + raw `ALTER TABLE`/`INSERT OR IGNORE`; нет revision ledger, downgrade, lock и общего schema evolution. `DATABASE_URL` создает ложное впечатление поддержки других DB.

### KI-014: доверие произвольному `X-Forwarded-For`

**Подтверждено.** Login throttle берет первый header value без trusted proxy list. При прямом доступе attacker может обходить backoff сменой header и раздувать table.

### KI-015: source path containment недостаточен

**Подтверждено условно.** Если discovery list пуст, validation принимает любой absolute path. Runtime directory traversal не проверяет resolved containment для symlinks. Authenticated admin может индексировать container-readable files; exact symlink behavior динамически не проверено.

### KI-016: небезопасные redirect sinks

**Подтверждено.** Ряд actions redirect-ит raw `Referer`; source edit использует raw `return_to`. При authenticated CSRF-valid request возможен external redirect.

### KI-017: bot tokens plaintext и возвращаются в DOM

**Подтверждено.** Tokens хранятся обычным string в SQLite и вставляются в password-type form field. DB/backups/DOM/browser context нужно считать секретными.

### KI-018: один scheduler на каждый process

**Подтверждено.** Lifespan каждого process запускает `AppScheduler`. Более одного worker/replica может дублировать scan и публикацию.

### KI-019: session cookie insecure по умолчанию

**Подтверждено.** `APP_SESSION_HTTPS_ONLY=false`, root Compose не меняет его и публикует HTTP. Риск зависит от network/reverse proxy.

### KI-020: direct post не проверяет enabled/active

**Подтвержденное intended behavior после исправления KI-001, намеренность не подтверждена.** Route не проверяет rule/channel/source enabled и `file.is_active`.

## Низкие И Maintainability

### KI-021: manual rule run не помечается manual

**Подтверждено.** `process_rule(manual=True)` не передает `manual_trigger` ни в одну history row; UI/filters считают запуск scheduled. Direct selected-file path устанавливает flag.

### KI-022: add-missing scan не реактивирует возвращенные files

**Подтверждено.** Existing rows обновляются/reactivate только в full mode.

### KI-023: fingerprint не является content hash, preview cache не очищается

**Подтверждено.** Fingerprint использует path/size/mtime. Замена content с теми же metadata может оставить stale preview. Старые preview variants не удаляются.

### KI-024: picker semantics расходятся с названиями

**Подтверждено.** `shuffle_cycle` не хранит shuffled order/cycle state. `source_selection_mode` сохраняется, но игнорируется. Rule-source helpers продублированы в трех модулях.

### KI-025: GET routes имеют side effects

**Подтверждено.** File browser GET сохраняет preferences; thumbnail GET генерирует files; page rendering может создать admin/CSRF state и preview directories.

### KI-026: UI inconsistencies

**Подтверждено статически.** Create-channel unchecked enabled checkbox интерпретируется как enabled; edit rule validation возвращает raw errors; большинство POST forms зависят от JS injection CSRF; duplicated source/rule templates могут расходиться.

### KI-027: container работает root

**Подтверждено.** `Dockerfile` не задает `USER`. Generated secret file permissions явно не ограничены.

### KI-028: tests и quality gates отсутствуют

**Подтверждено.** Не найдены tests, pytest config, lint, formatting, type checking или coverage. Текущие missing imports были бы обнаружены route smoke test.

### KI-029: local DB paths расходятся

**Подтверждено.** Direct default: `data/app.db`; Compose: `db/app.db`. В workspace присутствует local `data/app.db`, а Compose target может быть другим. Не переносить/запускать без уточнения.

### KI-030: root Compose монтирует content не в ожидаемый путь

**Подтверждено.** Root Compose содержит `./content:/example:ro`, тогда как scanner/UI обнаруживают только `/content`. README показывает правильный destination `/content`. Без ручной корректировки mount приложение не предложит каталог из root Compose как source.

## Неподтвержденные Вопросы

- Middleware order и наличие security headers на auth-generated redirects не проверялись runtime.
- Browser normalization нестандартных redirect paths не проверялась.
- Symlink escape из `/content` не воспроизводился.
- Production TLS/proxy/firewall, filesystem permissions и backup process неизвестны.
- Реальные Telegram permissions, rate limits и media sizes не проверялись.
- Намеренность manual burst, immediate first run и disabled direct-post behavior не описана.
- Поддерживаемые historical database versions неизвестны.

## Исправлено

### KI-032: нельзя было выбрать тему Telegram-группы

**Исправлено поддержкой форумов.** Канал хранит optional `message_thread_id`, формы создания и настроек принимают положительный ID темы, а Telegram publisher передает его для фото, анимаций, видео и документов. При `chat_id_override` тема канала намеренно отключается.

### KI-031: CSS блокировался как mixed content за HTTPS proxy

**Исправлено в этой ветке.** Шаблоны строили absolute CSS URL через `request.url_for()`. Если Uvicorn не доверял proxy headers, URL получал схему `http`, и HTTPS-браузер блокировал stylesheet. `base.html` и `login.html` теперь используют path-only часть URL с сохранением ASGI `root_path`. Требования к forwarded headers описаны в `docs/setup.md`.

## Приоритет Исправлений

1. KI-001, затем route smoke tests.
2. KI-002 и KI-003 до доверия панели недоверенному контенту/операциям удаления.
3. KI-004/KI-005: DB-backed execution claim, idempotency и normalized errors.
4. KI-007/KI-008/KI-009/KI-010: timezone-aware scheduler и вынесение blocking jobs.
5. KI-006/KI-012/KI-013/KI-014/KI-019: deployment и data integrity hardening.
