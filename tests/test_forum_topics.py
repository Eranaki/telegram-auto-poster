from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_test_runtime = tempfile.TemporaryDirectory()
unittest.addModuleCleanup(_test_runtime.cleanup)
os.environ.setdefault("APP_DATA_DIR", str(Path(_test_runtime.name) / "data"))
os.environ.setdefault("APP_DB_PATH", str(Path(_test_runtime.name) / "db" / "app.db"))
os.environ.setdefault("APP_SESSION_SECRET", "test-only-not-for-production")

from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.db as db_module  # noqa: E402
from app.models import TelegramChannel  # noqa: E402
from app.services.telegram import publish_file  # noqa: E402
from app.web import create_channel, parse_optional_message_thread_id, update_channel_settings  # noqa: E402


class FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"ok": True, "result": {"message_id": 123}}


class FakeAsyncClient:
    payload: dict[str, object] | None = None
    url: str | None = None
    file_fields: set[str] | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, data, files):
        self.__class__.payload = dict(data)
        self.__class__.url = url
        self.__class__.file_fields = set(files)
        return FakeResponse()


class ForumTopicMigrationTests(unittest.TestCase):
    def test_new_database_has_thread_id_and_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_engine(f"sqlite:///{Path(temp_dir) / 'new.db'}")

            with patch.object(db_module, "engine", engine):
                db_module.init_db()
                db_module.migrate_schema()

            columns = {column["name"] for column in inspect(engine).get_columns("telegram_channels")}
            self.assertIn("message_thread_id", columns)
            engine.dispose()

    def test_existing_channel_table_gets_nullable_thread_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_engine(f"sqlite:///{Path(temp_dir) / 'legacy.db'}")
            with engine.begin() as connection:
                connection.execute(
                    text("CREATE TABLE telegram_channels (id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL)")
                )
                connection.execute(text("INSERT INTO telegram_channels (id, name) VALUES (1, 'Основной')"))

            with patch.object(db_module, "engine", engine):
                db_module.migrate_schema()

            columns = {column["name"] for column in inspect(engine).get_columns("telegram_channels")}
            self.assertIn("message_thread_id", columns)
            with engine.connect() as connection:
                value = connection.execute(
                    text("SELECT message_thread_id FROM telegram_channels WHERE id = 1")
                ).scalar_one()
            self.assertIsNone(value)
            engine.dispose()


class ForumTopicPayloadTests(unittest.TestCase):
    def setUp(self):
        FakeAsyncClient.payload = None
        FakeAsyncClient.url = None
        FakeAsyncClient.file_fields = None

    def make_channel(self, message_thread_id=456):
        return SimpleNamespace(
            bot_token="test-token",
            chat_id="-1001234567890",
            message_thread_id=message_thread_id,
            default_caption=None,
            disable_notification=False,
            protect_content=False,
            parse_mode="HTML",
        )

    def make_rule(self, chat_id_override=None):
        return SimpleNamespace(
            chat_id_override=chat_id_override,
            caption_template=None,
            convert_heic_to_jpeg=False,
            send_as_document=False,
        )

    def test_topic_is_sent_for_all_media_methods(self):
        cases = {
            "photo": ("sendPhoto", "photo"),
            "animation": ("sendAnimation", "animation"),
            "video": ("sendVideo", "video"),
            "document": ("sendDocument", "document"),
        }
        for media_kind, (method, field_name) in cases.items():
            with self.subTest(media_kind=media_kind), tempfile.NamedTemporaryFile() as source_file:
                file_record = SimpleNamespace(absolute_path=source_file.name, media_kind=media_kind)
                with patch("app.services.telegram.httpx.AsyncClient", FakeAsyncClient):
                    message_id = asyncio.run(publish_file(self.make_channel(), self.make_rule(), file_record))

                self.assertEqual(message_id, "123")
                self.assertEqual(FakeAsyncClient.payload["message_thread_id"], "456")
                self.assertTrue(FakeAsyncClient.url.endswith(f"/{method}"))
                self.assertEqual(FakeAsyncClient.file_fields, {field_name})

    def test_topic_is_omitted_when_rule_overrides_chat(self):
        with tempfile.NamedTemporaryFile(suffix=".jpg") as source_file:
            file_record = SimpleNamespace(absolute_path=source_file.name, media_kind="photo")
            with patch("app.services.telegram.httpx.AsyncClient", FakeAsyncClient):
                asyncio.run(publish_file(self.make_channel(), self.make_rule("@another_chat"), file_record))

        self.assertNotIn("message_thread_id", FakeAsyncClient.payload)

    def test_topic_id_validation(self):
        self.assertIsNone(parse_optional_message_thread_id(""))
        self.assertEqual(parse_optional_message_thread_id(" 42 "), 42)
        self.assertEqual(parse_optional_message_thread_id("2147483647"), 2_147_483_647)
        for invalid_value in ("0", "-1", "2147483648", "topic"):
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaises(ValueError):
                    parse_optional_message_thread_id(invalid_value)


class ForumTopicFormTests(unittest.TestCase):
    def test_create_and_update_channel_persist_topic_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_engine(f"sqlite:///{Path(temp_dir) / 'forms.db'}")
            db_module.Base.metadata.create_all(engine)
            session_factory = sessionmaker(bind=engine, expire_on_commit=False)
            request = SimpleNamespace(url_for=lambda name, **params: f"/channels/{params.get('channel_id', '')}")

            with session_factory() as session:
                asyncio.run(
                    create_channel(
                        request=request,
                        name="Forum topic",
                        bot_token="test-token",
                        chat_id="-1001234567890",
                        message_thread_id="456",
                        parse_mode="HTML",
                        default_caption="",
                        disable_notification=None,
                        protect_content=None,
                        enabled="on",
                        _=None,
                        session=session,
                    )
                )
                channel = session.get(TelegramChannel, 1)
                self.assertEqual(channel.message_thread_id, 456)

                asyncio.run(
                    update_channel_settings(
                        request=request,
                        channel_id=channel.id,
                        name=channel.name,
                        bot_token="test-token",
                        chat_id=channel.chat_id,
                        message_thread_id="789",
                        parse_mode="HTML",
                        default_caption="",
                        disable_notification=None,
                        protect_content=None,
                        enabled="on",
                        _=None,
                        session=session,
                    )
                )
                self.assertEqual(channel.message_thread_id, 789)

            engine.dispose()


if __name__ == "__main__":
    unittest.main()
