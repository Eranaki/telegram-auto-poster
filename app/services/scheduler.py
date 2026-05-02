from __future__ import annotations

import random
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.config import APP_TIMEZONE, PREVIEW_BATCH_SIZE, SCAN_TICK_SECONDS, SCHEDULER_TICK_SECONDS
from app.db import SessionLocal
from sqlalchemy.orm import Session

from app.models import ContentSource, FileBrowserSettings, PostHistory, PostingRule, RuleSource, TelegramChannel
from app.services.picker import pick_file_for_rule
from app.services.previews import generate_missing_previews
from app.services.scanner import scan_source
from app.services.telegram import TelegramPublishError, publish_file


def is_rule_in_allowed_window(rule: PostingRule, current_time: datetime) -> bool:
    if rule.allowed_from_hour is None or rule.allowed_to_hour is None:
        return True

    start = rule.allowed_from_hour % 24
    end = rule.allowed_to_hour % 24
    hour = current_time.hour
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def move_to_window(rule: PostingRule, next_time: datetime) -> datetime:
    if rule.allowed_from_hour is None or rule.allowed_to_hour is None:
        return next_time
    if is_rule_in_allowed_window(rule, next_time):
        return next_time

    aligned = next_time.replace(
        hour=rule.allowed_from_hour % 24,
        minute=0,
        second=0,
        microsecond=0,
    )
    if aligned <= next_time:
        aligned += timedelta(days=1)
    return aligned


def compute_next_run(rule: PostingRule, from_time: datetime | None = None) -> datetime:
    base_time = from_time or datetime.now()
    minutes = max(rule.interval_minutes, 1)
    next_time = base_time + timedelta(minutes=minutes)
    if rule.jitter_minutes:
        delta = random.randint(-rule.jitter_minutes * 60, rule.jitter_minutes * 60)
        next_time += timedelta(seconds=delta)
    if next_time <= base_time:
        next_time = base_time + timedelta(minutes=1)
    return move_to_window(rule, next_time)


def compute_burst_run(rule: PostingRule, from_time: datetime | None = None) -> datetime:
    base_time = from_time or datetime.now()
    minutes = max(rule.burst_interval_minutes, 1)
    next_time = base_time + timedelta(minutes=minutes)
    if next_time <= base_time:
        next_time = base_time + timedelta(minutes=1)
    return move_to_window(rule, next_time)


def schedule_after_publish_attempt(rule: PostingRule, from_time: datetime) -> None:
    burst_size = max(rule.burst_post_count, 1)
    remaining_followups = max(rule.pending_burst_posts, 0)

    if remaining_followups > 0:
        remaining_followups -= 1
    else:
        remaining_followups = burst_size - 1

    if remaining_followups > 0:
        rule.pending_burst_posts = remaining_followups
        rule.next_run_at = compute_burst_run(rule, from_time)
    else:
        rule.pending_burst_posts = 0
        rule.next_run_at = compute_next_run(rule, from_time)


def reset_burst_schedule(rule: PostingRule, from_time: datetime) -> None:
    rule.pending_burst_posts = 0
    rule.next_run_at = compute_next_run(rule, from_time)


def get_rule_sources(session: Session, rule: PostingRule) -> list[ContentSource]:
    source_ids = session.scalars(
        select(RuleSource.source_id).where(RuleSource.rule_id == rule.id).distinct()
    ).all()
    if not source_ids and getattr(rule, "source_id", None) is not None:
        source_ids = [rule.source_id]
    if not source_ids:
        return []
    return session.scalars(select(ContentSource).where(ContentSource.id.in_(source_ids))).all()


