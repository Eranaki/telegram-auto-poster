from __future__ import annotations

import html
import re
import tempfile
import warnings
from pathlib import Path

import httpx
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from app.config import MAX_CAPTION_LENGTH
from app.models import FileRecord, PostingRule, TelegramChannel

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/{method}"
METHODS = {
    "photo": ("sendPhoto", "photo"),
    "animation": ("sendAnimation", "animation"),
    "video": ("sendVideo", "video"),
    "document": ("sendDocument", "document"),
}
MISSING_FILE_ERROR_PREFIX = "Файл не найден:"
PHOTO_INVALID_DIMENSIONS = "PHOTO_INVALID_DIMENSIONS"
PHOTO_DOCUMENT_FALLBACK_ERROR_PREFIX = "Не удалось отправить проблемное фото как документ:"
MISSING_FILE_REQUEUED_MARKER = "Файл возвращен в очередь"
PHOTO_MAX_DIMENSION_SUM = 10_000
PHOTO_RESIZE_DIMENSION_SUM = 9_900
PHOTO_MAX_ASPECT_RATIO = 20
PHOTO_TARGET_BYTES = 9_500_000
PHOTO_MAX_DECODE_PIXELS = 40_000_000
PROCESSING_LOG_MAX_ENTRIES = 32
PROCESSING_LOG_MAX_ENTRY_LENGTH = 500


class TelegramPublishError(RuntimeError):
    pass


class RequeueableFilePublishError(TelegramPublishError):
    pass


class FileNotFoundPublishError(RequeueableFilePublishError):
    pass


class PhotoDocumentFallbackError(RequeueableFilePublishError):
    pass


HEIF_BRANDS = (
    b"heic",
    b"heix",
    b"hevc",
    b"hevx",
    b"heim",
    b"heis",
    b"mif1",
    b"msf1",
)

register_heif_opener()


def is_requeueable_history_message(message: str | None, media_kind: str | None = None) -> bool:
    return bool(
        message
        and (
            message.startswith((MISSING_FILE_ERROR_PREFIX, PHOTO_DOCUMENT_FALLBACK_ERROR_PREFIX))
            or (
                media_kind == "photo"
                and message == f"Ошибка Telegram API: Bad Request: {PHOTO_INVALID_DIMENSIONS}"
            )
        )
    )


def add_processing_log(processing_log: list[str] | None, message: str) -> None:
    if processing_log is not None and len(processing_log) < PROCESSING_LOG_MAX_ENTRIES:
        processing_log.append(message[:PROCESSING_LOG_MAX_ENTRY_LENGTH])


def format_processing_size(size: int) -> str:
    if size < 1024:
        return f"{size} Б"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} КБ"
    return f"{size / (1024 * 1024):.1f} МБ"


def escape_caption_value(value: str, parse_mode: str | None) -> str:
    normalized_mode = (parse_mode or "").lower()
    if normalized_mode == "html":
        return html.escape(value, quote=False)
    if normalized_mode == "markdownv2":
        return re.sub(r"([_\-*\[\]()~`>#+=|{}.!\\])", r"\\\1", value)
    if normalized_mode == "markdown":
        return re.sub(r"([_*\[\]()`\\])", r"\\\1", value)
    return value


def render_caption(channel: TelegramChannel, rule: PostingRule, file_record: FileRecord) -> str | None:
    template = rule.caption_template or channel.default_caption
    file_path = Path(file_record.absolute_path)
    caption = ""
    if template:
        escaped_values = {
            "filename": escape_caption_value(file_path.name, channel.parse_mode),
            "stem": escape_caption_value(file_path.stem, channel.parse_mode),
            "suffix": escape_caption_value(file_path.suffix, channel.parse_mode),
            "source": escape_caption_value(file_record.source.name, channel.parse_mode),
            "relative_path": escape_caption_value(file_record.relative_path, channel.parse_mode),
        }
        try:
            caption = template.format(**escaped_values)
        except (AttributeError, KeyError, ValueError, IndexError) as exc:
            raise TelegramPublishError("Не удалось сформировать подпись: проверьте шаблон") from exc

    if getattr(rule, "include_filename_in_caption", False):
        filename = (
            file_record.relative_path
            if getattr(rule, "include_file_path_in_caption", False)
            else Path(file_record.relative_path).name
        )
        escaped_filename = escape_caption_value(filename, channel.parse_mode)
        caption = f"{caption}\n{escaped_filename}" if caption else escaped_filename

    if not caption:
        return None
    if len(caption) > MAX_CAPTION_LENGTH:
        raise TelegramPublishError(
            f"Подпись слишком длинная: {len(caption)} символов при лимите {MAX_CAPTION_LENGTH}"
        )
    return caption


