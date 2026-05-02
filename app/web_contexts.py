from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, selectinload

from app.models import ChannelSource, ContentSource, FileRecord, PostHistory, PostingRule, RuleSource, TelegramChannel
from app.services.previews import preview_exists
from app.services.scanner import MEDIA_TYPE_LABELS
from app.web_sources import annotate_source, attach_source_file_counts, build_source_form_defaults

SELECTION_MODE_LABELS = {
    "random_no_repeat": "Случайно без повторов",
    "shuffle_cycle": "Перемешанный цикл",
    "oldest_first": "По очереди от старых",
    "random_with_repeat": "Полный случайный выбор",
}
SOURCE_SELECTION_MODE_LABELS = {
    "merged_pool": "Общий пул",
}


def format_bytes(size: int) -> str:
    value = float(size)
    for suffix in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or suffix == "ТБ":
            if suffix == "Б":
                return f"{int(value)} {suffix}"
            return f"{value:.1f} {suffix}"
        value /= 1024
    return f"{int(size)} Б"


def annotate_rule(rule: PostingRule) -> PostingRule:
    rule.selection_mode_label = SELECTION_MODE_LABELS.get(rule.selection_mode, rule.selection_mode)
    rule.source_selection_mode_label = SOURCE_SELECTION_MODE_LABELS.get(
        getattr(rule, "source_selection_mode", "merged_pool"),
        getattr(rule, "source_selection_mode", "merged_pool"),
    )
    rule.burst_post_count = max(getattr(rule, "burst_post_count", 1), 1)
    rule.burst_interval_minutes = max(getattr(rule, "burst_interval_minutes", 1), 1)
    rule.pending_burst_posts = max(getattr(rule, "pending_burst_posts", 0), 0)
    rule.burst_summary_label = (
        f"{rule.burst_post_count} постов, шаг {rule.burst_interval_minutes} мин."
        if rule.burst_post_count > 1
        else "1 пост за событие"
    )
    rule.burst_state_label = (
        f"Серия активна, осталось {rule.pending_burst_posts} пост."
        if rule.pending_burst_posts > 0
        else "Серия не активна"
    )
    attached_sources = [link.source for link in getattr(rule, "source_links", []) if getattr(link, "source", None)]
    if not attached_sources and getattr(rule, "source", None) is not None:
        attached_sources = [rule.source]
    rule.attached_sources = sorted(attached_sources, key=lambda source: source.name.lower())
    rule.attached_source_ids = [source.id for source in rule.attached_sources]
    rule.attached_source_names = [source.name for source in rule.attached_sources]
    rule.attached_sources_label = ", ".join(rule.attached_source_names) if rule.attached_source_names else "Источники не выбраны"
    return rule


def annotate_file_record(file_record: FileRecord, thumbnail_size_px: int) -> FileRecord:
    file_record.human_size = format_bytes(file_record.size)
    file_record.thumbnail_size_px = thumbnail_size_px
    file_record.preview_ready = preview_exists(file_record, thumbnail_size_px)
    return file_record


def annotate_channel(channel: TelegramChannel) -> TelegramChannel:
    channel.rule_count = len(channel.rules) if hasattr(channel, "rules") else 0
    channel.active_rule_count = sum(1 for rule in getattr(channel, "rules", []) if rule.enabled)
    channel.source_count = len(getattr(channel, "source_links", []) or [])
    return channel


def annotate_history_item(item: PostHistory, thumbnail_size_px: int = 96) -> PostHistory:
    status_map = {
        "sent": ("Отправлено", "ok"),
        "failed": ("Ошибка", "danger"),
        "skipped": ("Пропущено", "muted"),
    }
    item.status_label, item.status_badge_class = status_map.get(item.status, (item.status, "muted"))
    item.rule_display_name = "Отправлено вручную" if getattr(item, "manual_trigger", False) else (
        item.rule.name if item.rule else "Неизвестное правило"
    )
    if item.file is not None:
        annotate_file_record(item.file, thumbnail_size_px)
        item.preview_thumbnail_url = f"/files/{item.file.id}/thumbnail"
        item.preview_original_url = f"/files/{item.file.id}/original"
        item.preview_image_url = item.preview_original_url if item.file.media_kind in {"photo", "animation"} else ""
    else:
        item.preview_thumbnail_url = ""
        item.preview_original_url = ""
        item.preview_image_url = ""
    return item


def annotate_history_items(items: list[PostHistory], thumbnail_size_px: int = 96) -> list[PostHistory]:
    return [annotate_history_item(item, thumbnail_size_px) for item in items]


def already_sent_exists(rule_id: int):
    return exists(
        select(PostHistory.id).where(
            PostHistory.rule_id == rule_id,
            PostHistory.file_id == FileRecord.id,
            PostHistory.status == "sent",
        )
    )


