import base64
import io
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import httpx
import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import TALB, TPE2
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from PIL import Image

from app.core.config import Deployment
from app.main import create_app
from app.services.qbittorrent import QBittorrent

DIGEST = "a" * 40
MAGNET = f"magnet:?xt=urn:btih:{DIGEST}&dn=Synthetic"
TOKEN = "test-token-" + "x" * 32
PASSWORD = "test-password-" + "x" * 20
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def epub_bytes(
    *, title="A Small Orbit", author="Ada Reed", extra=None, cover=False, mimetype="standard"
):
    image = io.BytesIO()
    if cover:
        Image.new("RGB", (24, 32), "#643bd1").save(image, "PNG")
    data = io.BytesIO()
    with ZipFile(data, "w", compression=ZIP_DEFLATED) as archive:
        if mimetype in {"standard", "compressed", "invalid"}:
            marker = "application/epub+zip"
            if mimetype == "invalid":
                marker += "\n"
            archive.writestr(
                "mimetype", marker, ZIP_DEFLATED if mimetype == "compressed" else ZIP_STORED
            )
        archive.writestr(
            "META-INF/container.xml",
            """
            <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
            <rootfiles><rootfile full-path="book/package.opf"
              media-type="application/oebps-package+xml"/></rootfiles></container>
        """,
        )
        cover_item = (
            '<item id="cv" href="cover.png" media-type="image/png" properties="cover-image"/>'
            if cover
            else ""
        )
        archive.writestr(
            "book/package.opf",
            f"""
            <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
            <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
               <dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>
               <dc:identifier>urn:isbn:978-1-2345-6789-7</dc:identifier>
               <dc:language>en</dc:language>
               <meta property="belongs-to-collection">Example Orbit</meta>
            </metadata>
            <manifest><item id="c" href="chapter.xhtml" media-type="application/xhtml+xml"/>
               {cover_item}</manifest>
            <spine><itemref idref="c"/></spine></package>
        """,
        )
        archive.writestr("book/chapter.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"/>')
        if cover:
            archive.writestr("book/cover.png", image.getvalue())
        if extra:
            archive.writestr(*extra)
        if mimetype == "late":
            archive.writestr("mimetype", "application/epub+zip", ZIP_STORED)
    return data.getvalue()


def write_audio(path: Path, *, title="A Small Orbit", author="Ada Reed") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".m4b":
        # 50 ms of generated AAC silence in an MP4 container. No recorded media.
        encoded = (
            "AAAAHGZ0eXBNNEEgAAACAE00QSBpc29taXNvMgAAAAhmcmVlAAAAJW1kYXTeAgBMYXZjNjIuMjguMTAwAAIwQA4BGCAH"
            "ARggBwAAAwdtb292AAAAbG12aGQAAAAAAAAAAAAAAAAAAAPoAAAAMwABAAABAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAA"
            "AAAAAAEAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAACMXRyYWsAAABcdGtoZAAA"
            "AAMAAAAAAAAAAAAAAAEAAAAAAAAAMwAAAAAAAAAAAAAAAQEAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAA"
            "AEAAAAAAAAAAAAAAAAAAACRlZHRzAAAAHGVsc3QAAAAAAAAAAQAAADIAAAQAAAEAAAAAAaltZGlhAAAAIG1kaGQAAAAA"
            "AAAAAAAAAAAAAFYiAAAIT1XEAAAAAAAtaGRscgAAAAAAAAAAc291bgAAAAAAAAAAAAAAAFNvdW5kSGFuZGxlcgAAAAFU"
            "bWluZgAAABBzbWhkAAAAAAAAAAAAAAAkZGluZgAAABxkcmVmAAAAAAAAAAEAAAAMdXJsIAAAAAEAAAEYc3RibAAAAGpz"
            "dHNkAAAAAAAAAAEAAABabXA0YQAAAAAAAAABAAAAAAAAAAAAAQAQAAAAAFYiAAAAAAA2ZXNkcwAAAAADgICAJQABAASA"
            "gIAXQBUAAAAAAQ2IAAAJZQWAgIAFE4hW5QAGgICAAQIAAAAgc3R0cwAAAAAAAAACAAAAAgAABAAAAAABAAAATwAAABxz"
            "dHNjAAAAAAAAAAEAAAABAAAAAwAAAAEAAAAgc3RzegAAAAAAAAAAAAAAAwAAABUAAAAEAAAABAAAABRzdGNvAAAAAAAA"
            "AAEAAAAsAAAAGnNncGQBAAAAcm9sbAAAAAIAAAAB//8AAAAcc2JncAAAAAByb2xsAAAAAQAAAAMAAAABAAAAYnVkdGEA"
            "AABabWV0YQAAAAAAAAAhaGRscgAAAAAAAAAAbWRpcmFwcGwAAAAAAAAAAAAAAAAtaWxzdAAAACWpdG9vAAAAHWRhdGEA"
            "AAABAAAAAExhdmY2Mi4xMi4xMDA="
        )
        path.write_bytes(base64.b64decode(encoded))
        media = MP4(path)
        media["©alb"] = [title]
        media["©ART"] = [author]
        media["----:com.apple.iTunes:NARRATOR"] = [b"Sample Narrator"]
    else:
        # MPEG-1 Layer III headers with silent frames.
        path.write_bytes((bytes.fromhex("fffb9064") + bytes(413)) * 14)
        media = MP3(path)
        media.add_tags()
        media.tags.add(TALB(text=[title]))
        media.tags.add(TPE2(text=[author]))
    media.save()
    return path


class FakeQBittorrent:
    def __init__(self):
        self.torrents = {}
        self.categories = {}
        self.file_entries = {}
        self.adds = 0
        self.submitted_magnets = []
        self.logins = 0
        self.requests = []
        self.break_after_add = False
        self.add_response = None
        self.modern_auth = False
        self.expire_once = False

    def client(self, settings):
        return QBittorrent(settings, transport=httpx.MockTransport(self.respond))

    def respond(self, request):
        action = request.url.path.removeprefix("/api/v2/")
        self.requests.append((request.method, action))
        assert request.headers["referer"] == "https://qbittorrent.example.test/"
        cookie = "QBT_SID_18022=test" if self.modern_auth else "SID=test"
        if action == "auth/login":
            self.logins += 1
            assert request.content == b"username=example&password=synthetic-password"
            return httpx.Response(
                204 if self.modern_auth else 200,
                text="" if self.modern_auth else "Ok.",
                headers={"Set-Cookie": f"{cookie}; Path=/"},
            )
        assert request.headers["cookie"] == cookie
        if self.expire_once:
            self.expire_once = False
            return httpx.Response(403)
        if action == "app/webapiVersion":
            return httpx.Response(200, text="2.11.3")
        if action == "torrents/info":
            digest = request.url.params["hashes"]
            return httpx.Response(
                200, json=[self.torrents[digest]] if digest in self.torrents else []
            )
        if action == "torrents/categories":
            return httpx.Response(200, json=self.categories)
        if action == "torrents/createCategory":
            assert request.content == b"category=shelfshuttle"
            self.categories["shelfshuttle"] = {"name": "shelfshuttle"}
            return httpx.Response(200)
        if action == "torrents/add":
            self.adds += 1
            assert b"\r\nmagnet:?xt=urn:btih:" in request.content
            assert b"\r\nshelfshuttle\r\n" in request.content
            magnet = request.content.split(b'name="urls"\r\n\r\n')[1].split(b"\r\n")[0].decode()
            self.submitted_magnets.append(magnet)
            tag = request.content.split(b'name="tags"\r\n\r\n')[1].split(b"\r\n")[0].decode()
            response = self.add_response or httpx.Response(200, text="Ok.")
            if response.is_success and response.text != "Fails.":
                self.torrents[DIGEST] = torrent("metaDL", 0, tags=tag)
            if self.break_after_add:
                self.break_after_add = False
                raise httpx.ReadTimeout("Simulate uncertain submission.")
            return response
        if action == "torrents/files":
            return httpx.Response(200, json=self.file_entries[request.url.params["hash"]])
        raise AssertionError(f"Unexpected qBittorrent request: {action}")

    def complete(self, download_root, paths, *, tags=""):
        self.torrents[DIGEST] = torrent("stalledUP", 1, tags=tags)
        self.file_entries[DIGEST] = [
            {
                "name": path.relative_to(download_root).as_posix(),
                "size": path.stat().st_size,
                "progress": 1,
                "priority": 1,
            }
            for path in paths
        ]


def torrent(state, progress, **extra):
    return {
        "hash": DIGEST,
        "category": "shelfshuttle",
        "save_path": "/srv/downloads",
        "state": state,
        "progress": progress,
        **extra,
    }


@pytest.fixture
def legacy_pair_routing(monkeypatch):
    from app.api import jobs
    from app.services.importing import destinations
    from app.workers import worker

    def previous_destinations(settings, kind, author, title):
        paths = destinations(settings, kind, author, title)
        if kind == "combined":
            paths.update(destinations(settings, "ebook", author, title))
        return paths

    @contextmanager
    def use_legacy_routing():
        with monkeypatch.context() as patch:
            patch.setattr(jobs, "destinations", previous_destinations)
            patch.setattr(worker, "destinations", previous_destinations)
            yield

    return use_legacy_routing


@pytest.fixture
def rig(tmp_path):
    root = tmp_path.resolve()
    downloads = root / "downloads"
    downloads.mkdir()
    deployment = Deployment(
        data=root / "state",
        downloads_mount=downloads,
        intake_mount=root / "intake",
        audiobook_mount=root / "audiobooks",
        ebook_mount=root / "ebooks",
        admin_password=PASSWORD,
        api_token=TOKEN,
        cookie_secure=False,
        max_upload_mib=1,
        worker_enabled=False,
    )
    fake = FakeQBittorrent()
    overrides = {
        "qbit_url": "https://qbittorrent.example.test",
        "qbit_username": "example",
        "qbit_password": "synthetic-password",
        "remote_download_root": "/srv/downloads",
    }

    def app_factory():
        return create_app(deployment, overrides=overrides, worker_client=fake.client)

    def submit(client, kind="ebook", *, epub=None, magnet=None, **fields):
        form = {"format": kind, "author": "Ada Reed", "title": "A Small Orbit", **fields}
        if kind in {"audio", "combined"}:
            form["torrent"] = DIGEST if magnet is None else magnet
        files = {}
        if kind in {"ebook", "combined"}:
            name, data = epub or ("Orbit.epub", epub_bytes())
            files["epub"] = (name, data, "application/epub+zip")
        return client.post("/api/jobs", data=form, files=files, headers=AUTH)

    app = app_factory()
    with TestClient(app) as client:
        yield SimpleNamespace(
            root=root,
            downloads=downloads,
            deployment=deployment,
            fake=fake,
            app_factory=app_factory,
            client=client,
            tick=app.state.worker.tick,
            submit=lambda *args, **kwargs: submit(client, *args, **kwargs),
            state=app.state,
        )


def get_job(rig, job_id):
    response = rig.client.get(f"/api/jobs/{job_id}", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def action(rig, job_id, intent="retry", **fields):
    response = rig.client.post(
        f"/api/jobs/{job_id}/action", headers=AUTH, json={"intent": intent, **fields}
    )
    assert response.status_code == 200, response.text
    return response.json()


def configure(rig, **fields):
    response = rig.client.patch("/api/settings", json=fields, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()