def is_heif_container(file_path: Path) -> bool:
    try:
        with file_path.open("rb") as handle:
            header = handle.read(32)
    except OSError:
        return False

    if len(header) < 12 or header[4:8] != b"ftyp":
        return False

    brands = (header[8:12], header[16:20], header[20:24])
    return any(brand in HEIF_BRANDS for brand in brands if brand)


def convert_heif_to_jpeg(file_path: Path) -> Path:
    temp_path: Path | None = None
    try:
        with Image.open(file_path) as image:
            image.load()
            if image.mode in {"RGBA", "LA"}:
                background = Image.new("RGB", image.size, (255, 255, 255))
                background.paste(image.convert("RGB"), mask=image.getchannel("A"))
                converted = background
            else:
                converted = image.convert("RGB")

            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
                temp_path = Path(temp_file.name)

            converted.save(temp_path, format="JPEG", quality=95, optimize=True)
            return temp_path
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def photo_dimensions_are_valid(width: int, height: int) -> bool:
    return bool(
        width > 0
        and height > 0
        and width + height <= PHOTO_MAX_DIMENSION_SUM
        and max(width, height) / min(width, height) <= PHOTO_MAX_ASPECT_RATIO
    )


def prepare_large_photo(file_path: Path, processing_log: list[str] | None = None) -> tuple[Path | None, bool]:
    temp_path: Path | None = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(file_path)
        with image:
            width, height = image.size
            orientation = image.getexif().get(274)
            if orientation in {5, 6, 7, 8}:
                width, height = height, width
            if width <= 0 or height <= 0:
                add_processing_log(processing_log, "Причина: изображение содержит некорректные размеры.")
                add_processing_log(processing_log, "Действие: перекодирование пропущено, выбран исходный документ.")
                return None, True
            if max(width, height) / min(width, height) > PHOTO_MAX_ASPECT_RATIO:
                add_processing_log(
                    processing_log,
                    f"Причина: пропорция {max(width, height) / min(width, height):.1f}:1 превышает лимит 20:1.",
                )
                add_processing_log(processing_log, "Действие: crop/stretch не применялись, выбран исходный документ.")
                return None, True

            original_size = file_path.stat().st_size
            needs_resize = width + height > PHOTO_MAX_DIMENSION_SUM
            needs_recompress = original_size > PHOTO_TARGET_BYTES
            if not needs_resize and not needs_recompress:
                return None, False
            add_processing_log(
                processing_log,
                f"Исходник: {width}x{height}, {format_processing_size(original_size)}.",
            )
            if needs_resize:
                add_processing_log(processing_log, "Причина: сумма сторон превышает 10 000 пикселей.")
            if needs_recompress:
                add_processing_log(processing_log, "Причина: размер файла превышает 9,5 МБ.")
            if width * height > PHOTO_MAX_DECODE_PIXELS:
                add_processing_log(processing_log, "Действие: decode пропущен из-за лимита 40 МП, выбран исходный документ.")
                return None, True

            image.load()
            prepared = ImageOps.exif_transpose(image)
            if orientation not in {None, 1}:
                add_processing_log(processing_log, f"Действие: применена EXIF orientation {orientation}.")

            if needs_resize:
                scale = PHOTO_RESIZE_DIMENSION_SUM / (width + height)
                resized_dimensions = (
                    max(1, round(width * scale)),
                    max(1, round(height * scale)),
                )
                if not photo_dimensions_are_valid(*resized_dimensions):
                    add_processing_log(processing_log, "Действие: безопасный resize невозможен, выбран исходный документ.")
                    return None, True
                prepared = prepared.resize(resized_dimensions, Image.Resampling.LANCZOS)
                add_processing_log(
                    processing_log,
                    f"Действие: размер уменьшен до {resized_dimensions[0]}x{resized_dimensions[1]}.",
                )

            if prepared.mode in {"RGBA", "LA"} or (prepared.mode == "P" and "transparency" in prepared.info):
                rgba_image = prepared.convert("RGBA")
                rgb_image = Image.new("RGB", rgba_image.size, (255, 255, 255))
                rgb_image.paste(rgba_image, mask=rgba_image.getchannel("A"))
                add_processing_log(processing_log, "Действие: прозрачность заменена белым фоном.")
            else:
                rgb_image = prepared.convert("RGB")

            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
                temp_path = Path(temp_file.name)

            for quality in (90, 85, 80, 75, 70, 65):
                rgb_image.save(temp_path, format="JPEG", quality=quality, optimize=True)
                if temp_path.stat().st_size <= PHOTO_TARGET_BYTES and photo_dimensions_are_valid(*rgb_image.size):
                    add_processing_log(
                        processing_log,
                        f"Результат обработки: JPEG {rgb_image.width}x{rgb_image.height}, "
                        f"quality {quality}, {format_processing_size(temp_path.stat().st_size)}.",
                    )
                    return temp_path, False

            for _ in range(20):
                current_width, current_height = rgb_image.size
                if current_width <= 1 and current_height <= 1:
                    break
                rgb_image = rgb_image.resize(
                    (max(1, int(current_width * 0.85)), max(1, int(current_height * 0.85))),
                    Image.Resampling.LANCZOS,
                )
                rgb_image.save(temp_path, format="JPEG", quality=80, optimize=True)
                if temp_path.stat().st_size <= PHOTO_TARGET_BYTES and photo_dimensions_are_valid(*rgb_image.size):
                    add_processing_log(
                        processing_log,
                        f"Результат обработки: JPEG {rgb_image.width}x{rgb_image.height}, "
                        f"quality 80, {format_processing_size(temp_path.stat().st_size)}.",
                    )
                    return temp_path, False
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        add_processing_log(processing_log, "Результат обработки: изображение не удалось декодировать или сохранить.")
        add_processing_log(processing_log, "Действие: выбран исходный документ.")
        return None, True

    if temp_path is not None:
        temp_path.unlink(missing_ok=True)
    add_processing_log(processing_log, "Результат обработки: не удалось уложиться в лимит 9,5 МБ.")
    add_processing_log(processing_log, "Действие: выбран исходный документ.")
    return None, True


