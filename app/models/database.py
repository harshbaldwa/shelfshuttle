import fcntl
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import JSON, Float, String, Text, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.core.paths import directory_fd


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    author: Mapped[str] = mapped_column(String(200))
    format: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(24), index=True, default="queued")
    detail: Mapped[str | None] = mapped_column(Text)
    review_kind: Mapped[str | None] = mapped_column(String(32))
    progress: Mapped[float | None] = mapped_column(Float)
    info_hash: Mapped[str | None] = mapped_column(String(40), unique=True)
    magnet: Mapped[str | None] = mapped_column(Text)
    submission_at: Mapped[float | None] = mapped_column(Float)
    torrent_seen: Mapped[bool] = mapped_column(default=False)
    use_existing: Mapped[bool] = mapped_column(default=False)
    accept_metadata: Mapped[bool] = mapped_column(default=False)
    epub_path: Mapped[str | None] = mapped_column(Text)
    cover: Mapped[bool] = mapped_column(default=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    destinations: Mapped[dict] = mapped_column(JSON)
    prepared: Mapped[dict | None] = mapped_column(JSON, default=None)
    dry_run: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class StoredSettings(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    values: Mapped[dict] = mapped_column(JSON)


@contextmanager
def queue_lock(path: Path):
    with directory_fd(path) as root:
        fd = os.open(
            ".worker.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=root,
        )
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True
        finally:
            os.close(fd)


def database(path: Path):
    with directory_fd(path, create=True, mode=0o700) as fd:
        fd_db = os.open(
            "shelfshuttle.sqlite3",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=fd,
        )
        try:
            if not stat.S_ISREG(os.fstat(fd_db).st_mode):
                raise ValueError("The database path must be a regular file.")
            os.fchmod(fd_db, 0o600)
        finally:
            os.close(fd_db)
    engine = create_engine(
        URL.create("sqlite", database=str(path / "shelfshuttle.sqlite3")),
        connect_args={"timeout": 15},
    )

    @event.listens_for(engine, "connect")
    def configure(connection, _):
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")

    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)
