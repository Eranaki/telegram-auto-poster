from __future__ import annotations

import math
import secrets
from datetime import datetime, timedelta

from fastapi import Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import DEFAULT_ADMIN_USERNAME, build_default_admin_credentials, uses_default_credentials
from app.models import AdminAccount, LoginThrottle

CSRF_SESSION_KEY = "csrf_token"
CSRF_ERROR_DETAIL = "Недействительный CSRF-токен"
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_BACKOFF_SECONDS = 60


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if token:
        return str(token)
    token = secrets.token_urlsafe(32)
    request.session[CSRF_SESSION_KEY] = token
    return token


def csrf_protect(request: Request, csrf_token: str = Form(...)) -> None:
    expected_token = ensure_csrf_token(request)
    if not csrf_token or csrf_token != expected_token:
        raise HTTPException(status_code=403, detail=CSRF_ERROR_DETAIL)


def ensure_admin_account(session: Session) -> AdminAccount:
    account = session.get(AdminAccount, 1)
    if account is None:
        username, password_salt, password_hash = build_default_admin_credentials()
        account = AdminAccount(
            id=1,
            username=username,
            password_salt=password_salt,
            password_hash=password_hash,
        )
        session.add(account)
        session.commit()
        session.refresh(account)
    return account


def get_auth_state(session: Session) -> dict[str, object]:
    account = ensure_admin_account(session)
    return {
        "current_admin_username": account.username,
        "auth_warning_active": uses_default_credentials(account.username, account.password_salt, account.password_hash),
    }


def get_request_client_key(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def get_login_backoff_seconds(session: Session, request: Request) -> int:
    client_key = get_request_client_key(request)
    state = session.scalar(select(LoginThrottle).where(LoginThrottle.client_key == client_key))
    if state is None:
        return 0

    now = datetime.utcnow()
    last_failed_at = state.last_failed_at or datetime.min
    if now - last_failed_at > timedelta(seconds=LOGIN_RATE_LIMIT_WINDOW_SECONDS):
        session.delete(state)
        session.commit()
        return 0

    blocked_until = state.blocked_until
    if blocked_until is None or blocked_until <= now:
        return 0

    return max(1, math.ceil((blocked_until - now).total_seconds()))


def register_failed_login(session: Session, request: Request) -> int:
    client_key = get_request_client_key(request)
    now = datetime.utcnow()
    state = session.scalar(select(LoginThrottle).where(LoginThrottle.client_key == client_key))

    if state is None:
        state = LoginThrottle(client_key=client_key)
        failures = 1
    else:
        last_failed_at = state.last_failed_at or datetime.min
        is_stale = now - last_failed_at > timedelta(seconds=LOGIN_RATE_LIMIT_WINDOW_SECONDS)
        failures = 1 if is_stale else state.failures + 1

    delay_seconds = min(2 ** max(failures - 1, 0), LOGIN_MAX_BACKOFF_SECONDS)
    state.failures = failures
    state.last_failed_at = now
    state.blocked_until = now + timedelta(seconds=delay_seconds)
    session.add(state)
    session.commit()
    return delay_seconds


def clear_failed_logins(session: Session, request: Request) -> None:
    client_key = get_request_client_key(request)
    state = session.scalar(select(LoginThrottle).where(LoginThrottle.client_key == client_key))
    if state is None:
        return
    session.delete(state)
    session.commit()


def sanitize_next_path(next_path: str | None) -> str:
    if not next_path or next_path in {"null", "undefined", "none"}:
        return "/"
    candidate = next_path.strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate
