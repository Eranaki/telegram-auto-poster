from __future__ import annotations

import math
import mimetypes
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.auth import DEFAULT_ADMIN_USERNAME, verify_password, hash_password
from app.db import get_session
from app.models import ContentSource, FileBrowserSettings, FileRecord, PostHistory, PostingRule
from app.services.scan_jobs import start_source_scan_job
from app.services.previews import build_placeholder_svg, generate_preview, get_preview_path, preview_exists
from app.services.scheduler import compute_next_run
from app.services.scanner import (
    MEDIA_TYPE_LABELS,
    SCAN_MODE_ADD_MISSING,
    SCAN_MODE_FULL,
    normalize_media_type_selection,
)
from app.web_contexts import (
    SELECTION_MODE_LABELS,
    SOURCE_SELECTION_MODE_LABELS,
    annotate_file_record,
    annotate_history_items,
    build_channel_overview_context,
    build_channel_rules_context,
    build_channel_sources_page_context,
    build_dashboard_context,
    build_rule_form_defaults,
    build_rule_source_groups,
    build_sources_page_context,
    ensure_channel_source_link,
    ensure_rule_source_links,
    get_channel_or_404,
    get_channel_source_ids,
    get_file_record_or_404,
    get_rule_or_404,
    get_rule_sources,
    get_sidebar_channels,
    get_source_or_404,
    make_unique_rule_name,
    preview_candidates_for_rule,
)
from app.web_security import (
    clear_failed_logins,
    csrf_protect,
    ensure_admin_account,
    ensure_csrf_token,
    get_auth_state,
    get_login_backoff_seconds,
    register_failed_login,
    sanitize_next_path,
)
from app.web_sources import (
    annotate_source,
    attach_source_file_counts,
    build_source_form_defaults,
    build_source_form_from_source,
    build_source_form_state,
    deserialize_media_type_selection,
    load_available_content_paths,
    refresh_available_content_paths,
    serialize_media_type_selection,
    to_bool,
    validate_source_payload,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
FILE_VIEW_MODE_OPTIONS = {"list", "grid"}
FILE_CARD_SIZE_OPTIONS = {"small", "large"}
FILE_PAGE_SIZE_OPTIONS = {20, 50, 100}
DEFAULT_FILE_VIEW_SETTINGS = {
    "file_view_mode": "grid",
    "file_card_size": "small",
    "file_page_size": 50,
    "thumbnail_size_px": 256,
}


def parse_optional_hour(raw_value: str | None) -> int | None:
    if raw_value is None or raw_value == "":
        return None
    value = int(raw_value)
    if value < 0 or value > 23:
        raise ValueError("Час должен быть в диапазоне от 0 до 23")
    return value


def clamp_file_page_size(value: int | None) -> int:
    if value in FILE_PAGE_SIZE_OPTIONS:
        return int(value)
    return DEFAULT_FILE_VIEW_SETTINGS["file_page_size"]


def clamp_thumbnail_size(value: int | None) -> int:
    if value is None:
        return DEFAULT_FILE_VIEW_SETTINGS["thumbnail_size_px"]
    return max(96, min(int(value), 640))


def ensure_file_browser_settings(session: Session) -> FileBrowserSettings:
    settings = session.get(FileBrowserSettings, 1)
    if settings is None:
        settings = FileBrowserSettings(
            id=1,
            file_view_mode=DEFAULT_FILE_VIEW_SETTINGS["file_view_mode"],
            file_card_size=DEFAULT_FILE_VIEW_SETTINGS["file_card_size"],
            file_page_size=DEFAULT_FILE_VIEW_SETTINGS["file_page_size"],
            thumbnail_size_px=DEFAULT_FILE_VIEW_SETTINGS["thumbnail_size_px"],
        )
        session.add(settings)
        session.commit()
        session.refresh(settings)
    return settings


def serialize_file_browser_settings(settings: FileBrowserSettings) -> dict[str, int | str]:
    return {
        "file_view_mode": settings.file_view_mode if settings.file_view_mode in FILE_VIEW_MODE_OPTIONS else "grid",
        "file_card_size": settings.file_card_size if settings.file_card_size in FILE_CARD_SIZE_OPTIONS else "small",
        "file_page_size": clamp_file_page_size(settings.file_page_size),
        "thumbnail_size_px": clamp_thumbnail_size(settings.thumbnail_size_px),
    }


def update_file_browser_settings(
    session: Session,
    settings: FileBrowserSettings,
    *,
    file_view_mode: str | None = None,
    file_card_size: str | None = None,
    file_page_size: int | None = None,
    thumbnail_size_px: int | None = None,
) -> dict[str, int | str]:
    if file_view_mode in FILE_VIEW_MODE_OPTIONS:
        settings.file_view_mode = file_view_mode
    if file_card_size in FILE_CARD_SIZE_OPTIONS:
        settings.file_card_size = file_card_size
    if file_page_size is not None:
        settings.file_page_size = clamp_file_page_size(file_page_size)
    if thumbnail_size_px is not None:
        settings.thumbnail_size_px = clamp_thumbnail_size(thumbnail_size_px)
    session.add(settings)
    session.commit()
    session.refresh(settings)
    return serialize_file_browser_settings(settings)


def render_page(
    request: Request,
    session: Session,
    template_name: str,
    context: dict,
    title: str = "Telegram Auto Poster",
    status_code: int = 200,
):
    base_context = {
        "title": title,
        "sidebar_channels": get_sidebar_channels(session),
        "csrf_token": ensure_csrf_token(request),
    }
    base_context.update(get_auth_state(session))
    base_context.update(context)
    return templates.TemplateResponse(request, template_name, base_context, status_code=status_code)


def build_auth_form_defaults(*, username: str = DEFAULT_ADMIN_USERNAME) -> dict[str, str]:
    return {
        "username": username,
        "current_password": "",
        "new_password": "",
        "confirm_password": "",
    }


def redirect_back_or_default(request: Request, fallback_url: str) -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or fallback_url, status_code=303)


@router.get("/login")
def login_page(
    request: Request,
    next: str = Query(default="/"),
    session: Session = Depends(get_session),
):
    if request.session.get("admin_authenticated"):
        return RedirectResponse(sanitize_next_path(next), status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "title": "Вход",
            "next_path": sanitize_next_path(next),
            "login_form": {"username": ""},
            "error_message": "",
            "csrf_token": ensure_csrf_token(request),
        },
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_path: str = Form(default="/"),
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    account = ensure_admin_account(session)
    normalized_username = username.strip()
    safe_next = sanitize_next_path(next_path)
    backoff_seconds = get_login_backoff_seconds(session, request)

    if backoff_seconds > 0:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "title": "Вход",
                "next_path": safe_next,
                "login_form": {"username": normalized_username},
                "error_message": f"Слишком много неудачных попыток входа. Повторите через {backoff_seconds} сек.",
                "csrf_token": ensure_csrf_token(request),
            },
            status_code=429,
        )

    if normalized_username != account.username or not verify_password(password, account.password_salt, account.password_hash):
        delay_seconds = register_failed_login(session, request)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "title": "Вход",
                "next_path": safe_next,
                "login_form": {"username": normalized_username},
                "error_message": f"Неверный логин или пароль. Следующая попытка будет доступна через {delay_seconds} сек.",
                "csrf_token": ensure_csrf_token(request),
            },
            status_code=400,
        )

    clear_failed_logins(session, request)
    request.session["admin_authenticated"] = True
    request.session["admin_username"] = account.username
    return RedirectResponse(safe_next, status_code=303)


