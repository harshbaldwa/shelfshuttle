import errno
import re
from pathlib import Path

from conftest import (
    BOOK_FILENAME_STEM,
    DIGEST,
    csrf_headers,
    epub_bytes,
    get_job,
    torrent,
    write_audio,
)
from fastapi.testclient import TestClient

from app.models.database import Job, queue_lock
from app.services import importing, queue
from app.workers import worker


def test_delete_through_dashboard_removes_upload_and_cover(rig):
    job = rig.submit(epub=("Orbit.epub", epub_bytes(cover=True))).json()
    job_id = job["id"]
    upload = rig.deployment.intake_mount / job_id / "Orbit.epub"
    cover = rig.deployment.data / "covers" / f"{job_id}.jpg"
    assert upload.is_file() and cover.is_file()
    page = rig.client.get("/")
    assert "Delete from queue" in page.text
    csrf = re.search(r'meta name="csrf-token" content="([^"]+)"', page.text)[1]
    action = f"/jobs/{job_id}/action"
    assert rig.client.post(action, data={"intent": "delete"}).status_code == 403
    assert upload.is_file()
    response = rig.client.post(
        action, data={"intent": "delete", "_csrf": csrf}, follow_redirects=False
    )
    assert response.status_code == 303
    assert rig.client.get(f"/api/jobs/{job_id}").status_code == 404
    assert not upload.parent.exists()
    assert not cover.exists()
    rig.tick()
    assert not Path(job["destinations"]["ebook"]).exists()


def test_delete_download_job_preserves_qbittorrent_and_files(rig):
    rig.fake.torrents[DIGEST] = torrent("downloading", 0.4)
    source = rig.downloads / "download.mp3"
    source.write_bytes(b"test download")
    job = rig.submit("audio").json()
    rig.tick()
    assert get_job(rig, job["id"])["status"] == "downloading"
    requests = rig.fake.requests.copy()
    endpoint = f"/api/jobs/{job['id']}"
    assert rig.client.delete(endpoint).status_code == 403
    assert rig.client.delete(endpoint, headers=csrf_headers(rig.client)).status_code == 204
    rig.tick()
    assert rig.fake.requests == requests
    assert source.read_bytes() == b"test download"
    assert rig.submit("audio").status_code == 201


def test_delete_conflict_preserves_existing_library_book(rig):
    job = rig.submit().json()
    library_book = Path(job["destinations"]["ebook"]) / "existing.epub"
    library_book.parent.mkdir(parents=True)
    library_book.write_bytes(b"test existing book")
    rig.tick()
    assert get_job(rig, job["id"])["status"] == "review"
    assert (
        rig.client.delete(f"/api/jobs/{job['id']}", headers=csrf_headers(rig.client)).status_code
        == 204
    )
    assert library_book.read_bytes() == b"test existing book"


def test_delete_cannot_race_worker_or_remove_completed_job(rig):
    job = rig.submit().json()
    endpoint = f"/api/jobs/{job['id']}"
    with queue_lock(rig.deployment.data) as locked:
        assert locked
        response = rig.client.delete(endpoint, headers=csrf_headers(rig.client))
        assert response.status_code == 409
        assert "busy" in response.json()["detail"]
    rig.tick()
    assert get_job(rig, job["id"])["status"] == "complete"
    assert rig.client.delete(endpoint, headers=csrf_headers(rig.client)).status_code == 409
    assert (Path(job["destinations"]["ebook"]) / f"{BOOK_FILENAME_STEM}.epub").is_file()


