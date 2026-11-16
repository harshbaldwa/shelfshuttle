import errno
import io
import os
import stat
from base64 import b32encode
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

import pytest
from conftest import (
    AUTH,
    DIGEST,
    MAGNET,
    action,
    configure,
    epub_bytes,
    get_job,
    torrent,
    write_audio,
)
from fastapi.testclient import TestClient

from app.services import importing


def submit(rig, *args, **kwargs):
    response = rig.submit(*args, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


def test_epub_only_metadata_and_cover(rig):
    raw = epub_bytes(cover=True)
    job = submit(rig, epub=("Orbit.epub", raw))
    assert job["status"] == "queued"
    assert job["metadata"]["ebook"]["series"] == "Example Orbit"
    assert job["metadata"]["ebook"]["language"] == "en"
    assert job["metadata"]["ebook"]["isbn"] == "9781234567897"
    response = rig.client.get(job["cover_url"], headers=AUTH)
    assert response.headers["content-type"] == "image/jpeg"
    assert rig.client.get(job["cover_url"]).status_code == 401
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "complete", result
    assert (Path(job["destinations"]["ebook"]) / "Orbit.epub").read_bytes() == raw
    assert not rig.fake.requests
    assert not rig.deployment.audiobook_mount.exists()


def test_audiobook_single_m4b(rig):
    audio = write_audio(rig.downloads / "Torrent Root" / "Original Name.m4b")
    rig.fake.complete(rig.downloads, [audio])
    job = submit(rig, "audio")
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "complete", result
    assert result["progress"] == 1
    assert result["metadata"]["audio"]["narrator"] == "Sample Narrator"
    assert result["metadata"]["audio"]["duration_seconds"] > 0
    assert (Path(job["destinations"]["audio"]) / audio.name).read_bytes() == audio.read_bytes()
    assert rig.fake.adds == 0


def test_multiple_mp3_natural_order_and_disc_folders(rig):
    paths = [
        write_audio(rig.downloads / "Root" / name)
        for name in ("Disc 2/1.mp3", "Disc 1/10.mp3", "Disc 1/2.mp3")
    ]
    rig.fake.complete(rig.downloads, paths)
    job = submit(rig, "audio")
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "complete", result
    assert result["metadata"]["audio"]["tracks"] == [
        "Disc 1/2.mp3",
        "Disc 1/10.mp3",
        "Disc 2/1.mp3",
    ]
    for source in paths:
        assert (
            Path(job["destinations"]["audio"]) / source.relative_to(rig.downloads / "Root")
        ).is_file()


def test_legacy_prepared_pair_recovers_after_first_atomic_rename(
    rig, monkeypatch, legacy_pair_routing
):
    audio = write_audio(rig.downloads / "Original.m4b")
    rig.fake.complete(rig.downloads, [audio])
    rename = importing._rename_exclusive

    def crash(*args):
        rename(*args)
        raise SystemExit("Power loss immediately after publishing the first target")

    monkeypatch.setattr(importing, "_rename_exclusive", crash)
    with legacy_pair_routing():
        job = submit(rig, "combined")
        with pytest.raises(SystemExit):
            rig.tick()
    book = Path(job["destinations"]["audio"])
    first_inode = book.stat().st_ino
    assert (book / "Orbit.epub").exists()
    assert not Path(job["destinations"]["ebook"]).exists()
    assert get_job(rig, job["id"])["prepared"]
    assert rig.client.delete(f"/api/jobs/{job['id']}", headers=AUTH).status_code == 409

    monkeypatch.setattr(importing, "_rename_exclusive", rename)
    app = rig.app_factory()
    with TestClient(app) as client:
        app.state.worker.tick()
        result = client.get(f"/api/jobs/{job['id']}", headers=AUTH).json()
        assert result["status"] == "complete", result
    rig.tick()
    assert book.stat().st_ino == first_inode
    assert (book / "Original.m4b").read_bytes() == audio.read_bytes()
    assert (Path(job["destinations"]["ebook"]) / "Orbit.epub").exists()


def test_queued_legacy_pair_routes_only_to_audiobook_library(rig, legacy_pair_routing):
    audio = write_audio(rig.downloads / "Original.m4b")
    rig.fake.complete(rig.downloads, [audio])
    with legacy_pair_routing():
        job = submit(rig, "combined")
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "complete", result
    assert result["destinations"] == {"audio": job["destinations"]["audio"]}
    folder = Path(result["destinations"]["audio"])
    assert sorted(entry.name for entry in folder.iterdir()) == ["Orbit.epub", "Original.m4b"]
    assert not Path(job["destinations"]["ebook"]).exists()


def test_stale_stage_rebuilt_after_preparation_crash(rig, monkeypatch):
    job = submit(rig)
    from app.workers import worker

    original = worker.stage_targets

    def interrupt(*args):
        original(*args)
        raise SystemExit

    monkeypatch.setattr(worker, "stage_targets", interrupt)
    with pytest.raises(SystemExit):
        rig.tick()
    assert not get_job(rig, job["id"])["prepared"]
    monkeypatch.setattr(worker, "stage_targets", original)
    rig.tick()
    assert get_job(rig, job["id"])["status"] == "complete"


def test_qbit_add_lost_response_recovers_and_tracks_real_progress(rig):
    rig.fake.break_after_add = True
    job = submit(rig, "audio", magnet=MAGNET)
    rig.tick()
    assert get_job(rig, job["id"])["status"] == "submitting"
    tag = rig.fake.torrents[DIGEST]["tags"]
    rig.fake.torrents[DIGEST] = torrent("downloading", 0.372, tags=tag)
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["progress"] == 0.372
    assert result["status"] == "downloading"
    audio = write_audio(rig.downloads / "book.m4b")
    rig.fake.complete(rig.downloads, [audio], tags=tag)
    rig.tick()
    assert get_job(rig, job["id"])["status"] == "complete"
    assert rig.fake.adds == 1
    assert rig.fake.logins >= 2
    assert rig.fake.requests.count(("POST", "torrents/createCategory")) == 1
    assert audio.is_file()


@pytest.mark.parametrize("digest", [DIGEST.upper(), b32encode(bytes.fromhex(DIGEST)).decode()])
def test_bare_hash_submits_and_tracks_download(rig, digest):
    rig.fake.break_after_add = True
    job = submit(rig, "audio", magnet=digest)
    rig.tick()
    assert get_job(rig, job["id"])["status"] == "submitting"
    assert rig.fake.submitted_magnets == [f"magnet:?xt=urn:btih:{DIGEST}"]
    rig.tick()
    waiting = get_job(rig, job["id"])
    assert (waiting["status"], waiting["progress"]) == ("downloading", 0)
    assert "Waiting for peers" in waiting["detail"]
    audio = write_audio(rig.downloads / "Orbit.m4b")
    rig.fake.complete(rig.downloads, [audio], tags=rig.fake.torrents[DIGEST]["tags"])
    rig.tick()
    assert get_job(rig, job["id"])["status"] == "complete"
    assert rig.fake.adds == 1


def test_duplicate_in_app_and_existing_torrent_review(rig):
    # Same SHA-1 arrives as a hex magnet and then a base32 hash.
    job = submit(rig, "audio", magnet=MAGNET)
    duplicate = rig.submit("audio", magnet=b32encode(bytes.fromhex(DIGEST)).decode())
    assert duplicate.status_code == 409
    audio = write_audio(rig.downloads / "book.m4b")
    rig.fake.complete(rig.downloads, [audio])
    rig.tick()
    assert get_job(rig, job["id"])["review_kind"] == "duplicate"
    action(rig, job["id"], "approve")
    rig.tick()
    assert get_job(rig, job["id"])["status"] == "complete"
    assert rig.fake.adds == 0


def test_existing_destination_remains_untouched_and_retry_works(rig):
    job = submit(rig)
    destination = Path(job["destinations"]["ebook"])
    destination.mkdir(parents=True)
    (destination / "existing").write_text("Keep this.")
    rig.tick()
    assert get_job(rig, job["id"])["status"] == "review"
    assert list(destination.iterdir()) == [destination / "existing"]
    action(rig, job["id"], "approve", title="Alternate Edition")
    rig.tick()
    assert get_job(rig, job["id"])["review_kind"] == "metadata"
    action(rig, job["id"], "approve")
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "complete", result
    assert Path(result["destinations"]["ebook"]).name == "Alternate Edition"
    assert (destination / "existing").read_text() == "Keep this."


def test_existing_directory_race_does_not_merge_or_replace(rig, monkeypatch):
    job = submit(rig)
    destination = Path(job["destinations"]["ebook"])
    rename = importing._rename_exclusive

    def arrive(*args):
        destination.mkdir()
        rename(*args)

    monkeypatch.setattr(importing, "_rename_exclusive", arrive)
    rig.tick()
    assert get_job(rig, job["id"])["status"] == "review"
    assert list(destination.iterdir()) == []
    assert get_job(rig, job["id"])["prepared"]


def test_metadata_conflict_needs_approval(rig):
    job = submit(rig, title="The title I chose")
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["review_kind"] == "metadata"
    assert not Path(job["destinations"]["ebook"]).exists()
    action(rig, job["id"], "approve")
    rig.tick()
    assert get_job(rig, job["id"])["status"] == "complete"
    assert Path(job["destinations"]["ebook"]).name == "The title I chose"


def test_title_traversal_is_sanitized_and_source_escape_is_rejected(rig):
    job = submit(rig, "audio", title="../Outside")
    assert Path(job["destinations"]["audio"]).name == "_Outside"
    rig.fake.torrents[DIGEST] = torrent("uploading", 1)
    rig.fake.file_entries[DIGEST] = [
        {"name": "../private.mp3", "size": 10, "priority": 1, "progress": 1},
    ]
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "failed"
    assert "unsafe" in result["detail"]
    assert not rig.deployment.audiobook_mount.exists()


def test_symlink_sources_are_rejected(rig):
    real = write_audio(rig.root / "external.m4b")
    alias = rig.downloads / "alias"
    alias.symlink_to(real.parent, target_is_directory=True)
    rig.fake.complete(rig.downloads, [alias / real.name])
    job = submit(rig, "audio")
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "failed"
    assert "symlink" in result["detail"]


def test_symlink_library_is_rejected(rig):
    job = submit(rig)
    external = rig.root / "external"
    external.mkdir()
    root = rig.deployment.ebook_mount
    root.mkdir()
    (root / "library").symlink_to(external, target_is_directory=True)
    rig.tick()
    assert get_job(rig, job["id"])["status"] == "failed"
    assert not list(external.iterdir())


def test_permissions_are_applied_to_published_media(rig):
    # UID/GID are left unset for runner-independent execution.
    configure(rig, file_mode="0640", directory_mode="0750")
    job = submit(rig)
    rig.tick()
    assert get_job(rig, job["id"])["status"] == "complete"
    path = Path(job["destinations"]["ebook"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o750
    assert stat.S_IMODE((path / "Orbit.epub").stat().st_mode) == 0o640
    assert (path / "Orbit.epub").stat().st_uid == os.geteuid()


def test_permission_errors_are_actionable_and_retriable(rig, monkeypatch):
    from app.workers import worker

    original = worker.stage_targets

    def deny(*args):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(worker, "stage_targets", deny)
    job = submit(rig)
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "failed"
    assert "UID/GID" in result["detail"]
    monkeypatch.setattr(worker, "stage_targets", original)
    action(rig, job["id"])
    rig.tick()
    assert get_job(rig, job["id"])["status"] == "complete"


def test_import_only_creates_missing_directories(rig, monkeypatch):
    root = rig.deployment.ebook_mount
    root.mkdir()
    job = submit(rig)
    mkdir = os.mkdir

    def reject_mkdir_on_read_only_ancestor(path, *args, **kwargs):
        if path == root.name:
            raise OSError(errno.EROFS, "Read-only filesystem")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", reject_mkdir_on_read_only_ancestor)
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "complete", result


def test_read_only_stage_reports_mount_layout(rig, monkeypatch):
    job = submit(rig)
    mkdir = os.mkdir

    def reject_staging_mkdir(path, *args, **kwargs):
        if path == ".shelfshuttle":
            raise OSError(errno.EROFS, "Read-only filesystem")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", reject_staging_mkdir)
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "failed", result
    assert "one writable parent mount" in result["detail"]
    assert not Path(job["destinations"]["ebook"]).exists()


def test_unsupported_directory_sync_still_imports(rig, monkeypatch):
    job = submit(rig)
    fsync = os.fsync
    file_syncs = []

    def reject_directory_sync(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            raise OSError(errno.EINVAL, "invalid argument")
        file_syncs.append(info.st_size)
        fsync(fd)

    monkeypatch.setattr(os, "fsync", reject_directory_sync)
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "complete", result
    assert file_syncs and all(file_syncs)
    assert (Path(job["destinations"]["ebook"]) / "Orbit.epub").is_file()


@pytest.mark.parametrize("code", [errno.EINVAL, errno.ENOTSUP])
def test_retry_prepared_import_when_mount_cannot_rename_exclusively(rig, monkeypatch, code):
    def fail_rename(*args):
        raise OSError(errno.EIO, "I/O error")

    job = submit(rig)
    monkeypatch.setattr(importing, "_rename_exclusive", fail_rename)
    rig.tick()
    failed = get_job(rig, job["id"])
    assert failed["status"] == "failed" and failed["prepared"]
    stage = rig.deployment.ebook_mount / ".shelfshuttle" / f"{job['id']}-ebook"
    expected = (stage / "Orbit.epub").read_bytes()

    def unsupported(*args):
        raise OSError(code, "Unsupported rename")

    monkeypatch.setattr(importing, "_rename_exclusive", unsupported)
    action(rig, job["id"])
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "complete", result
    assert (Path(job["destinations"]["ebook"]) / "Orbit.epub").read_bytes() == expected
    assert not stage.exists()


def test_exclusive_copy_rejects_directory_created_during_publish(rig, monkeypatch):
    job = submit(rig)
    destination = Path(job["destinations"]["ebook"])

    def appear_before_fallback(*args):
        destination.mkdir()
        (destination / "existing.epub").write_bytes(b"existing data")
        raise OSError(errno.EINVAL, "Unsupported rename")

    monkeypatch.setattr(importing, "_rename_exclusive", appear_before_fallback)
    rig.tick()
    assert get_job(rig, job["id"])["status"] == "review"
    assert [file.name for file in destination.iterdir()] == ["existing.epub"]
    assert (destination / "existing.epub").read_bytes() == b"existing data"


def test_exclusive_copy_does_not_overwrite_a_file_in_the_reserved_directory(rig, monkeypatch):
    job = submit(rig)
    destination = Path(job["destinations"]["ebook"]) / "Orbit.epub"
    copy_staged_file = importing._copy_staged_file

    def unsupported(*args):
        raise OSError(errno.EINVAL, "Unsupported rename")

    def introduce_file(*args):
        destination.write_bytes(b"existing data")
        copy_staged_file(*args)

    monkeypatch.setattr(importing, "_rename_exclusive", unsupported)
    monkeypatch.setattr(importing, "_copy_staged_file", introduce_file)
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "review" and "already exists" in result["detail"]
    assert destination.read_bytes() == b"existing data"


def test_exclusive_copy_resumes_partial_mp3_after_restart(rig, monkeypatch):
    paths = [
        write_audio(rig.downloads / "Torrent" / disc / "01.mp3") for disc in ("Disc 1", "Disc 2")
    ]
    rig.fake.complete(rig.downloads, paths)
    job = submit(rig, "audio")
    copy = importing.shutil.copyfileobj

    def unsupported(*args):
        def interrupt(source, destination, length):
            destination.write(source.read(128))
            raise OSError(errno.EIO, "Interrupted file copy")

        monkeypatch.setattr(importing.shutil, "copyfileobj", interrupt)
        raise OSError(errno.EINVAL, "Unsupported rename")

    monkeypatch.setattr(importing, "_rename_exclusive", unsupported)
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "failed" and "Copying library file" in result["detail"]
    destination = Path(job["destinations"]["audio"])
    [partial] = destination.glob("*/*.mp3")
    assert partial.stat().st_size == 128
    original_inode = partial.stat().st_ino
    monkeypatch.setattr(importing.shutil, "copyfileobj", copy)
    action(rig, job["id"])
    app = rig.app_factory()
    with TestClient(app) as client:
        app.state.worker.tick()
        result = client.get(f"/api/jobs/{job['id']}", headers=AUTH).json()
        assert result["status"] == "complete", result
    assert partial.stat().st_ino == original_inode
    for path in paths:
        assert (destination / path.parent.name / path.name).read_bytes() == path.read_bytes()


@pytest.mark.parametrize("mimetype", ["late", "compressed", "missing", "invalid"])
def test_nonconforming_epub_marker_is_normalized_on_import(rig, mimetype):
    raw = epub_bytes(mimetype=mimetype, cover=True)
    inspected = rig.client.post(
        "/api/epub/inspect", files={"epub": ("Orbit.epub", raw)}, headers=AUTH
    )
    assert inspected.status_code == 200, inspected.text
    assert inspected.json()["title"] == "A Small Orbit"
    job = submit(rig, epub=("Orbit.epub", raw))
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "complete", result
    destination = Path(job["destinations"]["ebook"]) / "Orbit.epub"
    with ZipFile(destination) as repaired, ZipFile(io.BytesIO(raw)) as original:
        first = repaired.infolist()[0]
        assert first.filename == "mimetype" and first.header_offset == 0
        assert first.compress_type == ZIP_STORED
        assert repaired.read(first) == b"application/epub+zip"
        assert {
            name: repaired.read(name) for name in repaired.namelist() if name != "mimetype"
        } == {name: original.read(name) for name in original.namelist() if name != "mimetype"}


def test_zip_without_epub_package_is_rejected(rig):
    raw = io.BytesIO()
    with ZipFile(raw, "w") as archive:
        archive.writestr("notes.txt", "A regular ZIP archive")
    response = rig.submit(epub=("renamed.epub", raw.getvalue()))
    assert response.status_code == 422
    assert "does not contain an EPUB package" in response.json()["detail"]
    assert not rig.deployment.intake_mount.exists()


@pytest.mark.parametrize(
    "raw",
    [
        b"not a ZIP",
        epub_bytes(extra=("../escape.xhtml", b"x")),
        epub_bytes(mimetype="missing", extra=("../escape.xhtml", b"x")),
        epub_bytes(extra=("book/duplicate.bin", b"x" * (2 * 1024**2))),
        b"x" * (1024**2 + 1),
    ],
)
def test_invalid_epub_does_not_write_intake(rig, raw):
    response = rig.submit(epub=("bad.epub", raw))
    assert response.status_code in (413, 422), response.text
    assert not rig.deployment.intake_mount.exists()


def test_archive_symlink_and_upload_filename_are_rejected(rig):
    member = ZipInfo("book/symlink")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    assert (
        rig.submit(epub=("bad.epub", epub_bytes(extra=(member, b"/somewhere")))).status_code == 422
    )
    assert rig.submit(epub=("../bad.epub", epub_bytes())).status_code == 422
    assert not rig.deployment.intake_mount.exists()


@pytest.mark.parametrize("torrent_input", [MAGNET, DIGEST])
def test_dry_run_skips_library_writes_and_torrent_submission(rig, torrent_input):
    configure(rig, dry_run=True)
    ebook = submit(rig)
    audio = submit(rig, "audio", magnet=torrent_input)
    rig.tick()
    assert get_job(rig, ebook["id"])["status"] == "dry_run"
    assert get_job(rig, audio["id"])["status"] == "review"
    assert rig.fake.adds == 0
    assert not rig.deployment.ebook_mount.exists()
    assert not rig.deployment.audiobook_mount.exists()


def test_invalid_audio_fails_without_touching_source(rig):
    audio = rig.downloads / "broken.m4b"
    audio.write_bytes(b"invalid audio content")
    rig.fake.complete(rig.downloads, [audio])
    job = submit(rig, "audio")
    rig.tick()
    assert get_job(rig, job["id"])["status"] == "failed"
    assert audio.read_bytes() == b"invalid audio content"


def test_changed_library_settings_require_explicit_confirmation(rig):
    job = submit(rig)
    new_root = rig.deployment.ebook_mount / "alternate"
    configure(rig, ebook_library=str(new_root))
    rig.tick()
    assert get_job(rig, job["id"])["review_kind"] == "settings"
    action(rig, job["id"], "approve")
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "complete"
    assert Path(result["destinations"]["ebook"]).is_relative_to(new_root)
