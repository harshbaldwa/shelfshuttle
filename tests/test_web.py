import re
import sqlite3
from pathlib import Path

import pytest
from conftest import BOOK_FILENAME_STEM, DIGEST, csrf_headers, epub_bytes, write_audio
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.qbittorrent import torrent_input


def test_proxy_managed_access_and_browser_csrf(rig):
    client = rig.client
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/api/jobs").json() == []
    assert client.get("/api/settings").status_code == 200
    assert client.get("/login").status_code == 404
    assert client.post("/logout").status_code == 404
    page = client.get("/")
    assert page.status_code == 200
    assert "Your queue is empty" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    csrf = re.search(r'meta name="csrf-token" content="([^"]+)"', page.text)[1]
    assert "Destination preview" in client.get("/add").text
    assert "Test saved connection" in client.get("/settings").text
    assert client.patch("/api/settings", json={"dry_run": True}).status_code == 403
    assert (
        client.post(
            "/api/epub/inspect",
            files={"epub": ("book.epub", epub_bytes())},
            headers={"X-CSRF-Token": csrf},
        ).json()["author"]
        == "Ada Reed"
    )
    assert (
        client.get(
            "/api/destinations",
            params={
                "author": "Ada Reed",
                "title": "A Small Orbit",
                "format": "ebook",
            },
        )
        .json()["ebook"]
        .endswith("/Ada Reed/A Small Orbit")
    )
    response = client.post(
        "/add",
        data={
            "_csrf": csrf,
            "format": "ebook",
            "author": "Ada Reed",
            "title": "A Small Orbit",
        },
        files={"epub": ("book.epub", epub_bytes())},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert "A Small Orbit" in client.get("/dashboard/content").text


def test_https_csrf_cookie_and_api_bootstrap(rig):
    deployment = rig.deployment.model_copy(update={"cookie_secure": True})
    app = create_app(deployment, overrides={})
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/csrf")
        cookie = response.headers["set-cookie"].lower()
        assert cookie.startswith("__host-shelfshuttle_csrf=")
        assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        assert response.headers["cache-control"] == "no-store"
        path = "/api/settings"
        changes = {"dry_run": True}
        assert client.patch(path, json=changes).status_code == 403
        assert (
            client.patch(path, json=changes, headers={"X-CSRF-Token": "invalid"}).status_code == 403
        )
        headers = {"X-CSRF-Token": response.json()["token"]}
        assert client.patch(path, json=changes, headers=headers).status_code == 200
        client.cookies.clear()
        assert client.patch(path, json=changes, headers=headers).status_code == 403


def test_add_book_form_imports_pair_with_misordered_epub_marker(rig):
    audio = write_audio(rig.downloads / "Orbit.m4b")
    rig.fake.complete(rig.downloads, [audio])
    client = rig.client
    page = client.get("/add")
    csrf = re.search(r'meta name="csrf-token" content="([^"]+)"', page.text)[1]
    response = client.post(
        "/add",
        data={
            "_csrf": csrf,
            "format": "combined",
            "author": "Ada Reed",
            "title": "A Small Orbit",
            "torrent": DIGEST,
        },
        files={"epub": ("Orbit.epub", epub_bytes(mimetype="late"))},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    rig.tick()
    (job,) = client.get("/api/jobs").json()
    assert job["status"] == "complete", job
    preview = client.get(
        "/api/destinations",
        params={"format": "combined", "author": "Ada Reed", "title": "A Small Orbit"},
    )
    folder = rig.deployment.audiobook_mount / "library" / "Ada Reed" / "A Small Orbit"
    assert preview.json() == job["destinations"] == {"audio": str(folder)}
    assert sorted(file.name for file in Path(job["destinations"]["audio"]).iterdir()) == [
        f"{BOOK_FILENAME_STEM}.epub",
        f"{BOOK_FILENAME_STEM}.m4b",
    ]
    assert not rig.deployment.ebook_mount.exists()


def test_password_is_encrypted_and_environment_values_are_read_only(rig):
    app = create_app(rig.deployment, overrides={})
    password = "only-used-as-a-fake-password"
    with TestClient(app) as client:
        headers = csrf_headers(client)
        result = client.patch("/api/settings", json={"qbit_password": password}, headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["qbit_password_set"]
        assert password not in result.text
        assert "qbit_password" not in result.json()
        with sqlite3.connect(rig.deployment.data / "shelfshuttle.sqlite3") as connection:
            fields = connection.execute('SELECT "values" FROM settings').fetchone()[0]
        assert "qbit_password" in fields and password not in fields
        with app.state.runtime.db() as session:
            assert (
                app.state.runtime.settings.get(session).qbit_password.get_secret_value() == password
            )
        response = client.patch(
            "/api/settings",
            json={
                "ebook_library": str(rig.root / "somewhere"),
            },
            headers=headers,
        )
        assert response.status_code == 422
        cleared = client.patch("/api/settings", json={"qbit_password": ""}, headers=headers)
        assert not cleared.json()["qbit_password_set"]
    assert (
        rig.client.patch(
            "/api/settings",
            json={
                "qbit_url": "https://example.test",
            },
            headers=csrf_headers(rig.client),
        ).status_code
        == 422
    )


def test_chunked_request_with_no_content_length_is_bounded(rig):
    def body():
        yield b'--demo\r\nContent-Disposition: form-data; name="epub"; filename="x.epub"\r\n\r\n'
        for _ in range(20):
            yield b"x" * 65536
        yield b"\r\n--demo--\r\n"

    response = rig.client.post(
        "/api/epub/inspect",
        content=body(),
        headers={
            **csrf_headers(rig.client),
            "Content-Type": "multipart/form-data; boundary=demo",
        },
    )
    assert response.status_code == 413, response.text


@pytest.mark.parametrize("modern_auth", [False, True])
def test_qbittorrent_connection_check_and_auth_refresh(rig, modern_auth):
    rig.fake.modern_auth = modern_auth
    rig.fake.expire_once = True
    response = rig.client.post("/api/settings/test-qbittorrent", headers=csrf_headers(rig.client))
    assert response.status_code == 200, response.text
    assert response.json() == {"api_version": "2.11.3"}
    assert rig.fake.logins == 2


@pytest.mark.parametrize(
    "value",
    [
        "magnet:?xt=urn:btmh:0122",
        f"magnet:?xt=urn:btih:{DIGEST}&tr=%0Aunsafe",
        "https://example.test/file.torrent",
        "magnet:?xt=urn:btih:not-a-hash",
    ],
)
def test_unsupported_torrent_input(value):
    with pytest.raises(ValueError):
        torrent_input(value)
