from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler as fastapi_http_exception_handler
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.config import APP_SESSION_HTTPS_ONLY, APP_SESSION_SECRET
from app.db import init_db
from app.services.scheduler import AppScheduler
from app.web import router
from app.web_security import CSRF_ERROR_DETAIL


templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    app_scheduler = AppScheduler()
    app_scheduler.start()
    app.state.app_scheduler = app_scheduler
    yield
    await app_scheduler.shutdown()


class RequireAdminAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == "/login" or path == "/favicon.ico" or path.startswith("/static"):
            await self.app(scope, receive, send)
            return

        session = scope.get("session") or {}
        if session.get("admin_authenticated"):
            await self.app(scope, receive, send)
            return

        query_string = scope.get("query_string", b"").decode("utf-8", errors="ignore")
        next_path = path
        if query_string:
            next_path = f"{next_path}?{query_string}"

        response = RedirectResponse(f"/login?next={quote(next_path, safe='/?=&')}", status_code=303)
        await response(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def send_with_security_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-frame-options", b"DENY"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"same-origin"),
                        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    ]
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


app = FastAPI(
    title="Telegram Auto Poster",
    description="Самохостинг-сервис для публикации файлов из подключенных папок в Telegram.",
    lifespan=lifespan,
)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequireAdminAuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=APP_SESSION_SECRET,
    same_site="lax",
    https_only=APP_SESSION_HTTPS_ONLY,
    session_cookie="tap_session",
    max_age=60 * 60 * 24 * 30,
)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")
app.include_router(router)


@app.exception_handler(StarletteHTTPException)
async def html_http_exception_handler(request: Request, exc: StarletteHTTPException):
    accepts_html = "text/html" in request.headers.get("accept", "").lower()
    if exc.status_code == 403 and str(exc.detail) == CSRF_ERROR_DETAIL and accepts_html:
        return templates.TemplateResponse(
            "error_403.html",
            {
                "request": request,
                "title": "Ошибка защиты формы",
                "message": "Страница устарела или сессия была обновлена. Обновите страницу и повторите действие.",
            },
            status_code=403,
        )
    return await fastapi_http_exception_handler(request, exc)
