# Configuration

ShelfShuttle reads deployment settings from the environment on startup. Runtime settings use this precedence from highest to lowest: environment, persisted settings, defaults. Settings supplied through environment variables are read-only in the UI. Restart the application after changing environment values.

## Deployment variables

All paths are paths inside ShelfShuttle unless identified as Compose source paths.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SHELFSHUTTLE_ADMIN_USERNAME` | `admin` | Admin login name. |
| `SHELFSHUTTLE_ADMIN_PASSWORD` | Required | Unique password of at least 16 characters. |
| `SHELFSHUTTLE_API_TOKEN` | Empty | Optional bearer token of at least 32 printable ASCII characters. |
| `SHELFSHUTTLE_COOKIE_SECURE` | `true` | Send the session cookie over HTTPS. Set `false` for loopback HTTP development. |
| `SHELFSHUTTLE_MAX_UPLOAD_MIB` | `64` | Compressed EPUB size limit, from 1 to 512 MiB. Set a matching request limit and timeout in your proxy. |
| `SHELFSHUTTLE_DATA` | `/app/data` | State directory for SQLite, encryption key, covers, and the worker lock. It needs a local filesystem and write access restricted to the process owner. |
| `SHELFSHUTTLE_DOWNLOADS_MOUNT` | `/downloads` | Allowed download mount root. Mount read-only. |
| `SHELFSHUTTLE_INTAKE_MOUNT` | `/app/intake` | Allowed EPUB intake mount root. |
| `SHELFSHUTTLE_AUDIOBOOK_MOUNT` | `/media/audiobooks` | Allowed audiobook library and staging mount root. |
| `SHELFSHUTTLE_EBOOK_MOUNT` | `/media/ebooks` | Allowed ebook library and staging mount root. |
| `SHELFSHUTTLE_WORKER_ENABLED` | `true` | Run the background worker. With the worker disabled, the UI and API can still queue jobs. |
| `SHELFSHUTTLE_PUID`, `SHELFSHUTTLE_PGID` | `10001`, `10001` | Build and run the Compose service with these non-root IDs. Use IDs that can access your bind mounts. |
| `SHELFSHUTTLE_BIND`, `SHELFSHUTTLE_PORT` | `127.0.0.1`, `8080` | Compose loopback address and port. Use the proxy override for HTTPS. |
| `SHELFSHUTTLE_{DATA,INTAKE,DOWNLOADS,AUDIOBOOK,EBOOK}_SOURCE` | Corresponding named volume | Compose volume name or absolute host path. |
| `SHELFSHUTTLE_PROXY_NETWORK` | Unset | Name of the existing external network when applying the proxy override. |

For `ADMIN_PASSWORD`, `API_TOKEN`, and `QBIT_PASSWORD`, you can set the corresponding `SHELFSHUTTLE_…_FILE` variable to a readable, mounted secret file instead of passing the value through the environment. For example, attach a secret from a Compose override and set `SHELFSHUTTLE_ADMIN_PASSWORD_FILE=/run/secrets/shelfshuttle_admin`. Remove the direct password variable, even if it was empty. Protect the host files and any backups.

Choose your UID and GID before creating the named volumes. Docker initializes fresh volumes from the image. If you change IDs on an existing deployment, migrate ownership of its state and intake storage before starting the rebuilt service.

## Runtime settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `SHELFSHUTTLE_QBIT_URL` | Empty | qBittorrent Web UI base URL, including a proxy subpath if applicable. |
| `SHELFSHUTTLE_QBIT_USERNAME`, `SHELFSHUTTLE_QBIT_PASSWORD` | Empty | qBittorrent Web UI credentials. This integration uses the Web UI login cookie. |
| `SHELFSHUTTLE_QBIT_CATEGORY` | `shelfshuttle` | Dedicated category for new submissions. |
| `SHELFSHUTTLE_REMOTE_DOWNLOAD_ROOT` | `/downloads` | qBittorrent-side absolute prefix and save path for new torrents. |
| `SHELFSHUTTLE_DOWNLOAD_ROOT` | Downloads mount root | ShelfShuttle-side directory corresponding to the remote prefix. |
| `SHELFSHUTTLE_INTAKE_ROOT` | Intake mount root | Directory for received EPUBs. |
| `SHELFSHUTTLE_AUDIOBOOK_LIBRARY` | `<audiobook mount>/library` | Audiobook library watched by your media app. |
| `SHELFSHUTTLE_AUDIOBOOK_STAGING` | `<audiobook mount>/.shelfshuttle` | Staging sibling on the same mount as the audiobook library. |
| `SHELFSHUTTLE_EBOOK_LIBRARY` | `<ebook mount>/library` | Ebook library watched by your media app. |
| `SHELFSHUTTLE_EBOOK_STAGING` | `<ebook mount>/.shelfshuttle` | Staging sibling on the same mount as the ebook library. |
| `SHELFSHUTTLE_FOLDER_TEMPLATE` | `{author}/{title}` | Relative folder template with both fields. For example, `{author} - {title}`. |
| `SHELFSHUTTLE_FILE_MODE`, `SHELFSHUTTLE_DIRECTORY_MODE` | `0644`, `0755` | Permissions for new media files and folders. |
| `SHELFSHUTTLE_OWNER_UID`, `SHELFSHUTTLE_OWNER_GID` | Unset | Optional ownership of newly created media. A non-root process can only change groups it belongs to and normally cannot assign another UID. Prefer setting the container UID/GID directly. |
| `SHELFSHUTTLE_POLL_SECONDS` | `5` | Delay between worker polling cycles, from 2 to 300 seconds. |
| `SHELFSHUTTLE_METADATA_PROVIDER` | `embedded` | Read metadata from uploaded and downloaded files. |
| `SHELFSHUTTLE_DRY_RUN` | `false` | Validate sources and check for target conflicts without submitting torrents or publishing library files. Existing torrents can be inspected. Dry-run jobs retain their intake files and job records. |

Use a dedicated category per ShelfShuttle deployment and a directory writable by qBittorrent. ShelfShuttle requests that path explicitly for new submissions. A 40-character hex or 32-character base32 v1 hash tracks an existing torrent or starts a new one with a trackerless magnet. Completion depends on peer discovery and availability. Provide the full magnet when the torrent needs its trackers. The integration uses the [qBittorrent Web API](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-%28qBittorrent-5.0%29), accepts the [session cookie used by recent versions](https://github.com/qbittorrent/qBittorrent/blob/v5_2_x/WebAPI_Changelog.md), and uses the [BitTorrent magnet format](https://www.bittorrent.org/beps/bep_0009.html).

## API

Protected API routes accept the signed session cookie or a configured `Authorization: Bearer <token>` header. Session-authenticated mutations require `X-CSRF-Token` from the page's `csrf-token` meta tag. The health probe at `GET /healthz` returns a minimal status without credentials.

| Method and path | Purpose |
| --- | --- |
| `GET /api/jobs?limit=50&offset=0` | List job summaries. Page size is capped at 100. |
| `POST /api/jobs` | Submit multipart fields `format` (`audio`, `ebook`, `combined`), `author`, `title`, and the applicable `epub` file and/or `torrent`. Audio-only forms may also be URL encoded. |
| `GET /api/jobs/{id}` | Retrieve job status, destinations, progress from 0 to 1, and extracted tags. |
| `GET /api/jobs/{id}/cover` | Retrieve a sanitized JPEG cover when available. |
| `DELETE /api/jobs/{id}` | Remove a queue job and its uploaded EPUB. Returns 204 on success or 409 when the worker is busy or a library import has started. qBittorrent downloads and library files stay in place. |
| `POST /api/jobs/{id}/action` | Send JSON with `intent` set to `retry` or `approve`, optional `author` and `title` for review, and optional `torrent` to supply a full magnet with the original info hash. |
| `GET /api/destinations` | Preview destination paths with query fields `format`, `author`, and `title`. |
| `POST /api/epub/inspect` | Inspect a multipart `epub` and return suggested metadata. |
| `GET /api/settings` | Read current settings and identify environment overrides. Passwords are omitted. |
| `PATCH /api/settings` | Send a JSON object of lower-case runtime setting keys. Set `qbit_password` to an empty string to clear it. |
| `POST /api/settings/test-qbittorrent` | Check access using saved settings and return the Web API version. |

```sh
curl -fsS http://localhost:8080/api/jobs \
  -H "Authorization: Bearer $SHELFSHUTTLE_API_TOKEN" \
  -F format=ebook -F 'author=Example Author' -F 'title=Example Book' \
  -F 'epub=@example.epub;type=application/epub+zip'
```
