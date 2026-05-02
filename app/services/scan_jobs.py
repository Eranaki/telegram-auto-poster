from __future__ import annotations

from threading import Lock, Thread

from app.db import SessionLocal
from app.models import ContentSource
from app.services.scanner import SCAN_MODE_ADD_MISSING, SCAN_MODE_FULL, scan_source

_active_sources: set[int] = set()
_active_sources_lock = Lock()


def start_source_scan_job(source_id: int, mode: str) -> bool:
    if mode not in {SCAN_MODE_FULL, SCAN_MODE_ADD_MISSING}:
        raise ValueError(f"Неизвестный режим сканирования: {mode}")

    with _active_sources_lock:
        if source_id in _active_sources:
            return False
        _active_sources.add(source_id)

    thread = Thread(target=_run_scan_job, args=(source_id, mode), daemon=True)
    thread.start()
    return True


def _run_scan_job(source_id: int, mode: str) -> None:
    try:
        with SessionLocal() as session:
            source = session.get(ContentSource, source_id)
            if source is None:
                return

            source.scan_in_progress = True
            source.scan_mode = mode
            source.scan_progress_current = 0
            source.scan_progress_total = 0
            source.scan_progress_percent = 0
            source.last_scan_result = (
                "Запущен полный рескан..." if mode == SCAN_MODE_FULL else "Запущено добавление отсутствующих файлов..."
            )
            session.commit()

            def progress_callback(current: int, total: int) -> None:
                source.scan_progress_current = current
                source.scan_progress_total = total
                source.scan_progress_percent = int(current * 100 / total) if total else 0
                if mode == SCAN_MODE_FULL:
                    source.last_scan_result = f"Полный рескан: обработано {current} из {total}"
                else:
                    source.last_scan_result = f"Добавление отсутствующих: обработано {current} из {total}"
                session.commit()

            try:
                scan_source(session, source, mode=mode, progress_callback=progress_callback)
            except Exception as exc:
                source.scan_in_progress = False
                source.scan_mode = mode
                source.scan_progress_percent = 0
                source.last_scan_result = f"Ошибка сканирования: {exc}"
                session.commit()
                return

            source.scan_in_progress = False
            source.scan_mode = mode
            source.scan_progress_current = source.scan_progress_total
            source.scan_progress_percent = 100 if source.scan_progress_total else 0
            session.commit()
    finally:
        with _active_sources_lock:
            _active_sources.discard(source_id)
