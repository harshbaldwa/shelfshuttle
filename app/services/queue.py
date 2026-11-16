from app.core.paths import UnsafePath, describe_filesystem_error
from app.services.importing import discard_prepared
from app.services.intake import remove_job_intake

DELETING = "deleting"
DELETABLE = {"queued", "submitting", "downloading", "failed", "review", "dry_run", DELETING}


def can_delete(job) -> bool:
    return job.status in DELETABLE


def finish_deletion(session, job, deployment) -> str | None:
    try:
        if job.prepared:
            discard_prepared(job.prepared, job.id, deployment)
        remove_job_intake(deployment, job.id, job.epub_path)
    except (OSError, UnsafePath) as exc:
        reason = describe_filesystem_error(exc) if isinstance(exc, OSError) else str(exc)
        job.detail = f"Deletion pending. {reason}"
        session.commit()
        return job.detail
    session.delete(job)
    session.commit()
    return None
