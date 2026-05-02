from __future__ import annotations

import hashlib
import hmac
import secrets


DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"
PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt_value = salt or secrets.token_hex(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt_value.encode("utf-8"),
        PBKDF2_ITERATIONS,
    ).hex()
    return salt_value, password_hash


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    _, actual_hash = hash_password(password, salt)
    return hmac.compare_digest(actual_hash, expected_hash)


def build_default_admin_credentials() -> tuple[str, str, str]:
    salt, password_hash = hash_password(DEFAULT_ADMIN_PASSWORD)
    return DEFAULT_ADMIN_USERNAME, salt, password_hash


def uses_default_credentials(username: str, password_salt: str, password_hash: str) -> bool:
    return username == DEFAULT_ADMIN_USERNAME or verify_password(DEFAULT_ADMIN_PASSWORD, password_salt, password_hash)
