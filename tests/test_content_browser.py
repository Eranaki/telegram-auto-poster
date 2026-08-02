from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_test_runtime = tempfile.TemporaryDirectory()
unittest.addModuleCleanup(_test_runtime.cleanup)
os.environ["APP_DATA_DIR"] = str(Path(_test_runtime.name) / "data")
os.environ["APP_DB_PATH"] = str(Path(_test_runtime.name) / "db" / "app.db")
os.environ["APP_SESSION_SECRET"] = "test-only-not-for-production"
os.environ.pop("DATABASE_URL", None)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base  # noqa: E402
from app.web_sources import (  # noqa: E402
    ContentPathChangedError,
    browse_content_paths,
    clear_content_path_cache,
    load_volume_paths_from_compose,
    validate_source_payload,
)
from app.web import router  # noqa: E402


class ContentBrowserTests(unittest.TestCase):
    def setUp(self):
        clear_content_path_cache()

    def test_browser_reads_only_one_directory_level(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "archive" / "year" / "month"
            nested.mkdir(parents=True)

            with patch("app.web_sources.CONTENT_ROOT", root), patch.object(
                Path,
                "rglob",
                side_effect=AssertionError("recursive traversal is forbidden"),
            ):
                root_page = browse_content_paths()
                archive_page = browse_content_paths(str(root / "archive"))

            self.assertEqual([item["name"] for item in root_page["children"]], ["archive"])
            self.assertNotIn("year", [item["name"] for item in root_page["children"]])
            self.assertEqual([item["name"] for item in archive_page["children"]], ["year"])

    def test_browser_paginates_large_single_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index in range(205):
                (root / f"folder-{index:03d}").mkdir()

            with patch("app.web_sources.CONTENT_ROOT", root):
                first_page = browse_content_paths(limit=200)
                second_page = browse_content_paths(
                    offset=200,
                    limit=200,
                    generation=int(first_page["generation"]),
                )

            self.assertEqual(len(first_page["children"]), 200)
            self.assertTrue(first_page["has_more"])
            self.assertIsInstance(first_page["generation"], str)
            self.assertEqual(len(second_page["children"]), 5)
            self.assertFalse(second_page["has_more"])

    def test_browser_stops_scanning_after_one_page(self):
        class CountingScandir:
            def __init__(self, directory):
                self.iterator = os.scandir(directory)
                self.count = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                self.iterator.close()

            def __iter__(self):
                return self

            def __next__(self):
                self.count += 1
                return next(self.iterator)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index in range(500):
                (root / f"folder-{index:03d}").mkdir()
            counter = CountingScandir(root)

            with patch("app.web_sources.CONTENT_ROOT", root), patch(
                "app.web_sources.os.scandir",
                return_value=counter,
            ):
                page = browse_content_paths(limit=200)

            self.assertEqual(len(page["children"]), 200)
            self.assertTrue(page["has_more"])
            self.assertLessEqual(counter.count, 201)

    def test_changed_generation_restarts_pagination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "one").mkdir()

            with patch("app.web_sources.CONTENT_ROOT", root):
                first_page = browse_content_paths(limit=1)
                with self.assertRaises(ContentPathChangedError):
                    browse_content_paths(offset=1, limit=1, generation=int(first_page["generation"]) - 1)

    def test_browser_caches_repeated_directory_reads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "one").mkdir()

            with patch("app.web_sources.CONTENT_ROOT", root), patch(
                "app.web_sources.os.scandir",
                wraps=os.scandir,
            ) as scandir:
                browse_content_paths()
                browse_content_paths()

            self.assertEqual(scandir.call_count, 1)

    def test_symlink_outside_content_is_hidden_and_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(temp_dir)
            outside = Path(outside_dir)
            (root / "safe").mkdir()
            (root / "escape").symlink_to(outside, target_is_directory=True)

            with patch("app.web_sources.CONTENT_ROOT", root):
                page = browse_content_paths()
                names = [item["name"] for item in page["children"]]
                with self.assertRaises(ValueError):
                    browse_content_paths(str(root / "escape"))

            self.assertEqual(names, ["safe"])

    def test_source_validation_uses_direct_containment_check(self):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(temp_dir)
            selected = root / "selected" / "deep"
            selected.mkdir(parents=True)
            engine = create_engine("sqlite:///:memory:")
            Base.metadata.create_all(engine)
            session_factory = sessionmaker(bind=engine)

            with session_factory() as session, patch("app.web_sources.CONTENT_ROOT", root), patch.object(
                Path,
                "rglob",
                side_effect=AssertionError("validation must not scan the tree"),
            ):
                source_name, source_path, media_types, error = validate_source_payload(
                    session,
                    name="Deep source",
                    path=str(selected),
                    file_types=["photo"],
                )
                _, _, _, outside_error = validate_source_payload(
                    session,
                    name="Outside source",
                    path=outside_dir,
                    file_types=["photo"],
                )

            engine.dispose()
            self.assertEqual(source_name, "Deep source")
            self.assertEqual(source_path, selected.resolve().as_posix())
            self.assertEqual(media_types, ["photo"])
            self.assertIsNone(error)
            self.assertIn("внутри /content", outside_error)

    def test_compose_fallback_accepts_only_exact_content_mounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            missing_root = base_dir / "missing-content"
            (base_dir / "docker-compose.yml").write_text(
                """
services:
  app:
    volumes:
      - ./photos:/content/photos:ro
      - ./private:/content-private:ro
      - ./escape:/content/../etc:ro
""".strip(),
                encoding="utf-8",
            )
            engine = create_engine("sqlite:///:memory:")
            Base.metadata.create_all(engine)
            session_factory = sessionmaker(bind=engine)

            with patch("app.web_sources.BASE_DIR", base_dir), patch("app.web_sources.CONTENT_ROOT", missing_root):
                self.assertEqual(load_volume_paths_from_compose(), ["/content/photos"])
                root_page = browse_content_paths()
                selected_page = browse_content_paths("/content/photos")
                with session_factory() as session:
                    _, _, _, exact_error = validate_source_payload(
                        session,
                        name="Photos",
                        path="/content/photos",
                        file_types=["photo"],
                    )
                    _, _, _, nested_error = validate_source_payload(
                        session,
                        name="Nested",
                        path="/content/photos/nested",
                        file_types=["photo"],
                    )

            engine.dispose()
            self.assertEqual(root_page["children"], [{"name": "photos", "path": "/content/photos"}])
            self.assertEqual(root_page["next_offset"], 1)
            self.assertTrue(selected_page["selectable"])
            self.assertIsNone(exact_error)
            self.assertIn("подключенной папки", nested_error)

    def test_route_returns_paginated_json_and_validates_queries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "photos").mkdir()
            app = FastAPI()
            app.include_router(router)
            with patch("app.web_sources.CONTENT_ROOT", root):
                with TestClient(app) as client:
                    response = client.get("/content-paths/options", params={"path": str(root)})
                    invalid_limit = client.get("/content-paths/options", params={"limit": 201})
                    outside_path = client.get("/content-paths/options", params={"path": str(root.parent)})
                    stale_page = client.get(
                        "/content-paths/options",
                        params={"path": str(root), "offset": 1, "generation": -1},
                    )

            payload = response.json()
            self.assertEqual(payload["children"][0]["name"], "photos")
            self.assertIn("generation", payload)
            self.assertEqual(invalid_limit.status_code, 422)
            self.assertEqual(outside_path.status_code, 400)
            self.assertEqual(stale_page.status_code, 409)

    def test_all_source_forms_use_lazy_picker(self):
        templates_dir = Path(__file__).resolve().parents[1] / "app" / "templates"
        sources_template = (templates_dir / "sources.html").read_text(encoding="utf-8-sig")
        channel_template = (templates_dir / "channel_sources.html").read_text(encoding="utf-8-sig")
        base_template = (templates_dir / "base.html").read_text(encoding="utf-8-sig")

        self.assertEqual(sources_template.count('_content_path_picker.html'), 2)
        self.assertEqual(channel_template.count('_content_path_picker.html'), 2)
        self.assertNotIn("data-content-path-select", sources_template + channel_template + base_template)
        self.assertIn("data-content-path-browser", base_template)
        self.assertIn("request.url_for('content_path_options').path", base_template)
        self.assertIn("toggleButton.focus()", base_template)
        self.assertIn("data-content-path-root", (templates_dir / "_content_path_picker.html").read_text(encoding="utf-8"))
        self.assertIn("aria-live=\"polite\"", (templates_dir / "_content_path_picker.html").read_text(encoding="utf-8"))
        self.assertIn("tabindex=\"-1\"", (templates_dir / "_content_path_picker.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
