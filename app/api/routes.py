from collections import defaultdict
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import case, func, select, text
from starlette.concurrency import run_in_threadpool

from app.api.jobs import (
    FORMATS,
    GROUPS,
    STATUS,
    book_details,
    change_job,
    create_job,
    delete_job,
    format_field,
    get_job,
    public_job,
    upload_field,
)
from app.core.config import Settings
from app.core.security import csrf_token, protect
from app.models.database import Job
from app.services.importing import destinations
from app.services.intake import cover_bytes, inspect_upload
from app.services.metadata import PROVIDERS
from app.services.qbittorrent import TorrentError
from app.workers.worker import ACTIVE

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = Jinja2Templates(directory=ROOT / "templates")
router = APIRouter()
SETTING_GROUPS = (
    (
        "qBittorrent",
        (
            ("qbit_url", "Web UI URL", "url"),
            ("qbit_username", "Username", "text"),
            ("qbit_password", "Password", "password"),
            ("qbit_category", "Torrent category", "text"),
            ("remote_download_root", "qBittorrent download path", "text"),
            ("download_root", "Mapped download path", "text"),
        ),
    ),
    (
        "Library & intake",
        (
            ("audiobook_library", "Audiobook library", "text"),
            ("audiobook_staging", "Audiobook staging directory", "text"),
            ("ebook_library", "Ebook library", "text"),
            ("ebook_staging", "Ebook staging directory", "text"),
            ("intake_root", "EPUB intake directory", "text"),
            ("folder_template", "Folder template", "text"),
        ),
    ),
    (
        "Import behavior",
        (
            ("owner_uid", "File owner UID", "number"),
            ("owner_gid", "File group GID", "number"),
            ("file_mode", "File permissions", "text"),
            ("directory_mode", "Directory permissions", "text"),
            ("poll_seconds", "Polling interval in seconds", "number"),
            ("metadata_provider", "Metadata provider", "provider"),
            ("dry_run", "Dry-run mode", "checkbox"),
        ),
    ),
)


def view(request, template, status_code=200, **context):
    return TEMPLATES.TemplateResponse(
        request,
        template,
        {
            "csrf": csrf_token(request),
            "formats": FORMATS,
            "statuses": STATUS,
            "group_labels": GROUPS,
            **context,
        },
        status_code=status_code,
    )


def settings_view(runtime):
    with runtime.db() as session:
        return runtime.settings.public(session)


def dashboard_view(runtime, batch=1):
    groups = defaultdict(list)
    limit = 30
    with runtime.db() as session:
        rows = session.execute(select(Job.status, func.count()).group_by(Job.status)).all()
        priority = case((Job.status.in_((*ACTIVE, "review")), 0), else_=1)
        jobs = session.scalars(
            select(Job)
            .order_by(priority, Job.created_at.desc(), Job.id)
            .limit(limit)
            .offset((batch - 1) * limit)
        ).all()
    counts = dict.fromkeys(GROUPS, 0)

    def group(status):
        return status if status in GROUPS else "active"

    for status, count in rows:
        counts[group(status)] += count
    for job in jobs:
        groups[group(job.status)].append(public_job(job))
    total = sum(counts.values())
    return {
        "groups": groups,
        "counts": counts,
        "total": total,
        "batch": batch,
        "has_next": batch * limit < total,
    }


def issue(exc) -> str:
    if isinstance(exc, ValidationError):
        return " ".join(
            f"{'.'.join(map(str, item['loc'])) or 'Settings'}: {item['msg']}"
            for item in exc.errors(include_url=False, include_context=False, include_input=False)
        )
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return str(exc)


def form(request):
    return request.form(max_files=1, max_fields=32, max_part_size=16 * 1024)


async def json_object(request):
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "Send a valid JSON object.") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "Send a valid JSON object.")
    return body


@router.get("/healthz", include_in_schema=False)
def health(request: Request):
    try:
        with request.app.state.runtime.db() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ok"}


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request, batch: Annotated[int, Query(ge=1, le=1_000_000)] = 1):
    runtime = request.app.state.runtime
    return view(request, "dashboard.html", page="dashboard", **dashboard_view(runtime, batch))


@router.get("/dashboard/content", response_class=HTMLResponse, include_in_schema=False)
def dashboard_content(request: Request, batch: Annotated[int, Query(ge=1, le=1_000_000)] = 1):
    return view(request, "joblist.html", **dashboard_view(request.app.state.runtime, batch))


@router.get("/add", response_class=HTMLResponse, include_in_schema=False)
def add_page(request: Request):
    limit = request.app.state.deployment.max_upload_mib
    return view(request, "add.html", page="add", limit=limit, values={})


@router.post("/add", response_class=HTMLResponse, include_in_schema=False)
async def add_book(request: Request):
    runtime = request.app.state.runtime
    async with form(request) as data:
        try:
            protect(request, data)
            await run_in_threadpool(create_job, runtime, data)
        except (ValueError, HTTPException) as exc:
            values = {key: str(data.get(key, ""))[:200] for key in ("author", "title", "format")}
            return view(
                request,
                "add.html",
                getattr(exc, "status_code", 422),
                page="add",
                limit=runtime.deployment.max_upload_mib,
                values=values,
                error=issue(exc),
            )
    return RedirectResponse("/", 303)


