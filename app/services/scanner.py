from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ContentSource, FileRecord

MEDIA_TYPE_LABELS = {
    "photo": "Изображения",
    "animation": "GIF",
    "video": "Видео",
    "document": "Документы",
}

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif"}
ANIMATION_EXTENSIONS = {".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
SCAN_MODE_FULL = "full"
SCAN_MODE_ADD_MISSING = "add_missing"


def normalize_media_type_selection(values: list[str]) -> list[str]:
    allowed = list(MEDIA_TYPE_LABELS)
    normalized: list[str] = []
    for value in values:
        item = value.strip().lower()
        if item in allowed and item not in normalized:
            normalized.append(item)
    return normalized


def detect_media_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in PHOTO_EXTENSIONS:
        return "photo"
    if suffix in ANIMATION_EXTENSIONS:
        return "animation"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return "document"


def build_fingerprint(relative_path: str, size: int, mtime_ns: int) -> str:
    payload = f"{relative_path}|{size}|{mtime_ns}".encode("utf-8", "ignore")
    return hashlib.sha1(payload).hexdigest()


def file_matches_source_filters(path: Path, source: ContentSource) -> bool:
    selected_types = normalize_media_type_selection(
        [] if not source.allowed_extensions else source.allowed_extensions.split(",")
    )
    if not selected_types:
        selected_types = list(MEDIA_TYPE_LABELS)
    return detect_media_kind(path) in selected_types


def collect_candidate_paths(source: ContentSource) -> list[Path]:
    root = Path(source.path)
    iterator = root.rglob("*") if source.recursive else root.glob("*")
    return [
        file_path
        for file_path in iterator
        if file_path.is_file() and file_matches_source_filters(file_path, source)
    ]


def scan_source(
    session: Session,
    source: ContentSource,
    mode: str = SCAN_MODE_FULL,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    root = Path(source.path)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Путь к источнику недоступен: {root}")

    if mode not in {SCAN_MODE_FULL, SCAN_MODE_ADD_MISSING}:
        raise ValueError(f"Неизвестный режим сканирования: {mode}")

    known_files = {
        record.relative_path: record
        for record in session.scalars(select(FileRecord).where(FileRecord.source_id == source.id)).all()
    }
    seen_paths: set[str] = set()
    scanned = 0
    created = 0
    updated = 0
    now = datetime.now()
    candidate_paths = collect_candidate_paths(source)
    total_candidates = len(candidate_paths)

    if progress_callback is not None:
        progress_callback(0, total_candidates)

    for index, file_path in enumerate(candidate_paths, start=1):
        scanned += 1
        relative_path = file_path.relative_to(root).as_posix()
        seen_paths.add(relative_path)
        stat = file_path.stat()
        record = known_files.get(relative_path)
        fingerprint = build_fingerprint(relative_path, stat.st_size, stat.st_mtime_ns)
        media_kind = detect_media_kind(file_path)

        if record is None:
            record = FileRecord(
                source_id=source.id,
                relative_path=relative_path,
                absolute_path=str(file_path.resolve()),
                media_kind=media_kind,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                fingerprint=fingerprint,
                is_active=True,
                discovered_at=now,
                last_seen_at=now,
            )
            session.add(record)
            created += 1
        elif mode == SCAN_MODE_FULL:
            if (
                record.size != stat.st_size
                or record.mtime_ns != stat.st_mtime_ns
                or record.absolute_path != str(file_path.resolve())
                or record.media_kind != media_kind
            ):
                updated += 1

            record.absolute_path = str(file_path.resolve())
            record.media_kind = media_kind
            record.size = stat.st_size
            record.mtime_ns = stat.st_mtime_ns
            record.fingerprint = fingerprint
            record.is_active = True
            record.last_seen_at = now

        if progress_callback is not None and (index == total_candidates or index % 25 == 0):
            progress_callback(index, total_candidates)

    deactivated = 0
    if mode == SCAN_MODE_FULL:
        for relative_path, record in known_files.items():
            if relative_path not in seen_paths and record.is_active:
                record.is_active = False
                deactivated += 1

    source.last_scanned_at = now
    source.last_scan_result = (
        f"{'Полный рескан' if mode == SCAN_MODE_FULL else 'Добавление отсутствующих'}: "
        f"просканировано {scanned} файлов, добавлено {created}, обновлено {updated}, отключено {deactivated}"
    )
    session.commit()

    return {
        "scanned": scanned,
        "created": created,
        "updated": updated,
        "deactivated": deactivated,
    }