async def publish_file(
    channel: TelegramChannel,
    rule: PostingRule,
    file_record: FileRecord,
    processing_log: list[str] | None = None,
) -> str:
    if not channel.bot_token:
        raise TelegramPublishError("Не заполнен токен бота")

    chat_id = rule.chat_id_override or channel.chat_id
    if not chat_id:
        raise TelegramPublishError("Не указан chat_id или имя канала")

    file_path = Path(file_record.absolute_path)
    if not file_path.exists():
        raise FileNotFoundPublishError(f"{MISSING_FILE_ERROR_PREFIX} {file_record.relative_path}")

    payload = {
        "chat_id": chat_id,
        "disable_notification": str(channel.disable_notification).lower(),
        "protect_content": str(channel.protect_content).lower(),
    }
    if not rule.chat_id_override and channel.message_thread_id is not None:
        payload["message_thread_id"] = str(channel.message_thread_id)
    caption = render_caption(channel, rule, file_record)
    if caption:
        payload["caption"] = caption
    if channel.parse_mode:
        payload["parse_mode"] = channel.parse_mode

    actual_file_path = file_path
    cleanup_paths: list[Path] = []
    is_heif = is_heif_container(file_path)

    if is_heif and rule.convert_heic_to_jpeg and not rule.send_as_document:
        try:
            actual_file_path = convert_heif_to_jpeg(file_path)
            cleanup_paths.append(actual_file_path)
            add_processing_log(processing_log, "Действие: HEIF перекодирован во временный JPEG quality 95.")
        except Exception as exc:  # pragma: no cover - runtime protection
            add_processing_log(processing_log, "Результат обработки: HEIF не удалось перекодировать.")
            raise TelegramPublishError(f"Не удалось конвертировать HEIC в JPEG: {exc}") from exc

    force_document = is_heif and not rule.convert_heic_to_jpeg
    media_kind = "document" if rule.send_as_document or force_document else file_record.media_kind
    if force_document:
        add_processing_log(processing_log, "Действие: HEIF-конвертация выключена, выбран исходный документ.")
    optimizer_document = False
    if media_kind == "photo" and getattr(rule, "optimize_large_photos", False):
        optimized_path, optimize_as_document = prepare_large_photo(actual_file_path, processing_log)
        if optimize_as_document:
            actual_file_path = file_path
            media_kind = "document"
            optimizer_document = True
        elif optimized_path is not None:
            actual_file_path = optimized_path
            cleanup_paths.append(optimized_path)
    method, field_name = METHODS.get(media_kind, METHODS["document"])

    fallback_from_invalid_photo = False
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            while True:
                url = TELEGRAM_API_URL.format(token=channel.bot_token, method=method)
                upload_path = file_path if fallback_from_invalid_photo else actual_file_path
                try:
                    with upload_path.open("rb") as handle:
                        files = {field_name: (upload_path.name, handle)}
                        response = await client.post(url, data=payload, files=files)
                except FileNotFoundError as exc:
                    add_processing_log(processing_log, "Итог: файл исчез до завершения отправки.")
                    raise FileNotFoundPublishError(
                        f"{MISSING_FILE_ERROR_PREFIX} {file_record.relative_path}"
                    ) from exc
                except (OSError, httpx.HTTPError) as exc:
                    add_processing_log(processing_log, "Итог: отправка прервана из-за ошибки чтения или сети.")
                    raise TelegramPublishError("Не удалось отправить файл из-за ошибки чтения или сети") from exc

                try:
                    result = response.json()
                except ValueError as exc:  # pragma: no cover
                    if fallback_from_invalid_photo or optimizer_document:
                        add_processing_log(processing_log, "Итог: sendDocument вернул неожиданный ответ, попытки прекращены.")
                        raise PhotoDocumentFallbackError(
                            f"{PHOTO_DOCUMENT_FALLBACK_ERROR_PREFIX} неожиданный ответ Telegram API"
                        ) from exc
                    if processing_log:
                        add_processing_log(processing_log, f"Итог: {method} вернул неожиданный ответ.")
                    raise TelegramPublishError(f"Неожиданный ответ Telegram API: {response.text}") from exc

                if response.status_code < 400 and result.get("ok"):
                    if processing_log:
                        add_processing_log(processing_log, f"Итог: файл успешно отправлен через {method}.")
                    message = result.get("result") or {}
                    return str(message.get("message_id", ""))

                description = result.get("description") or response.text
                if method == "sendPhoto" and PHOTO_INVALID_DIMENSIONS in description:
                    add_processing_log(processing_log, "Результат sendPhoto: PHOTO_INVALID_DIMENSIONS.")
                    add_processing_log(processing_log, "Действие: выполняется единственная повторная отправка исходника как документа.")
                    method, field_name = METHODS["document"]
                    fallback_from_invalid_photo = True
                    continue
                if fallback_from_invalid_photo or optimizer_document:
                    add_processing_log(processing_log, "Итог: sendDocument отклонен, дальнейших попыток не будет.")
                    raise PhotoDocumentFallbackError(
                        f"{PHOTO_DOCUMENT_FALLBACK_ERROR_PREFIX} {description}"
                    )
                if processing_log:
                    add_processing_log(processing_log, f"Итог: {method} отклонен Telegram.")
                raise TelegramPublishError(f"Ошибка Telegram API: {description}")
    finally:
        for cleanup_path in cleanup_paths:
            cleanup_path.unlink(missing_ok=True)
