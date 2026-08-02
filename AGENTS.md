# AGENTS.md

## Назначение

Этот репозиторий содержит self-hosted Telegram Auto Poster. Любое изменение следует делать с учетом того, что запуск scheduler или вызов маршрутов публикации может отправить реальное сообщение. Не считайте локальную базу и локальные cookie тестовыми без явного подтверждения пользователя.

## Карта Репозитория

| Путь | Назначение |
| --- | --- |
| `app/main.py` | FastAPI-приложение, middleware, lifecycle, запуск scheduler |
| `app/web.py` | Все HTTP-маршруты и form handlers |
| `app/web_contexts.py` | Запросы и контексты Jinja2, helpers связей каналов/правил/источников |
| `app/web_sources.py` | Поиск `/content`, проверка и отображение источников |
| `app/web_security.py` | CSRF, admin account, login throttling, redirect sanitizer |
| `app/auth.py` | PBKDF2 и начальные `admin/admin` |
| `app/config.py` | Переменные окружения, пути, session secret |
| `app/db.py` | Engine/session, `create_all`, ad hoc startup migrations |
| `app/models.py` | 11 SQLAlchemy-таблиц и отношения |
| `app/services/scheduler.py` | APScheduler, due rules, серии, основной pipeline публикации |
| `app/services/scanner.py` | Обход файлов и синхронизация индекса |
| `app/services/scan_jobs.py` | Ручные daemon-thread scans и прогресс |
| `app/services/picker.py` | Выбор файла по правилу |
| `app/services/telegram.py` | Multipart-запрос к Telegram Bot API, подписи, HEIF |
| `app/services/previews.py` | Pillow/ffmpeg-превью и кеш |
| `app/templates/` | Server-rendered web UI |
| `app/static/styles.css` | Стили; отдельного frontend toolchain нет |
| `Dockerfile` | Python 3.12 image, ffmpeg, Uvicorn, healthcheck |
| `docker-compose.yml` | GHCR image, одна реплика, порт 1338, `/data`, `/db`, content mount |
| `.env.example` | Полный пример runtime-переменных без секретных значений |
| `.github/workflows/` | Публикация GHCR и Docker Hub images |
| `Screens/` | Скриншоты для README |
| `LICENSE` | MIT license |
| `data/`, `db/` | Локальные persistent-данные; считать чувствительными, не редактировать |
| `content/` | Ожидаемый host-каталог originals; не должен попадать в Git |

## Архитектурные Решения И Инварианты

- Один Uvicorn-процесс одновременно обслуживает UI/API и запускает один in-process APScheduler.
- Не увеличивать число workers/replicas без распределенной блокировки: каждый процесс запустит собственный scheduler.
- UI серверный: Jinja2 формы отправляют form-urlencoded/multipart POST; отдельного REST frontend нет.
- SQLite является фактически поддерживаемой БД. `DATABASE_URL` существует, но миграции содержат SQLite SQL.
- Файлы остаются на диске; `files.absolute_path` должен соответствовать доступному read-only mount.
- `posting_rules.source_id` является legacy primary source, а актуальный multi-source набор хранится в `rule_sources`.
- `telegram_config` сохранена только для миграции старой single-channel схемы.
- Успешная отправка фиксируется после ответа Telegram; резервирования кандидата до внешнего запроса нет.
- Все даты бизнес-логики сейчас naive; `APP_TIMEZONE` применяется только к APScheduler.
- Startup изменяет схему и может создать admin/session secret. Простой импорт `app.main` также создает каталоги и secret.

## Опасные Участки

- `app/services/telegram.py:87`: реальный сетевой вызов и токен в URL.
- `app/services/scheduler.py:178`: полный pipeline, включая сканирование и публикацию.
- `app/web.py:1621`: ручной запуск правила вызывает pipeline синхронно из запроса.
- `app/web.py:1637`: ручная отправка конкретного файла; сейчас сломана отсутствующим импортом.
- `app/web.py:1003`, `1233`, `1691`: необратимые удаления каналов, источников и правил.
- `app/models.py:122`: `delete-orphan` у legacy source может удалить multi-source rules и историю.
- `app/db.py:29`: невeрсионированные startup migrations рабочей БД.
- `app/web.py:554`: inline-раздача оригиналов с same-origin active-content риском.
- `data/`, `db/`, `auth-cookies.txt`, `.env`, `.session_secret`: секреты или пользовательские данные.

