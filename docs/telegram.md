# Публикация В Telegram

## Конфигурация

`telegram_channels` хранит bot token, default chat ID, parse mode, caption и flags. Rule может переопределить chat ID/caption и принудительно отправить файл как document. Token хранится plaintext и подставляется в URL `https://api.telegram.org/bot{token}/{method}`.

Ни один документ или log не должен содержать реальный token. База и ее backups являются секретными.

## Выбор Telegram Method

| `media_kind` | Bot API method | Multipart field |
| --- | --- | --- |
| `photo` | `sendPhoto` | `photo` |
| `animation` | `sendAnimation` | `animation` |
| `video` | `sendVideo` | `video` |
| `document`/unknown | `sendDocument` | `document` |

`rule.send_as_document` принудительно выбирает document. HEIF container определяется по `ftyp` brands, а не только extension:

- `convert_heic_to_jpeg=true` и не document: временный JPEG через Pillow;
- иначе HEIF отправляется как document;
- temporary JPEG удаляется в `finally`.

## Подписи

Template берется из `rule.caption_template`, затем `channel.default_caption`. Поддерживаются поля:

- `{filename}`;
- `{stem}`;
- `{suffix}`;
- `{source}`;
- `{relative_path}`.

Используется обычный `str.format()`, после чего строка срезается до `MAX_CAPTION_LENGTH`. Неизвестный/malformed placeholder вызывает исключение, которое caller сейчас не нормализует. Truncation не учитывает parse-mode markup.

`parse_mode` передается как сохранено в channel и server-side whitelist не имеет. Caption для document тоже передается стандартным Telegram field `caption`.

## Основной Pipeline

1. Scheduler находит enabled due rule либо UI вызывает `process_rule(..., manual=True)`.
2. Загружаются channel и sources из `rule_sources` с fallback на legacy `source_id`.
3. Проверяются enabled sources и channel.
4. Scheduled run проверяет allowed hours; manual run пропускает проверку.
5. Due non-manual source получает синхронный full scan перед публикацией.
6. Picker выбирает active file из общего пула.
7. `publish_file` проверяет token, chat target и существование файла.
8. Файл при необходимости преобразуется и отправляется одним multipart POST через новый `httpx.AsyncClient(timeout=180)`.
9. Успех определяется по HTTP status и JSON `ok`; возвращается `result.message_id`.
10. Scheduler обновляет file counters, history, rule result и next/burst run в одной DB commit после внешнего call.

## Режимы Выбора

- `oldest_first`: never-posted, затем самый старый `last_posted_at`, затем discovery.
- `random_no_repeat`: random file без sent history этого rule; после exhaustion optional random повтор.
- `shuffle_cycle`: random unsent; после exhaustion выбирается oldest-posted. Persistent shuffle order отсутствует.
- другое значение, включая UI `random_with_repeat`: unrestricted random.

`source_selection_mode` фактически игнорируется; все linked sources образуют merged pool.

## Серии И Schedule

После successful publish `burst_post_count - 1` follow-up runs планируются через `burst_interval_minutes`. После последнего follow-up вычисляется обычный interval+jitter. Failed/skipped/scan-disabled paths сбрасывают burst и вычисляют normal next run.

Manual rule run также запускает burst state и пересчитывает schedule. Намеренность этого поведения не подтверждена. Direct selected-file post, если исправить текущий `NameError`, schedule не меняет.

## Обработка Ошибок

В `TelegramPublishError` преобразуются:

- отсутствующий token/chat ID/file;
- HEIF conversion failure;
- non-JSON Telegram response;
- HTTP error или JSON `ok=false`.

Не преобразуются и могут выйти из workflow:

- `httpx` timeout/network exceptions;
- file open race/permission errors;
- invalid caption formatting;
- другие unexpected runtime exceptions.

В таком случае scheduler не пишет failed history и не пересчитывает due rule. Повтор на каждом tick возможен. Retry/backoff для 429/5xx отсутствует; due rules обрабатываются последовательно, поэтому один request может блокировать остальные до 180 секунд.

## Конкурентность И Идемпотентность

До Telegram request отсутствуют reservation row, rule lock и idempotency key. `max_instances=1` защищает только одну APScheduler job внутри процесса. Manual calls и другие processes не входят в эту блокировку. Возможна двойная отправка одного файла до фиксации первой history row.

## Безопасное Тестирование

- Никогда не использовать реальный token/chat ID.
- Mock `httpx.AsyncClient.post` или весь `publish_file`.
- Использовать temporary files и temporary SQLite.
- Не запускать FastAPI lifespan без замены scheduler.
- Покрыть photo/animation/video/document, HEIF cleanup, 200/400/429/500, timeout, malformed JSON и malformed caption.
- Проверять, что error messages/log records не содержат token URL.

## Неподтвержденное

- Telegram permissions реальных bots/channels.
- Допустимые media limits и API behavior конкретного Telegram deployment на текущую дату.
- Желаемая retry policy и semantics manual burst.
