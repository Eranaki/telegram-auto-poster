from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.models import ContentSource, FileRecord
from app.services.scanner import MEDIA_TYPE_LABELS, normalize_media_type_selection

COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)
CONTENT_ROOT = Path("/content")
_content_paths_cache: dict[str, object] = {
    "paths": [],
}


def to_bool(value: str | None) -> bool:
    return value in {"on", "true", "1", "yes"}


def load_volume_paths_from_compose() -> list[str]:
    compose_path = next((BASE_DIR / name for name in COMPOSE_FILES if (BASE_DIR / name).exists()), None)
    if compose_path is None:
        return []

    destinations: list[str] = []
    for line in compose_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue

        entry = stripped[2:].strip().strip('"').strip("'")
        if "/" not in entry or ":" not in entry:
            continue

        parts = entry.split(":")
        destination = parts[-1] if parts[-1].startswith("/") else parts[-2] if len(parts) >= 2 else None
        if not destination or not destination.startswith("/content"):
            continue
        if destination not in destinations:
            destinations.append(destination)

    return sorted(destinations)


def collect_runtime_content_paths(root_path: Path) -> list[str]:
    collected: list[str] = []
    seen: set[str] = set()

    def add_directory(directory: Path) -> None:
        posix_path = directory.as_posix()
        if posix_path not in seen:
            seen.add(posix_path)
            collected.append(posix_path)

    for child in sorted(root_path.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir():
            continue
        add_directory(child)
        for nested in sorted(child.rglob("*"), key=lambda item: item.as_posix().lower()):
            if nested.is_dir():
                add_directory(nested)

    return collected


def load_available_content_paths() -> list[str]:
    if _content_paths_cache["paths"]:
        return list(_content_paths_cache["paths"])

    if CONTENT_ROOT.exists() and CONTENT_ROOT.is_dir():
        runtime_paths = collect_runtime_content_paths(CONTENT_ROOT)
        if runtime_paths:
            _content_paths_cache["paths"] = runtime_paths
            return list(runtime_paths)

    compose_paths = load_volume_paths_from_compose()
    _content_paths_cache["paths"] = compose_paths
    return list(compose_paths)


def refresh_available_content_paths() -> list[str]:
    _content_paths_cache["paths"] = []
    return load_available_content_paths()


def serialize_media_type_selection(file_types: list[str]) -> str:
    return ",".join(normalize_media_type_selection(file_types))


def deserialize_media_type_selection(raw_value: str | None) -> list[str]:
    return normalize_media_type_selection([] if not raw_value else raw_value.split(","))


def annotate_source(source: ContentSource) -> ContentSource:
    source.selected_media_types = deserialize_media_type_selection(source.allowed_extensions)
    source.selected_media_labels = [MEDIA_TYPE_LABELS[item] for item in source.selected_media_types]
    source.scan_schedule_label = (
        "Только вручную" if getattr(source, "manual_scan_only", False) else f"Каждые {max(source.scan_interval_minutes, 1)} мин."
    )
    source.scan_mode_label = {
        "full": "Полный рескан",
        "add_missing": "Добавление отсутствующих",
    }.get(source.scan_mode, "Сканирование")
    linked_channels = [link.channel for link in getattr(source, "channel_links", []) if getattr(link, "channel", None)]
    linked_channels = sorted(linked_channels, key=lambda channel: channel.name.lower())
    source.linked_channels = linked_channels
    source.linked_channel_names = [channel.name for channel in linked_channels]
    source.linked_channel_count = len(linked_channels)
    source.file_count = getattr(source, "file_count", 0)
    return source


def attach_source_file_counts(session: Session, sources: list[ContentSource]) -> list[ContentSource]:
    if not sources:
        return sources

    source_ids = [source.id for source in sources]
    counts = {
        source_id: file_count
        for source_id, file_count in session.execute(
            select(FileRecord.source_id, func.count(FileRecord.id))
            .where(FileRecord.source_id.in_(source_ids))
            .group_by(FileRecord.source_id)
        ).all()
    }
    for source in sources:
        source.file_count = int(counts.get(source.id, 0))
    return sources


def validate_source_payload(
    session: Session,
    *,
    name: str,
    path: str,
    file_types: list[str],
    exclude_source_id: int | None = None,
) -> tuple[str, str, list[str], str | None]:
    source_name = name.strip()
    source_path = Path(path)
    available_paths = load_available_content_paths()

    if not source_name:
        return "", "", [], "Нужно указать название источника."
    if not source_path.is_absolute():
        return source_name, path, [], "Путь должен быть абсолютным путем внутри контейнера."
    if available_paths and str(source_path) not in available_paths:
        return source_name, str(source_path), [], "Выбран путь, которого нет среди доступных папок внутри /content."

    normalized_types = normalize_media_type_selection(file_types)
    if not normalized_types:
        return source_name, str(source_path), [], "Нужно выбрать хотя бы один тип файлов."

    existing_source_with_name = session.scalar(select(ContentSource).where(ContentSource.name == source_name))
    if existing_source_with_name is not None:
        if exclude_source_id is not None and existing_source_with_name.id == exclude_source_id:
            existing_source_with_name = None
        else:
            return source_name, str(source_path), normalized_types, f"Источник с именем '{source_name}' уже существует."

    existing_source_with_path = session.scalar(select(ContentSource).where(ContentSource.path == str(source_path)))
    if existing_source_with_path is not None:
        if exclude_source_id is not None and existing_source_with_path.id == exclude_source_id:
            existing_source_with_path = None
        else:
            return (
                source_name,
                str(source_path),
                normalized_types,
                f"Путь {source_path} уже используется источником '{existing_source_with_path.name}'.",
            )

    return source_name, str(source_path), normalized_types, None


def build_source_form_state(
    *,
    name: str,
    path: str,
    recursive: str | None,
    enabled: str | None,
    file_types: list[str],
    scan_interval_minutes: int,
    manual_scan_only: str | None,
) -> dict[str, object]:
    normalized_types = normalize_media_type_selection(file_types) or list(MEDIA_TYPE_LABELS)
    is_manual_only = to_bool(manual_scan_only)
    return {
        "name": name,
        "path": path,
        "recursive": to_bool(recursive),
        "enabled": to_bool(enabled),
        "file_types": normalized_types,
        "scan_interval_minutes": max(scan_interval_minutes, 1),
        "manual_scan_only": is_manual_only,
    }


def build_source_form_defaults() -> dict[str, object]:
    return {
        "name": "",
        "path": "",
        "recursive": True,
        "enabled": True,
        "file_types": list(MEDIA_TYPE_LABELS),
        "scan_interval_minutes": 10,
        "manual_scan_only": False,
    }


def build_source_form_from_source(source: ContentSource) -> dict[str, object]:
    return {
        "name": source.name,
        "path": source.path,
        "recursive": source.recursive,
        "enabled": source.enabled,
        "file_types": list(source.selected_media_types),
        "scan_interval_minutes": max(source.scan_interval_minutes, 1),
        "manual_scan_only": bool(getattr(source, "manual_scan_only", False)),
    }
