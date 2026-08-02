# HTTP API И Маршруты

## Общие Правила

- Приложение предназначено для server-rendered browser UI, а не стабильного public REST API.
- Все routes, кроме `/login`, `/favicon.ico` и `/static*`, требуют signed session с `admin_authenticated`.
- Все application POST routes требуют form field `csrf_token`.
- POST payloads являются HTML form data; большинство ответов `303` redirect или HTML.
- JSON используется только для content path options и scan status.
- FastAPI также создает `/docs`, `/redoc` и `/openapi.json`; auth middleware защищает их, но schema не описывает middleware session requirement полностью.
- Route/version prefix отсутствует.

## Аутентификация И Dashboard

| Метод | Путь | Назначение / side effects |
| --- | --- | --- |
| GET | `/login` | Public login form, создает CSRF session token |
| POST | `/login` | Public + CSRF; throttle check, проверка admin, установка session |
| POST | `/logout` | Очищает session |
| GET | `/account` | Форма account; может lazy-create admin |
| POST | `/account` | Меняет username/password hash |
| GET | `/` | Dashboard channels/stats/recent history |

## Источники И Файлы

| Метод | Путь | Назначение / side effects |
| --- | --- | --- |
| GET | `/sources` | Список глобальных sources |
| GET | `/content-paths/options` | JSON `{paths: [...]}` из process cache/runtime `/content` |
| POST | `/content-paths/refresh` | Сбрасывает cache и пересканирует path options |
| POST | `/settings/file-browser` | Сохраняет singleton UI preferences |
| GET | `/sources/{source_id}/files` | Фильтры/pagination; query `view|size|per_page` записывает preferences |
| GET | `/files/{file_id}/thumbnail` | Отдает/generates PNG либо SVG placeholder |
| GET | `/files/{file_id}/original` | Отдает original inline; см. security issue |
| GET | `/sources/{source_id}/scan-status` | JSON persisted scan progress |
| POST | `/sources` | Создает source, scan не запускает |
| POST | `/sources/{source_id}/edit` | Изменяет source, scan не запускает |
| POST | `/sources/{source_id}/toggle` | Переключает enabled |
| POST | `/sources/{source_id}/scan-full` | Ставит full scan и запускает daemon thread |
| POST | `/sources/{source_id}/scan-add` | Запускает add-missing scan |
| POST | `/sources/{source_id}/delete` | Удаляет source; потенциально rules/history через cascade |

`scan-status` response:

```json
{
  "id": 1,
  "scan_in_progress": true,
  "scan_mode": "full",
  "scan_mode_label": "Полный рескан",
  "scan_progress_current": 25,
  "scan_progress_total": 100,
  "scan_progress_percent": 25,
  "last_scan_result": "..."
}
```

## Каналы И Связи Источников

| Метод | Путь | Назначение / side effects |
| --- | --- | --- |
| GET | `/channels/{channel_id}` | Alias overview |
| GET | `/channels/{channel_id}/overview` | Channel settings/stats/history |
| GET | `/channels/{channel_id}/rules` | Rules и import candidates |
| GET | `/channels/{channel_id}/sources` | Attached/attachable sources |
| GET | `/channels/{channel_id}/history` | Filtered channel history |
| POST | `/channels` | Создает channel; принимает optional positive integer `message_thread_id`, сохраняет token plaintext |
| POST | `/channels/{channel_id}/settings` | Обновляет Telegram settings, включая optional forum topic ID |
| POST | `/channels/{channel_id}/toggle` | Переключает enabled |
| POST | `/channels/{channel_id}/delete` | Удаляет channel и ORM-dependent rules/history |
| POST | `/channels/{channel_id}/sources/attach` | Создает channel-source link |
| POST | `/channels/{channel_id}/sources/create` | Создает global source и link в одной transaction |
| POST | `/channels/{channel_id}/sources/{source_id}/detach` | Удаляет link, если source не используется rule канала |

## Правила, Очередь И История

| Метод | Путь | Назначение / side effects |
| --- | --- | --- |
| GET | `/channels/{channel_id}/rules/{rule_id}/edit` | Rule edit form, проверяет ownership |
| GET | `/rules/{rule_id}/queue` | Preview до 30 candidates; сейчас `NameError` |
| GET | `/history` | Global filtered/paginated history |
| POST | `/rules` | Создает rule и source links; enabled rule сразу due (`next_run_at=NULL`) |
| POST | `/channels/{channel_id}/rules/import` | Копирует rule; сейчас `NameError` после flush |
| POST | `/channels/{channel_id}/rules/{rule_id}/edit` | Обновляет rule, links и `next_run_at` |
| POST | `/rules/{rule_id}/toggle` | Переключает enabled без пересчета schedule |
| POST | `/rules/{rule_id}/run` | Немедленно запускает scan/select/Telegram pipeline |
| POST | `/rules/{rule_id}/files/{file_id}/post-now` | Intended direct post; сейчас `NameError` до Telegram call |
| POST | `/rules/{rule_id}/delete` | Удаляет rule, links и history |

Подтвержденный defect: `app/web.py` не импортирует `get_rule_source_ids` и `already_sent_exists`, хотя использует их в queue/import/post-now. Эти endpoints нельзя считать рабочими.

## Статика И Framework Routes

| Метод | Путь | Назначение |
| --- | --- | --- |
| GET | `/static/{path}` | Public static files |
| GET | `/docs` | Swagger UI, требуется session из-за middleware |
| GET | `/redoc` | ReDoc, требуется session |
| GET | `/openapi.json` | OpenAPI schema, требуется session |

Auth exemption использует `path.startswith("/static")`, то есть шире фактического mount `/static`.

## Ошибки

- Route handlers обычно используют 404 для missing entity и 400 для validation.
- CSRF HTML requests получают custom 403 page.
- Validation UX неоднороден: create forms часто re-render, edit rule возвращает raw HTTP error.
- Нет общего exception normalization для Telegram/network failures.
- Redirects после ряда actions используют raw `Referer` или `return_to` и могут быть внешними.

## Совместимость

API versioning и external consumer contract отсутствуют. При изменении route/form field одновременно проверяйте templates, CSRF injection и redirects. Не вызывайте publication POST endpoints в smoke tests; dependency override сам по себе не отключает scheduler lifecycle.
