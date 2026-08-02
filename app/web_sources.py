from __future__ import annotations

import os
from pathlib import Path
from threading import Lock
from time import monotonic

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
CONTENT_PATH_PREFIX = Path("/content")
CONTENT_PATH_PAGE_LIMIT = 200
CONTENT_PATH_CACHE_TTL_SECONDS = 30
CONTENT_PATH_CACHE_MAX_ENTRIES = 256
_content_paths_cache: dict[tuple[str, int, int], tuple[int, float, list[dict[str, str]], bool]] = {}
_content_paths_cache_lock = Lock()


class ContentPathChangedError(RuntimeError):
    pass


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
        if not destination:
            continue
        destination_path = Path(destination).resolve(strict=False)
        content_prefix = CONTENT_PATH_PREFIX.resolve(strict=False)
        if destination_path != content_prefix and not destination_path.is_relative_to(content_prefix):
            continue
        normalized_destination = destination_path.as_posix()
        if normalized_destination not in destinations:
            destinations.append(normalized_destination)

    return sorted(destinations)


def clear_content_path_cache() -> None:
    with _content_paths_cache_lock:
        _content_paths_cache.clear()


def resolve_runtime_content_path(raw_path: str | None = None) -> tuple[Path, Path]:
    try:
        root_path = CONTENT_ROOT.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise FileNotFoundError("Корневая папка /content недоступна") from exc

    candidate = Path(raw_path) if raw_path else root_path
    if not candidate.is_absolute():
        raise ValueError("Путь должен быть абсолютным путем внутри /content")
    try:
        resolved_path = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise FileNotFoundError(f"Папка недоступна: {candidate}") from exc
    if not resolved_path.is_relative_to(root_path):
        raise ValueError("Путь должен находиться внутри /content")
    if not resolved_path.is_dir():
        raise ValueError("Выбранный путь не является папкой")
    return root_path, resolved_path


def _load_content_path_children(
    root_path: Path,
    directory: Path,
    *,
    offset: int,
    limit: int,
) -> tuple[int, list[dict[str, str]], bool]:
    cache_key = (directory.as_posix(), offset, limit)
    directory_mtime = directory.stat().st_mtime_ns
    with _content_paths_cache_lock:
        cached = _content_paths_cache.get(cache_key)
        if (
            cached is not None
            and cached[0] == directory_mtime
            and monotonic() - cached[1] < CONTENT_PATH_CACHE_TTL_SECONDS
        ):
            return directory_mtime, list(cached[2]), cached[3]

    children: list[dict[str, str]] = []
    valid_index = 0
    has_more = False
    with os.scandir(directory) as entries:
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=True):
                    continue
                child_path = Path(entry.path).resolve(strict=True)
                if not child_path.is_relative_to(root_path) or not child_path.is_dir():
                    continue
            except (FileNotFoundError, OSError):
                continue
            if valid_index < offset:
                valid_index += 1
                continue
            if len(children) >= limit:
                has_more = True
                break
            children.append({"name": entry.name, "path": child_path.as_posix()})
            valid_index += 1

    children.sort(key=lambda item: (item["name"].casefold(), item["path"]))
    with _content_paths_cache_lock:
        if cache_key not in _content_paths_cache and len(_content_paths_cache) >= CONTENT_PATH_CACHE_MAX_ENTRIES:
            _content_paths_cache.pop(next(iter(_content_paths_cache)))
        _content_paths_cache[cache_key] = (directory_mtime, monotonic(), children, has_more)
    return directory_mtime, list(children), has_more


def browse_content_paths(
    raw_path: str | None = None,
    *,
    offset: int = 0,
    limit: int = CONTENT_PATH_PAGE_LIMIT,
    generation: int | None = None,
) -> dict[str, object]:
    safe_offset = max(offset, 0)
    safe_limit = max(1, min(limit, CONTENT_PATH_PAGE_LIMIT))

    if not CONTENT_ROOT.exists() or not CONTENT_ROOT.is_dir():
        compose_paths = load_volume_paths_from_compose()
        if raw_path and raw_path != "/content":
            if raw_path not in compose_paths:
                raise ValueError("Путь должен соответствовать подключенной папке /content")
            return {
                "current_path": raw_path,
                "parent_path": "/content",
                "children": [],
                "offset": 0,
                "next_offset": 0,
                "has_more": False,
                "selectable": True,
                "generation": "0",
            }
        children = [{"name": Path(path).name, "path": path} for path in compose_paths]
        page_children = children[safe_offset : safe_offset + safe_limit]
        return {
            "current_path": "/content",
            "parent_path": None,
            "children": page_children,
            "offset": safe_offset,
            "next_offset": safe_offset + len(page_children),
            "has_more": safe_offset + safe_limit < len(children),
            "selectable": False,
            "generation": "0",
        }

    root_path, current_path = resolve_runtime_content_path(raw_path)
    current_generation = current_path.stat().st_mtime_ns
    if safe_offset > 0 and generation != current_generation:
        raise ContentPathChangedError("Содержимое папки изменилось; список нужно загрузить заново")
    directory_generation, children, has_more = _load_content_path_children(
        root_path,
        current_path,
        offset=safe_offset,
        limit=safe_limit,
    )
    next_offset = safe_offset + len(children)
    return {
        "current_path": current_path.as_posix(),
        "parent_path": current_path.parent.as_posix() if current_path != root_path else None,
        "children": children,
        "offset": safe_offset,
        "next_offset": next_offset,
        "has_more": has_more,
        "selectable": True,
        "generation": str(directory_generation),
    }


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

    if not source_name:
        return "", "", [], "Нужно указать название источника."
    if not source_path.is_absolute():
        return source_name, path, [], "Путь должен быть абсолютным путем внутри контейнера."
    if CONTENT_ROOT.exists() and CONTENT_ROOT.is_dir():
        try:
            _, source_path = resolve_runtime_content_path(str(source_path))
        except (FileNotFoundError, ValueError) as exc:
            return source_name, str(source_path), [], str(exc)
    else:
        source_path = source_path.resolve(strict=False)
        compose_paths = [Path(item).resolve(strict=False) for item in load_volume_paths_from_compose()]
        if source_path not in compose_paths:
            return source_name, str(source_path), [], "Путь должен находиться внутри подключенной папки /content."

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
