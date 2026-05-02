from __future__ import annotations

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


class TelegramPublishError(RuntimeError):
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


def render_caption(channel: TelegramChannel, rule: PostingRule, file_record: FileRecord) -> str | None:
    template = rule.caption_template or channel.default_caption
    if not template:
        return None

    file_path = Path(file_record.absolute_path)
    caption = template.format(
        filename=file_path.name,
        stem=file_path.stem,
        suffix=file_path.suffix,
        source=file_record.source.name,
        relative_path=file_record.relative_path,
    )
    return caption[:MAX_CAPTION_LENGTH]


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
        raise TelegramPublishError(f"Файл не найден: {file_record.absolute_path}")

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
    url = TELEGRAM_API_URL.format(token=channel.bot_token, method=method)
    payload = {
        "chat_id": chat_id,
        "disable_notification": str(channel.disable_notification).lower(),
        "protect_content": str(channel.protect_content).lower(),
    }
    caption = render_caption(channel, rule, file_record)
    if caption:
        payload["caption"] = caption
    if channel.parse_mode:
        payload["parse_mode"] = channel.parse_mode

    try:
        with actual_file_path.open("rb") as handle:
            files = {field_name: (actual_file_path.name, handle)}
            async with httpx.AsyncClient(timeout=180) as client:
                response = await client.post(url, data=payload, files=files)
    finally:
        if cleanup_path is not None:
            cleanup_path.unlink(missing_ok=True)

    try:
        result = response.json()
    except ValueError as exc:  # pragma: no cover
        raise TelegramPublishError(f"Неожиданный ответ Telegram API: {response.text}") from exc

    if response.status_code >= 400 or not result.get("ok"):
        description = result.get("description") or response.text
        raise TelegramPublishError(f"Ошибка Telegram API: {description}")

    message = result.get("result") or {}
    return str(message.get("message_id", ""))
