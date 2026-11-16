import io
import re
import stat
import struct
import unicodedata
import warnings
import zipfile
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import BinaryIO, Protocol
from urllib.parse import unquote, urlsplit

from defusedxml import ElementTree as XML
from defusedxml.common import DefusedXmlException
from mutagen import MutagenError
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from PIL import Image

from app.core.paths import relative_parts


class InvalidMedia(ValueError):
    pass


@dataclass
class BookMetadata:
    title: str = ""
    author: str = ""
    isbn: str = ""
    language: str = ""
    series: str = ""
    narrator: str = ""
    duration_seconds: float | None = None
    chapters: list[dict] = field(default_factory=list)
    cover: bytes | None = field(default=None, repr=False)

    def public(self) -> dict:
        return {key: value for key, value in asdict(self).items() if key != "cover" and value}


class MetadataProvider(Protocol):
    def ebook(self, source: BinaryIO) -> BookMetadata: ...
    def audio(self, source: BinaryIO, suffix: str) -> BookMetadata: ...


MAX_EXPANDED = 256 * 1024**2
MAX_DIRECTORY = 2 * 1024**2
MAX_AUDIO_READ = 24 * 1024**2
MAX_XML = 2 * 1024**2
MAX_COVER = 6 * 1024**2
NS_OPF = "http://www.idpf.org/2007/opf"
NS_DC = "http://purl.org/dc/elements/1.1/"


def clean_tag(value, limit: int = 200) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = "".join(char for char in str(value) if not unicodedata.category(char).startswith("C"))
    return " ".join(text.split())[:limit]


def safe_cover(raw: bytes) -> bytes | None:
    if not raw or len(raw) > MAX_COVER:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw), formats=["JPEG", "PNG", "WEBP", "GIF"]) as image:
                if image.width * image.height > 12_000_000:
                    return None
                image.thumbnail((512, 512))
                clean_image = image.convert("RGB")
                clean_image.info.clear()
                output = io.BytesIO()
                clean_image.save(output, "JPEG", quality=85)
                return output.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None


def _archive_members(archive):
    entries = archive.infolist()
    if not entries or len(entries) > 4096 or sum(info.file_size for info in entries) > MAX_EXPANDED:
        raise InvalidMedia("The EPUB exceeds safe ZIP expansion limits.")
    names = set()
    for info in entries:
        name = info.orig_filename.rstrip("/")
        relative_parts(name, archive=True)
        key = unicodedata.normalize("NFC", name).casefold()
        file_type = stat.S_IFMT(info.external_attr >> 16)
        if (
            key in names
            or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}
            or info.flag_bits & 1
            or info.compress_type not in {0, 8}
            or info.file_size > max(1024**2, info.compress_size * 250)
            or info.orig_filename != info.filename
        ):
            raise InvalidMedia("The EPUB has unsafe, duplicated, or unsupported ZIP entries.")
        names.add(key)
    first = entries[0]
    if first.filename != "mimetype" or first.compress_type != 0 or first.file_size != 20:
        raise InvalidMedia("The ZIP lacks a valid EPUB mimetype entry.")
    if archive.read(first) != b"application/epub+zip" or archive.testzip() is not None:
        raise InvalidMedia("The EPUB has an invalid mimetype or corrupted ZIP data.")


def _check_zip_directory(source: BinaryIO) -> None:
    # Check the end record before ZipFile allocates an object for each member.
    end = source.seek(0, io.SEEK_END)
    start = max(0, end - (65535 + 22))
    source.seek(start)
    tail = source.read()
    index = tail.rfind(b"PK\x05\x06")
    if index < 0 or len(tail) - index < 22:
        raise InvalidMedia("The EPUB ZIP is missing its central directory.")
    _, disk, first_disk, disk_entries, entries, size, offset, comment = struct.unpack_from(
        "<4s4H2IH", tail, index
    )
    if (
        disk
        or first_disk
        or disk_entries != entries
        or not 0 < entries <= 4096
        or not 0 < size <= MAX_DIRECTORY
        or offset + size != start + index
        or index + 22 + comment != len(tail)
    ):
        raise InvalidMedia("The EPUB ZIP central directory is malformed or exceeds safe limits.")


