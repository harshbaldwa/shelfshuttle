import errno
import re
from pathlib import Path

from conftest import AUTH, DIGEST, epub_bytes, get_job, torrent, write_audio
from test_web import sign_in

from app.models.database import queue_lock
from app.services import importing, queue


def test_delete_through_dashboard_removes_upload_and_cover(rig):
    job = rig.submit(epub=("Orbit.epub", epub_bytes(cover=True))).json()
    job_id = job["id"]
    upload = rig.deployment.intake_mount / job_id / "Orbit.epub"
    cover = rig.deployment.data / "covers" / f"{job_id}.jpg"
    assert upload.is_file() and cover.is_file()
    page = sign_in(rig.client)
    assert "Delete from queue" in page.text
    csrf = re.search(r'meta name="csrf-token" content="([^"]+)"', page.text)[1]
    action = f"/jobs/{job_id}/action"
    assert rig.client.post(action, data={"intent": "delete"}).status_code == 403
    assert upload.is_file()
    response = rig.client.post(
        action, data={"intent": "delete", "_csrf": csrf}, follow_redirects=False
    )
    assert response.status_code == 303
    assert rig.client.get(f"/api/jobs/{job_id}", headers=AUTH).status_code == 404
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
    assert rig.client.delete(endpoint).status_code == 401
    assert rig.client.delete(endpoint, headers=AUTH).status_code == 204
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
    assert rig.client.delete(f"/api/jobs/{job['id']}", headers=AUTH).status_code == 204
    assert library_book.read_bytes() == b"test existing book"


def test_delete_cannot_race_worker_or_remove_completed_job(rig):
    job = rig.submit().json()
    endpoint = f"/api/jobs/{job['id']}"
    with queue_lock(rig.deployment.data) as locked:
        assert locked
        response = rig.client.delete(endpoint, headers=AUTH)
        assert response.status_code == 409
        assert "busy" in response.json()["detail"]
    rig.tick()
    assert get_job(rig, job["id"])["status"] == "complete"
    assert rig.client.delete(endpoint, headers=AUTH).status_code == 409
    assert (Path(job["destinations"]["ebook"]) / "Orbit.epub").is_file()


def test_worker_finishes_deletion_after_cleanup_failure(rig, monkeypatch):
    job = rig.submit().json()
    endpoint = f"/api/jobs/{job['id']}"

    def temporarily_unwritable(*args):
        raise OSError(errno.EROFS, "read only")

    with monkeypatch.context() as patch:
        patch.setattr(queue, "remove_job_intake", temporarily_unwritable)
        assert rig.client.delete(endpoint, headers=AUTH).status_code == 409
        assert get_job(rig, job["id"])["status"] == "deleting"
    rig.tick()
    assert rig.client.get(endpoint, headers=AUTH).status_code == 404
    assert not (rig.deployment.intake_mount / job["id"]).exists()
    assert not Path(job["destinations"]["ebook"]).exists()


def test_failed_publish_shows_delete_and_exact_filesystem_error(rig, monkeypatch):
    job = rig.submit().json()
    destination = Path(job["destinations"]["ebook"])
    stage = rig.deployment.ebook_mount / ".shelfshuttle" / f"{job['id']}-ebook"

    def rejected_rename(*args):
        raise OSError(errno.EINVAL, "invalid argument")

    monkeypatch.setattr(importing, "_rename_exclusive", rejected_rename)
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "failed" and result["prepared"], result
    assert f"Publishing library directory at {destination} failed (EINVAL)" in result["detail"]
    assert "no-replace renames" in result["detail"]
    assert stage.is_dir()
    assert "Delete from queue" in sign_in(rig.client).text
    assert rig.client.delete(f"/api/jobs/{job['id']}", headers=AUTH).status_code == 204
    assert not stage.exists()
    assert not destination.exists()


def test_delete_preserves_published_half_of_failed_pairing(rig, monkeypatch):
    audio = write_audio(rig.downloads / "Orbit.m4b")
    rig.fake.complete(rig.downloads, [audio])
    job = rig.submit("combined").json()
    original_rename = importing._rename_exclusive

    def reject_second_publish(src_fd, name, dest_fd, dest_name):
        if name.endswith("-ebook"):
            raise OSError(errno.EIO, "input/output error")
        original_rename(src_fd, name, dest_fd, dest_name)

    monkeypatch.setattr(importing, "_rename_exclusive", reject_second_publish)
    rig.tick()
    result = get_job(rig, job["id"])
    assert result["status"] == "failed" and result["prepared"], result
    published = Path(job["destinations"]["audio"])
    assert (published / "Orbit.epub").is_file()
    assert rig.client.delete(f"/api/jobs/{job['id']}", headers=AUTH).status_code == 204
    assert (published / "Orbit.epub").is_file()
    assert (published / audio.name).read_bytes() == audio.read_bytes()
    assert not Path(job["destinations"]["ebook"]).exists()
    assert not (rig.deployment.ebook_mount / ".shelfshuttle" / f"{job['id']}-ebook").exists()
