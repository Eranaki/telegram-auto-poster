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
os.environ["APP_DATA_DIR"] = str(Path(_test_runtime.name) / "data")
os.environ["APP_DB_PATH"] = str(Path(_test_runtime.name) / "db" / "app.db")
os.environ["APP_SESSION_SECRET"] = "test-only-not-for-production"
os.environ.pop("DATABASE_URL", None)

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, inspect, select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

import app.db as db_module  # noqa: E402
from app.db import Base, get_session  # noqa: E402
from app.models import (  # noqa: E402
    ChannelSource,
    ContentSource,
    FileRecord,
    PostHistory,
    PostingRule,
    RuleSource,
    TelegramChannel,
)
from app.services.telegram import (  # noqa: E402
    FileNotFoundPublishError,
    publish_file,
    render_caption,
)
from app.web import (  # noqa: E402
    import_rule_to_channel,
    parse_optional_filter_id,
    post_queue_file_now,
    requeue_missing_history_file,
    router,
)


def make_rule(channel_id: int, source_id: int, name: str = "Rule") -> PostingRule:
    return PostingRule(
        name=name,
        channel_id=channel_id,
        source_id=source_id,
        interval_minutes=60,
        selection_mode="random_no_repeat",
    )


class TelegramResponse:
    def __init__(self, status_code: int, payload: dict[str, object]):
        self.status_code = status_code
        self.payload = payload
        self.text = str(payload)

    def json(self):
        return self.payload


class SequencedTelegramClient:
    responses: list[TelegramResponse] = []
    calls: list[tuple[str, set[str]]] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, data, files):
        self.__class__.calls.append((url, set(files)))
        return self.__class__.responses.pop(0)


class RuleHistoryFixTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.engine = create_engine(
            f"sqlite:///{Path(self.temp_dir.name) / 'test.db'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)

    def seed_channel_source_rule(self, session, *, source_path: str):
        channel = TelegramChannel(name="Channel", bot_token="test-token", chat_id="test-chat")
        source = ContentSource(name="Source", path=source_path, allowed_extensions="photo")
        session.add_all([channel, source])
        session.flush()
        rule = make_rule(channel.id, source.id)
        session.add(rule)
        session.flush()
        session.add_all(
            [
                ChannelSource(channel_id=channel.id, source_id=source.id),
                RuleSource(rule_id=rule.id, source_id=source.id),
            ]
        )
        session.commit()
        return channel, source, rule

    def test_import_existing_rule_copies_sources_and_caption_filename_settings(self):
        with self.session_factory() as session:
            source_path = str(Path(self.temp_dir.name) / "source")
            source = ContentSource(name="Source", path=source_path)
            first_channel = TelegramChannel(name="First")
            target_channel = TelegramChannel(name="Target")
            session.add_all([source, first_channel, target_channel])
            session.flush()
            template_rule = make_rule(first_channel.id, source.id, "Template")
            template_rule.include_filename_in_caption = True
            template_rule.include_file_path_in_caption = True
            session.add(template_rule)
            session.flush()
            session.add(RuleSource(rule_id=template_rule.id, source_id=source.id))
            session.commit()

            request = SimpleNamespace(url_for=lambda name, **params: f"/channels/{params['channel_id']}/rules")
            response = asyncio.run(
                import_rule_to_channel(
                    request=request,
                    channel_id=target_channel.id,
                    template_rule_id=template_rule.id,
                    _=None,
                    session=session,
                )
            )

            copied_rule = session.scalar(select(PostingRule).where(PostingRule.channel_id == target_channel.id))
            self.assertEqual(response.status_code, 303)
            self.assertIsNotNone(copied_rule)
            self.assertTrue(copied_rule.include_filename_in_caption)
            self.assertTrue(copied_rule.include_file_path_in_caption)
            self.assertEqual(
                session.scalars(select(RuleSource.source_id).where(RuleSource.rule_id == copied_rule.id)).all(),
                [source.id],
            )
            self.assertIsNotNone(
                session.scalar(
                    select(ChannelSource).where(
                        ChannelSource.channel_id == target_channel.id,
                        ChannelSource.source_id == source.id,
                    )
                )
            )

    def test_global_history_accepts_blank_and_legacy_none_filters(self):
        with self.session_factory() as session:
            _, source, rule = self.seed_channel_source_rule(session, source_path=self.temp_dir.name)
            for index in range(51):
                session.add(
                    PostHistory(
                        rule_id=rule.id,
                        source_id=source.id,
                        status="skipped",
                        message=f"Entry {index}",
                    )
                )
            session.commit()

        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key="test-only-not-for-production")
        app.mount(
            "/static",
            StaticFiles(directory=str(Path(__file__).resolve().parents[1] / "app" / "static")),
            name="static",
        )
        app.include_router(router)

        def session_override():
            with self.session_factory() as session:
                yield session

        app.dependency_overrides[get_session] = session_override
        with TestClient(app) as client:
            blank_response = client.get("/history?source_id=&channel_id=&rule_id=&per_page=50")
            legacy_response = client.get("/history?page=2&source_id=None&channel_id=None&rule_id=None")

        self.assertEqual(blank_response.status_code, 200)
        self.assertEqual(legacy_response.status_code, 200)
        self.assertNotIn("source_id=None", blank_response.text)
        self.assertNotIn("channel_id=None", blank_response.text)
        self.assertNotIn("rule_id=None", blank_response.text)
        self.assertEqual(parse_optional_filter_id(" 42 ", "ID"), 42)

    def test_missing_file_can_be_reactivated_without_publication(self):
        source_root = Path(self.temp_dir.name) / "source"
        source_root.mkdir()
        file_path = source_root / "photo.jpg"

        with self.session_factory() as session:
            channel, source, rule = self.seed_channel_source_rule(session, source_path=str(source_root))
            file_record = FileRecord(
                source_id=source.id,
                relative_path="photo.jpg",
                absolute_path=str(file_path),
                media_kind="photo",
                size=1,
                mtime_ns=1,
                fingerprint="old",
            )
            session.add(file_record)
            session.commit()

            request = SimpleNamespace(
                headers={},
                url_for=lambda name, **params: f"/rules/{params['rule_id']}/queue",
            )
            response = asyncio.run(
                post_queue_file_now(
                    request=request,
                    rule_id=rule.id,
                    file_id=file_record.id,
                    _=None,
                    session=session,
                )
            )
            self.assertEqual(response.status_code, 303)
            self.assertFalse(file_record.is_active)
            history = session.scalar(select(PostHistory).where(PostHistory.file_id == file_record.id))
            self.assertTrue(history.message.startswith("Файл не найден:"))

            file_path.write_bytes(b"restored")
            with patch("app.services.scanner.CONTENT_ROOT", source_root), patch(
                "app.services.telegram.publish_file"
            ) as publish_mock:
                requeue_response = asyncio.run(
                    requeue_missing_history_file(
                        history_id=history.id,
                        return_to="/history?page=2",
                        _=None,
                        session=session,
                    )
                )
            publish_mock.assert_not_called()

            self.assertEqual(requeue_response.status_code, 303)
            self.assertEqual(requeue_response.headers["location"], "/history?page=2")
            self.assertTrue(file_record.is_active)
            self.assertEqual(file_record.size, len(b"restored"))
            self.assertNotEqual(file_record.fingerprint, "old")
            self.assertEqual(history.status, "failed")
            self.assertIn("Файл возвращен в очередь", history.message)
            self.assertEqual(file_record.post_count, 0)
            self.assertIsNone(rule.next_run_at)

            with self.assertRaises(HTTPException) as repeated_request:
                asyncio.run(
                    requeue_missing_history_file(
                        history_id=history.id,
                        return_to="/history",
                        _=None,
                        session=session,
                    )
                )
            self.assertEqual(repeated_request.exception.status_code, 409)

    def test_missing_publish_uses_typed_error_without_http(self):
        channel = SimpleNamespace(bot_token="token", chat_id="chat", message_thread_id=None)
        rule = SimpleNamespace(chat_id_override=None)
        file_record = SimpleNamespace(absolute_path="/missing", relative_path="safe/name.jpg")
        with patch("app.services.telegram.httpx.AsyncClient") as client:
            with self.assertRaises(FileNotFoundPublishError):
                asyncio.run(publish_file(channel, rule, file_record))
        client.assert_not_called()

    def test_invalid_photo_dimensions_falls_back_to_document_once(self):
        channel = SimpleNamespace(
            bot_token="token",
            chat_id="chat",
            message_thread_id=None,
            default_caption=None,
            disable_notification=False,
            protect_content=False,
            parse_mode=None,
        )
        rule = SimpleNamespace(
            chat_id_override=None,
            caption_template=None,
            convert_heic_to_jpeg=False,
            send_as_document=False,
            include_filename_in_caption=False,
        )
        SequencedTelegramClient.calls = []
        SequencedTelegramClient.responses = [
            TelegramResponse(400, {"ok": False, "description": "Bad Request: PHOTO_INVALID_DIMENSIONS"}),
            TelegramResponse(200, {"ok": True, "result": {"message_id": 321}}),
        ]

        with tempfile.NamedTemporaryFile(suffix=".jpg") as source_file:
            file_record = SimpleNamespace(
                absolute_path=source_file.name,
                relative_path="photo.jpg",
                media_kind="photo",
                source=SimpleNamespace(name="Source"),
            )
            with patch("app.services.telegram.httpx.AsyncClient", SequencedTelegramClient):
                message_id = asyncio.run(publish_file(channel, rule, file_record))

        self.assertEqual(message_id, "321")
        self.assertEqual(len(SequencedTelegramClient.calls), 2)
        self.assertTrue(SequencedTelegramClient.calls[0][0].endswith("/sendPhoto"))
        self.assertEqual(SequencedTelegramClient.calls[0][1], {"photo"})
        self.assertTrue(SequencedTelegramClient.calls[1][0].endswith("/sendDocument"))
        self.assertEqual(SequencedTelegramClient.calls[1][1], {"document"})

    def test_failed_document_fallback_stops_and_can_be_requeued(self):
        source_root = Path(self.temp_dir.name) / "fallback-source"
        source_root.mkdir()
        file_path = source_root / "photo.jpg"
        file_path.write_bytes(b"photo")

        with self.session_factory() as session:
            _, source, rule = self.seed_channel_source_rule(session, source_path=str(source_root))
            file_record = FileRecord(
                source_id=source.id,
                relative_path="photo.jpg",
                absolute_path=str(file_path),
                media_kind="photo",
                size=file_path.stat().st_size,
                mtime_ns=file_path.stat().st_mtime_ns,
                fingerprint="fingerprint",
            )
            session.add(file_record)
            session.commit()
            request = SimpleNamespace(
                headers={},
                url_for=lambda name, **params: f"/rules/{params['rule_id']}/queue",
            )
            SequencedTelegramClient.calls = []
            SequencedTelegramClient.responses = [
                TelegramResponse(400, {"ok": False, "description": "Bad Request: PHOTO_INVALID_DIMENSIONS"}),
                TelegramResponse(400, {"ok": False, "description": "Bad Request: document rejected"}),
            ]

            with patch("app.services.telegram.httpx.AsyncClient", SequencedTelegramClient):
                response = asyncio.run(
                    post_queue_file_now(
                        request=request,
                        rule_id=rule.id,
                        file_id=file_record.id,
                        _=None,
                        session=session,
                    )
                )

            history = session.scalar(select(PostHistory).where(PostHistory.file_id == file_record.id))
            self.assertEqual(response.status_code, 303)
            self.assertEqual(len(SequencedTelegramClient.calls), 2)
            self.assertFalse(file_record.is_active)
            self.assertTrue(history.message.startswith("Не удалось отправить проблемное фото как документ:"))

            with patch("app.services.scanner.CONTENT_ROOT", source_root):
                requeue_response = asyncio.run(
                    requeue_missing_history_file(
                        history_id=history.id,
                        return_to="/history",
                        _=None,
                        session=session,
                    )
                )
            self.assertEqual(requeue_response.status_code, 303)
            self.assertTrue(file_record.is_active)