## Запрещено Без Явного Разрешения

- запускать приложение или scheduler против существующей `data/app.db` или `db/app.db`;
- вызывать `/rules/*/run`, `*/post-now` или напрямую `publish_file`;
- обращаться с реальным токеном к Telegram API, включая `getUpdates` и тестовые сообщения;
- читать, печатать или помещать в документацию bot tokens, cookies, session secrets, пароли и строки БД;
- удалять/перемещать БД, контент, previews, backups, channels, sources, rules или history;
- выполнять `docker compose down -v`, `rm`, destructive SQL, reset migrations или очистку volumes;
- запускать production Compose/Portainer stack или публиковать image;
- менять persisted schema без backup-плана и явного согласования;
- считать `auth-cookies.txt` безопасным тестовым файлом.

## Основные Команды

```bash
# Статическая проверка без запуска lifespan
python -m compileall -q app

# Проверка Compose без запуска контейнеров
docker compose config

# Сборка образа; сеть используется для пакетов, приложение не запускается
docker build -t telegram-folder-poster:latest .

# Только с разрешением пользователя
docker compose up -d
docker compose logs app
docker compose down
```

Безопасный smoke import выполняйте только с изолированными путями и заведомо фиктивным secret:

```bash
tmpdir="$(mktemp -d)"
APP_DATA_DIR="$tmpdir/data" APP_DB_PATH="$tmpdir/db/app.db" \
APP_SESSION_SECRET="test-only-not-for-production" \
python -c 'from app.main import app; print(len(app.routes))'
rm -rf "$tmpdir"
```

Не запускайте lifespan и не отправляйте HTTP POST к publication endpoints. В репозитории нет автоматических тестов, lint/type-check конфигурации или Alembic.

## Порядок Работы

1. Прочитать `docs/architecture.md`, профильный документ и `docs/known-issues.md`.
2. Проверить текущие файлы и локальные изменения; не перезаписывать чужие изменения.
3. Проследить путь UI form -> route -> service -> model до редактирования.
4. Для DB-изменения проверить как новую БД, так и upgrade существующей схемы в копии/временной БД.
5. Для scheduler/Telegram-изменения использовать mocks/fakes; запретить реальную сеть.
6. Добавить тест на исправляемый дефект. Если test infrastructure отсутствует, сначала согласовать минимальный pytest setup.
7. Выполнить проверки от узких к широким, не запуская production data или Telegram.
8. Обновить соответствующий документ и `docs/known-issues.md`.

## Проверка Изменений

- Python: `python -m compileall -q app`.
- Маршруты/config: import только с временными `APP_DATA_DIR`, `APP_DB_PATH`, `APP_SESSION_SECRET`.
- Compose: `docker compose config`.
- Templates/routes: проверить совпадение form action, имен полей, CSRF и redirect.
- DB: временная SQLite, `Base.metadata`, migration path, FK/cascade semantics.
- Scheduler: fake clock, изолированная DB, mocked scanner и Telegram client.
- Telegram: mocked `httpx`; покрыть success, 4xx/5xx, 429, timeout, malformed JSON, invalid caption и cleanup HEIF.
- Security: auth boundary, CSRF, active content, redirects, trusted proxy behavior.

## Критерии Готовности

- поведение подтверждено тестом или воспроизводимой безопасной проверкой;
- нет обращения к реальному Telegram и нет изменений пользовательских данных;
- ошибки не раскрывают токен, absolute paths или чувствительные ответы;
- миграция безопасна для существующей SQLite либо изменение схемы отсутствует;
- одна операция не может неожиданно удалить связанные настройки/историю;
- scheduler остается корректным при повторном/параллельном вызове;
- UI, API и документация согласованы;
- выполнены применимые проверки, а невыполненные явно перечислены;
- новые env vars добавлены в `.env.example` и `docs/configuration.md`;
- известная проблема закрыта или обновлена в `docs/known-issues.md`.

## Неподтвержденное

Production topology, reverse proxy, опубликованные registry images, реальный Telegram setup, поддерживаемая история schema upgrades и браузерная совместимость не определены репозиторием. Не предполагать их без данных пользователя.
