from __future__ import annotations

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.models import FileRecord, PostHistory, PostingRule, RuleSource


def get_rule_source_ids(session: Session, rule: PostingRule) -> list[int]:
    source_ids = session.scalars(
        select(RuleSource.source_id).where(RuleSource.rule_id == rule.id).distinct()
    ).all()
    if source_ids:
        return source_ids
    return [rule.source_id] if getattr(rule, "source_id", None) is not None else []


def pick_file_for_rule(session: Session, rule: PostingRule) -> FileRecord | None:
    source_ids = get_rule_source_ids(session, rule)
    if not source_ids:
        return None

    base_query = select(FileRecord).where(
        FileRecord.source_id.in_(source_ids),
        FileRecord.is_active.is_(True),
    )

    if rule.selection_mode == "oldest_first":
        return session.scalar(
            base_query.order_by(
                FileRecord.last_posted_at.is_not(None),
                FileRecord.last_posted_at.asc(),
                FileRecord.discovered_at.asc(),
            ).limit(1)
        )

    already_sent = exists(
        select(PostHistory.id).where(
            PostHistory.rule_id == rule.id,
            PostHistory.file_id == FileRecord.id,
            PostHistory.status == "sent",
        )
    )
    fresh_query = base_query.where(~already_sent)

    if rule.selection_mode == "random_no_repeat":
        candidate = session.scalar(fresh_query.order_by(func.random()).limit(1))
        if candidate or not rule.repeat_after_exhaustion:
            return candidate
        return session.scalar(base_query.order_by(func.random()).limit(1))

    if rule.selection_mode == "shuffle_cycle":
        candidate = session.scalar(fresh_query.order_by(func.random()).limit(1))
        if candidate or not rule.repeat_after_exhaustion:
            return candidate
        return session.scalar(base_query.order_by(FileRecord.last_posted_at.asc()).limit(1))

    return session.scalar(base_query.order_by(func.random()).limit(1))
