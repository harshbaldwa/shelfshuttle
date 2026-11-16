# ShelfShuttle

**From download to bookshelf, without the busywork.**

ShelfShuttle is a self-hosted ebook and audiobook intake manager. Upload an EPUB, track a download in your existing qBittorrent instance, and deliver the finished files to Audiobookshelf or another library manager that reads directories.

## Features

- Add an ebook, an audiobook, or a paired edition. ShelfShuttle suggests author and title from the EPUB and previews folders using the names you confirm.
- Start downloads with a BitTorrent v1 magnet or info hash and track actual qBittorrent progress through restarts.
- Import EPUBs and single M4Bs as `Author - Title`. MP3s keep their original filenames and disc structure. A paired edition puts the EPUB beside the audio in one audiobook library folder.
- Review mismatched tags, existing destinations, and duplicate torrents. Retry failed jobs from the dashboard.
- Validate sources in dry-run mode. Imports stage copies outside watched folders and use an atomic rename or exclusive file copies to protect existing files.
- Run with one authenticated admin, responsive light and dark themes, an optional API token, and non-root Docker defaults.

Use torrents for content you have permission to download and share. ShelfShuttle accepts sources you provide and has no catalog or acquisition integrations.

## Screenshots

The dashboard and Add Book form with a generated EPUB in a temporary library.

![Dashboard showing a completed ebook import](docs/screenshots/dashboard.png)

![Add Book form with extracted metadata and a destination preview](docs/screenshots/add-book.png)

## Install with Docker Compose

Use Docker with Compose v2.24.4 or newer for the optional reverse-proxy override. From the repository checkout:

```sh
umask 077
cp .env.example .env
```

Set `SHELFSHUTTLE_ADMIN_PASSWORD` in `.env` to a unique password of at least 16 characters. For local HTTP use, set `SHELFSHUTTLE_COOKIE_SECURE=false` and leave the default loopback binding. Keep secure cookies enabled behind HTTPS.

```sh
docker compose up -d --build
docker compose ps
```

Open [the local ShelfShuttle UI](http://localhost:8080) and sign in as `admin`. The default named volumes persist state, intake files, and libraries across container recreation. EPUB intake works with the default volumes. To connect an existing download client or library, configure its mounts before importing.

An example host layout gives each library a parent directory with a separate watched subdirectory and staging directory:

```text
/srv/media/audiobooks/
  library/          # watched by the library manager
  .shelfshuttle/    # private import staging, created as needed
/srv/media/ebooks/
  library/          # watched by the library manager
  .shelfshuttle/    # private import staging, created as needed
```

An example `.env` for the library and qBittorrent integration:

```dotenv
SHELFSHUTTLE_DOWNLOADS_SOURCE=/srv/media/downloads
SHELFSHUTTLE_AUDIOBOOK_SOURCE=/srv/media/audiobooks
SHELFSHUTTLE_EBOOK_SOURCE=/srv/media/ebooks
SHELFSHUTTLE_QBIT_URL=https://qbittorrent.example.test
SHELFSHUTTLE_QBIT_USERNAME=books
SHELFSHUTTLE_REMOTE_DOWNLOAD_ROOT=/downloads/books
SHELFSHUTTLE_DOWNLOAD_ROOT=/downloads/books
```

In this example both services see the same downloads under `/downloads/books`. The remote prefix is qBittorrent's view. The mapped download root is ShelfShuttle's view. Set the qBittorrent password in Settings or pass it through `SHELFSHUTTLE_QBIT_PASSWORD` or its `_FILE` variant. Save the settings and run **Test saved connection**. qBittorrent must already be reachable from ShelfShuttle. If it lives on an existing Docker network, attach ShelfShuttle with an override. Leave qBittorrent's Web UI authentication enabled.

For HTTPS through Traefik or another proxy, configure its routing and TLS on an existing external network, use `SHELFSHUTTLE_COOKIE_SECURE=true`, and launch with the port-removing override:

```sh
SHELFSHUTTLE_PROXY_NETWORK=your-proxy-network \
  docker compose -f docker-compose.yaml -f compose.proxy.yaml up -d --build
```

Point the proxy at service `shelfshuttle` on port `8080`. The proxy and qBittorrent can use separate networks. Avoid broad filesystem mounts. Set the process UID/GID to a host identity that already has the access needed for the mounted directories.

## Local development

Use Python 3.12 or newer on Linux or macOS. The commands below keep all files under an ignored, disposable directory in the checkout.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
mkdir -p .local/downloads
export SHELFSHUTTLE_DATA="$PWD/.local/state"
export SHELFSHUTTLE_INTAKE_MOUNT="$PWD/.local/intake"
export SHELFSHUTTLE_DOWNLOADS_MOUNT="$PWD/.local/downloads"
export SHELFSHUTTLE_AUDIOBOOK_MOUNT="$PWD/.local/audiobooks"
export SHELFSHUTTLE_EBOOK_MOUNT="$PWD/.local/ebooks"
export SHELFSHUTTLE_ADMIN_PASSWORD='replace-with-a-local-password'
export SHELFSHUTTLE_COOKIE_SECURE=false
uvicorn app.main:app --host 127.0.0.1 --port 8080 --no-access-log
```

```sh
ruff check .
ruff format --check .
pytest
```

Tests generate synthetic content in temporary directories and use mocked qBittorrent responses. They require no media services, library files, or container daemon.

## Documentation

- [Configuration, deployment, and API](docs/configuration.md)
- [Architecture and recovery model](docs/architecture.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Contributing](CONTRIBUTING.md)
- [Security reporting](SECURITY.md)
- [MIT license](LICENSE)

## Current scope

This release targets a single admin and a single deployment using local SQLite storage. It ships an embedded metadata provider and an interface for future providers. The intake worker accepts BitTorrent v1 magnets and hashes, one EPUB per job, and either one M4B or a set of MP3s. Imports use copies to preserve seeding. Disk capacity and supported filesystem semantics determine whether atomic import is available.
