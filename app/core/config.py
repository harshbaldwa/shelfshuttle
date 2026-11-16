import os
import re
import secrets
import stat
from contextlib import suppress
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.core.paths import absolute_path, book_folder, directory_fd


class Settings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    qbit_url: str = Field("", max_length=500)
    qbit_username: str = Field("", max_length=200)
    qbit_password: SecretStr = SecretStr("")
    qbit_category: str = "shelfshuttle"
    remote_download_root: str = "/downloads"
    download_root: Path
    intake_root: Path
    audiobook_library: Path
    audiobook_staging: Path
    ebook_library: Path
    ebook_staging: Path
    folder_template: str = "{author}/{title}"
    file_mode: str = "0644"
    directory_mode: str = "0755"
    owner_uid: int | None = Field(None, ge=1, le=2147483647)
    owner_gid: int | None = Field(None, ge=1, le=2147483647)
    poll_seconds: int = Field(5, ge=2, le=300)
    metadata_provider: str = "embedded"
    dry_run: bool = False

    @field_validator("qbit_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        if value:
            url = urlsplit(value)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or "@" in url.netloc
                or url.query
                or url.fragment
                or url.port == 0
                or any(char.isspace() for char in value)
            ):
                raise ValueError("Enter an HTTP(S) URL without credentials or query parameters.")
        return value.rstrip("/")

    @field_validator("qbit_category")
    @classmethod
    def valid_category(cls, value: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value):
            raise ValueError("Use 1–64 letters, digits, hyphens, or underscores.")
        return value

    @field_validator("remote_download_root")
    @classmethod
    def valid_remote_root(cls, value: str) -> str:
        return str(absolute_path(value, PurePosixPath))

    @field_validator("folder_template")
    @classmethod
    def valid_template(cls, value: str) -> str:
        book_folder(value, "Author", "Book")
        return value

    @field_validator("file_mode", "directory_mode")
    @classmethod
    def valid_mode(cls, value: str) -> str:
        if not re.fullmatch(r"0[0-7]{3}", value) or int(value, 8) & 0o700 < 0o600:
            raise ValueError("Use a four-digit octal mode with owner read/write access.")
        return value

    @field_validator("metadata_provider")
    @classmethod
    def valid_provider(cls, value: str) -> str:
        if value != "embedded":
            raise ValueError("Embedded metadata is the provider available in this release.")
        return value

    @model_validator(mode="after")
    def valid_permissions(self):
        if not int(self.directory_mode, 8) & 0o100:
            raise ValueError("The directory owner needs execute permission.")
        return self


class Deployment(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    data: Path = Path("/app/data")
    downloads_mount: Path = Path("/downloads")
    intake_mount: Path = Path("/app/intake")
    audiobook_mount: Path = Path("/media/audiobooks")
    ebook_mount: Path = Path("/media/ebooks")
    cookie_secure: bool = True
    max_upload_mib: int = Field(64, ge=1, le=512)
    worker_enabled: bool = True

    @model_validator(mode="after")
    def validate_deployment(self):
        for key in ("data", "downloads_mount", "intake_mount", "audiobook_mount", "ebook_mount"):
            absolute_path(str(getattr(self, key)))
        return self

    @property
    def upload_limit(self) -> int:
        return self.max_upload_mib * 1024**2

    def defaults(self) -> dict:
        return {
            "download_root": self.downloads_mount,
            "intake_root": self.intake_mount,
            "audiobook_library": self.audiobook_mount / "library",
            "audiobook_staging": self.audiobook_mount / ".shelfshuttle",
            "ebook_library": self.ebook_mount / "library",
            "ebook_staging": self.ebook_mount / ".shelfshuttle",
        }

    def validate_settings(self, settings: Settings) -> Settings:
        restricted = {
            "download_root": self.downloads_mount,
            "intake_root": self.intake_mount,
            "audiobook_library": self.audiobook_mount,
            "audiobook_staging": self.audiobook_mount,
            "ebook_library": self.ebook_mount,
            "ebook_staging": self.ebook_mount,
        }
        paths = [("data", self.data)]
        for key, mount in restricted.items():
            path = absolute_path(str(getattr(settings, key)))
            if not path.is_relative_to(mount):
                raise ValueError(f"{key} must be inside {mount}.")
            for other_key, other in paths:
                if path.is_relative_to(other) or other.is_relative_to(path):
                    raise ValueError(f"{key} and {other_key} must be separate directories.")
            paths.append((key, path))
        return settings


def environment(fields: dict, secret_fields: set[str]) -> dict:
    values = {}
    for name in fields:
        key = f"SHELFSHUTTLE_{name.upper()}"
        location = os.environ.get(f"{key}_FILE") if name in secret_fields else None
        if location:
            if key in os.environ:
                raise ValueError(f"Set either {key} or {key}_FILE.")
            values[name] = Path(location).read_text(encoding="utf-8").rstrip("\r\n")
        elif key in os.environ:
            values[name] = os.environ[key]
    return values


def deployment_from_env() -> Deployment:
    return Deployment.model_validate(environment(Deployment.model_fields, set()))


def private_key(directory: Path) -> bytes:
    with directory_fd(directory, create=True, mode=0o700) as fd:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError(
                "The state directory must be owned by ShelfShuttle and private to write."
            )
        try:
            with open_key(fd) as file:
                key = file.read(33)
        except FileNotFoundError:
            key = secrets.token_bytes(32)
            temporary = f".key-{secrets.token_hex(12)}"
            try:
                with os.fdopen(
                    os.open(
                        temporary,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=fd,
                    ),
                    "wb",
                ) as file:
                    file.write(key)
                    file.flush()
                    os.fsync(file.fileno())
                os.link(temporary, ".key", src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                os.fsync(fd)
            except FileExistsError:
                with open_key(fd) as file:
                    key = file.read(33)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=fd)
    if len(key) != 32:
        raise ValueError("The application state key is invalid.")
    return key


def open_key(fd: int):
    key_fd = os.open(".key", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    info = os.fstat(key_fd)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        os.close(key_fd)
        raise ValueError("The application state key requires private file permissions.")
    return os.fdopen(key_fd, "rb")