def get_rule_source_ids(session: Session, rule: PostingRule) -> list[int]:
    source_ids = session.scalars(
        select(RuleSource.source_id).where(RuleSource.rule_id == rule.id).distinct()
    ).all()
    if source_ids:
        return source_ids
    return [rule.source_id] if getattr(rule, "source_id", None) is not None else []


def get_rule_sources(session: Session, rule: PostingRule) -> list[ContentSource]:
    source_ids = get_rule_source_ids(session, rule)
    if not source_ids:
        return []
    sources = session.scalars(
        select(ContentSource)
        .where(ContentSource.id.in_(source_ids))
        .order_by(ContentSource.name.asc(), ContentSource.id.asc())
    ).all()
    return [annotate_source(source) for source in sources]


def ensure_rule_source_links(session: Session, rule: PostingRule, source_ids: list[int]) -> list[int]:
    unique_source_ids = sorted({int(source_id) for source_id in source_ids})
    for source_id in unique_source_ids:
        existing_link = session.scalar(
            select(RuleSource).where(RuleSource.rule_id == rule.id, RuleSource.source_id == source_id)
        )
        if existing_link is None:
            session.add(RuleSource(rule_id=rule.id, source_id=source_id))

    for stale_link in session.scalars(select(RuleSource).where(RuleSource.rule_id == rule.id)).all():
        if stale_link.source_id not in unique_source_ids:
            session.delete(stale_link)

    if unique_source_ids:
        rule.source_id = unique_source_ids[0]
        if getattr(rule, "last_source_id", None) not in unique_source_ids:
            rule.last_source_id = None

    return unique_source_ids


def preview_candidates_for_rule(session: Session, rule: PostingRule, limit: int = 30) -> tuple[list[FileRecord], str]:
    source_ids = get_rule_source_ids(session, rule)
    if not source_ids:
        return [], "У правила нет источников."

    base_query = select(FileRecord).where(
        FileRecord.source_id.in_(source_ids),
        FileRecord.is_active.is_(True),
    )
    fresh_query = base_query.where(~already_sent_exists(rule.id))

    if rule.selection_mode == "oldest_first":
        files = session.scalars(
            base_query.order_by(
                FileRecord.last_posted_at.is_not(None),
                FileRecord.last_posted_at.asc(),
                FileRecord.discovered_at.asc(),
            ).limit(limit)
        ).all()
        return files, "Для этого режима показана реальная последовательность кандидатов по приоритету."

    if rule.selection_mode == "random_no_repeat":
        files = session.scalars(fresh_query.order_by(func.random()).limit(limit)).all()
        if files or not rule.repeat_after_exhaustion:
            return files, "Показаны случайные еще не отправленные файлы для этого правила."
        files = session.scalars(base_query.order_by(func.random()).limit(limit)).all()
        return files, "Уникальные файлы закончились, поэтому показаны случайные кандидаты из полного списка."

    if rule.selection_mode == "shuffle_cycle":
        files = session.scalars(fresh_query.order_by(func.random()).limit(limit)).all()
        if files or not rule.repeat_after_exhaustion:
            return files, "Показаны еще не отправленные файлы в порядке текущего случайного предпросмотра."
        files = session.scalars(
            base_query.order_by(
                FileRecord.last_posted_at.is_(None).desc(),
                FileRecord.last_posted_at.asc(),
                FileRecord.discovered_at.asc(),
            ).limit(limit)
        ).all()
        return files, "Уникальные файлы закончились, поэтому показаны самые давно отправленные кандидаты."

    files = session.scalars(base_query.order_by(func.random()).limit(limit)).all()
    return files, "Для полностью случайного режима это предпросмотр вероятных кандидатов, а не фиксированная очередь."


