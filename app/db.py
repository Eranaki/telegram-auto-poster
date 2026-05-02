from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.auth import build_default_admin_credentials
from app.config import DATABASE_URL


class Base(DeclarativeBase):
    pass


connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def migrate_schema() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as connection:
        if "posting_rules" in existing_tables:
            rule_columns = {column["name"] for column in inspector.get_columns("posting_rules")}
            if "channel_id" not in rule_columns:
                connection.execute(text("ALTER TABLE posting_rules ADD COLUMN channel_id INTEGER"))
            if "burst_post_count" not in rule_columns:
                connection.execute(
                    text("ALTER TABLE posting_rules ADD COLUMN burst_post_count INTEGER DEFAULT 1 NOT NULL")
                )
            if "burst_interval_minutes" not in rule_columns:
                connection.execute(
                    text("ALTER TABLE posting_rules ADD COLUMN burst_interval_minutes INTEGER DEFAULT 1 NOT NULL")
                )
            if "pending_burst_posts" not in rule_columns:
                connection.execute(
                    text("ALTER TABLE posting_rules ADD COLUMN pending_burst_posts INTEGER DEFAULT 0 NOT NULL")
                )
            if "convert_heic_to_jpeg" not in rule_columns:
                connection.execute(
                    text("ALTER TABLE posting_rules ADD COLUMN convert_heic_to_jpeg INTEGER DEFAULT 0 NOT NULL")
                )
            if "source_selection_mode" not in rule_columns:
                connection.execute(
                    text("ALTER TABLE posting_rules ADD COLUMN source_selection_mode VARCHAR(40) DEFAULT 'merged_pool' NOT NULL")
                )
            if "last_source_id" not in rule_columns:
                connection.execute(text("ALTER TABLE posting_rules ADD COLUMN last_source_id INTEGER"))

        if "post_history" in existing_tables:
            history_columns = {column["name"] for column in inspector.get_columns("post_history")}
            if "manual_trigger" not in history_columns:
                connection.execute(
                    text("ALTER TABLE post_history ADD COLUMN manual_trigger INTEGER DEFAULT 0 NOT NULL")
                )

        channel_rows = []
        if "telegram_channels" in existing_tables:
            channel_rows = connection.execute(
                text("SELECT id, name FROM telegram_channels ORDER BY id ASC")
            ).mappings().all()

        if not channel_rows:
            config_row = None
            if "telegram_config" in existing_tables:
                config_row = connection.execute(
                    text(
                        """
                        SELECT bot_token, default_chat_id, parse_mode, default_caption,
                               disable_notification, protect_content
                        FROM telegram_config
                        ORDER BY id ASC
                        LIMIT 1
                        """
                    )
                ).mappings().first()

            channel_id = connection.execute(
                text(
                    """
                    INSERT INTO telegram_channels (
                        name, bot_token, chat_id, parse_mode, default_caption,
                        disable_notification, protect_content, enabled, created_at, updated_at
                    ) VALUES (
                        :name, :bot_token, :chat_id, :parse_mode, :default_caption,
                        :disable_notification, :protect_content, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "name": "Основной",
                    "bot_token": config_row["bot_token"] if config_row else None,
                    "chat_id": config_row["default_chat_id"] if config_row else None,
                    "parse_mode": config_row["parse_mode"] if config_row else "HTML",
                    "default_caption": config_row["default_caption"] if config_row else None,
                    "disable_notification": int(bool(config_row["disable_notification"])) if config_row else 0,
                    "protect_content": int(bool(config_row["protect_content"])) if config_row else 0,
                },
            ).lastrowid
        else:
            channel_id = channel_rows[0]["id"]

        if "posting_rules" in existing_tables:
            connection.execute(
                text("UPDATE posting_rules SET channel_id = :channel_id WHERE channel_id IS NULL"),
                {"channel_id": channel_id},
            )
            if "channel_sources" in existing_tables:
                connection.execute(
                    text(
                        """
                        INSERT OR IGNORE INTO channel_sources (channel_id, source_id)
                        SELECT channel_id, source_id
                        FROM posting_rules
                        WHERE channel_id IS NOT NULL AND source_id IS NOT NULL
                        """
                    )
                )
            if "rule_sources" in existing_tables:
                connection.execute(
                    text(
                        """
                        INSERT OR IGNORE INTO rule_sources (rule_id, source_id)
                        SELECT id, source_id
                        FROM posting_rules
                        WHERE source_id IS NOT NULL
                        """
                    )
                )

        if "content_sources" in existing_tables:
            source_columns = {column["name"] for column in inspector.get_columns("content_sources")}
            if "manual_scan_only" not in source_columns:
                connection.execute(
                    text("ALTER TABLE content_sources ADD COLUMN manual_scan_only INTEGER DEFAULT 0 NOT NULL")
                )
            if "scan_in_progress" not in source_columns:
                connection.execute(
                    text("ALTER TABLE content_sources ADD COLUMN scan_in_progress INTEGER DEFAULT 0 NOT NULL")
                )
            if "scan_mode" not in source_columns:
                connection.execute(text("ALTER TABLE content_sources ADD COLUMN scan_mode VARCHAR(32)"))
            if "scan_progress_current" not in source_columns:
                connection.execute(
                    text("ALTER TABLE content_sources ADD COLUMN scan_progress_current INTEGER DEFAULT 0 NOT NULL")
                )
            if "scan_progress_total" not in source_columns:
                connection.execute(
                    text("ALTER TABLE content_sources ADD COLUMN scan_progress_total INTEGER DEFAULT 0 NOT NULL")
                )
            if "scan_progress_percent" not in source_columns:
                connection.execute(
                    text("ALTER TABLE content_sources ADD COLUMN scan_progress_percent INTEGER DEFAULT 0 NOT NULL")
                )

        if "file_browser_settings" in existing_tables:
            settings_exists = connection.execute(
                text("SELECT id FROM file_browser_settings ORDER BY id ASC LIMIT 1")
            ).mappings().first()
            if settings_exists is None:
                connection.execute(
                    text(
                        """
                        INSERT INTO file_browser_settings (
                            id, file_view_mode, file_card_size, file_page_size,
                            thumbnail_size_px, created_at, updated_at
                        ) VALUES (
                            1, 'grid', 'small', 50, 256, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    )
                )

        if "admin_accounts" in existing_tables:
            admin_exists = connection.execute(
                text("SELECT id FROM admin_accounts ORDER BY id ASC LIMIT 1")
            ).mappings().first()
            if admin_exists is None:
                username, password_salt, password_hash = build_default_admin_credentials()
                connection.execute(
                    text(
                        """
                        INSERT INTO admin_accounts (
                            id, username, password_salt, password_hash, created_at, updated_at
                        ) VALUES (
                            1, :username, :password_salt, :password_hash, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "username": username,
                        "password_salt": password_salt,
                        "password_hash": password_hash,
                    },
                )


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    migrate_schema()