def test_remove_completed_record_preserves_library_and_qbittorrent(rig):
    source = write_audio(rig.downloads / "download.m4b")
    rig.fake.complete(rig.downloads, [source])
    job = rig.submit("combined", epub=("Orbit.epub", epub_bytes(cover=True))).json()
    endpoint = f"/api/jobs/{job['id']}"
    assert (
        rig.client.delete(f"{endpoint}/record", headers=csrf_headers(rig.client)).status_code == 409
    )

    rig.tick()
    completed = get_job(rig, job["id"])
    assert completed["status"] == "complete"
    assert completed["can_forget"]
    assert not (rig.deployment.intake_mount / job["id"]).exists()
    assert rig.client.get(completed["cover_url"]).status_code == 200
    library = Path(job["destinations"]["audio"])
    media = {path.name: path.read_bytes() for path in library.iterdir()}
    assert set(media) == {f"{BOOK_FILENAME_STEM}.epub", f"{BOOK_FILENAME_STEM}.m4b"}
    requests = rig.fake.requests.copy()

    page = rig.client.get("/")
    assert "Remove from history" in page.text
    assert rig.client.delete(f"{endpoint}/record").status_code == 403
    csrf = re.search(r'meta name="csrf-token" content="([^"]+)"', page.text)[1]
    response = rig.client.post(
        f"/jobs/{job['id']}/action",
        data={"intent": "forget", "_csrf": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert rig.client.get(endpoint).status_code == 404
    assert {path.name: path.read_bytes() for path in library.iterdir()} == media
    assert source.is_file()
    assert rig.fake.requests == requests


def test_worker_retries_completed_cleanup_after_restart(rig, monkeypatch):
    job = rig.submit().json()
    upload = rig.deployment.intake_mount / job["id"] / "Orbit.epub"
    unrelated = upload.parent / "keep.txt"
    unrelated.write_text("unrelated")

    def temporarily_unwritable(*args):
        raise PermissionError(errno.EACCES, "permission denied")

    with monkeypatch.context() as patch:
        patch.setattr(worker, "remove_job_upload", temporarily_unwritable)
        rig.tick()
        pending = get_job(rig, job["id"])
        assert pending["status"] == "complete"
        assert "Intake cleanup will retry" in pending["detail"]
        assert upload.exists()

    # Older versions left the same upload reference on completed rows without a warning.
    with rig.state.runtime.db() as session:
        session.get(Job, job["id"]).detail = None
        session.commit()
    app = rig.app_factory()
    with TestClient(app) as client:
        app.state.worker.tick()
        assert not upload.exists()
        assert unrelated.read_text() == "unrelated"
        endpoint = f"/api/jobs/{job['id']}"
        assert client.delete(f"{endpoint}/record", headers=csrf_headers(client)).status_code == 204
        assert client.get(endpoint).status_code == 404


def test_worker_finishes_deletion_after_cleanup_failure(rig, monkeypatch):
    job = rig.submit().json()
    endpoint = f"/api/jobs/{job['id']}"

    def temporarily_unwritable(*args):
        raise OSError(errno.EROFS, "read only")

    with monkeypatch.context() as patch:
        patch.setattr(queue, "remove_job_intake", temporarily_unwritable)
        assert rig.client.delete(endpoint, headers=csrf_headers(rig.client)).status_code == 409
        assert get_job(rig, job["id"])["status"] == "deleting"
    rig.tick()
    assert rig.client.get(endpoint).status_code == 404
    assert not (rig.deployment.intake_mount / job["id"]).exists()
    assert not Path(job["destinations"]["ebook"]).exists()


def test_failed_publish_shows_delete_and_exact_filesystem_error(rig, monkeypatch):
    job = rig.submit().json()
    destination = Path(job["destinations"]["ebook"])
    stage = rig.deployment.ebook_mount / ".shelfshuttle" / f"{job['id']}-ebook"

    def rejected_rename(*args):
        raise OSError(errno.EIO, "input/output error")

    monkeypatch.setattr(importing, "_rename_exclusive", rejected_rename)
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "failed" and result["prepared"], result
    assert f"Publishing library directory at {destination} failed (EIO)" in result["detail"]
    assert stage.is_dir()
    assert "Delete from queue" in rig.client.get("/").text
    assert (
        rig.client.delete(f"/api/jobs/{job['id']}", headers=csrf_headers(rig.client)).status_code
        == 204
    )
    assert not stage.exists()
    assert not destination.exists()


def test_delete_preserves_published_half_of_legacy_pair(rig, monkeypatch, legacy_pair_routing):
    audio = write_audio(rig.downloads / "Orbit.m4b")
    rig.fake.complete(rig.downloads, [audio])
    original_rename = importing._rename_exclusive

    def reject_second_publish(src_fd, name, dest_fd, dest_name):
        if name.endswith("-ebook"):
            raise OSError(errno.EIO, "input/output error")
        original_rename(src_fd, name, dest_fd, dest_name)

    monkeypatch.setattr(importing, "_rename_exclusive", reject_second_publish)
    with legacy_pair_routing():
        job = rig.submit("combined").json()
        rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "failed" and result["prepared"], result
    published = Path(job["destinations"]["audio"])
    assert (published / f"{BOOK_FILENAME_STEM}.epub").is_file()
    assert (
        rig.client.delete(f"/api/jobs/{job['id']}", headers=csrf_headers(rig.client)).status_code
        == 204
    )
    assert (published / f"{BOOK_FILENAME_STEM}.epub").is_file()
    assert (published / f"{BOOK_FILENAME_STEM}.m4b").read_bytes() == audio.read_bytes()
    assert not Path(job["destinations"]["ebook"]).exists()
    assert not (rig.deployment.ebook_mount / ".shelfshuttle" / f"{job['id']}-ebook").exists()