class AudioReadBudget:
    def __init__(self, source: BinaryIO):
        self.source = source
        self.remaining = MAX_AUDIO_READ
        self.seek = source.seek
        self.tell = source.tell

    def read(self, size: int = -1) -> bytes:
        if not 0 <= size <= self.remaining:
            raise InvalidMedia("The audio file exceeds safe metadata parsing limits.")
        data = self.source.read(size)
        self.remaining -= len(data)
        return data


def _read(archive, name: str, limit: int) -> bytes:
    if archive.getinfo(name).file_size > limit:
        raise InvalidMedia("An EPUB metadata resource exceeds its size limit.")
    return archive.read(name)


def _epub_href(package: str, href: str) -> str:
    url = urlsplit(href)
    if url.scheme or url.netloc or url.query:
        raise InvalidMedia("An EPUB resource must reference a local ZIP member.")
    parts = package.split("/")[:-1]
    for item in unquote(url.path).split("/"):
        if item == ".":
            continue
        if item == ".." and parts:
            parts.pop()
        else:
            parts.extend(relative_parts(item, archive=True))
    if not parts:
        raise InvalidMedia("An EPUB resource must reference a file.")
    return "/".join(parts)


def _ebook(archive) -> BookMetadata:
    _archive_members(archive)
    container = XML.fromstring(_read(archive, "META-INF/container.xml", MAX_XML))
    root = container.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
    if root is None or root.get("media-type") != "application/oebps-package+xml":
        raise InvalidMedia("The EPUB is missing a package document.")
    package = "/".join(relative_parts(root.attrib["full-path"], archive=True))
    opf = XML.fromstring(_read(archive, package, MAX_XML))
    metadata = opf.find(f"{{{NS_OPF}}}metadata")
    if opf.tag != f"{{{NS_OPF}}}package" or metadata is None:
        raise InvalidMedia("The EPUB package is missing its metadata.")
    manifest = {
        item.get("id"): item for item in opf.findall(f"{{{NS_OPF}}}manifest/{{{NS_OPF}}}item")
    }
    spine = opf.findall(f"{{{NS_OPF}}}spine/{{{NS_OPF}}}itemref")
    if not spine:
        raise InvalidMedia("The EPUB has no reading order.")
    for item in spine:
        resource = manifest[item.attrib["idref"]]
        if archive.getinfo(_epub_href(package, resource.attrib["href"])).is_dir():
            raise InvalidMedia("An EPUB reading-order resource is a directory.")

    def dc(tag: str) -> str:
        return clean_tag(metadata.findtext(f"{{{NS_DC}}}{tag}", ""))

    identifiers = [
        clean_tag(item.text or "") for item in metadata.findall(f"{{{NS_DC}}}identifier")
    ]
    isbn = ""
    for value in identifiers:
        code = re.sub(r"[ -]", "", re.sub(r"(?i)^urn:isbn:", "", value))
        if re.fullmatch(r"(?:97[89][0-9]{10}|[0-9]{9}[0-9Xx])", code):
            isbn = code.upper()
            break
    series = ""
    cover_id = None
    for item in metadata.findall(f"{{{NS_OPF}}}meta"):
        if item.get("name") == "calibre:series":
            series = clean_tag(item.get("content", ""))
        if item.get("property") == "belongs-to-collection" and not series:
            series = clean_tag(item.text or "")
        if item.get("name") == "cover":
            cover_id = item.get("content")
    cover_item = next(
        (item for item in manifest.values() if "cover-image" in item.get("properties", "").split()),
        manifest.get(cover_id),
    )
    cover = None
    if cover_item is not None:
        try:
            name = _epub_href(package, cover_item.attrib["href"])
            cover = safe_cover(_read(archive, name, MAX_COVER))
        except (ValueError, KeyError):
            pass
    return BookMetadata(
        title=dc("title"),
        author=dc("creator"),
        isbn=isbn,
        language=dc("language"),
        series=series,
        cover=cover,
    )