class AppScheduler:
    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler(timezone=APP_TIMEZONE)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self.scheduler.add_job(
            self.dispatch_due_rules,
            IntervalTrigger(seconds=SCHEDULER_TICK_SECONDS),
            id="dispatch_due_rules",
            max_instances=1,
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.scan_due_sources,
            IntervalTrigger(seconds=SCAN_TICK_SECONDS),
            id="scan_due_sources",
            max_instances=1,
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.backfill_previews,
            IntervalTrigger(seconds=SCAN_TICK_SECONDS),
            id="backfill_previews",
            max_instances=1,
            replace_existing=True,
        )
        self.scheduler.start()
        self._started = True

    async def shutdown(self) -> None:
        if self._started:
            self.scheduler.shutdown(wait=False)
            self._started = False

    async def scan_due_sources(self) -> None:
        with SessionLocal() as session:
            sources = session.scalars(select(ContentSource).where(ContentSource.enabled.is_(True))).all()
            for source in sources:
                if source.scan_in_progress:
                    continue
                if getattr(source, "manual_scan_only", False):
                    continue
                last_scan = source.last_scanned_at or datetime.fromtimestamp(0)
                if datetime.now() - last_scan < timedelta(minutes=max(source.scan_interval_minutes, 1)):
                    continue
                try:
                    scan_source(session, source)
                except Exception as exc:  # pragma: no cover
                    source.last_scan_result = f"Ошибка сканирования: {exc}"
                    session.commit()

    async def backfill_previews(self) -> None:
        with SessionLocal() as session:
            settings = session.get(FileBrowserSettings, 1)
            thumbnail_size_px = settings.thumbnail_size_px if settings else 256
            generate_missing_previews(session, thumbnail_size_px=thumbnail_size_px, limit=PREVIEW_BATCH_SIZE)

    async def dispatch_due_rules(self) -> None:
        with SessionLocal() as session:
            rules = session.scalars(select(PostingRule).where(PostingRule.enabled.is_(True))).all()
            due_rule_ids = [
                rule.id
                for rule in rules
                if rule.next_run_at is None or rule.next_run_at <= datetime.now()
            ]

        for rule_id in due_rule_ids:
            await self.process_rule(rule_id)

    async def process_rule(self, rule_id: int, manual: bool = False) -> None:
        with SessionLocal() as session:
            rule = session.get(PostingRule, rule_id)
            if rule is None:
                return
            if not rule.enabled and not manual:
                return

            channel = session.get(TelegramChannel, rule.channel_id)
            now = datetime.now()
            sources = get_rule_sources(session, rule)
            enabled_sources = [source for source in sources if source.enabled]
            fallback_source_id = enabled_sources[0].id if enabled_sources else (sources[0].id if sources else rule.source_id)

            if not enabled_sources:
                rule.last_result = "Источник отключен или недоступен"
                reset_burst_schedule(rule, now)
                session.commit()
                return

            if channel is None or not channel.enabled:
                rule.last_result = "Канал отключен или недоступен"
                reset_burst_schedule(rule, now)
                session.commit()
                return

            if not manual and not is_rule_in_allowed_window(rule, now):
                rule.next_run_at = move_to_window(rule, now)
                rule.last_result = "Пропущено: текущее время вне разрешенного окна публикации"
                session.commit()
                return

            try:
                for source in enabled_sources:
                    last_scan = source.last_scanned_at or datetime.fromtimestamp(0)
                    if (
                        not getattr(source, "manual_scan_only", False)
                        and datetime.now() - last_scan >= timedelta(minutes=max(source.scan_interval_minutes, 1))
                    ):
                        scan_source(session, source)
            except Exception as exc:
                history = PostHistory(
                    rule_id=rule.id,
                    source_id=fallback_source_id,
                    status="failed",
                    message=f"Не удалось просканировать источник перед публикацией: {exc}",
                )
                session.add(history)
                rule.last_result = history.message
                reset_burst_schedule(rule, now)
                session.commit()
                return

            file_record = pick_file_for_rule(session, rule)
            if file_record is None:
                history = PostHistory(
                    rule_id=rule.id,
                    source_id=fallback_source_id,
                    status="skipped",
                    message="Для этого правила не найдено подходящих файлов",
                )
                session.add(history)
                rule.last_run_at = now
                rule.last_result = history.message
                reset_burst_schedule(rule, now)
                session.commit()
                return

            try:
                message_id = await publish_file(channel, rule, file_record)
            except TelegramPublishError as exc:
                history = PostHistory(
                    rule_id=rule.id,
                    source_id=file_record.source_id,
                    file_id=file_record.id,
                    status="failed",
                    message=str(exc),
                )
                session.add(history)
                rule.last_run_at = now
                rule.last_result = str(exc)
                reset_burst_schedule(rule, now)
                session.commit()
                return

            file_record.last_posted_at = now
            file_record.post_count += 1
            history = PostHistory(
                rule_id=rule.id,
                source_id=file_record.source_id,
                file_id=file_record.id,
                status="sent",
                message=f"Опубликован файл {file_record.relative_path}",
                telegram_message_id=message_id,
            )
            session.add(history)
            rule.last_run_at = now
            rule.last_result = history.message
            schedule_after_publish_attempt(rule, now)
            session.commit()