class CaptionFilenameTests(unittest.TestCase):
    def make_objects(self, *, include_filename: bool, include_path: bool, parse_mode: str | None = "HTML"):
        channel = SimpleNamespace(default_caption=None, parse_mode=parse_mode)
        rule = SimpleNamespace(
            caption_template="Caption",
            include_filename_in_caption=include_filename,
            include_file_path_in_caption=include_path,
        )
        file_record = SimpleNamespace(
            absolute_path="/content/source/folder/a&b.jpg",
            relative_path="folder/a&b.jpg",
            source=SimpleNamespace(name="Source"),
        )
        return channel, rule, file_record

    def test_caption_appends_basename_or_relative_path(self):
        channel, rule, file_record = self.make_objects(include_filename=True, include_path=False)
        self.assertEqual(render_caption(channel, rule, file_record), "Caption\na&amp;b.jpg")

        rule.include_file_path_in_caption = True
        self.assertEqual(render_caption(channel, rule, file_record), "Caption\nfolder/a&amp;b.jpg")

        rule.include_filename_in_caption = False
        self.assertEqual(render_caption(channel, rule, file_record), "Caption")

    def test_filename_can_be_the_only_caption_and_markdown_is_escaped(self):
        channel, rule, file_record = self.make_objects(
            include_filename=True,
            include_path=True,
            parse_mode="MarkdownV2",
        )
        rule.caption_template = None
        file_record.relative_path = "folder/a_b.jpg"
        self.assertEqual(render_caption(channel, rule, file_record), r"folder/a\_b\.jpg")

    def test_template_values_are_escaped_and_invalid_or_long_captions_are_normalized(self):
        channel, rule, file_record = self.make_objects(include_filename=False, include_path=False)
        rule.caption_template = "<b>{relative_path}</b>"
        self.assertEqual(render_caption(channel, rule, file_record), "<b>folder/a&amp;b.jpg</b>")

        rule.caption_template = "{filename.missing}"
        with self.assertRaisesRegex(RuntimeError, "проверьте шаблон"):
            render_caption(channel, rule, file_record)

        rule.caption_template = "12345"
        with patch("app.services.telegram.MAX_CAPTION_LENGTH", 4):
            with self.assertRaisesRegex(RuntimeError, "слишком длинная"):
                render_caption(channel, rule, file_record)