@router.post("/jobs/{job_id}/action", include_in_schema=False)
async def action_page(request: Request, job_id: str):
    runtime = request.app.state.runtime
    async with form(request) as data:
        try:
            protect(request, data)
            if data.get("intent") == "delete":
                await run_in_threadpool(delete_job, runtime, job_id)
            else:
                await run_in_threadpool(change_job, runtime, job_id, data)
        except (ValueError, HTTPException) as exc:
            summary = await run_in_threadpool(dashboard_view, runtime)
            return view(
                request,
                "dashboard.html",
                getattr(exc, "status_code", 422),
                page="dashboard",
                error=issue(exc),
                **summary,
            )
    return RedirectResponse("/", 303)


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
def settings_page(request: Request):
    return view(
        request,
        "settings.html",
        page="settings",
        sections=SETTING_GROUPS,
        values=settings_view(request.app.state.runtime),
        saved=request.query_params.get("saved") == "1",
    )


def update_settings(runtime, updates):
    with runtime.db() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        runtime.settings.update(session, updates)
        return runtime.settings.public(session)


@router.post("/settings", include_in_schema=False)
async def save_settings(request: Request):
    runtime = request.app.state.runtime
    values = await run_in_threadpool(settings_view, runtime)
    async with form(request) as data:
        try:
            protect(request, data)
            updates = {
                key: data[key]
                for key in Settings.model_fields
                if key in data and key not in runtime.settings.overrides
            }
            if "qbit_password" in updates:
                if data.get("clear_password") == "on":
                    updates["qbit_password"] = ""
                elif not updates["qbit_password"]:
                    updates.pop("qbit_password")
            if "dry_run" not in runtime.settings.overrides:
                updates["dry_run"] = data.get("dry_run") == "on"
            for key in ("owner_uid", "owner_gid"):
                if updates.get(key) == "":
                    updates[key] = None
            await run_in_threadpool(update_settings, runtime, updates)
        except (ValueError, HTTPException) as exc:
            return view(
                request,
                "settings.html",
                getattr(exc, "status_code", 422),
                page="settings",
                sections=SETTING_GROUPS,
                values=values,
                error=issue(exc),
            )
    return RedirectResponse("/settings?saved=1", 303)


@router.get("/api/csrf")
def csrf_api(request: Request):
    return {"token": csrf_token(request)}


@router.get("/api/jobs")
def jobs(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    with request.app.state.runtime.db() as session:
        entries = session.scalars(
            select(Job).order_by(Job.created_at.desc()).limit(limit).offset(offset),
        )
        return [public_job(job) for job in entries]


@router.post("/api/jobs", status_code=201)
async def add_api(request: Request):
    async with form(request) as data:
        protect(request, data)
        try:
            return await run_in_threadpool(create_job, request.app.state.runtime, data)
        except ValueError as exc:
            raise HTTPException(422, issue(exc)) from exc


@router.get("/api/jobs/{job_id}")
def job_api(request: Request, job_id: str):
    with request.app.state.runtime.db() as session:
        return public_job(get_job(session, job_id))


@router.get("/api/jobs/{job_id}/cover")
def cover_api(request: Request, job_id: str):
    runtime = request.app.state.runtime
    with runtime.db() as session:
        job = get_job(session, job_id)
        if not job.cover:
            raise HTTPException(404, "No cover is available.")
    try:
        return Response(cover_bytes(runtime.deployment.data, job_id), media_type="image/jpeg")
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "No cover is available.") from exc


@router.delete("/api/jobs/{job_id}", status_code=204)
def delete_api(request: Request, job_id: str):
    protect(request)
    delete_job(request.app.state.runtime, job_id)
    return Response(status_code=204)


@router.post("/api/jobs/{job_id}/action")
async def action_api(request: Request, job_id: str):
    protect(request)
    data = await json_object(request)
    try:
        return await run_in_threadpool(change_job, request.app.state.runtime, job_id, data)
    except ValueError as exc:
        raise HTTPException(422, issue(exc)) from exc


@router.get("/api/destinations")
def preview(
    request: Request,
    author: Annotated[str, Query(max_length=200)],
    title: Annotated[str, Query(max_length=200)],
    format: Annotated[str, Query(max_length=10)],
):
    runtime = request.app.state.runtime
    try:
        with runtime.db() as session:
            confirmed = book_details({"author": author, "title": title})
            return destinations(
                runtime.settings.get(session), format_field({"format": format}), *confirmed
            )
    except ValueError as exc:
        raise HTTPException(422, issue(exc)) from exc


@router.post("/api/epub/inspect")
async def inspect(request: Request):
    runtime = request.app.state.runtime
    async with form(request) as data:
        protect(request, data)
        upload = upload_field(data)
        if upload is None:
            raise HTTPException(422, "Choose an EPUB to inspect.")
        try:
            _, book = await run_in_threadpool(
                inspect_upload,
                upload,
                runtime.deployment.upload_limit,
                PROVIDERS["embedded"],
            )
        except ValueError as exc:
            raise HTTPException(422, issue(exc)) from exc
        return book.public()


@router.get("/api/settings")
def settings_api(request: Request):
    return settings_view(request.app.state.runtime)


@router.patch("/api/settings")
async def patch_settings(request: Request):
    protect(request)
    updates = await json_object(request)
    try:
        return await run_in_threadpool(update_settings, request.app.state.runtime, updates)
    except ValueError as exc:
        raise HTTPException(422, issue(exc)) from exc


@router.post("/api/settings/test-qbittorrent")
def test_qbittorrent(request: Request):
    protect(request)
    runtime = request.app.state.runtime
    with runtime.db() as session:
        settings = runtime.settings.get(session)
    try:
        with runtime.qbittorrent(settings) as client:
            return {"api_version": client.test()}
    except TorrentError as exc:
        raise HTTPException(502, issue(exc)) from exc
