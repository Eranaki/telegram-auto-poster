# Конфигурация

Все runtime-переменные читаются в `app/config.py` при импорте. Невалидные integer values останавливают import/startup с `ValueError`. Изменения требуют перезапуска процесса.

| Переменная | Default в коде | Назначение | Примечания |
| --- | --- | --- | --- |
| `APP_DATA_DIR` | `<repo>/data` | Корень previews и generated session secret | Compose задает `/data`; каталог создается при импорте |
| `APP_DB_PATH` | `${APP_DATA_DIR}/app.db` | Путь SQLite для default URL | Compose задает `/db/app.db`; parent создается при импорте |
| `DATABASE_URL` | `sqlite:///${APP_DB_PATH}` | Полный SQLAlchemy URL | Формально override; фактически non-SQLite migrations не поддержаны |
| `APP_TIMEZONE` | `UTC` | Timezone объекта APScheduler | Не управляет naive business-time calculations, подтвержденный дефект |
| `SCHEDULER_TICK_SECONDS` | `30` | Период поиска due rules | Integer, нет lower-bound validation |
| `SCAN_TICK_SECONDS` | `120` | Период source scan job и preview backfill | Integer, обе jobs используют одно значение |
| `MAX_CAPTION_LENGTH` | `1024` | Slice rendered caption перед Telegram | Integer; truncation может разорвать HTML entity/tag |
| `PREVIEW_BATCH_SIZE` | `24` | Максимум generated previews за pass | Integer; отрицательные/нулевые значения отдельно не проверяются |
| `APP_SESSION_SECRET` | пусто | Подпись `tap_session` | Для production обязателен стабильный длинный random secret |
| `APP_SESSION_HTTPS_ONLY` | `false` | Cookie `Secure` | True для `1`, `true`, `yes`, `on` без учета регистра |

`APP_SESSION_SECRET` при пустом значении загружается из `${APP_DATA_DIR}/.session_secret`. Если файла нет, генерируется 48-byte URL-safe value и записывается туда. Если запись не удалась, secret остается только в памяти, и restart инвалидирует sessions. Permissions файла явно не ограничиваются кодом.

## Compose Значения

Root `docker-compose.yml` задает:

```yaml
APP_DATA_DIR: /data
APP_DB_PATH: /db/app.db
APP_TIMEZONE: Europe/Moscow
SCHEDULER_TICK_SECONDS: 30
SCAN_TICK_SECONDS: 120
```

Он не передает `APP_SESSION_SECRET`, `APP_SESSION_HTTPS_ONLY`, `DATABASE_URL`, `MAX_CAPTION_LENGTH` и `PREVIEW_BATCH_SIZE`; для них действуют defaults кода. `.env.example` перечисляет все runtime-переменные. При добавлении новых значений синхронизируйте код, Compose example, `.env.example` и этот файл.

## Runtime Paths

| Путь в Compose | Host bind | Содержимое |
| --- | --- | --- |
| `/data` | `./data` | previews и generated `.session_secret` |
| `/db` | `./db` | `app.db` |
| `/example` | `./content` | Текущий root Compose mount; расходится с ожидаемым `/content` |

Приложение обнаруживает sources под `/content`, поэтому текущий destination `/example` следует заменить на `/content`. Прямой запуск без env использует `<repo>/data/app.db`, тогда как Compose использует `<repo>/db/app.db`. Это две разные базы.

## Не Environment Настройки

Следующие значения хранятся в SQLite и управляются UI:

- bot token, chat ID, parse mode, default caption и Telegram flags;
- sources и scan intervals;
- rule timing, selection, caption override и HEIF behavior;
- admin username/password hash;
- file browser preferences.

Bot tokens не шифруются. Не добавляйте их в `.env.example`, документацию, logs или issue reports.

## Build И Release Variables

Workflows в `.github/workflows/` используют:

| Имя | Назначение |
| --- | --- |
| `IMAGE_TAG` | Tag published image, default `latest` |
| `DOCKERHUB_USERNAME` | Docker Hub namespace/login workflow input |
| `DOCKERHUB_TOKEN` | GitHub Actions secret для Docker Hub |
| `GITHUB_TOKEN` | Автоматический GitHub Actions token для GHCR |
| `IMAGE_NAME` | Workflow-level value `telegram-auto-poster` |

Наличие Docker Hub secrets и актуальность опубликованных tags не подтверждены статическим анализом.
