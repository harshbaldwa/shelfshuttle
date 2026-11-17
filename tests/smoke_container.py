"""Run against a disposable Compose service from inside its container."""

import io
import os
import stat
import time
from pathlib import Path
from zipfile import ZipFile

import httpx


def sample_epub() -> bytes:
    data = io.BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        archive.writestr(
            "book.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>Test Book</dc:title><dc:creator>Test Author</dc:creator></metadata>"
            '<manifest><item id="page" href="page.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="page"/></spine></package>',
        )
        archive.writestr("page.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"/>')
    return data.getvalue()


def main() -> None:
    uid, gid = os.getuid(), os.getgid()
    assert uid > 0 and gid > 0
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/1/status").read_text().splitlines()
        if ":" in line
    )
    assert status["Uid"].split() == [str(uid)] * 4
    assert status["Gid"].split() == [str(gid)] * 4
    assert int(status["CapEff"], 16) == int(status["CapPrm"], 16) == 0
    assert status["NoNewPrivs"].strip() == "1"

    state = Path("/app/data")
    assert state.stat().st_uid == uid and state.stat().st_gid == gid
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / ".key").stat().st_mode) == 0o600

    with httpx.Client(base_url="http://127.0.0.1:8080") as client:
        assert client.get("/healthz").status_code == 200
        settings = client.get("/api/settings").raise_for_status().json()
        for kind, path in (("audiobook", "audiobooks"), ("ebook", "books")):
            root = f"/media/books/{path}"
            assert settings[f"{kind}_library"] == root
            assert settings[f"{kind}_staging"] == f"{root}/.shelfshuttle"

        book = {"author": "Test Author", "title": "Test Book"}
        preview = client.get("/api/destinations", params={**book, "format": "combined"})
        assert preview.raise_for_status().json() == {
            "audio": "/media/books/audiobooks/Test Author/Test Book"
        }

        csrf = client.get("/api/csrf").raise_for_status()
        content = sample_epub()
        job = (
            client.post(
                "/api/jobs",
                data={**book, "format": "ebook"},
                files={"epub": ("test.epub", content, "application/epub+zip")},
                headers={
                    "X-CSRF-Token": csrf.json()["token"],
                    "Cookie": csrf.headers["set-cookie"].split(";", 1)[0],
                },
            )
            .raise_for_status()
            .json()
        )
        for _ in range(30):
            job = client.get(f"/api/jobs/{job['id']}").raise_for_status().json()
            if job["status"] != "queued" and job["status"] != "importing":
                break
            time.sleep(1)

        assert job["status"] == "complete", job
        library = Path("/media/books/books")
        assert (
            library / "Test Author/Test Book/Test Author - Test Book.epub"
        ).read_bytes() == content
        assert not list((library / ".shelfshuttle").iterdir())
        for _ in range(10):
            job = client.get(f"/api/jobs/{job['id']}").raise_for_status().json()
            if job["can_forget"]:
                break
            time.sleep(1)
        assert job["can_forget"], job
        assert not (Path("/app/intake") / job["id"]).exists()


if __name__ == "__main__":
    main()