def get_source_or_404(session: Session, source_id: int) -> ContentSource:
    source = session.scalar(
        select(ContentSource)
        .options(
            selectinload(ContentSource.rules),
            selectinload(ContentSource.channel_links).selectinload(ChannelSource.channel),
        )
        .where(ContentSource.id == source_id)
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Источник не найден")
    return annotate_source(source)


def get_rule_or_404(session: Session, rule_id: int) -> PostingRule:
    rule = session.scalar(
        select(PostingRule)
        .options(
            selectinload(PostingRule.channel),
            selectinload(PostingRule.source),
            selectinload(PostingRule.source_links).selectinload(RuleSource.source),
        )
        .where(PostingRule.id == rule_id)
    )
    if rule is None:
        raise HTTPException(status_code=404, detail="Правило не найдено")
    return annotate_rule(rule)


def get_channel_or_404(session: Session, channel_id: int) -> TelegramChannel:
    channel = session.scalar(
        select(TelegramChannel)
        .options(selectinload(TelegramChannel.rules), selectinload(TelegramChannel.source_links))
        .where(TelegramChannel.id == channel_id)
    )
    if channel is None:
        raise HTTPException(status_code=404, detail="Канал не найден")
    return annotate_channel(channel)


def get_sidebar_channels(session: Session) -> list[TelegramChannel]:
    channels = session.scalars(
        select(TelegramChannel)
        .options(selectinload(TelegramChannel.rules), selectinload(TelegramChannel.source_links))
        .order_by(TelegramChannel.name.asc())
    ).all()
    return [annotate_channel(channel) for channel in channels]


def get_channel_source_ids(session: Session, channel_id: int) -> list[int]:
    return session.scalars(
        select(ChannelSource.source_id).where(ChannelSource.channel_id == channel_id).distinct()
    ).all()


def get_all_sources(session: Session) -> list[ContentSource]:
    sources = session.scalars(
        select(ContentSource)
        .options(
            selectinload(ContentSource.channel_links).selectinload(ChannelSource.channel),
        )
        .order_by(ContentSource.name.asc())
    ).all()
    attach_source_file_counts(session, sources)
    return [annotate_source(source) for source in sources]


def get_channel_sources(session: Session, channel_id: int) -> list[ContentSource]:
    sources = session.scalars(
        select(ContentSource)
        .join(ChannelSource, ChannelSource.source_id == ContentSource.id)
        .options(
            selectinload(ContentSource.channel_links).selectinload(ChannelSource.channel),
        )
        .where(ChannelSource.channel_id == channel_id)
        .order_by(ContentSource.name.asc())
    ).all()
    attach_source_file_counts(session, sources)
    return [annotate_source(source) for source in sources]


def build_rule_source_groups(session: Session, channel_id: int) -> tuple[list[ContentSource], list[ContentSource]]:
    linked_ids = set(get_channel_source_ids(session, channel_id))
    all_sources = get_all_sources(session)
    linked_sources = [source for source in all_sources if source.id in linked_ids]
    other_sources = [source for source in all_sources if source.id not in linked_ids]
    return linked_sources, other_sources


def ensure_channel_source_link(session: Session, channel_id: int, source_id: int) -> None:
    existing_link = session.scalar(
        select(ChannelSource).where(ChannelSource.channel_id == channel_id, ChannelSource.source_id == source_id)
    )
    if existing_link is None:
        session.add(ChannelSource(channel_id=channel_id, source_id=source_id))


def build_channel_stats(session: Session, channel: TelegramChannel) -> dict[str, int]:
    sent_count = session.scalar(
        select(func.count(PostHistory.id))
        .join(PostingRule, PostHistory.rule_id == PostingRule.id)
        .where(PostingRule.channel_id == channel.id, PostHistory.status == "sent")
    ) or 0
    failed_count = session.scalar(
        select(func.count(PostHistory.id))
        .join(PostingRule, PostHistory.rule_id == PostingRule.id)
        .where(PostingRule.channel_id == channel.id, PostHistory.status == "failed")
    ) or 0
    source_ids = get_channel_source_ids(session, channel.id)
    active_files = (
        session.scalar(
            select(func.count(FileRecord.id)).where(
                FileRecord.source_id.in_(source_ids),
                FileRecord.is_active.is_(True),
            )
        )
        if source_ids
        else 0
    ) or 0
    return {
        "rules": channel.rule_count,
        "active_rules": channel.active_rule_count,
        "sent_posts": sent_count,
        "failed_posts": failed_count,
        "active_files": active_files,
    }


def build_channel_form_defaults() -> dict[str, object]:
    return {
        "name": "",
        "chat_id": "",
        "bot_token": "",
        "parse_mode": "HTML",
        "default_caption": "",
        "disable_notification": False,
        "protect_content": False,
        "enabled": True,
    }


def build_dashboard_context(session: Session) -> dict:
    channels = get_sidebar_channels(session)

    stats = {
        "channels": len(channels),
        "sources": session.scalar(select(func.count(ContentSource.id))) or 0,
        "rules": session.scalar(select(func.count(PostingRule.id))) or 0,
        "sent_posts": session.scalar(select(func.count(PostHistory.id)).where(PostHistory.status == "sent")) or 0,
    }

    channel_cards = [
        {
            "channel": channel,
            "stats": build_channel_stats(session, channel),
        }
        for channel in channels
    ]

    recent_posts = session.scalars(
        select(PostHistory)
        .options(
            selectinload(PostHistory.rule).selectinload(PostingRule.channel),
            selectinload(PostHistory.file),
        )
        .order_by(PostHistory.attempted_at.desc())
        .limit(12)
    ).all()
    recent_posts = annotate_history_items(recent_posts, thumbnail_size_px=96)

    return {
        "stats": stats,
        "channel_cards": channel_cards,
        "recent_posts": recent_posts,
        "channel_form": build_channel_form_defaults(),
    }


def build_channel_overview_context(
    session: Session,
    channel: TelegramChannel,
    *,
    error_message: str = "",
    success_message: str = "",
) -> dict:
    recent_posts = session.scalars(
        select(PostHistory)
        .join(PostingRule, PostHistory.rule_id == PostingRule.id)
        .options(selectinload(PostHistory.rule), selectinload(PostHistory.file))
        .where(PostingRule.channel_id == channel.id)
        .order_by(PostHistory.attempted_at.desc())
        .limit(15)
    ).all()
    return {
        "channel": channel,
        "stats": build_channel_stats(session, channel),
        "recent_posts": annotate_history_items(recent_posts, thumbnail_size_px=96),
        "error_message": error_message,
        "success_message": success_message,
    }


def build_sources_page_context(
    session: Session,
    *,
    edit_source_id: int | None = None,
    edit_source_form: dict[str, object] | None = None,
) -> dict:
    return {
        "sources": get_all_sources(session),
        "media_type_options": list(MEDIA_TYPE_LABELS.items()),
        "source_form": build_source_form_defaults(),
        "edit_source_id": edit_source_id,
        "edit_source_form": edit_source_form,
    }


def build_channel_sources_page_context(
    session: Session,
    channel: TelegramChannel,
    *,
    edit_source_id: int | None = None,
    edit_source_form: dict[str, object] | None = None,
) -> dict:
    attached_sources = get_channel_sources(session, channel.id)
    attached_ids = {source.id for source in attached_sources}
    attachable_query = select(ContentSource).order_by(ContentSource.name.asc())
    if attached_ids:
        attachable_query = attachable_query.where(~ContentSource.id.in_(attached_ids))
    attachable_sources = session.scalars(attachable_query).all()
    return {
        "channel": channel,
        "sources": attached_sources,
        "attachable_sources": attachable_sources,
        "media_type_options": list(MEDIA_TYPE_LABELS.items()),
        "source_form": build_source_form_defaults(),
        "edit_source_id": edit_source_id,
        "edit_source_form": edit_source_form,
    }


def build_rule_form_defaults(channel_id: int) -> dict[str, object]:
    return {
        "channel_id": channel_id,
        "name": "",
        "source_id": None,
        "source_ids": [],
        "source_selection_mode": "merged_pool",
        "interval_minutes": 180,
        "jitter_minutes": 15,
        "burst_post_count": 1,
        "burst_interval_minutes": 2,
        "allowed_from_hour": "",
        "allowed_to_hour": "",
        "selection_mode": "random_no_repeat",
        "chat_id_override": "",
        "caption_template": "",
        "repeat_after_exhaustion": True,
        "send_as_document": False,
        "convert_heic_to_jpeg": False,
        "enabled": True,
    }


def build_channel_rules_context(session: Session, channel: TelegramChannel) -> dict:
    rules = session.scalars(
        select(PostingRule)
        .options(
            selectinload(PostingRule.source),
            selectinload(PostingRule.channel),
            selectinload(PostingRule.source_links).selectinload(RuleSource.source),
        )
        .where(PostingRule.channel_id == channel.id)
        .order_by(PostingRule.name.asc())
    ).all()
    linked_sources, other_sources = build_rule_source_groups(session, channel.id)
    importable_rules = [
        annotate_rule(rule)
        for rule in session.scalars(
            select(PostingRule)
            .options(
                selectinload(PostingRule.channel),
                selectinload(PostingRule.source),
                selectinload(PostingRule.source_links).selectinload(RuleSource.source),
            )
            .where(PostingRule.channel_id != channel.id)
            .order_by(PostingRule.channel_id.asc(), PostingRule.name.asc())
        ).all()
    ]
    return {
        "channel": channel,
        "rules": [annotate_rule(rule) for rule in rules],
        "linked_sources": linked_sources,
        "other_sources": other_sources,
        "source_selection_modes": list(SOURCE_SELECTION_MODE_LABELS.items()),
        "selection_modes": list(SELECTION_MODE_LABELS.items()),
        "rule_form": build_rule_form_defaults(channel.id),
        "importable_rules": importable_rules,
    }


def make_unique_rule_name(session: Session, base_name: str) -> str:
    normalized = base_name.strip() or "Правило"
    if session.scalar(select(PostingRule.id).where(PostingRule.name == normalized)) is None:
        return normalized

    counter = 2
    while True:
        candidate = f"{normalized} ({counter})"
        if session.scalar(select(PostingRule.id).where(PostingRule.name == candidate)) is None:
            return candidate
        counter += 1


def get_file_record_or_404(session: Session, file_id: int) -> FileRecord:
    file_record = session.get(FileRecord, file_id)
    if file_record is None:
        raise HTTPException(status_code=404, detail="Файл не найден")
    return file_record
