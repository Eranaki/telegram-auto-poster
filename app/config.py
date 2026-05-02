from __future__ import annotations

import os
import secrets
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("APP_DATA_DIR", BASE_DIR / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
APP_DB_PATH = Path(os.getenv("APP_DB_PATH", DATA_DIR / "app.db")).resolve()
APP_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
PREVIEWS_DIR = (DATA_DIR / "previews").resolve()
PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{APP_DB_PATH.as_posix()}")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "UTC")
SCHEDULER_TICK_SECONDS = int(os.getenv("SCHEDULER_TICK_SECONDS", "30"))
SCAN_TICK_SECONDS = int(os.getenv("SCAN_TICK_SECONDS", "120"))
MAX_CAPTION_LENGTH = int(os.getenv("MAX_CAPTION_LENGTH", "1024"))
PREVIEW_BATCH_SIZE = int(os.getenv("PREVIEW_BATCH_SIZE", "24"))


def load_or_create_session_secret() -> str:
    configured_secret = os.getenv("APP_SESSION_SECRET", "").strip()
    if configured_secret:
        return configured_secret

    secret_path = DATA_DIR / ".session_secret"
    if secret_path.exists():
        stored_secret = secret_path.read_text(encoding="utf-8").strip()
        if stored_secret:
            return stored_secret

    generated_secret = secrets.token_urlsafe(48)
    try:
        secret_path.write_text(generated_secret, encoding="utf-8")
    except OSError:
        return generated_secret
    return generated_secret


APP_SESSION_SECRET = load_or_create_session_secret()
APP_SESSION_HTTPS_ONLY = os.getenv("APP_SESSION_HTTPS_ONLY", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
