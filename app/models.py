from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ChannelSource(Base):
    __tablename__ = "channel_sources"
    __table_args__ = (UniqueConstraint("channel_id", "source_id", name="uq_channel_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("telegram_channels.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("content_sources.id", ondelete="CASCADE"), nullable=False)

    channel: Mapped["TelegramChannel"] = relationship(back_populates="source_links")
    source: Mapped["ContentSource"] = relationship(back_populates="channel_links")


class RuleSource(Base):
    __tablename__ = "rule_sources"
    __table_args__ = (UniqueConstraint("rule_id", "source_id", name="uq_rule_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("posting_rules.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("content_sources.id", ondelete="CASCADE"), nullable=False)

    rule: Mapped["PostingRule"] = relationship(back_populates="source_links")
    source: Mapped["ContentSource"] = relationship(back_populates="rule_links")


class TelegramChannel(TimestampMixin, Base):
    __tablename__ = "telegram_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    bot_token: Mapped[str | None] = mapped_column(String(255))
    chat_id: Mapped[str | None] = mapped_column(String(255))
    parse_mode: Mapped[str | None] = mapped_column(String(32), default="HTML")
    default_caption: Mapped[str | None] = mapped_column(Text)
    disable_notification: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    protect_content: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    rules: Mapped[list["PostingRule"]] = relationship(back_populates="channel", cascade="all, delete-orphan")
    source_links: Mapped[list["ChannelSource"]] = relationship(back_populates="channel", cascade="all, delete-orphan")


class TelegramConfig(TimestampMixin, Base):
    __tablename__ = "telegram_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_token: Mapped[str | None] = mapped_column(String(255))
    default_chat_id: Mapped[str | None] = mapped_column(String(255))
    parse_mode: Mapped[str | None] = mapped_column(String(32), default="HTML")
    default_caption: Mapped[str | None] = mapped_column(Text)
    disable_notification: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    protect_content: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class FileBrowserSettings(TimestampMixin, Base):
    __tablename__ = "file_browser_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_view_mode: Mapped[str] = mapped_column(String(16), default="grid", nullable=False)
    file_card_size: Mapped[str] = mapped_column(String(16), default="small", nullable=False)
    file_page_size: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    thumbnail_size_px: Mapped[int] = mapped_column(Integer, default=256, nullable=False)


class AdminAccount(TimestampMixin, Base):
    __tablename__ = "admin_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    password_salt: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)


class LoginThrottle(Base):
    __tablename__ = "login_throttles"

    client_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime)


class ContentSource(TimestampMixin, Base):
    __tablename__ = "content_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    path: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    recursive: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    allowed_extensions: Mapped[str | None] = mapped_column(String(500))
    scan_interval_minutes: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    manual_scan_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_scan_result: Mapped[str | None] = mapped_column(Text)
    scan_in_progress: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scan_mode: Mapped[str | None] = mapped_column(String(32))
    scan_progress_current: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scan_progress_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scan_progress_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    files: Mapped[list["FileRecord"]] = relationship(back_populates="source", cascade="all, delete-orphan")
    rules: Mapped[list["PostingRule"]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        foreign_keys="PostingRule.source_id",
    )
    channel_links: Mapped[list["ChannelSource"]] = relationship(back_populates="source", cascade="all, delete-orphan")
    rule_links: Mapped[list["RuleSource"]] = relationship(back_populates="source", cascade="all, delete-orphan")


class FileRecord(Base):
    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("source_id", "relative_path", name="uq_file_source_relpath"),
        Index("ix_files_source_active", "source_id", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("content_sources.id", ondelete="CASCADE"), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(600), nullable=False)
    absolute_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    media_kind: Mapped[str] = mapped_column(String(32), default="document", nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    last_posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    post_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    source: Mapped["ContentSource"] = relationship(back_populates="files")
    posts: Mapped[list["PostHistory"]] = relationship(back_populates="file")


class PostingRule(TimestampMixin, Base):
    __tablename__ = "posting_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    channel_id: Mapped[int] = mapped_column(ForeignKey("telegram_channels.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("content_sources.id"), nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    allowed_from_hour: Mapped[int | None] = mapped_column(Integer)
    allowed_to_hour: Mapped[int | None] = mapped_column(Integer)
    jitter_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    burst_post_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    burst_interval_minutes: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    pending_burst_posts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_selection_mode: Mapped[str] = mapped_column(String(40), default="merged_pool", nullable=False)
    selection_mode: Mapped[str] = mapped_column(String(40), default="random_no_repeat", nullable=False)
    repeat_after_exhaustion: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    caption_template: Mapped[str | None] = mapped_column(Text)
    chat_id_override: Mapped[str | None] = mapped_column(String(255))
    send_as_document: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    convert_heic_to_jpeg: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_source_id: Mapped[int | None] = mapped_column(ForeignKey("content_sources.id"))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_result: Mapped[str | None] = mapped_column(Text)

    channel: Mapped["TelegramChannel"] = relationship(back_populates="rules")
    source: Mapped["ContentSource"] = relationship(
        back_populates="rules",
        foreign_keys=[source_id],
    )
    source_links: Mapped[list["RuleSource"]] = relationship(back_populates="rule", cascade="all, delete-orphan")
    posts: Mapped[list["PostHistory"]] = relationship(back_populates="rule", cascade="all, delete-orphan")


class PostHistory(Base):
    __tablename__ = "post_history"
    __table_args__ = (Index("ix_post_history_rule_status", "rule_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("posting_rules.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("content_sources.id", ondelete="CASCADE"), nullable=False)
    file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    manual_trigger: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    telegram_message_id: Mapped[str | None] = mapped_column(String(120))
    attempted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)

    rule: Mapped["PostingRule"] = relationship(back_populates="posts")
    file: Mapped["FileRecord"] = relationship(back_populates="posts")
