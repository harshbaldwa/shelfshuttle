import base64
import binascii
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from app import __version__
from app.core.config import Settings


class TorrentError(Exception):
    pass


class TorrentUnavailable(TorrentError):
    pass


def info_hash(value: str) -> str:
    if re.fullmatch(r"[0-9a-fA-F]{40}", value):
        return value.lower()
    if re.fullmatch(r"[A-Za-z2-7]{32}", value):
        return base64.b32decode(value, casefold=True).hex()
    raise ValueError(
        "Enter a BitTorrent v1 magnet or a 40-character hex / 32-character base32 hash."
    )


def torrent_input(value: str) -> tuple[str, str | None]:
    value = value.strip()
    if len(value) > 4000 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("The torrent link is too long or contains control characters.")
    if not value.lower().startswith("magnet:"):
        return info_hash(value), None
    url = urlsplit(value)
    if url.scheme != "magnet" or url.netloc or url.path or url.fragment:
        raise ValueError("Use a valid magnet URI.")
    params = parse_qsl(url.query, keep_blank_values=True, max_num_fields=50)
    if any(
        any(ord(char) < 32 or ord(char) == 127 for char in item) for pair in params for item in pair
    ):
        raise ValueError("The magnet contains invalid control characters.")
    hashes = set()
    for name, val in params:
        if name == "xt" and val.lower().startswith("urn:btih:"):
            try:
                hashes.add(info_hash(val[9:]))
            except (ValueError, binascii.Error) as exc:
                raise ValueError("The magnet contains an invalid BitTorrent v1 hash.") from exc
    if len(hashes) != 1:
        raise ValueError("The magnet must identify exactly one BitTorrent v1 info hash.")
    digest = hashes.pop()
    # Only preserve display name and trackers. Web seeds and alternate sources are unsupported.
    extras = [(key, val) for key, val in params if key in {"dn", "tr"}]
    return digest, "magnet:?" + urlencode([("xt", f"urn:btih:{digest}"), *extras], safe=":")


class QBittorrent:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.client = httpx.Client(
            base_url=settings.qbit_url + "/api/v2/",
            headers={
                "Referer": settings.qbit_url + "/",
                "User-Agent": f"ShelfShuttle/{__version__}",
            },
            timeout=httpx.Timeout(10, connect=5),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def __enter__(self):
        try:
            if not self.settings.qbit_url:
                raise TorrentError("Configure the qBittorrent URL in Settings.")
            if self.has_credentials:
                self.login()
            return self
        except BaseException:
            self.client.close()
            raise

    def __exit__(self, *_):
        self.client.close()

    @property
    def has_credentials(self) -> bool:
        return bool(self.settings.qbit_username or self.settings.qbit_password.get_secret_value())

    def login(self) -> None:
        self.client.cookies.clear()
        response = self.request(
            "POST",
            "auth/login",
            data={
                "username": self.settings.qbit_username,
                "password": self.settings.qbit_password.get_secret_value(),
            },
        )
        cookie = any(
            entry.value and (entry.name == "SID" or re.fullmatch(r"QBT_SID_[0-9]+", entry.name))
            for entry in self.client.cookies.jar
        )
        if not cookie or (response.status_code != 204 and response.text.strip() != "Ok."):
            raise TorrentError("qBittorrent rejected the configured credentials.")

    def request(self, method: str, route: str, *, retry_auth: bool = True, **kwargs):
        try:
            response = self.client.request(method, route, **kwargs)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise TorrentUnavailable(
                "qBittorrent is unreachable. Retrying on the next poll."
            ) from exc
        if response.status_code in {401, 403}:
            if retry_auth and route != "auth/login" and self.has_credentials:
                self.login()
                return self.request(method, route, retry_auth=False, **kwargs)
            raise TorrentError("qBittorrent denied access. Check credentials and Web UI settings.")
        if response.status_code >= 500 or response.status_code == 429:
            raise TorrentUnavailable(
                "qBittorrent is temporarily unavailable. Retrying on the next poll."
            )
        if not response.is_success:
            raise TorrentError(f"qBittorrent returned HTTP {response.status_code}.")
        return response

    def data(self, route: str, kind, **params):
        try:
            data = self.request("GET", route, params=params).json()
        except ValueError as exc:
            raise TorrentError("qBittorrent returned an invalid API response.") from exc
        if not isinstance(data, kind):
            raise TorrentError("qBittorrent returned an unexpected API response.")
        return data

    def test(self) -> str:
        version = self.request("GET", "app/webapiVersion").text.strip()
        if not re.fullmatch(r"2\.[0-9]+(?:\.[0-9]+)?", version):
            raise TorrentError("The URL does not serve a supported qBittorrent Web API.")
        return version

    def find(self, digest: str) -> dict | None:
        for torrent in self.data("torrents/info", list, hashes=digest):
            if isinstance(torrent, dict) and str(torrent.get("hash", "")).lower() == digest:
                return torrent
        return None

    def add(self, magnet: str, tag: str) -> None:
        category = self.settings.qbit_category
        if category not in self.data("torrents/categories", dict):
            try:
                self.request("POST", "torrents/createCategory", data={"category": category})
            except TorrentError:
                if category not in self.data("torrents/categories", dict):
                    raise
        response = self.request(
            "POST",
            "torrents/add",
            files={
                "urls": (None, magnet),
                "category": (None, category),
                "tags": (None, tag),
                "savepath": (None, self.settings.remote_download_root),
                "autoTMM": (None, "false"),
            },
        )
        body = response.text.strip()
        if body in {"Ok.", ""}:
            return
        if body == "Fails.":
            raise TorrentError("qBittorrent rejected the magnet submission.")
        # Web API 2.14+ reports the result as counts, including asynchronous acceptance.
        try:
            result = response.json()
        except ValueError:
            result = None
        if isinstance(result, dict):
            added, pending, failed = (
                result.get(key) for key in ("success_count", "pending_count", "failure_count")
            )
            if all(type(count) is int and count >= 0 for count in (added, pending, failed)):
                if added or pending:
                    return
                if failed:
                    raise TorrentError("qBittorrent rejected the magnet submission.")
        # A successful HTTP request may have queued the torrent even if its body is unfamiliar.
        raise TorrentUnavailable(
            "qBittorrent returned an unrecognized submission response. Checking for the torrent."
        )

    def files(self, digest: str) -> list[dict]:
        files = self.data("torrents/files", list, hash=digest)
        if not all(isinstance(item, dict) for item in files):
            raise TorrentError("qBittorrent returned invalid torrent file entries.")
        return files