@router.post("/logout")
async def logout(request: Request, _: None = Depends(csrf_protect)):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/account")
def account_page(
    request: Request,
    updated: int = Query(default=0),
    session: Session = Depends(get_session),
):
    account = ensure_admin_account(session)
    return render_page(
        request,
        session,
        "account.html",
        {
            "account_form": build_auth_form_defaults(username=account.username),
            "success_message": "Данные входа обновлены." if updated else "",
            "error_message": "",
        },
        title="Аккаунт",
    )


@router.post("/account")
async def account_update(
    request: Request,
    username: str = Form(...),
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    account = ensure_admin_account(session)
    normalized_username = username.strip()
    form_state = build_auth_form_defaults(username=normalized_username or account.username)

    error_message = ""
    if not normalized_username:
        error_message = "Нужно указать логин."
    elif not verify_password(current_password, account.password_salt, account.password_hash):
        error_message = "Текущий пароль указан неверно."
    elif len(new_password) < 4:
        error_message = "Новый пароль должен быть не короче 4 символов."
    elif new_password != confirm_password:
        error_message = "Подтверждение пароля не совпадает."

    if error_message:
        return render_page(
            request,
            session,
            "account.html",
            {
                "account_form": form_state,
                "success_message": "",
                "error_message": error_message,
            },
            title="Аккаунт",
            status_code=400,
        )

    password_salt, password_hash = hash_password(new_password)
    account.username = normalized_username
    account.password_salt = password_salt
    account.password_hash = password_hash
    session.add(account)
    session.commit()
    request.session["admin_authenticated"] = True
    request.session["admin_username"] = account.username
    return RedirectResponse("/account?updated=1", status_code=303)


@router.get("/")
def dashboard(request: Request, session: Session = Depends(get_session)):
    return render_page(
        request,
        session,
        "index.html",
        build_dashboard_context(session),
        title="Главный дашборд",
    )


@router.get("/sources")
def sources_page(request: Request, session: Session = Depends(get_session)):
    return render_page(
        request,
        session,
        "sources.html",
        build_sources_page_context(session),
        title="Источники",
    )


@router.get("/content-paths/options")
def content_path_options():
    return JSONResponse({"paths": load_available_content_paths()})


@router.post("/content-paths/refresh")
async def refresh_content_paths(request: Request, _: None = Depends(csrf_protect)):
    refresh_available_content_paths()
    return redirect_back_or_default(request, "/sources")


@router.post("/settings/file-browser")
async def update_file_browser_preferences(
    request: Request,
    file_view_mode: str = Form(default="grid"),
    file_card_size: str = Form(default="small"),
    file_page_size: int = Form(default=50),
    thumbnail_size_px: int = Form(default=256),
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    settings = ensure_file_browser_settings(session)
    update_file_browser_settings(
        session,
        settings,
        file_view_mode=file_view_mode,
        file_card_size=file_card_size,
        file_page_size=file_page_size,
        thumbnail_size_px=thumbnail_size_px,
    )
    return redirect_back_or_default(request, request.url_for("dashboard"))


@router.get("/channels/{channel_id}")
@router.get("/channels/{channel_id}/overview")
def channel_overview(request: Request, channel_id: int, session: Session = Depends(get_session)):
    channel = get_channel_or_404(session, channel_id)
    return render_page(
        request,
        session,
        "channel_overview.html",
        build_channel_overview_context(session, channel),
        title=f"Канал: {channel.name}",
    )


@router.get("/channels/{channel_id}/rules")
def channel_rules(request: Request, channel_id: int, session: Session = Depends(get_session)):
    channel = get_channel_or_404(session, channel_id)
    context = build_channel_rules_context(session, channel)

    return render_page(
        request,
        session,
        "channel_rules.html",
        context,
        title=f"Правила канала: {channel.name}",
    )


@router.get("/channels/{channel_id}/sources")
def channel_sources_page(request: Request, channel_id: int, session: Session = Depends(get_session)):
    channel = get_channel_or_404(session, channel_id)
    return render_page(
        request,
        session,
        "channel_sources.html",
        build_channel_sources_page_context(session, channel),
        title=f"Источники канала: {channel.name}",
    )


@router.get("/channels/{channel_id}/rules/{rule_id}/edit")
def edit_rule_page(request: Request, channel_id: int, rule_id: int, session: Session = Depends(get_session)):
    channel = get_channel_or_404(session, channel_id)
    rule = get_rule_or_404(session, rule_id)
    if rule.channel_id != channel.id:
        raise HTTPException(status_code=404, detail="Правило не принадлежит этому каналу")

    linked_sources, other_sources = build_rule_source_groups(session, channel.id)

    return render_page(
        request,
        session,
        "rule_edit.html",
        {
            "channel": channel,
            "rule": rule,
            "linked_sources": linked_sources,
            "other_sources": other_sources,
            "source_selection_modes": list(SOURCE_SELECTION_MODE_LABELS.items()),
            "selection_modes": list(SELECTION_MODE_LABELS.items()),
        },
        title=f"Редактирование правила: {rule.name}",
    )


@router.get("/channels/{channel_id}/history")
def channel_history(
    request: Request,
    channel_id: int,
    status: str = Query(default="all"),
    rule_id: str | None = Query(default=None),
    page: int = Query(default=1),
    per_page: int = Query(default=50),
    session: Session = Depends(get_session),
):
    channel = get_channel_or_404(session, channel_id)
    current_per_page = clamp_file_page_size(per_page)
    filters = [PostingRule.channel_id == channel.id]
    if status != "all":
        filters.append(PostHistory.status == status)
    selected_rule_id: int | None = None
    if rule_id == "manual":
        filters.append(PostHistory.manual_trigger.is_(True))
    elif rule_id:
        try:
            selected_rule_id = int(rule_id)
        except ValueError:
            selected_rule_id = None
        if selected_rule_id is not None:
            filters.extend([PostHistory.rule_id == selected_rule_id, PostHistory.manual_trigger.is_(False)])

    total_matching = session.scalar(
        select(func.count(PostHistory.id))
        .join(PostingRule, PostHistory.rule_id == PostingRule.id)
        .where(*filters)
    ) or 0
    total_pages = max(1, math.ceil(total_matching / current_per_page)) if total_matching else 1
    current_page = min(max(page, 1), total_pages)
    offset = (current_page - 1) * current_per_page

    query = (
        select(PostHistory)
        .join(PostingRule, PostHistory.rule_id == PostingRule.id)
        .options(selectinload(PostHistory.rule), selectinload(PostHistory.file))
        .where(*filters)
        .order_by(PostHistory.attempted_at.desc())
        .offset(offset)
        .limit(current_per_page)
    )

    items = annotate_history_items(session.scalars(query).all(), thumbnail_size_px=96)
    page_numbers = list(range(max(1, current_page - 2), min(total_pages, current_page + 2) + 1))
    rules = [
        annotate_rule(rule)
        for rule in session.scalars(
            select(PostingRule).where(PostingRule.channel_id == channel.id).order_by(PostingRule.name.asc())
        ).all()
    ]

    return render_page(
        request,
        session,
        "channel_history.html",
        {
            "channel": channel,
            "items": items,
            "status": status,
            "rule_id": rule_id,
            "rules": rules,
            "current_page": current_page,
            "current_per_page": current_per_page,
            "total_pages": total_pages,
            "total_matching": total_matching,
            "page_numbers": page_numbers,
            "page_size_options": sorted(FILE_PAGE_SIZE_OPTIONS),
            "status_options": [
                ("all", "Все"),
                ("sent", "Успешно"),
                ("failed", "Ошибки"),
                ("skipped", "Пропуски"),
            ],
        },
        title=f"История канала: {channel.name}",
    )


@router.get("/files/{file_id}/thumbnail")
def file_thumbnail(file_id: int, session: Session = Depends(get_session)):
    file_record = get_file_record_or_404(session, file_id)
    settings = ensure_file_browser_settings(session)
    thumbnail_size_px = clamp_thumbnail_size(settings.thumbnail_size_px)
    preview_path = get_preview_path(file_record, thumbnail_size_px)
    if not preview_path.exists():
        generated_path = generate_preview(file_record, thumbnail_size_px)
        if generated_path is not None:
            preview_path = generated_path

    if preview_path.exists():
        return FileResponse(preview_path, media_type="image/png")

    placeholder = build_placeholder_svg(file_record.media_kind, thumbnail_size_px)
    return Response(content=placeholder, media_type="image/svg+xml")


@router.get("/files/{file_id}/original")
def file_original(file_id: int, session: Session = Depends(get_session)):
    file_record = get_file_record_or_404(session, file_id)
    file_path = Path(file_record.absolute_path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Оригинал файла недоступен")

    media_type, _ = mimetypes.guess_type(file_path.name)
    return FileResponse(
        path=file_path,
        media_type=media_type or "application/octet-stream",
        filename=file_path.name,
        content_disposition_type="inline",
    )


@router.get("/sources/{source_id}/files")
def source_files(
    request: Request,
    source_id: int,
    status: str = Query(default="all"),
    q: str = Query(default=""),
    kinds: str = Query(default=""),
    sort: str = Query(default="path"),
    page: int = Query(default=1, ge=1),
    per_page: int | None = Query(default=None),
    view: str | None = Query(default=None),
    size: str | None = Query(default=None),
    session: Session = Depends(get_session),
):
    source = get_source_or_404(session, source_id)
    settings = ensure_file_browser_settings(session)
    file_browser_settings = serialize_file_browser_settings(settings)

    requested_mode = view if view in FILE_VIEW_MODE_OPTIONS else None
    requested_size = size if size in FILE_CARD_SIZE_OPTIONS else None
    requested_per_page = clamp_file_page_size(per_page) if per_page is not None else None
    if any(param in request.query_params for param in ("view", "size", "per_page")):
        file_browser_settings = update_file_browser_settings(
            session,
            settings,
            file_view_mode=requested_mode,
            file_card_size=requested_size,
            file_page_size=requested_per_page,
        )

    current_view_mode = requested_mode or str(file_browser_settings["file_view_mode"])
    current_card_size = requested_size or str(file_browser_settings["file_card_size"])
    current_per_page = requested_per_page or int(file_browser_settings["file_page_size"])
    thumbnail_size_px = int(file_browser_settings["thumbnail_size_px"])
    selected_media_kinds = normalize_media_type_selection(kinds.split(",")) or list(MEDIA_TYPE_LABELS)

    filters = [FileRecord.source_id == source.id]
    if status == "active":
        filters.append(FileRecord.is_active.is_(True))
    elif status == "inactive":
        filters.append(FileRecord.is_active.is_(False))
    elif status == "posted":
        filters.append(FileRecord.post_count > 0)
    elif status == "unposted":
        filters.extend([FileRecord.post_count == 0, FileRecord.is_active.is_(True)])

    if q.strip():
        filters.append(FileRecord.relative_path.ilike(f"%{q.strip()}%"))
    if selected_media_kinds:
        filters.append(FileRecord.media_kind.in_(selected_media_kinds))

    ordering = (
        [FileRecord.media_kind.asc(), FileRecord.relative_path.asc()]
        if sort == "media_kind"
        else [FileRecord.relative_path.asc()]
    )

    total_matching = session.scalar(select(func.count(FileRecord.id)).where(*filters)) or 0
    total_pages = max(1, math.ceil(total_matching / current_per_page)) if total_matching else 1
    current_page = min(max(page, 1), total_pages)
    offset = (current_page - 1) * current_per_page

    files = session.scalars(
        select(FileRecord)
        .where(*filters)
        .order_by(*ordering)
        .offset(offset)
        .limit(current_per_page)
    ).all()
    files = [annotate_file_record(file_record, thumbnail_size_px) for file_record in files]

    recent_source_posts = session.scalars(
        select(PostHistory)
        .options(
            selectinload(PostHistory.rule).selectinload(PostingRule.channel),
            selectinload(PostHistory.file),
        )
        .where(PostHistory.source_id == source.id)
        .order_by(PostHistory.attempted_at.desc())
        .limit(20)
    ).all()
    recent_source_posts = annotate_history_items(recent_source_posts, thumbnail_size_px=96)

    stats = {
        "total": session.scalar(select(func.count(FileRecord.id)).where(FileRecord.source_id == source.id)) or 0,
        "active": session.scalar(
            select(func.count(FileRecord.id)).where(FileRecord.source_id == source.id, FileRecord.is_active.is_(True))
        ) or 0,
        "posted": session.scalar(
            select(func.count(FileRecord.id)).where(FileRecord.source_id == source.id, FileRecord.post_count > 0)
        ) or 0,
        "unposted": session.scalar(
            select(func.count(FileRecord.id)).where(
                FileRecord.source_id == source.id,
                FileRecord.is_active.is_(True),
                FileRecord.post_count == 0,
            )
        ) or 0,
    }
    page_numbers = list(range(max(1, current_page - 2), min(total_pages, current_page + 2) + 1))

    return render_page(
        request,
        session,
        "source_files.html",
        {
            "source": source,
            "files": files,
            "recent_source_posts": recent_source_posts,
            "status": status,
            "q": q,
            "kinds": ",".join(selected_media_kinds),
            "sort": sort,
            "stats": stats,
            "current_page": current_page,
            "total_pages": total_pages,
            "total_matching": total_matching,
            "page_numbers": page_numbers,
            "file_browser_settings": file_browser_settings,
            "current_view_mode": current_view_mode,
            "current_card_size": current_card_size,
            "current_per_page": current_per_page,
            "thumbnail_size_px": thumbnail_size_px,
            "selected_media_kinds": selected_media_kinds,
            "file_view_mode_options": [("list", "Список"), ("grid", "Сетка")],
            "file_card_size_options": [("small", "Маленький"), ("large", "Большой")],
            "file_page_size_options": sorted(FILE_PAGE_SIZE_OPTIONS),
            "file_media_type_options": list(MEDIA_TYPE_LABELS.items()),
            "file_sort_options": [("path", "По пути"), ("media_kind", "По типу контента")],
            "status_options": [
                ("all", "Все"),
                ("active", "Активные"),
                ("inactive", "Отключенные"),
                ("unposted", "Еще не отправлялись"),
                ("posted", "Уже отправлялись"),
            ],
        },
        title=f"Файлы источника: {source.name}",
    )


@router.get("/rules/{rule_id}/queue")
def rule_queue_preview(request: Request, rule_id: int, session: Session = Depends(get_session)):
    rule = get_rule_or_404(session, rule_id)
    get_rule_sources(session, rule)
    source_ids = get_rule_source_ids(session, rule)

    candidates, note = preview_candidates_for_rule(session, rule)
    thumbnail_size_px = int(serialize_file_browser_settings(ensure_file_browser_settings(session))["thumbnail_size_px"])
    candidates = [annotate_file_record(file_record, thumbnail_size_px) for file_record in candidates]
    total_active = (
        session.scalar(
            select(func.count(FileRecord.id)).where(
                FileRecord.source_id.in_(source_ids),
                FileRecord.is_active.is_(True),
            )
        )
        if source_ids
        else 0
    ) or 0
    unique_remaining = (
        session.scalar(
            select(func.count(FileRecord.id)).where(
                FileRecord.source_id.in_(source_ids),
                FileRecord.is_active.is_(True),
                ~already_sent_exists(rule.id),
            )
        )
        if source_ids
        else 0
    ) or 0
    recent_rule_posts = session.scalars(
        select(PostHistory)
        .options(
            selectinload(PostHistory.rule).selectinload(PostingRule.channel),
            selectinload(PostHistory.file),
        )
        .where(PostHistory.rule_id == rule.id)
        .order_by(PostHistory.attempted_at.desc())
        .limit(20)
    ).all()
    recent_rule_posts = annotate_history_items(recent_rule_posts, thumbnail_size_px=96)

    return render_page(
        request,
        session,
        "queue_preview.html",
        {
            "rule": rule,
            "candidates": candidates,
            "note": note,
            "thumbnail_size_px": thumbnail_size_px,
            "stats": {
                "active_files": total_active,
                "unique_remaining": unique_remaining,
                "history_count": len(recent_rule_posts),
            },
            "recent_rule_posts": recent_rule_posts,
        },
        title=f"Очередь правила: {rule.name}",
    )


@router.get("/history")
def history_page(
    request: Request,
    status: str = Query(default="all"),
    source_id: int | None = Query(default=None),
    rule_id: str | None = Query(default=None),
    channel_id: int | None = Query(default=None),
    page: int = Query(default=1),
    per_page: int = Query(default=50),
    session: Session = Depends(get_session),
):
    current_per_page = clamp_file_page_size(per_page)
    filters = []
    if status != "all":
        filters.append(PostHistory.status == status)
    if source_id:
        filters.append(PostHistory.source_id == source_id)
    if rule_id == "manual":
        filters.append(PostHistory.manual_trigger.is_(True))
    elif rule_id:
        try:
            selected_rule_id = int(rule_id)
        except ValueError:
            selected_rule_id = None
        if selected_rule_id is not None:
            filters.extend([PostHistory.rule_id == selected_rule_id, PostHistory.manual_trigger.is_(False)])
    if channel_id:
        filters.append(PostingRule.channel_id == channel_id)

    total_matching = session.scalar(
        select(func.count(PostHistory.id))
        .join(PostingRule, PostHistory.rule_id == PostingRule.id)
        .where(*filters)
    ) or 0
    total_pages = max(1, math.ceil(total_matching / current_per_page)) if total_matching else 1
    current_page = min(max(page, 1), total_pages)
    offset = (current_page - 1) * current_per_page

    query = (
        select(PostHistory)
        .join(PostingRule, PostHistory.rule_id == PostingRule.id)
        .options(
            selectinload(PostHistory.rule).selectinload(PostingRule.channel),
            selectinload(PostHistory.file),
        )
        .where(*filters)
        .order_by(PostHistory.attempted_at.desc())
        .offset(offset)
        .limit(current_per_page)
    )

    items = annotate_history_items(session.scalars(query).all(), thumbnail_size_px=96)
    page_numbers = list(range(max(1, current_page - 2), min(total_pages, current_page + 2) + 1))
    sources = [
        annotate_source(source)
        for source in session.scalars(select(ContentSource).order_by(ContentSource.name.asc())).all()
    ]
    rules = [
        annotate_rule(rule)
        for rule in session.scalars(select(PostingRule).order_by(PostingRule.name.asc())).all()
    ]
    channels = get_sidebar_channels(session)

    return render_page(
        request,
        session,
        "history.html",
        {
            "items": items,
            "status": status,
            "source_id": source_id,
            "rule_id": rule_id,
            "channel_id": channel_id,
            "sources": sources,
            "rules": rules,
            "channels": channels,
            "current_page": current_page,
            "current_per_page": current_per_page,
            "total_pages": total_pages,
            "total_matching": total_matching,
            "page_numbers": page_numbers,
            "page_size_options": sorted(FILE_PAGE_SIZE_OPTIONS),
            "status_options": [
                ("all", "Все"),
                ("sent", "Успешно"),
                ("failed", "Ошибки"),
                ("skipped", "Пропуски"),
            ],
        },
        title="Глобальная история",
    )


@router.post("/channels")
async def create_channel(
    request: Request,
    name: str = Form(...),
    bot_token: str = Form(default=""),
    chat_id: str = Form(default=""),
    parse_mode: str = Form(default="HTML"),
    default_caption: str = Form(default=""),
    disable_notification: str | None = Form(default=None),
    protect_content: str | None = Form(default=None),
    enabled: str | None = Form(default=None),
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    normalized_name = name.strip()
    if not normalized_name:
        context = build_dashboard_context(session)
        context["error_message"] = "Нужно указать название канала."
        context["open_modal_id"] = "create-channel-modal"
        context["channel_form"] = {
            "name": name,
            "chat_id": chat_id,
            "bot_token": bot_token,
            "parse_mode": parse_mode,
            "default_caption": default_caption,
            "disable_notification": to_bool(disable_notification),
            "protect_content": to_bool(protect_content),
            "enabled": to_bool(enabled) or enabled is None,
        }
        return render_page(request, session, "index.html", context, title="Главный дашборд", status_code=400)

    existing_channel = session.scalar(select(TelegramChannel).where(TelegramChannel.name == normalized_name))
    if existing_channel is not None:
        context = build_dashboard_context(session)
        context["error_message"] = f"Канал с именем '{normalized_name}' уже существует."
        context["open_modal_id"] = "create-channel-modal"
        context["channel_form"] = {
            "name": normalized_name,
            "chat_id": chat_id,
            "bot_token": bot_token,
            "parse_mode": parse_mode,
            "default_caption": default_caption,
            "disable_notification": to_bool(disable_notification),
            "protect_content": to_bool(protect_content),
            "enabled": to_bool(enabled) or enabled is None,
        }
        return render_page(request, session, "index.html", context, title="Главный дашборд", status_code=400)

    channel = TelegramChannel(
        name=normalized_name,
        bot_token=bot_token.strip() or None,
        chat_id=chat_id.strip() or None,
        parse_mode=parse_mode.strip() or None,
        default_caption=default_caption.strip() or None,
        disable_notification=to_bool(disable_notification),
        protect_content=to_bool(protect_content),
        enabled=to_bool(enabled) or enabled is None,
    )
    session.add(channel)
    session.commit()
    return RedirectResponse(request.url_for("channel_overview", channel_id=channel.id), status_code=303)


@router.post("/channels/{channel_id}/settings")
async def update_channel_settings(
    request: Request,
    channel_id: int,
    name: str = Form(...),
    bot_token: str = Form(default=""),
    chat_id: str = Form(default=""),
    parse_mode: str = Form(default="HTML"),
    default_caption: str = Form(default=""),
    disable_notification: str | None = Form(default=None),
    protect_content: str | None = Form(default=None),
    enabled: str | None = Form(default=None),
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    channel = session.get(TelegramChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Канал не найден")

    normalized_name = name.strip()
    if not normalized_name:
        return render_page(
            request,
            session,
            "channel_overview.html",
            build_channel_overview_context(session, channel, error_message="Нужно указать название канала."),
            title=f"Канал: {channel.name}",
            status_code=400,
        )

    existing_channel = session.scalar(
        select(TelegramChannel).where(TelegramChannel.name == normalized_name, TelegramChannel.id != channel.id)
    )
    if existing_channel is not None:
        return render_page(
            request,
            session,
            "channel_overview.html",
            build_channel_overview_context(
                session,
                channel,
                error_message=f"Канал с именем '{normalized_name}' уже существует.",
            ),
            title=f"Канал: {channel.name}",
            status_code=400,
        )

    channel.name = normalized_name
    channel.bot_token = bot_token.strip() or None
    channel.chat_id = chat_id.strip() or None
    channel.parse_mode = parse_mode.strip() or None
    channel.default_caption = default_caption.strip() or None
    channel.disable_notification = to_bool(disable_notification)
    channel.protect_content = to_bool(protect_content)
    channel.enabled = to_bool(enabled)
    session.commit()
    return RedirectResponse(request.url_for("channel_overview", channel_id=channel.id), status_code=303)


@router.post("/channels/{channel_id}/toggle")
async def toggle_channel(
    request: Request,
    channel_id: int,
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    channel = session.get(TelegramChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Канал не найден")
    channel.enabled = not channel.enabled
    session.commit()
    return redirect_back_or_default(request, request.url_for("dashboard"))


@router.post("/channels/{channel_id}/delete")
async def delete_channel(
    request: Request,
    channel_id: int,
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    channel = session.get(TelegramChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Канал не найден")
    session.delete(channel)
    session.commit()
    return RedirectResponse(request.url_for("dashboard"), status_code=303)


@router.post("/sources")
async def create_source(
    request: Request,
    name: str = Form(...),
    path: str = Form(...),
    recursive: str | None = Form(default=None),
    enabled: str | None = Form(default=None),
    file_types: list[str] = Form(default=[]),
    scan_interval_minutes: int = Form(default=10),
    manual_scan_only: str | None = Form(default=None),
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    source_name, source_path, normalized_types, validation_error = validate_source_payload(
        session,
        name=name,
        path=path,
        file_types=file_types,
    )
    if validation_error:
        context = build_sources_page_context(session)
        context["error_message"] = validation_error
        context["open_modal_id"] = "create-source-modal"
        context["source_form"] = build_source_form_state(
            name=name,
            path=path,
            recursive=recursive,
            enabled=enabled,
            file_types=file_types,
            scan_interval_minutes=scan_interval_minutes,
            manual_scan_only=manual_scan_only,
        )
        return render_page(request, session, "sources.html", context, title="Источники", status_code=400)

    source = ContentSource(
        name=source_name,
        path=source_path,
        recursive=to_bool(recursive),
        enabled=to_bool(enabled),
        allowed_extensions=serialize_media_type_selection(normalized_types),
        scan_interval_minutes=max(scan_interval_minutes, 1),
        manual_scan_only=to_bool(manual_scan_only),
    )
    session.add(source)
    session.commit()
    return RedirectResponse(request.url_for("sources_page"), status_code=303)


@router.post("/sources/{source_id}/edit")
async def update_source(
    request: Request,
    source_id: int,
    name: str = Form(...),
    path: str = Form(...),
    recursive: str | None = Form(default=None),
    enabled: str | None = Form(default=None),
    file_types: list[str] = Form(default=[]),
    scan_interval_minutes: int = Form(default=10),
    manual_scan_only: str | None = Form(default=None),
    return_to: str = Form(default="/sources"),
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    source = get_source_or_404(session, source_id)
    source_name, source_path, normalized_types, validation_error = validate_source_payload(
        session,
        name=name,
        path=path,
        file_types=file_types,
        exclude_source_id=source.id,
    )
    form_state = build_source_form_state(
        name=name,
        path=path,
        recursive=recursive,
        enabled=enabled,
        file_types=file_types,
        scan_interval_minutes=scan_interval_minutes,
        manual_scan_only=manual_scan_only,
    )
    if validation_error:
        if return_to.startswith("/channels/") and return_to.endswith("/sources"):
            try:
                channel_id = int(return_to.strip("/").split("/")[1])
            except (IndexError, ValueError):
                channel_id = 0

            if channel_id:
                channel = get_channel_or_404(session, channel_id)
                context = build_channel_sources_page_context(
                    session,
                    channel,
                    edit_source_id=source.id,
                    edit_source_form=form_state,
                )
                context["error_message"] = validation_error
                context["open_modal_id"] = f"edit-source-modal-{source.id}"
                return render_page(
                    request,
                    session,
                    "channel_sources.html",
                    context,
                    title=f"Источники канала: {channel.name}",
                    status_code=400,
                )

        context = build_sources_page_context(
            session,
            edit_source_id=source.id,
            edit_source_form=form_state,
        )
        context["error_message"] = validation_error
        context["open_modal_id"] = f"edit-source-modal-{source.id}"
        return render_page(
            request,
            session,
            "sources.html",
            context,
            title="Источники",
            status_code=400,
        )

    source.name = source_name
    source.path = source_path
    source.recursive = to_bool(recursive)
    source.enabled = to_bool(enabled)
    source.allowed_extensions = serialize_media_type_selection(normalized_types)
    source.scan_interval_minutes = max(scan_interval_minutes, 1)
    source.manual_scan_only = to_bool(manual_scan_only)
    session.add(source)
    session.commit()
    return RedirectResponse(return_to or request.url_for("sources_page"), status_code=303)


@router.post("/sources/{source_id}/toggle")
async def toggle_source(
    request: Request,
    source_id: int,
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    source = session.get(ContentSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Источник не найден")
    source.enabled = not source.enabled
    session.commit()
    return redirect_back_or_default(request, request.url_for("dashboard"))


@router.get("/sources/{source_id}/scan-status")
def source_scan_status(source_id: int, session: Session = Depends(get_session)):
    source = session.get(ContentSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Источник не найден")
    return JSONResponse(
        {
            "id": source.id,
            "scan_in_progress": source.scan_in_progress,
            "scan_mode": source.scan_mode,
            "scan_mode_label": {
                SCAN_MODE_FULL: "Полный рескан",
                SCAN_MODE_ADD_MISSING: "Добавление отсутствующих",
            }.get(source.scan_mode, "Сканирование"),
            "scan_progress_current": source.scan_progress_current,
            "scan_progress_total": source.scan_progress_total,
            "scan_progress_percent": source.scan_progress_percent,
            "last_scan_result": source.last_scan_result or "",
        }
    )


@router.post("/sources/{source_id}/scan-full")
async def run_source_full_scan(
    request: Request,
    source_id: int,
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    source = session.get(ContentSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Источник не найден")
    if not source.scan_in_progress:
        source.scan_in_progress = True
        source.scan_mode = SCAN_MODE_FULL
        source.scan_progress_current = 0
        source.scan_progress_total = 0
        source.scan_progress_percent = 0
        source.last_scan_result = "Полный рескан поставлен в очередь..."
        session.commit()
        start_source_scan_job(source_id, SCAN_MODE_FULL)
    return redirect_back_or_default(request, request.url_for("dashboard"))


@router.post("/sources/{source_id}/scan-add")
async def run_source_add_missing_scan(
    request: Request,
    source_id: int,
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    source = session.get(ContentSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Источник не найден")
    if not source.scan_in_progress:
        source.scan_in_progress = True
        source.scan_mode = SCAN_MODE_ADD_MISSING
        source.scan_progress_current = 0
        source.scan_progress_total = 0
        source.scan_progress_percent = 0
        source.last_scan_result = "Добавление отсутствующих файлов поставлено в очередь..."
        session.commit()
        start_source_scan_job(source_id, SCAN_MODE_ADD_MISSING)
    return redirect_back_or_default(request, request.url_for("dashboard"))


@router.post("/sources/{source_id}/delete")
async def delete_source(
    request: Request,
    source_id: int,
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    source = session.get(ContentSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Источник не найден")
    session.delete(source)
    session.commit()
    return RedirectResponse(request.url_for("sources_page"), status_code=303)


@router.post("/channels/{channel_id}/sources/attach")
async def attach_channel_source(
    request: Request,
    channel_id: int,
    source_id: int = Form(...),
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    channel = get_channel_or_404(session, channel_id)
    source = session.get(ContentSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Источник не найден")

    ensure_channel_source_link(session, channel.id, source.id)
    session.commit()
    return RedirectResponse(request.url_for("channel_sources_page", channel_id=channel.id), status_code=303)


@router.post("/channels/{channel_id}/sources/create")
async def create_channel_source(
    request: Request,
    channel_id: int,
    name: str = Form(...),
    path: str = Form(...),
    recursive: str | None = Form(default=None),
    enabled: str | None = Form(default=None),
    file_types: list[str] = Form(default=[]),
    scan_interval_minutes: int = Form(default=10),
    manual_scan_only: str | None = Form(default=None),
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    channel = get_channel_or_404(session, channel_id)
    source_name, source_path, normalized_types, validation_error = validate_source_payload(
        session,
        name=name,
        path=path,
        file_types=file_types,
    )
    if validation_error:
        context = build_channel_sources_page_context(session, channel)
        context["error_message"] = validation_error
        context["open_modal_id"] = "channel-source-modal"
        context["source_form"] = build_source_form_state(
            name=name,
            path=path,
            recursive=recursive,
            enabled=enabled,
            file_types=file_types,
            scan_interval_minutes=scan_interval_minutes,
            manual_scan_only=manual_scan_only,
        )
        return render_page(
            request,
            session,
            "channel_sources.html",
            context,
            title=f"Источники канала: {channel.name}",
            status_code=400,
        )

    source = ContentSource(
        name=source_name,
        path=source_path,
        recursive=to_bool(recursive),
        enabled=to_bool(enabled),
        allowed_extensions=serialize_media_type_selection(normalized_types),
        scan_interval_minutes=max(scan_interval_minutes, 1),
        manual_scan_only=to_bool(manual_scan_only),
    )
    session.add(source)
    session.flush()
    ensure_channel_source_link(session, channel.id, source.id)
    session.commit()
    return RedirectResponse(request.url_for("channel_sources_page", channel_id=channel.id), status_code=303)


@router.post("/channels/{channel_id}/sources/{source_id}/detach")
async def detach_channel_source(
    request: Request,
    channel_id: int,
    source_id: int,
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    channel = get_channel_or_404(session, channel_id)
    link = session.scalar(
        select(ChannelSource).where(ChannelSource.channel_id == channel_id, ChannelSource.source_id == source_id)
    )
    if link is None:
        raise HTTPException(status_code=404, detail="Привязка источника не найдена")

    blocking_rule = session.scalar(
        select(PostingRule)
        .join(RuleSource, RuleSource.rule_id == PostingRule.id)
        .where(PostingRule.channel_id == channel_id, RuleSource.source_id == source_id)
    )
    if blocking_rule is not None:
        context = build_channel_sources_page_context(session, channel)
        context["error_message"] = f"Источник нельзя отвязать: его использует правило '{blocking_rule.name}'."
        return render_page(
            request,
            session,
            "channel_sources.html",
            context,
            title=f"Источники канала: {channel.name}",
            status_code=400,
        )

    session.delete(link)
    session.commit()
    return RedirectResponse(request.url_for("channel_sources_page", channel_id=channel.id), status_code=303)


@router.post("/rules")
async def create_rule(
    request: Request,
    channel_id: int = Form(...),
    name: str = Form(...),
    source_ids: list[int] = Form(default=[]),
    source_selection_mode: str = Form(default="merged_pool"),
    interval_minutes: int = Form(default=60),
    allowed_from_hour: str = Form(default=""),
    allowed_to_hour: str = Form(default=""),
    jitter_minutes: int = Form(default=0),
    burst_post_count: int = Form(default=1),
    burst_interval_minutes: int = Form(default=2),
    selection_mode: str = Form(default="random_no_repeat"),
    repeat_after_exhaustion: str | None = Form(default=None),
    caption_template: str = Form(default=""),
    chat_id_override: str = Form(default=""),
    send_as_document: str | None = Form(default=None),
    convert_heic_to_jpeg: str | None = Form(default=None),
    enabled: str | None = Form(default=None),
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    normalized_source_ids = sorted({int(source_id) for source_id in source_ids})

    def build_form_state() -> dict[str, object]:
        return {
            "channel_id": channel_id,
            "name": name,
            "source_id": normalized_source_ids[0] if normalized_source_ids else None,
            "source_ids": normalized_source_ids,
            "source_selection_mode": source_selection_mode,
            "interval_minutes": interval_minutes,
            "jitter_minutes": jitter_minutes,
            "burst_post_count": burst_post_count,
            "burst_interval_minutes": burst_interval_minutes,
            "allowed_from_hour": allowed_from_hour,
            "allowed_to_hour": allowed_to_hour,
            "selection_mode": selection_mode,
            "chat_id_override": chat_id_override,
            "caption_template": caption_template,
            "repeat_after_exhaustion": to_bool(repeat_after_exhaustion),
            "send_as_document": to_bool(send_as_document),
            "convert_heic_to_jpeg": to_bool(convert_heic_to_jpeg),
            "enabled": to_bool(enabled),
        }

    try:
        from_hour = parse_optional_hour(allowed_from_hour)
        to_hour = parse_optional_hour(allowed_to_hour)
    except ValueError as exc:
        channel = get_channel_or_404(session, channel_id)
        context = build_channel_rules_context(session, channel)
        context["error_message"] = str(exc)
        context["open_modal_id"] = "create-rule-modal"
        context["rule_form"] = build_form_state()
        return render_page(request, session, "channel_rules.html", context, title=f"Правила канала: {channel.name}", status_code=400)

    channel = session.get(TelegramChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Канал не найден")
    if not normalized_source_ids:
        context = build_channel_rules_context(session, channel)
        context["error_message"] = "Нужно выбрать хотя бы один источник."
        context["open_modal_id"] = "create-rule-modal"
        context["rule_form"] = build_form_state()
        return render_page(request, session, "channel_rules.html", context, title=f"Правила канала: {channel.name}", status_code=400)

    existing_sources = session.scalars(select(ContentSource).where(ContentSource.id.in_(normalized_source_ids))).all()
    if len(existing_sources) != len(normalized_source_ids):
        raise HTTPException(status_code=404, detail="Один или несколько источников не найдены")

    duplicate_rule = session.scalar(select(PostingRule).where(PostingRule.name == name.strip()))
    if duplicate_rule is not None:
        context = build_channel_rules_context(session, channel)
        context["error_message"] = f"Правило с именем '{name.strip()}' уже существует."
        context["open_modal_id"] = "create-rule-modal"
        context["rule_form"] = build_form_state()
        return render_page(request, session, "channel_rules.html", context, title=f"Правила канала: {channel.name}", status_code=400)

    rule = PostingRule(
        name=name.strip(),
        channel_id=channel_id,
        source_id=normalized_source_ids[0],
        source_selection_mode=source_selection_mode,
        interval_minutes=max(interval_minutes, 1),
        allowed_from_hour=from_hour,
        allowed_to_hour=to_hour,
        jitter_minutes=max(jitter_minutes, 0),
        burst_post_count=max(burst_post_count, 1),
        burst_interval_minutes=max(burst_interval_minutes, 1),
        selection_mode=selection_mode,
        repeat_after_exhaustion=to_bool(repeat_after_exhaustion),
        caption_template=caption_template.strip() or None,
        chat_id_override=chat_id_override.strip() or None,
        send_as_document=to_bool(send_as_document),
        convert_heic_to_jpeg=to_bool(convert_heic_to_jpeg),
        enabled=to_bool(enabled),
    )
    session.add(rule)
    session.flush()
    for linked_source_id in normalized_source_ids:
        ensure_channel_source_link(session, channel_id, linked_source_id)
    ensure_rule_source_links(session, rule, normalized_source_ids)
    session.commit()
    return RedirectResponse(request.url_for("channel_rules", channel_id=channel_id), status_code=303)


@router.post("/channels/{channel_id}/rules/import")
async def import_rule_to_channel(
    request: Request,
    channel_id: int,
    template_rule_id: int = Form(...),
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    channel = get_channel_or_404(session, channel_id)
    template_rule = session.scalar(
        select(PostingRule)
        .options(
            selectinload(PostingRule.channel),
            selectinload(PostingRule.source),
            selectinload(PostingRule.source_links).selectinload(RuleSource.source),
        )
        .where(PostingRule.id == template_rule_id)
    )
    if template_rule is None or template_rule.channel_id == channel.id:
        context = build_channel_rules_context(session, channel)
        context["error_message"] = "Не удалось выбрать правило из другого канала."
        context["open_modal_id"] = "import-rule-modal"
        return render_page(request, session, "channel_rules.html", context, title=f"Правила канала: {channel.name}", status_code=400)

    copied_rule = PostingRule(
        name=make_unique_rule_name(session, template_rule.name),
        enabled=template_rule.enabled,
        channel_id=channel.id,
        source_id=template_rule.source_id,
        source_selection_mode=getattr(template_rule, "source_selection_mode", "merged_pool"),
        interval_minutes=max(template_rule.interval_minutes, 1),
        allowed_from_hour=template_rule.allowed_from_hour,
        allowed_to_hour=template_rule.allowed_to_hour,
        jitter_minutes=max(template_rule.jitter_minutes, 0),
        burst_post_count=max(template_rule.burst_post_count, 1),
        burst_interval_minutes=max(template_rule.burst_interval_minutes, 1),
        pending_burst_posts=0,
        selection_mode=template_rule.selection_mode,
        repeat_after_exhaustion=template_rule.repeat_after_exhaustion,
        caption_template=template_rule.caption_template,
        chat_id_override=template_rule.chat_id_override,
        send_as_document=template_rule.send_as_document,
        convert_heic_to_jpeg=template_rule.convert_heic_to_jpeg,
        next_run_at=None,
        last_run_at=None,
        last_result=None,
    )
    session.add(copied_rule)
    session.flush()
    imported_source_ids = get_rule_source_ids(session, template_rule)
    if not imported_source_ids and template_rule.source_id is not None:
        imported_source_ids = [template_rule.source_id]
    for linked_source_id in imported_source_ids:
        ensure_channel_source_link(session, channel.id, linked_source_id)
    ensure_rule_source_links(session, copied_rule, imported_source_ids)
    session.commit()
    return RedirectResponse(request.url_for("channel_rules", channel_id=channel.id), status_code=303)


@router.post("/channels/{channel_id}/rules/{rule_id}/edit")
async def update_rule(
    request: Request,
    channel_id: int,
    rule_id: int,
    name: str = Form(...),
    source_ids: list[int] = Form(default=[]),
    source_selection_mode: str = Form(default="merged_pool"),
    interval_minutes: int = Form(default=60),
    allowed_from_hour: str = Form(default=""),
    allowed_to_hour: str = Form(default=""),
    jitter_minutes: int = Form(default=0),
    burst_post_count: int = Form(default=1),
    burst_interval_minutes: int = Form(default=2),
    selection_mode: str = Form(default="random_no_repeat"),
    repeat_after_exhaustion: str | None = Form(default=None),
    caption_template: str = Form(default=""),
    chat_id_override: str = Form(default=""),
    send_as_document: str | None = Form(default=None),
    convert_heic_to_jpeg: str | None = Form(default=None),
    enabled: str | None = Form(default=None),
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    rule = session.get(PostingRule, rule_id)
    if rule is None or rule.channel_id != channel_id:
        raise HTTPException(status_code=404, detail="Правило не найдено")

    try:
        from_hour = parse_optional_hour(allowed_from_hour)
        to_hour = parse_optional_hour(allowed_to_hour)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    channel = session.get(TelegramChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Канал не найден")

    normalized_source_ids = sorted({int(source_id) for source_id in source_ids})
    if not normalized_source_ids:
        raise HTTPException(status_code=400, detail="Нужно выбрать хотя бы один источник")
    existing_sources = session.scalars(select(ContentSource).where(ContentSource.id.in_(normalized_source_ids))).all()
    if len(existing_sources) != len(normalized_source_ids):
        raise HTTPException(status_code=404, detail="Один или несколько источников не найдены")

    duplicate_rule = session.scalar(
        select(PostingRule).where(PostingRule.name == name.strip(), PostingRule.id != rule.id)
    )
    if duplicate_rule is not None:
        raise HTTPException(status_code=400, detail="Правило с таким именем уже существует")

    rule.name = name.strip()
    rule.source_id = normalized_source_ids[0]
    rule.source_selection_mode = source_selection_mode
    rule.interval_minutes = max(interval_minutes, 1)
    rule.allowed_from_hour = from_hour
    rule.allowed_to_hour = to_hour
    rule.jitter_minutes = max(jitter_minutes, 0)
    rule.burst_post_count = max(burst_post_count, 1)
    rule.burst_interval_minutes = max(burst_interval_minutes, 1)
    rule.pending_burst_posts = 0
    rule.selection_mode = selection_mode
    rule.repeat_after_exhaustion = to_bool(repeat_after_exhaustion)
    rule.caption_template = caption_template.strip() or None
    rule.chat_id_override = chat_id_override.strip() or None
    rule.send_as_document = to_bool(send_as_document)
    rule.convert_heic_to_jpeg = to_bool(convert_heic_to_jpeg)
    rule.enabled = to_bool(enabled)
    rule.next_run_at = compute_next_run(rule)
    for linked_source_id in normalized_source_ids:
        ensure_channel_source_link(session, channel_id, linked_source_id)
    ensure_rule_source_links(session, rule, normalized_source_ids)
    session.commit()
    return RedirectResponse(request.url_for("channel_rules", channel_id=channel_id), status_code=303)


@router.post("/rules/{rule_id}/toggle")
async def toggle_rule(
    request: Request,
    rule_id: int,
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    rule = session.get(PostingRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Правило не найдено")
    channel_id = rule.channel_id
    rule.enabled = not rule.enabled
    session.commit()
    return redirect_back_or_default(request, request.url_for("channel_rules", channel_id=channel_id))


@router.post("/rules/{rule_id}/run")
async def run_rule_now(
    request: Request,
    rule_id: int,
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    rule = session.get(PostingRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Правило не найдено")
    channel_id = rule.channel_id
    scheduler = request.app.state.app_scheduler
    await scheduler.process_rule(rule_id, manual=True)
    return redirect_back_or_default(request, request.url_for("channel_rules", channel_id=channel_id))


@router.post("/rules/{rule_id}/files/{file_id}/post-now")
async def post_queue_file_now(
    request: Request,
    rule_id: int,
    file_id: int,
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    rule = session.get(PostingRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Правило не найдено")

    channel = session.get(TelegramChannel, rule.channel_id)
    file_record = session.get(FileRecord, file_id)
    allowed_source_ids = set(get_rule_source_ids(session, rule))
    if channel is None:
        raise HTTPException(status_code=404, detail="Канал не найден")
    if file_record is None or file_record.source_id not in allowed_source_ids:
        raise HTTPException(status_code=404, detail="Файл не найден в источниках правила")

    from app.services.telegram import TelegramPublishError, publish_file

    now = datetime.now()
    try:
        message_id = await publish_file(channel, rule, file_record)
    except TelegramPublishError as exc:
        history = PostHistory(
            rule_id=rule.id,
            source_id=file_record.source_id,
            file_id=file_record.id,
            status="failed",
            manual_trigger=True,
            message=str(exc),
        )
        session.add(history)
        session.commit()
        return redirect_back_or_default(request, request.url_for("rule_queue_preview", rule_id=rule.id))

    file_record.last_posted_at = now
    file_record.post_count += 1
    history = PostHistory(
        rule_id=rule.id,
        source_id=file_record.source_id,
        file_id=file_record.id,
        status="sent",
        manual_trigger=True,
        message=f"Файл {file_record.relative_path} опубликован вручную",
        telegram_message_id=message_id,
    )
    session.add(history)
    session.commit()
    return redirect_back_or_default(request, request.url_for("rule_queue_preview", rule_id=rule.id))


@router.post("/rules/{rule_id}/delete")
async def delete_rule(
    request: Request,
    rule_id: int,
    _: None = Depends(csrf_protect),
    session: Session = Depends(get_session),
):
    rule = session.get(PostingRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Правило не найдено")
    channel_id = rule.channel_id
    session.delete(rule)
    session.commit()
    return RedirectResponse(request.url_for("channel_rules", channel_id=channel_id), status_code=303)

