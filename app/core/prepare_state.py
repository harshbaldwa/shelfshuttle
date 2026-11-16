"""Prepare the dedicated state mount before the non-root app starts."""

import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path

STATE_FILES = (
    ".key",
    ".worker.lock",
    "shelfshuttle.sqlite3",
    "shelfshuttle.sqlite3-journal",
    "shelfshuttle.sqlite3-wal",
    "shelfshuttle.sqlite3-shm",
)


@contextmanager
def state_entry(parent: int, name: str, *, directory: bool = False):
    try:
        entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        yield None
        return
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(entry.st_mode) or (not directory and entry.st_nlink != 1):
        raise ValueError(f"Unsafe ShelfShuttle state entry: {name}")

    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if directory:
        flags |= os.O_DIRECTORY
    fd = os.open(name, flags, dir_fd=parent)
    try:
        opened = os.fstat(fd)
        if (
            not os.path.samestat(entry, opened)
            or opened.st_dev != os.fstat(parent).st_dev
            or (not directory and opened.st_nlink != 1)
        ):
            raise ValueError(f"Unsafe ShelfShuttle state entry: {name}")
        yield fd
    finally:
        os.close(fd)


def set_owner(fd: int, uid: int, gid: int) -> None:
    info = os.fstat(fd)
    if (info.st_uid, info.st_gid) != (uid, gid):
        os.fchown(fd, uid, gid)


def set_mode(fd: int, mode: int) -> None:
    if stat.S_IMODE(os.fstat(fd).st_mode) != mode:
        os.fchmod(fd, mode)


def prepare_state(path: Path, uid: int, gid: int) -> None:
    if not (0 < uid <= 2147483647 and 0 < gid <= 2147483647):
        raise ValueError("Set PUID and PGID to positive non-root IDs.")

    root = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        set_mode(root, 0o700)
        for name in STATE_FILES:
            with state_entry(root, name) as fd:
                if fd is not None:
                    set_mode(fd, 0o600)
                    set_owner(fd, uid, gid)

        with state_entry(root, "covers", directory=True) as covers:
            if covers is not None:
                set_mode(covers, 0o700)
                for name in os.listdir(covers):
                    if re.fullmatch(r"[0-9a-f]{32}\.(jpg|tmp)", name):
                        with state_entry(covers, name) as fd:
                            if fd is not None:
                                set_mode(fd, 0o600)
                                set_owner(fd, uid, gid)
                set_owner(covers, uid, gid)
        set_owner(root, uid, gid)
    finally:
        os.close(root)
