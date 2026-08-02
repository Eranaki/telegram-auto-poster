from __future__ import annotations

import html
import re
import tempfile
from pathlib import Path

import httpx
from PIL import Image
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


async def publish_file(channel: TelegramChannel, rule: PostingRule, file_record: FileRecord) -> str:
    if not channel.bot_token:
        raise TelegramPublishError("Не заполнен токен бота")

    chat_id = rule.chat_id_override or channel.chat_id
    if not chat_id:
        raise TelegramPublishError("Не указан chat_id или имя канала")

    file_path = Path(file_record.absolute_path)
    if not file_path.exists():
        raise FileNotFoundPublishError(f"{MISSING_FILE_ERROR_PREFIX} {file_record.relative_path}")

    actual_file_path = file_path
    cleanup_path: Path | None = None
    is_heif = is_heif_container(file_path)

    if is_heif and rule.convert_heic_to_jpeg and not rule.send_as_document:
        try:
            actual_file_path = convert_heif_to_jpeg(file_path)
            cleanup_path = actual_file_path
        except Exception as exc:  # pragma: no cover - runtime protection
            raise TelegramPublishError(f"Не удалось конвертировать HEIC в JPEG: {exc}") from exc

    force_document = is_heif and not rule.convert_heic_to_jpeg
    media_kind = "document" if rule.send_as_document or force_document else file_record.media_kind
    method, field_name = METHODS.get(media_kind, METHODS["document"])
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

    fallback_from_invalid_photo = False
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            while True:
                url = TELEGRAM_API_URL.format(token=channel.bot_token, method=method)
                upload_path = file_path if fallback_from_invalid_photo else actual_file_path
                with upload_path.open("rb") as handle:
                    files = {field_name: (upload_path.name, handle)}
                    response = await client.post(url, data=payload, files=files)

                try:
                    result = response.json()
                except ValueError as exc:  # pragma: no cover
                    if fallback_from_invalid_photo:
                        raise PhotoDocumentFallbackError(
                            f"{PHOTO_DOCUMENT_FALLBACK_ERROR_PREFIX} неожиданный ответ Telegram API"
                        ) from exc
                    raise TelegramPublishError(f"Неожиданный ответ Telegram API: {response.text}") from exc

                if response.status_code < 400 and result.get("ok"):
                    message = result.get("result") or {}
                    return str(message.get("message_id", ""))

                description = result.get("description") or response.text
                if method == "sendPhoto" and PHOTO_INVALID_DIMENSIONS in description:
                    method, field_name = METHODS["document"]
                    fallback_from_invalid_photo = True
                    continue
                if fallback_from_invalid_photo:
                    raise PhotoDocumentFallbackError(
                        f"{PHOTO_DOCUMENT_FALLBACK_ERROR_PREFIX} {description}"
                    )
                raise TelegramPublishError(f"Ошибка Telegram API: {description}")
    finally:
        if cleanup_path is not None:
            cleanup_path.unlink(missing_ok=True)
