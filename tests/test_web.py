import re
import sqlite3

import pytest
from conftest import AUTH, DIGEST, PASSWORD, epub_bytes
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.qbittorrent import torrent_input


def sign_in(client):
    page = client.get("/login")
    assert page.status_code == 200, page.text
    token = re.search(r'meta name="csrf-token" content="([^"]+)"', page.text)[1]
    page = client.post(
        "/login",
        data={"username": "admin", "password": PASSWORD, "_csrf": token},
        follow_redirects=False,
    )
    assert page.status_code == 303, page.text
    cookie = page.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    return client.get("/")


def test_authentication_csrf_ui_and_cover(rig):
    client = rig.client
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/add", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/settings").status_code == 401
    assert (
        client.post("/login", data={"username": "admin", "password": PASSWORD}).status_code == 403
    )
    page = sign_in(client)
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
    assert client.post("/logout", data={"_csrf": csrf}, follow_redirects=False).status_code == 303
    assert client.get("/api/jobs").status_code == 401


def test_password_is_encrypted_and_environment_values_are_read_only(rig):
    app = create_app(rig.deployment, overrides={})
    password = "only-used-as-a-fake-password"
    with TestClient(app) as client:
        result = client.patch("/api/settings", json={"qbit_password": password}, headers=AUTH)
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
            headers=AUTH,
        )
        assert response.status_code == 422
        cleared = client.patch("/api/settings", json={"qbit_password": ""}, headers=AUTH)
        assert not cleared.json()["qbit_password_set"]
    assert (
        rig.client.patch(
            "/api/settings",
            json={
                "qbit_url": "https://example.test",
            },
            headers=AUTH,
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
            **AUTH,
            "Content-Type": "multipart/form-data; boundary=demo",
        },
    )
    assert response.status_code == 413, response.text


@pytest.mark.parametrize("modern_auth", [False, True])
def test_qbittorrent_connection_check_and_auth_refresh(rig, modern_auth):
    rig.fake.modern_auth = modern_auth
    rig.fake.expire_once = True
    response = rig.client.post("/api/settings/test-qbittorrent", headers=AUTH)
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
