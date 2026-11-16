import logging
import math
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.core.paths import UnsafePath, book_file_stem, describe_filesystem_error, regular_file
from app.models.database import Job, queue_lock
from app.services.importing import (
    NeedsReview,
    Source,
    destinations,
    finish_targets,
    preflight,
    stage_targets,
    tracked_audio,
)
from app.services.intake import save_cover
from app.services.metadata import PROVIDERS, InvalidMedia, aggregate_audio, mismatches
from app.services.qbittorrent import QBittorrent, TorrentError, TorrentUnavailable
from app.services.queue import DELETING, finish_deletion

ACTIVE = ("queued", "submitting", "downloading", "importing", DELETING)
SEED_STATES = {"uploading", "stalledUP", "forcedUP", "queuedUP", "pausedUP", "stoppedUP"}
LOG = logging.getLogger(__name__)


class Worker:
    def __init__(self, db, settings, client=QBittorrent):
        self.db = db
        self.settings = settings
        self.client = client
        self.connection = None
        self.connection_profile = None

    def close(self) -> None:
        if self.connection is not None:
            self.connection.__exit__(None, None, None)
            self.connection = self.connection_profile = None

    def connected(self, settings):
        profile = (
            settings.qbit_url,
            settings.qbit_username,
            settings.qbit_password,
            settings.qbit_category,
            settings.remote_download_root,
        )
        if self.connection is None or self.connection_profile != profile:
            self.close()
            self.connection = self.client(settings).__enter__()
            self.connection_profile = profile
        return self.connection

    def run(self, stop):
        while not stop.is_set():
            delay = 5
            try:
                with self.db() as session:
                    delay = self.settings.get(session).poll_seconds
                self.tick(stop)
            except Exception:
                LOG.error("Worker polling failed. Check application state and configuration.")
            stop.wait(delay)

    def tick(self, stop=None):
        with queue_lock(self.settings.deployment.data) as locked:
            if locked:
                self._tick_locked(stop)

    def _tick_locked(self, stop):
        with self.db() as session:
            ids = session.scalars(
                select(Job.id).where(Job.status.in_(ACTIVE)).order_by(Job.created_at)
            ).all()
        for job_id in ids:
            if stop is not None and stop.is_set():
                break
            with self.db() as session:
                job = session.get(Job, job_id)
                if job is None or job.status not in ACTIVE:
                    continue
                if job.status == DELETING:
                    finish_deletion(session, job, self.settings.deployment)
                    continue
                try:
                    self.process(session, job, self.settings.get(session))
                except NeedsReview as exc:
                    job.status, job.detail, job.review_kind = "review", str(exc), exc.kind
                except TorrentUnavailable as exc:
                    self.close()
                    job.detail = str(exc)
                except TorrentError as exc:
                    self.close()
                    job.status, job.detail = "failed", str(exc)
                except (InvalidMedia, UnsafePath) as exc:
                    job.status, job.detail = "failed", str(exc)
                except OSError as exc:
                    job.status, job.detail = "failed", describe_filesystem_error(exc)
                except Exception:
                    session.rollback()
                    job.status, job.detail = (
                        "failed",
                        "Unexpected processing error. Check the application state.",
                    )
                    LOG.error("Processing failed for job %s.", job.id)
                session.commit()

    def process(self, session, job, settings):
        job.detail = None
        if job.prepared:
            if settings.dry_run and not job.dry_run:
                raise NeedsReview(
                    "Dry-run is enabled. Turn it off to resume the prepared import.", "settings"
                )
            deployment = self.settings.deployment
            for target, item in job.prepared.items():
                mount = deployment.audiobook_mount if target == "audio" else deployment.ebook_mount
                if any(
                    not Path(item[key]).is_relative_to(mount) for key in ("stage", "destination")
                ):
                    raise NeedsReview(
                        "Restore the library mounts used to prepare this import.", "settings"
                    )
            self._finish_import(session, job)
            return
        current = destinations(settings, job.format, job.author, job.title)
        if current != job.destinations:
            # Unprepared jobs from older releases can drop their extra ebook destination.
            if (
                job.format == "combined"
                and set(job.destinations) == {"audio", "ebook"}
                and current == {"audio": job.destinations["audio"]}
            ):
                job.destinations = current
            else:
                raise NeedsReview(
                    "Library settings changed. Confirm the updated destinations.", "settings"
                )
        dry_run = job.dry_run or settings.dry_run
        audio = []
        if job.info_hash:
            audio = self._download(session, job, settings, dry_run)
            if audio is None:
                return
        job.status = "importing"
        session.commit()
        provider = PROVIDERS[settings.metadata_provider]
        stem = book_file_stem(job.author, job.title)
        ebook = []
        tagged = []
        metadata = {}
        cover = None
        warnings = set()
        if job.epub_path:
            path = Path(job.epub_path)
            if not path.is_relative_to(self.settings.deployment.intake_mount):
                raise UnsafePath("The EPUB is outside the intake mount.")
            with regular_file(path) as source:
                stamp = Source.signature(source.fileno())
                if stamp[2] > self.settings.deployment.upload_limit:
                    raise InvalidMedia("The EPUB exceeds the configured upload limit.")
                book = provider.ebook(source)
                ebook.append(Source(path, Path(f"{stem}.epub"), stamp))
            metadata["ebook"] = book.public()
            cover = book.cover
            warnings.update(f"EPUB {field}" for field in mismatches(book, job.author, job.title))
        audio_sources = []
        for path, name, size in audio:
            suffix = path.suffix.lower()
            if suffix == ".m4b":
                name = Path(f"{stem}.m4b")
            with regular_file(path) as source:
                stamp = Source.signature(source.fileno())
                if stamp[2] != size:
                    raise InvalidMedia(
                        "A downloaded file does not match the size reported by qBittorrent."
                    )
                book = provider.audio(source, suffix)
                audio_sources.append(Source(path, name, stamp))
            warnings.update(f"audio {field}" for field in mismatches(book, job.author, job.title))
            tagged.append((name, book))
            cover = cover or book.cover
        if tagged:
            metadata["audio"] = aggregate_audio(tagged)
        job.metadata_ = metadata
        if cover and not job.cover:
            save_cover(self.settings.deployment.data, job.id, cover)
            job.cover = True
        if warnings and not job.accept_metadata:
            fields = ", ".join(sorted(warnings))
            raise NeedsReview(
                f"Embedded metadata differs from the confirmed details: {fields}.", "metadata"
            )
        groups = {"audio": audio_sources + ebook, "ebook": ebook}
        preflight(current, groups)
        if dry_run:
            job.dry_run = True
            job.status = "dry_run"
            job.detail = "Validation passed. No library files were written."
            return
        job.prepared = stage_targets(job.id, settings, current, groups)
        self._finish_import(session, job)

    @staticmethod
    def _finish_import(session, job):
        job.status = "importing"
        session.commit()

        def checkpoint():
            flag_modified(job, "prepared")
            session.commit()

        finish_targets(job.prepared, checkpoint)
        job.status = "complete"

    @staticmethod
    def _progress(job, torrent):
        try:
            progress = float(torrent["progress"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TorrentError("qBittorrent did not return valid download progress.") from exc
        if not math.isfinite(progress) or not 0 <= progress <= 1:
            raise TorrentError("qBittorrent did not return valid download progress.")
        job.progress = progress
        state = str(torrent.get("state", "unknown"))
        if state in {"error", "missingFiles", "unknown"}:
            raise TorrentError("qBittorrent reports an error or missing torrent files.")
        return state

    def _download(self, session, job, settings, dry_run):
        tag = f"shelfshuttle-{job.id}"
        client = self.connected(settings)
        torrent = client.find(job.info_hash)
        if torrent is None:
            if dry_run:
                raise NeedsReview(
                    "Dry-run can inspect an existing torrent. No torrent was submitted.", "torrent"
                )
            if job.torrent_seen:
                raise NeedsReview("The tracked torrent disappeared from qBittorrent.", "missing")
            if job.submission_at is not None:
                if time.time() - job.submission_at > 120:
                    raise TorrentError(
                        "qBittorrent did not list the torrent after submission. Check the Web UI and retry."
                    )
                job.status = "submitting"
                return None
            job.status, job.submission_at = "submitting", time.time()
            session.commit()
            client.add(job.magnet or f"magnet:?xt=urn:btih:{job.info_hash}", tag)
            return None
        tags = {part.strip() for part in str(torrent.get("tags", "")).split(",")}
        if not job.torrent_seen and not job.use_existing and tag not in tags and job.magnet:
            raise NeedsReview(
                "This torrent already exists in qBittorrent. Confirm to monitor it without changing it.",
                "duplicate",
            )
        job.status, job.torrent_seen = "downloading", True
        state = self._progress(job, torrent)
        job.detail = "qBittorrent: " + (
            state if state.isascii() and state.isalpha() and len(state) <= 30 else "unknown state"
        )
        if not job.magnet and tag in tags and state in {"metaDL", "forcedMetaDL", "stalledDL"}:
            job.detail = "Waiting for peers or torrent metadata. Check peer discovery and trackers in qBittorrent."
        if job.progress != 1 or state not in SEED_STATES:
            return None
        files = client.files(job.info_hash)
        job.detail = None
        return tracked_audio(settings, torrent, files)
