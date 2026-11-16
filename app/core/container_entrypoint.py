"""Initialize Docker mounts and permanently drop privileges before serving."""

import ctypes
import os
import sys
from pathlib import Path
from urllib.request import urlopen

from app.core.prepare_state import prepare_state, set_mode, set_owner

PR_SET_NO_NEW_PRIVS = 38


def run_as(uid: int, gid: int) -> None:
    # Prevent exec from granting privileges again, including via setuid binaries.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    os.setgroups([])
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)


def prepare_empty_intake(uid: int, gid: int) -> None:
    fd = os.open("/app/intake", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid == 0:
            with os.scandir(fd) as entries:
                if next(entries, None) is None:
                    set_mode(fd, 0o700)
                    set_owner(fd, uid, gid)
    finally:
        os.close(fd)


def main(args: list[str]) -> None:
    healthcheck = args == ["--healthcheck"]
    if os.geteuid() == 0:
        try:
            uid = int(os.environ.get("PUID", os.environ.get("SHELFSHUTTLE_PUID", "10001")))
            gid = int(os.environ.get("PGID", os.environ.get("SHELFSHUTTLE_PGID", "10001")))
            if not (0 < uid <= 2147483647 and 0 < gid <= 2147483647):
                raise ValueError
        except ValueError:
            raise SystemExit("Set PUID and PGID to positive non-root IDs.") from None
        try:
            if not healthcheck:
                prepare_state(Path("/app/data"), uid, gid)
                prepare_empty_intake(uid, gid)
            run_as(uid, gid)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Could not initialize ShelfShuttle: {exc}") from None

    if healthcheck:
        urlopen("http://127.0.0.1:8080/healthz", timeout=3).close()
    else:
        os.execvp(args[0], args)


if __name__ == "__main__":
    main(sys.argv[1:])
