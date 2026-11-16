import os
import re
import stat
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from string import Formatter


class UnsafePath(ValueError):
    pass


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


def clean_component(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = "".join(
        "_" if char in '/\\<>:"|?*' or unicodedata.category(char).startswith("C") else char
        for char in value
    )
    value = value.strip(" .")
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", value):
        value = f"_{value}"
    value = value.encode()[:180].decode(errors="ignore").rstrip(" .")
    if not value:
        raise UnsafePath("Author, title and naming template must produce nonempty names.")
    return value


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
        for part in path.parts[1:]:
            made = False
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode | 0o700, dir_fd=fd)
                    made = True
                    os.fsync(fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            if made:
                set_permissions(fd, mode, owner)
                os.fsync(fd)
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
    import errno

    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
        return "A filesystem path contains a symlink or a non-directory component."
    if error.errno in {errno.EXDEV, errno.ENOTSUP, errno.ENOSYS}:
        return "Staging and library must share a mount that supports atomic, no-replace renames."
    if error.errno == errno.EROFS:
        return (
            "A required path is on a read-only filesystem. "
            "In Docker, use one writable parent mount for each library and its staging directory."
        )
    if error.errno in {errno.EACCES, errno.EPERM}:
        return "Filesystem access denied. Check mount permissions and configured UID/GID."
    if error.errno == errno.ENOSPC:
        return "The filesystem has insufficient space for staging or application state."
    if error.errno == errno.ENOENT:
        return (
            "A configured directory or source file is missing. Check the mounts and path mapping."
        )
    return "A filesystem operation failed. Check the configured mounts and available space."
