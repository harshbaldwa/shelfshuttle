from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import UploadFile

from app.core.paths import describe_filesystem_error
from app.models.database import Job, queue_lock
from app.services.importing import destinations
from app.services.intake import discard_upload, inspect_upload, receive_upload, save_cover
from app.services.metadata import PROVIDERS
from app.services.qbittorrent import torrent_input
from app.services.queue import DELETING, can_delete, finish_deletion

FORMATS = {"combined": "Audiobook + ebook", "audio": "Audiobook", "ebook": "Ebook"}
STATUS = {
    "queued": "Queued",
    "submitting": "Submitting",
    "downloading": "Downloading",
    "importing": "Importing",
    DELETING: "Deleting",
    "complete": "Imported",
    "dry_run": "Dry run",
    "failed": "Failed",
    "review": "Needs review",
}
GROUPS = {
    "active": "Active queue",
    "review": "Needs review",
    "complete": "Completed imports",
    "failed": "Failed imports",
    "dry_run": "Dry runs",
}


def public_job(job: Job) -> dict:
    return {
        "id": job.id,
        "title": job.title,
        "author": job.author,
        "format": job.format,
        "status": job.status,
        "detail": job.detail,
        "progress": job.progress,
        "destinations": job.destinations,
        "metadata": job.metadata_,
        "dry_run": job.dry_run,
        "review_kind": job.review_kind,
        "prepared": bool(job.prepared),
        "can_delete": can_delete(job),
        "created_at": job.created_at,
        "cover_url": f"/api/jobs/{job.id}/cover" if job.cover else None,
    }


def get_job(session, job_id: str) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "This job does not exist.")
    return job


def book_details(data) -> tuple[str, str]:
    answer = []
    for field in ("author", "title"):
        value = data.get(field, "")
        if not isinstance(value, str):
            raise ValueError(f"Enter a valid {field}.")
        value = value.strip()
        if (
            not value
            or len(value) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError(f"Enter a {field} between 1 and 200 characters.")
        answer.append(value)
    return tuple(answer)


def format_field(data) -> str:
    kind = data.get("format", "combined")
    if not isinstance(kind, str) or kind not in FORMATS:
        raise ValueError("Choose an intake format.")
    return kind


def upload_field(data) -> UploadFile | None:
    upload = data.get("epub")
    if isinstance(upload, UploadFile) and upload.filename:
        return upload
    return None


def create_job(runtime, data) -> dict:
    kind = format_field(data)
    author, title = book_details(data)
    upload = upload_field(data)
    torrent = data.get("torrent", "")
    if not isinstance(torrent, str):
        raise ValueError("Enter a valid torrent magnet or hash.")
    if bool(upload) != (kind in {"combined", "ebook"}):
        raise ValueError("Choose one EPUB for the selected intake format.")
    if bool(torrent.strip()) != (kind in {"combined", "audio"}):
        raise ValueError("Enter a magnet or hash for the selected intake format.")
    digest, magnet = torrent_input(torrent) if torrent.strip() else (None, None)
    job_id = uuid4().hex
    with runtime.db() as session:
        settings = runtime.settings.get(session)
        target = destinations(settings, kind, author, title)
        if digest and session.scalar(select(Job.id).where(Job.info_hash == digest)):
            raise HTTPException(
                409, "That torrent already has a ShelfShuttle job. Open it in the dashboard."
            )
        # Close the read transaction before validating a potentially large upload.
        session.rollback()
        path = None
        cover = False
        metadata = {}
        try:
            if upload:
                provider = PROVIDERS[settings.metadata_provider]
                name, book = inspect_upload(upload, runtime.deployment.upload_limit, provider)
                path = receive_upload(settings.intake_root, job_id, name, upload)
                metadata["ebook"] = book.public()
                if book.cover:
                    save_cover(runtime.deployment.data, job_id, book.cover)
                    cover = True
            job = Job(
                id=job_id,
                format=kind,
                author=author,
                title=title,
                info_hash=digest,
                magnet=magnet,
                epub_path=str(path) if path else None,
                cover=cover,
                metadata_=metadata,
                destinations=target,
                dry_run=settings.dry_run,
            )
            session.add(job)
            session.commit()
        except BaseException as exc:
            if path:
                discard_upload(path)
            if cover:
                (runtime.deployment.data / "covers" / f"{job_id}.jpg").unlink(missing_ok=True)
            if isinstance(exc, IntegrityError) and digest:
                raise HTTPException(409, "That torrent already has a ShelfShuttle job.") from exc
            if isinstance(exc, OSError):
                raise ValueError(describe_filesystem_error(exc)) from exc
            raise
        return public_job(job)


def change_job(runtime, job_id, payload):
    intent = payload.get("intent")
    if intent not in ("retry", "approve"):
        raise ValueError("Choose retry or confirm review.")
    # A write transaction prevents two concurrent review submissions from both changing the job.
    with runtime.db() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        job = get_job(session, job_id)
        if job.status not in {"failed", "review"} or (
            intent == "approve" and job.status != "review"
        ):
            raise HTTPException(409, "This job is no longer awaiting that action.")
        settings = runtime.settings.get(session)
        if intent == "approve" and not job.prepared:
            author, title = book_details(
                {
                    "author": payload.get("author", job.author),
                    "title": payload.get("title", job.title),
                }
            )
            job.destinations = destinations(settings, job.format, author, title)
            job.author, job.title = author, title
            if job.review_kind == "metadata":
                job.accept_metadata = True
            if job.review_kind == "duplicate":
                job.use_existing = True
        if replacement := payload.get("torrent"):
            if (
                not isinstance(replacement, str)
                or not job.info_hash
                or job.torrent_seen
                or job.prepared
            ):
                raise ValueError("A replacement magnet is unavailable for this job.")
            digest, magnet = torrent_input(replacement)
            if digest != job.info_hash or not magnet:
                raise ValueError("Use a full magnet with the same info hash as the existing job.")
            job.magnet = magnet
        if not job.torrent_seen:
            job.submission_at = None
        job.status, job.detail, job.review_kind = "queued", None, None
        session.commit()
        return public_job(job)


def delete_job(runtime, job_id: str) -> None:
    with queue_lock(runtime.deployment.data) as locked:
        if not locked:
            raise HTTPException(409, "The queue worker is busy. Try deleting again in a moment.")
        with runtime.db() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = get_job(session, job_id)
            if not can_delete(job):
                raise HTTPException(
                    409, "This job is importing or has completed its library import."
                )
            job.status, job.detail = DELETING, None
            # Record the intent before deleting files so the worker can finish after a restart.
            session.commit()
            if detail := finish_deletion(session, job, runtime.deployment):
                raise HTTPException(409, detail)
