import ctypes
import errno
import os
import re
import shutil
import stat
import sys
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.core.config import Deployment, Settings
from app.core.paths import (
    UnsafePath,
    absolute_path,
    book_folder,
    directory_fd,
    filesystem_step,
    regular_file,
    relative_parts,
    set_permissions,
    sync_directory,
)
from app.services.metadata import InvalidMedia


class NeedsReview(Exception):
    def __init__(self, message: str, kind: str = "conflict"):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Source:
    path: Path
    name: Path
    stamp: tuple[int, int, int, int]

    @staticmethod
    def signature(fd: int):
        info = os.fstat(fd)
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    @property
    def size(self) -> int:
        return self.stamp[2]


def destinations(settings: Settings, kind: str, author: str, title: str) -> dict[str, str]:
    relative = book_folder(settings.folder_template, author, title)
    result = {}
    if kind in {"audio", "combined"}:
        result["audio"] = str(settings.audiobook_library / relative)
    if kind in {"ebook", "combined"}:
        result["ebook"] = str(settings.ebook_library / relative)
    return result


def tracked_audio(
    settings: Settings, torrent: dict, items: list[dict]
) -> list[tuple[Path, Path, int]]:
    try:
        save_path = absolute_path(torrent["save_path"], PurePosixPath)
    except (KeyError, TypeError, AttributeError) as exc:
        raise UnsafePath("qBittorrent did not supply a valid save path.") from exc
    remote_root = PurePosixPath(settings.remote_download_root)
    candidates = []
    for item in items:
        name = item.get("name")
        if not isinstance(name, str) or item.get("priority") == 0:
            continue
        suffix = PurePosixPath(name).suffix.lower()
        if suffix not in {".m4b", ".mp3"}:
            continue
        parts = relative_parts(name)
        path = save_path.joinpath(*parts)
        if not path.is_relative_to(remote_root):
            raise UnsafePath("A torrent file is outside the configured qBittorrent path mapping.")
        try:
            size = int(item["size"])
            progress = float(item["progress"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidMedia("qBittorrent returned invalid file size or progress.") from exc
        if size < 1 or progress != 1.0:
            raise InvalidMedia("A selected audiobook file is empty or incomplete.")
        candidates.append(
            (
                settings.download_root.joinpath(*path.relative_to(remote_root).parts),
                parts,
                size,
                suffix,
            )
        )
    if not candidates:
        raise NeedsReview("The download contains no selected M4B or MP3 audio files.", "media")
    if len(candidates) > 1 and any(entry[3] == ".m4b" for entry in candidates):
        raise NeedsReview(
            "Select one M4B or just the MP3 tracks in qBittorrent, then retry.",
            "media",
        )
    shared = os.path.commonprefix([parts[:-1] for _, parts, _, _ in candidates])
    result = [(path, Path(*parts[len(shared) :]), size) for path, parts, size, _ in candidates]
    result.sort(
        key=lambda entry: tuple(
            (0, int(token)) if token.isdecimal() and len(token) < 100 else (1, token)
            for token in re.split(r"([0-9]+)", str(entry[1]).casefold())
        )
    )
    return result


def _rename_exclusive(src_fd: int, src: str, dest_fd: int, dest: str) -> None:
    """Use the kernel's atomic no-replace primitive or fail closed."""
    libc = ctypes.CDLL(None, use_errno=True)
    symbol, flag = ("renameatx_np", 4) if sys.platform == "darwin" else ("renameat2", 1)
    rename = getattr(libc, symbol, None)
    if rename is None:
        raise OSError(errno.ENOTSUP, "Atomic no-replace rename is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(src_fd, os.fsencode(src), dest_fd, os.fsencode(dest), flag) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _entry(fd: int, name: str):
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _identity(info) -> list[int] | None:
    if info is None or not stat.S_ISDIR(info.st_mode):
        return None
    return [info.st_dev, info.st_ino]


def _check_conflict(destination: Path) -> None:
    try:
        with directory_fd(destination.parent) as fd:
            info = _entry(fd, destination.name)
    except FileNotFoundError:
        return
    if info is not None:
        raise NeedsReview(f"The destination already exists: {destination}")


def _check_names(sources: list[Source]) -> None:
    import unicodedata

    files: set[tuple[str, ...]] = set()
    folders: dict[tuple[str, ...], tuple[str, ...]] = {}
    for source in sources:
        original = relative_parts(source.name.as_posix())
        name = tuple(unicodedata.normalize("NFC", part).casefold() for part in original)
        if name in files or name in folders:
            raise NeedsReview("Selected files have conflicting names.", "media")
        files.add(name)
        for index in range(1, len(name)):
            prefix = name[:index]
            actual = original[:index]
            if prefix in files or folders.setdefault(prefix, actual) != actual:
                raise NeedsReview("Selected files have conflicting directory names.", "media")


def preflight(target_paths: dict, source_groups: dict) -> None:
    for target, path in target_paths.items():
        _check_names(source_groups[target])
        _check_conflict(Path(path))


def _stage_root(path: Path):
    return directory_fd(path, create=True, mode=0o700)


def _private_stage(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise UnsafePath(
            "The staging directory must be owned by ShelfShuttle and private to write."
        )
    return info


def stage_targets(job_id: str, settings: Settings, target_paths: dict, source_groups: dict) -> dict:
    owner = (settings.owner_uid, settings.owner_gid)
    file_mode = int(settings.file_mode, 8)
    directory_mode = int(settings.directory_mode, 8)
    prepared = {}
    for target, path in target_paths.items():
        dest = Path(path)
        root = settings.audiobook_staging if target == "audio" else settings.ebook_staging
        label = f"{job_id}-{target}"
        stage = root / label
        with (
            _stage_root(root) as stage_fd,
            directory_fd(
                dest.parent,
                create=True,
                mode=directory_mode,
                owner=owner,
            ) as dest_fd,
        ):
            root_info = _private_stage(stage_fd)
            if root_info.st_dev != os.fstat(dest_fd).st_dev:
                raise OSError(errno.EXDEV, "Staging must share the library filesystem")
            if _entry(dest_fd, dest.name) is not None:
                raise NeedsReview(f"The destination already exists: {dest}")
            if _entry(stage_fd, label) is not None:
                # An earlier attempt before its prepare commit can only leave private staging here.
                shutil.rmtree(label, dir_fd=stage_fd)
            with filesystem_step("Creating staging directory", stage):
                os.mkdir(label, 0o700, dir_fd=stage_fd)
            for source in source_groups[target]:
                with (
                    directory_fd(
                        stage / source.name.parent, create=True, mode=directory_mode, owner=owner
                    ) as parent,
                    regular_file(source.path) as original,
                    filesystem_step("Writing staged file", stage / source.name),
                ):
                    if Source.signature(original.fileno()) != source.stamp:
                        raise InvalidMedia(
                            "A source file changed during validation. Retry when stable."
                        )
                    fd = os.open(
                        source.name.name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=parent,
                    )
                    with os.fdopen(fd, "wb") as output:
                        shutil.copyfileobj(original, output, 1024**2)
                        output.flush()
                        if (
                            output.tell() != source.size
                            or Source.signature(original.fileno()) != source.stamp
                        ):
                            raise InvalidMedia(
                                "A source file changed while staging. Retry when stable."
                            )
                        set_permissions(output.fileno(), file_mode, owner)
                        os.fsync(output.fileno())
                    sync_directory(parent, stage / source.name.parent)
            with directory_fd(stage) as book_fd:
                with filesystem_step("Setting directory permissions", stage):
                    set_permissions(book_fd, directory_mode, owner)
                sync_directory(book_fd, stage)
                identity = _identity(os.fstat(book_fd))
            sync_directory(stage_fd, root)
            prepared[target] = {"stage": str(stage), "destination": str(dest), "identity": identity}
    return prepared


def finish_targets(plan: dict) -> None:
    # Check every target before publishing any additional directories.
    with ExitStack() as stack:
        entries = []
        for item in plan.values():
            dest = Path(item["destination"])
            stage = Path(item["stage"])
            dest_fd = stack.enter_context(directory_fd(dest.parent))
            expected = item["identity"]
            current = _entry(dest_fd, dest.name)
            if current is not None:
                if _identity(current) != expected:
                    raise NeedsReview(f"The destination already exists: {dest}")
                sync_directory(dest_fd, dest.parent)
                continue
            stage_fd = stack.enter_context(directory_fd(stage.parent))
            if _identity(_entry(stage_fd, stage.name)) != expected:
                raise NeedsReview(
                    "Prepared files are missing or changed. Restore the staging directory."
                )
            entries.append((stage_fd, stage, dest_fd, dest))
        for stage_fd, stage, dest_fd, dest in entries:
            with filesystem_step("Publishing library directory", dest):
                try:
                    _rename_exclusive(stage_fd, stage.name, dest_fd, dest.name)
                except OSError as exc:
                    if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                        raise NeedsReview(f"The destination already exists: {dest}") from exc
                    raise
            sync_directory(dest_fd, dest.parent)
            sync_directory(stage_fd, stage.parent)


def discard_prepared(plan: dict, job_id: str, deployment: Deployment) -> None:
    for target, item in plan.items():
        mount = deployment.audiobook_mount if target == "audio" else deployment.ebook_mount
        stage, dest = Path(item["stage"]), Path(item["destination"])
        expected = item["identity"]
        if (
            not expected
            or not stage.is_relative_to(mount)
            or not dest.is_relative_to(mount)
            or stage.name != f"{job_id}-{target}"
            or stage.is_relative_to(dest)
            or dest.is_relative_to(stage)
        ):
            continue
        with suppress(FileNotFoundError), directory_fd(stage.parent) as fd:
            if _identity(_entry(fd, stage.name)) != expected:
                continue
            try:
                with directory_fd(dest.parent) as dest_fd:
                    if _identity(_entry(dest_fd, dest.name)) == expected:
                        continue
            except FileNotFoundError:
                pass
            _private_stage(fd)
            with filesystem_step("Removing staged directory", stage):
                shutil.rmtree(stage.name, dir_fd=fd)
            sync_directory(fd, stage.parent)