class CaptionMigrationTests(unittest.TestCase):
    def test_new_and_legacy_databases_have_caption_filename_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            new_engine = create_engine(f"sqlite:///{Path(temp_dir) / 'new.db'}")
            Base.metadata.create_all(new_engine)
            new_columns = {column["name"] for column in inspect(new_engine).get_columns("posting_rules")}
            self.assertIn("include_filename_in_caption", new_columns)
            self.assertIn("include_file_path_in_caption", new_columns)
            new_engine.dispose()

            legacy_engine = create_engine(f"sqlite:///{Path(temp_dir) / 'legacy.db'}")
            with legacy_engine.begin() as connection:
                connection.execute(text("CREATE TABLE telegram_channels (id INTEGER PRIMARY KEY, name VARCHAR(120))"))
                connection.execute(text("INSERT INTO telegram_channels (id, name) VALUES (1, 'Channel')"))
                connection.execute(
                    text(
                        "CREATE TABLE posting_rules ("
                        "id INTEGER PRIMARY KEY, channel_id INTEGER, source_id INTEGER"
                        ")"
                    )
                )
                connection.execute(text("INSERT INTO posting_rules (id, channel_id, source_id) VALUES (1, 1, 1)"))

            with patch.object(db_module, "engine", legacy_engine):
                db_module.migrate_schema()
                db_module.migrate_schema()

            legacy_columns = {column["name"] for column in inspect(legacy_engine).get_columns("posting_rules")}
            self.assertIn("include_filename_in_caption", legacy_columns)
            self.assertIn("include_file_path_in_caption", legacy_columns)
            with legacy_engine.connect() as connection:
                values = connection.execute(
                    text(
                        "SELECT include_filename_in_caption, include_file_path_in_caption "
                        "FROM posting_rules WHERE id = 1"
                    )
                ).one()
            self.assertEqual(tuple(values), (0, 0))
            legacy_engine.dispose()


if __name__ == "__main__":
    unittest.main()
