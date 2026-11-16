import errno
import os
import re
import stat
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from string import Formatter


class UnsafePath(ValueError):
    pass


@contextmanager
def filesystem_step(operation: str, path: Path):
    try:
        yield
    except OSError as exc:
        if not hasattr(exc, "shelfshuttle_step"):
            exc.shelfshuttle_step = (operation, path)
        raise


def sync_directory(fd: int, path: Path) -> None:
    with filesystem_step("Syncing directory", path):
        try:
            os.fsync(fd)
        except OSError as exc:
            # Some mounted filesystems support regular-file sync but reject directory sync.
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.ENOSYS}:
                raise


def absolute_path(value: str, kind=Path):
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(part in {".", ".."} for part in value.split("/"))
        or any(ord(char) < 32 for char in value)
    ):
        raise UnsafePath("Paths must be absolute, with no traversal or control characters.")
    return kind(value)


def relative_parts(value: str, *, archive: bool = False) -> tuple[str, ...]:
    parts = tuple(value.split("/"))
    if any(
        part in {"", ".", ".."}
        or "\\" in part
        or (archive and ":" in part)
        or any(ord(char) < 32 or ord(char) == 127 for char in part)
        for part in parts
    ):
        raise UnsafePath("A file path contains an unsafe component.")
    return parts


def clean_component(value: str, *, max_bytes: int = 180) -> str:
    value = unicodedata.normalize("NFC", value)
    value = "".join(
        "_" if char in '/\\<>:"|?*' or unicodedata.category(char).startswith("C") else char
        for char in value
    )
    value = value.strip(" .")
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", value):
        value = f"_{value}"
    value = value.encode()[:max_bytes].decode(errors="ignore").rstrip(" .")
    if not value:
        raise UnsafePath("Author, title and naming template must produce nonempty names.")
    return value


def book_file_stem(author: str, title: str) -> str:
    author, title = clean_component(author), clean_component(title)
    available = 255 - len(" - .epub")
    if len(author.encode()) + len(title.encode()) > available:
        author = clean_component(
            author, max_bytes=max(available // 2, available - len(title.encode()))
        )
        title = clean_component(title, max_bytes=available - len(author.encode()))
    return f"{author} - {title}"


def book_folder(template: str, author: str, title: str) -> Path:
    if len(template) > 300:
        raise UnsafePath("The folder template is too long.")
    template = template.removesuffix("/")
    parts = relative_parts(template)
    fields = set()
    names = []
    for part in parts:
        for _, field, spec, conversion in Formatter().parse(part):
            if field is not None:
                if field not in {"author", "title"} or spec or conversion:
                    raise UnsafePath("Only {author} and {title} fields are supported.")
                fields.add(field)
        names.append(
            clean_component(
                part.format(
                    author=clean_component(author),
                    title=clean_component(title),
                )
            )
        )
    if fields != {"author", "title"} or len(names) > 6:
        raise UnsafePath("Use both {author} and {title} across at most six folders.")
    return Path(*names)


@contextmanager
def directory_fd(path: Path, *, create: bool = False, mode: int = 0o755, owner=(None, None)):
    absolute_path(str(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        current = Path("/")
        for part in path.parts[1:]:
            parent = current
            current /= part
            made = False
            try:
                with filesystem_step("Opening directory", current):
                    next_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    with filesystem_step("Creating directory", current):
                        os.mkdir(part, mode | 0o700, dir_fd=fd)
                    made = True
                    sync_directory(fd, parent)
                except FileExistsError:
                    pass
                with filesystem_step("Opening directory", current):
                    next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            if made:
                with filesystem_step("Setting directory permissions", current):
                    set_permissions(fd, mode, owner)
                sync_directory(fd, current)
        yield fd
    finally:
        os.close(fd)


def set_permissions(fd: int, mode: int, owner: tuple[int | None, int | None]) -> None:
    uid, gid = owner
    info = os.fstat(fd)
    if (uid is not None and info.st_uid != uid) or (gid is not None and info.st_gid != gid):
        os.fchown(fd, uid if uid is not None else -1, gid if gid is not None else -1)
    os.fchmod(fd, mode)


@contextmanager
def regular_file(path: Path):
    with directory_fd(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                raise UnsafePath("The source must be a nonempty regular file.")
            yield source


def describe_filesystem_error(error: OSError) -> str:
    step = getattr(error, "shelfshuttle_step", None)
    code = errno.errorcode.get(error.errno, "unknown error")
    prefix = f"{step[0]} at {step[1]} failed" if step else "Filesystem operation failed"
    return f"{prefix} ({code}). {_filesystem_error_hint(error.errno, step)}"


def _filesystem_error_hint(code, step) -> str:
    if code in {errno.ELOOP, errno.ENOTDIR}:
        return "A filesystem path contains a symlink or a non-directory component."
    if code in {errno.EXDEV, errno.ENOTSUP, errno.ENOSYS} or (
        code == errno.EINVAL and step and step[0] == "Publishing library directory"
    ):
        return "Staging and library must share a mount that supports atomic, no-replace renames."
    if code == errno.EROFS:
        return (
            "A required path is on a read-only filesystem. "
            "In Docker, use one writable parent mount for each library and its staging directory."
        )
    if code in {errno.EACCES, errno.EPERM}:
        return "Filesystem access denied. Check mount permissions and configured UID/GID."
    if code in {errno.ENOSPC, errno.EDQUOT}:
        return "The filesystem has insufficient space for staging or application state."
    if code == errno.ENOENT:
        return (
            "A configured directory or source file is missing. Check the mounts and path mapping."
        )
    return "Check the configured mounts and available space."