def _audio_tags(media, suffix: str) -> BookMetadata:
    tags = media.tags or {}
    if suffix == ".m4b":

        def first(*keys):
            return next((clean_tag(tags[key][0]) for key in keys if tags.get(key)), "")

        images = tags.get("covr", [])
        return BookMetadata(
            title=first("©alb", "©nam"),
            author=first("aART", "©ART", "©wrt"),
            narrator=first("----:com.apple.iTunes:NARRATOR", "©nrt"),
            duration_seconds=media.info.length,
            chapters=[
                {"start_seconds": chapter.start, "title": clean_tag(chapter.title)}
                for chapter in (media.chapters or [])[:1000]
            ],
            cover=safe_cover(images[0]) if images else None,
        )

    def first_mp3(*keys):
        return next((clean_tag(tags[key].text[0]) for key in keys if tags.get(key)), "")

    chapters = tags.getall("CHAP")[:1000] if isinstance(tags, ID3) else []
    chapters.sort(key=lambda chapter: chapter.start_time)
    images = tags.getall("APIC") if isinstance(tags, ID3) else []
    cover = next((image for image in images if image.type == 3), images[0] if images else None)
    return BookMetadata(
        title=first_mp3("TALB"),
        author=first_mp3("TPE2", "TPE1", "TCOM"),
        narrator=first_mp3("TXXX:NARRATOR", "TXXX:Narrator"),
        duration_seconds=media.info.length,
        chapters=[
            {
                "start_seconds": chapter.start_time / 1000,
                "title": clean_tag(str(chapter.sub_frames.get("TIT2", ""))),
            }
            for chapter in chapters
        ],
        cover=safe_cover(cover.data) if cover else None,
    )


class EmbeddedMetadata:
    def ebook(self, source: BinaryIO) -> BookMetadata:
        try:
            _check_zip_directory(source)
            with zipfile.ZipFile(source) as archive:
                return _ebook(archive)
        except InvalidMedia:
            raise
        except (
            ValueError,
            KeyError,
            XML.ParseError,
            DefusedXmlException,
            zipfile.BadZipFile,
            RuntimeError,
            EOFError,
            OSError,
            zlib.error,
        ) as exc:
            raise InvalidMedia("The upload is not a valid, supported EPUB.") from exc

    def audio(self, source: BinaryIO, suffix: str) -> BookMetadata:
        try:
            reader = AudioReadBudget(source)
            media = MP4(reader) if suffix == ".m4b" else MP3(reader)
            if media.info.length <= 0 or not media.info.channels:
                raise InvalidMedia("The audio file has no playable audio stream.")
            return _audio_tags(media, suffix)
        except (MutagenError, ValueError, IndexError, TypeError, EOFError, OSError) as exc:
            raise InvalidMedia(
                "A downloaded audio file has invalid or unsupported audio data."
            ) from exc


PROVIDERS: dict[str, MetadataProvider] = {"embedded": EmbeddedMetadata()}


def mismatches(book: BookMetadata, author: str, title: str) -> list[str]:
    def words(value: str) -> str:
        return "".join(
            char for char in unicodedata.normalize("NFKC", value).casefold() if char.isalnum()
        )

    return [
        label
        for label, given, embedded in (
            ("title", title, book.title),
            ("author", author, book.author),
        )
        if embedded and words(given) != words(embedded)
    ]


def aggregate_audio(tracks: list[tuple[Path, BookMetadata]]) -> dict:
    offset = 0.0
    chapters = []
    result: dict = {}
    for name, track in tracks:
        for field_name in ("title", "author", "narrator"):
            value = getattr(track, field_name)
            if value:
                result.setdefault(field_name, value)
        for chapter in track.chapters:
            if len(chapters) == 1000:
                break
            chapters.append(
                {
                    **chapter,
                    "start_seconds": round(offset + chapter["start_seconds"], 3),
                    "file": str(name),
                }
            )
        offset += track.duration_seconds or 0
    return {
        **result,
        "duration_seconds": round(offset, 3),
        "chapters": chapters,
        "tracks": [str(name) for name, _ in tracks],
    }
