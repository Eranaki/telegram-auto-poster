from __future__ import annotations

from io import BytesIO
from pathlib import Path
import subprocess
from xml.sax.saxutils import escape

from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import PREVIEWS_DIR
from app.models import FileRecord

PREVIEWABLE_MEDIA_KINDS = {"photo", "animation", "video"}
PREVIEW_PLACEHOLDER_LABELS = {
    "photo": "Фото",
    "animation": "GIF",
    "video": "Видео",
    "document": "Документ",
}

register_heif_opener()
ImageFile.LOAD_TRUNCATED_IMAGES = True


def can_generate_preview(file_record: FileRecord) -> bool:
    return file_record.media_kind in PREVIEWABLE_MEDIA_KINDS and file_record.is_active


def get_preview_path(file_record: FileRecord, thumbnail_size_px: int) -> Path:
    directory = PREVIEWS_DIR / file_record.media_kind
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{file_record.id}-{file_record.fingerprint}-{thumbnail_size_px}.png"


def preview_exists(file_record: FileRecord, thumbnail_size_px: int) -> bool:
    return get_preview_path(file_record, thumbnail_size_px).exists()


def generate_preview(file_record: FileRecord, thumbnail_size_px: int) -> Path | None:
    if not can_generate_preview(file_record):
        return None

    source_path = Path(file_record.absolute_path)
    if not source_path.exists() or not source_path.is_file():
        return None

    preview_path = get_preview_path(file_record, thumbnail_size_px)
    if preview_path.exists():
        return preview_path

    try:
        if file_record.media_kind == "video":
            preview_bytes = generate_video_preview_bytes(source_path)
            if not preview_bytes:
                return None
            with Image.open(BytesIO(preview_bytes)) as image:
                save_preview_image(image, file_record, preview_path, thumbnail_size_px)
        else:
            with Image.open(source_path) as image:
                save_preview_image(image, file_record, preview_path, thumbnail_size_px)
    except (FileNotFoundError, OSError, UnidentifiedImageError):
        return None

    return preview_path if preview_path.exists() else None


def save_preview_image(
    image: Image.Image,
    file_record: FileRecord,
    preview_path: Path,
    thumbnail_size_px: int,
) -> None:
    image = ImageOps.exif_transpose(image)
    if file_record.media_kind == "animation":
        image.seek(0)

    image = image.convert("RGBA")
    image.thumbnail((thumbnail_size_px, thumbnail_size_px))

    background = Image.new("RGBA", image.size, (255, 255, 255, 0))
    background.paste(image, (0, 0), image)

    buffer = BytesIO()
    background.save(buffer, format="PNG", optimize=True)
    preview_path.write_bytes(buffer.getvalue())


def generate_video_preview_bytes(source_path: Path) -> bytes | None:
    preview_offset = compute_video_preview_offset_seconds(source_path)
    for offset in (preview_offset, 0.0):
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{offset:.3f}",
            "-i",
            str(source_path),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None

        if result.returncode == 0 and result.stdout:
            return result.stdout
    return None


def compute_video_preview_offset_seconds(source_path: Path) -> float:
    duration_seconds = probe_video_duration_seconds(source_path)
    if duration_seconds is None or duration_seconds <= 0:
        return 2.0

    preferred_offset = max(duration_seconds * 0.1, 2.0)
    preferred_offset = min(preferred_offset, 5.0)
    return max(0.0, min(preferred_offset, max(duration_seconds - 0.1, 0.0)))


def probe_video_duration_seconds(source_path: Path) -> float | None:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(source_path),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            text=True,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    raw_value = result.stdout.strip()
    if not raw_value or raw_value == "N/A":
        return None

    try:
        return float(raw_value)
    except ValueError:
        return None


def generate_missing_previews(session: Session, thumbnail_size_px: int, limit: int = 24) -> int:
    candidates = session.scalars(
        select(FileRecord)
        .where(
            FileRecord.is_active.is_(True),
            FileRecord.media_kind.in_(tuple(sorted(PREVIEWABLE_MEDIA_KINDS))),
        )
        .order_by(FileRecord.post_count.desc(), FileRecord.id.asc())
        .limit(max(limit * 20, 200))
    ).all()

    generated = 0
    for file_record in candidates:
        if preview_exists(file_record, thumbnail_size_px):
            continue
        if generate_preview(file_record, thumbnail_size_px):
            generated += 1
        if generated >= limit:
            break
    return generated


def build_placeholder_svg(media_kind: str, thumbnail_size_px: int) -> str:
    label = PREVIEW_PLACEHOLDER_LABELS.get(media_kind, "Файл")
    safe_label = escape(label)
    safe_kind = escape(media_kind.upper())
    accent = {
        "photo": "#1f6feb",
        "animation": "#ff8f1f",
        "video": "#c0392b",
        "document": "#5d6b7d",
    }.get(media_kind, "#5d6b7d")
    font_size = max(18, thumbnail_size_px // 7)
    sub_font_size = max(12, thumbnail_size_px // 11)
    return f"""
<svg xmlns="http://www.w3.org/2000/svg" width="{thumbnail_size_px}" height="{thumbnail_size_px}" viewBox="0 0 {thumbnail_size_px} {thumbnail_size_px}">
  <rect width="100%" height="100%" rx="20" fill="#eef3f9"/>
  <rect x="12" y="12" width="{max(thumbnail_size_px - 24, 0)}" height="{max(thumbnail_size_px - 24, 0)}" rx="16" fill="#ffffff" stroke="#d9e2ec" stroke-width="2"/>
  <circle cx="{thumbnail_size_px / 2}" cy="{thumbnail_size_px / 2 - 16}" r="{max(thumbnail_size_px / 7, 14)}" fill="{accent}" fill-opacity="0.12"/>
  <text x="50%" y="48%" text-anchor="middle" fill="{accent}" font-family="Segoe UI, Trebuchet MS, sans-serif" font-size="{font_size}" font-weight="700">{safe_label}</text>
  <text x="50%" y="63%" text-anchor="middle" fill="#5d6b7d" font-family="Segoe UI, Trebuchet MS, sans-serif" font-size="{sub_font_size}">{safe_kind}</text>
</svg>
""".strip()
