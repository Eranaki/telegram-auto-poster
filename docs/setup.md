# Установка И Запуск

## Docker Compose

Требования: Docker Engine и Compose plugin. Root Compose использует опубликованный image `ghcr.io/eranaki/telegram-auto-poster:latest`.

```bash
mkdir -p data db content
docker compose pull
docker compose config
docker compose up -d
```

Панель: `http://localhost:1338`. Healthcheck обращается к `/login` внутри контейнера.

Начальный вход: `admin` / `admin`. Это публично известные credentials; смените их до открытия порта недоверенной сети.

### Подключение Контента

Добавьте read-only mounts в `docker-compose.yml`:

```yaml
services:
  app:
    volumes:
      - ./data:/data
      - ./db:/db
      - /srv/media/photos:/content/photos:ro
      - /srv/media/video:/content/video:ro
```

В текущем root Compose строка `./content:/example:ro` расходится с приложением и README: UI ищет каталоги только под `/content`. До исправления Compose замените destination на `./content:/content:ro` либо используйте собственные mounts `/content/...` из примера выше.

После изменения:

```bash
docker compose up -d
```

В UI обновите список content paths, создайте source и выполните scan. Не монтируйте чувствительные каталоги контейнера или host в `/content`.

### Production Минимум

Root Compose не является hardened production deployment. Перед публикацией:

- задайте длинный случайный `APP_SESSION_SECRET`;
- поставьте HTTPS reverse proxy и `APP_SESSION_HTTPS_ONLY=true`;
- ограничьте port `1338` firewall/VPN либо bind address;
- смените admin credentials;
- оставьте одну replica и одного Uvicorn worker;
- защитите `/db`, `/data` и backups правами filesystem;
- настройте проверяемые SQLite backups;
- не используйте placeholder secret из Portainer template.

### HTTPS Reverse Proxy

Прокси должен передавать исходную схему и адрес приложения:

```nginx
location / {
    proxy_pass http://127.0.0.1:1338;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

Uvicorn принимает forwarded headers только от доверенных адресов. По умолчанию доверен `127.0.0.1`; если прокси находится в другом контейнере или на другом адресе, задайте его IP или подсеть через `FORWARDED_ALLOW_IPS`. Не используйте `*`, если прямой доступ к Uvicorn не закрыт и прокси не удаляет входящие spoofed headers.

Ссылки на CSS в шаблонах являются path-only и поэтому не зависят от того, видит приложение входящую схему как `http` или `https`. Forwarded headers все равно нужны для корректных redirects и других absolute URLs. Если прокси удаляет prefix, например `/tap`, дополнительно настройте Uvicorn `--root-path /tap` и согласованный routing прокси.

Пример environment fragment:

```yaml
environment:
  APP_DATA_DIR: /data
  APP_DB_PATH: /db/app.db
  APP_TIMEZONE: Europe/Moscow
  APP_SESSION_SECRET: ${APP_SESSION_SECRET:?set APP_SESSION_SECRET}
  APP_SESSION_HTTPS_ONLY: "true"
```

Обратите внимание: `APP_TIMEZONE` сейчас задает timezone APScheduler, но business calculations используют naive system-local `datetime.now()`. До исправления нельзя полагаться на эту переменную для allowed hours.

## Локальный Python

Подтвержденная container runtime версия Python: 3.12. Прямой запуск возможен из структуры кода, но отдельная developer configuration не предоставлена.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
APP_DATA_DIR=./.local-data \
APP_DB_PATH=./.local-db/app.db \
APP_SESSION_SECRET=local-development-only \
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Нужны `ffmpeg` и `ffprobe` в `PATH` для video previews. Startup создаст/изменит указанную БД, создаст admin и запустит scheduler. Не указывайте существующие project `data/`/`db/` для экспериментов.

## Обновление

Перед обновлением сделайте согласованную backup SQLite и `/data/.session_secret`, если secret генерируется автоматически.

```bash
docker compose pull
docker compose up -d
```

На startup приложение автоматически выполняет ad hoc migrations без revision ledger и rollback. Сначала проверяйте upgrade на копии БД. Для собственного image соберите отдельный tag и замените `image:` в Compose перед запуском.

## Резервное Копирование

Критичные данные:

- `/db/app.db` в root Compose;
- `/data/.session_secret`, если `APP_SESSION_SECRET` не задан;
- `/content` или возможность восстановить те же файлы/пути.

`/data/previews` можно пересоздать. Простое копирование активного SQLite-файла может быть несогласованным; используйте остановленное приложение либо SQLite backup mechanism.

## Проверки Без Запуска Сервиса

```bash
python -m compileall -q app
docker compose config
```

Сборка image безопасна относительно Telegram, пока контейнер не запускается:

```bash
docker build -t telegram-folder-poster:latest .
```

Автоматических tests и quality gates в репозитории нет.

## Диагностика

```bash
docker compose ps
docker compose logs app
```

Не публикуйте logs без редактирования: URL Telegram request потенциально может содержать bot token. Не используйте `docker compose down -v`: bind/volume semantics и последствия удаления должны быть проверены отдельно.
