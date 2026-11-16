import errno
import os
import shutil
from contextlib import suppress
from pathlib import Path

from app.core.paths import UnsafePath, clean_component, directory_fd, regular_file, sync_directory
from app.services.metadata import InvalidMedia


def upload_name(filename: str) -> str:
    if len(filename) > 255 or Path(filename).suffix.lower() != ".epub":
        raise InvalidMedia("Choose a file ending in .epub.")
    if "/" in filename or "\\" in filename or any(ord(char) < 32 for char in filename):
        raise InvalidMedia("The upload filename contains an unsafe path or control characters.")
    return clean_component(filename[:-5]) + filename[-5:]


def inspect_upload(upload, limit: int, provider):
    name = upload_name(upload.filename or "")
    upload.file.seek(0, os.SEEK_END)
    size = upload.file.tell()
    upload.file.seek(0)
    if not 0 < size <= limit:
        raise InvalidMedia(f"Choose a nonempty EPUB up to {limit // 1024**2} MiB.")
    book = provider.ebook(upload.file)
    upload.file.seek(0)
    return name, book


def receive_upload(root: Path, job_id: str, name: str, upload) -> Path:
    with directory_fd(root, create=True, mode=0o700) as root_fd:
        os.mkdir(job_id, 0o700, dir_fd=root_fd)
        try:
            with directory_fd(root / job_id) as job_fd:
                fd = os.open(
                    name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=job_fd
                )
                with os.fdopen(fd, "wb") as output:
                    shutil.copyfileobj(upload.file, output, 1024**2)
                    output.flush()
                    os.fsync(output.fileno())
                sync_directory(job_fd, root / job_id)
            sync_directory(root_fd, root)
        except BaseException:
            shutil.rmtree(job_id, dir_fd=root_fd)
            raise
    return root / job_id / name


def discard_upload(path: Path) -> None:
    with directory_fd(path.parent.parent) as fd:
        shutil.rmtree(path.parent.name, dir_fd=fd)


def remove_job_intake(deployment, job_id: str, epub_path: str | None) -> None:
    if epub_path:
        path = Path(epub_path)
        if (
            not path.parent.parent.is_relative_to(deployment.intake_mount)
            or path.parent.name != job_id
            or path.suffix.lower() != ".epub"
        ):
            raise UnsafePath("Restore the original intake mount before retrying deletion.")
        with suppress(FileNotFoundError), directory_fd(path.parent.parent) as intake_fd:
            with directory_fd(path.parent) as job_fd:
                with suppress(FileNotFoundError):
                    os.unlink(path.name, dir_fd=job_fd)
                sync_directory(job_fd, path.parent)
            try:
                os.rmdir(job_id, dir_fd=intake_fd)
            except OSError as exc:
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
            sync_directory(intake_fd, path.parent.parent)
    with suppress(FileNotFoundError), directory_fd(deployment.data / "covers") as cover_fd:
        for suffix in (".jpg", ".tmp"):
            with suppress(FileNotFoundError):
                os.unlink(f"{job_id}{suffix}", dir_fd=cover_fd)
        sync_directory(cover_fd, deployment.data / "covers")


def save_cover(root: Path, job_id: str, data: bytes) -> None:
    with directory_fd(root / "covers", create=True, mode=0o700) as cover_fd:
        name = f"{job_id}.jpg"
        tmp = f"{job_id}.tmp"
        with suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=cover_fd)
        fd = os.open(
            tmp, os.O_WRONLY | os.O_NOFOLLOW | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=cover_fd
        )
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, name, src_dir_fd=cover_fd, dst_dir_fd=cover_fd)
        sync_directory(cover_fd, root / "covers")


def cover_bytes(root: Path, job_id: str) -> bytes:
    with regular_file(root / "covers" / f"{job_id}.jpg") as source:
        return source.read(1024**2)
