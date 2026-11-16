# Configuration

ShelfShuttle reads deployment settings from the environment on startup. Runtime settings use this precedence from highest to lowest: environment, persisted settings, defaults. Settings supplied through environment variables are read-only in the UI. Restart the application after changing environment values.

## Deployment variables

All paths are paths inside ShelfShuttle unless identified as Compose source paths.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SHELFSHUTTLE_COOKIE_SECURE` | `true` | Send the CSRF cookie over HTTPS. Set `false` for loopback HTTP development. |
| `SHELFSHUTTLE_MAX_UPLOAD_MIB` | `64` | Compressed EPUB size limit, from 1 to 512 MiB. Set a matching request limit and timeout in your proxy. |
| `SHELFSHUTTLE_DATA` | `/app/data` | State directory for SQLite, encryption key, covers, and the worker lock. It needs a local filesystem and write access restricted to the process owner. |
| `SHELFSHUTTLE_DOWNLOADS_MOUNT` | `/downloads` | Allowed download mount root. Mount read-only. |
| `SHELFSHUTTLE_INTAKE_MOUNT` | `/app/intake` | Allowed EPUB intake mount root. |
| `SHELFSHUTTLE_AUDIOBOOK_MOUNT` | `/media/audiobooks` | Allowed audiobook library and staging mount root. |
| `SHELFSHUTTLE_EBOOK_MOUNT` | `/media/ebooks` | Allowed ebook library and staging mount root. |
| `SHELFSHUTTLE_WORKER_ENABLED` | `true` | Run the background worker. With the worker disabled, the UI and API can still queue jobs. |
| `SHELFSHUTTLE_PUID`, `SHELFSHUTTLE_PGID` | `10001`, `10001` | Build and run the Compose service with these non-root IDs. Use IDs that can access your bind mounts. |
| `SHELFSHUTTLE_{DATA,INTAKE,DOWNLOADS,AUDIOBOOK,EBOOK}_SOURCE` | Corresponding named volume | Compose volume name or absolute host path. |
| `SHELFSHUTTLE_PROXY_NETWORK` | Unset | Name of the existing external network when applying the proxy override. |

Set `SHELFSHUTTLE_QBIT_PASSWORD_FILE` to a readable, mounted secret file if you prefer to supply the qBittorrent password that way. Remove the direct password variable before setting the file path.

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
| `SHELFSHUTTLE_EBOOK_LIBRARY` | `<ebook mount>/library` | Destination for ebook-only jobs. |
| `SHELFSHUTTLE_EBOOK_STAGING` | `<ebook mount>/.shelfshuttle` | Staging sibling on the same mount as the ebook library. |
| `SHELFSHUTTLE_FOLDER_TEMPLATE` | `{author}/{title}` | Relative folder template with both fields. For example, `{author} - {title}`. |
| `SHELFSHUTTLE_FILE_MODE`, `SHELFSHUTTLE_DIRECTORY_MODE` | `0644`, `0755` | Permissions for new media files and folders. |
| `SHELFSHUTTLE_OWNER_UID`, `SHELFSHUTTLE_OWNER_GID` | Unset | Optional ownership of newly created media. A non-root process can only change groups it belongs to and normally cannot assign another UID. Prefer setting the container UID/GID directly. |
| `SHELFSHUTTLE_POLL_SECONDS` | `5` | Delay between worker polling cycles, from 2 to 300 seconds. |
| `SHELFSHUTTLE_METADATA_PROVIDER` | `embedded` | Read metadata from uploaded and downloaded files. |
| `SHELFSHUTTLE_DRY_RUN` | `false` | Validate sources and check for target conflicts without submitting torrents or publishing library files. Existing torrents can be inspected. Dry-run jobs retain their intake files and job records. |

For `combined` jobs, the audiobook library and folder template determine a single book folder containing the audio and the uploaded EPUB. Ebook-only jobs use the ebook library. ShelfShuttle names imported EPUBs and single M4B files `Author - Title.epub` and `Author - Title.m4b` using the confirmed book details. MP3 filenames and disc subfolders carry through to the library.

Use a dedicated category per ShelfShuttle deployment and a directory writable by qBittorrent. ShelfShuttle requests that path explicitly for new submissions. A 40-character hex or 32-character base32 v1 hash tracks an existing torrent or starts a new one with a trackerless magnet. Completion depends on peer discovery and availability. Provide the full magnet when the torrent needs its trackers. The integration uses the [qBittorrent Web API](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-%28qBittorrent-5.0%29), accepts the [session cookie used by recent versions](https://github.com/qbittorrent/qBittorrent/blob/v5_2_x/WebAPI_Changelog.md), and uses the [BitTorrent magnet format](https://www.bittorrent.org/beps/bep_0009.html).

## API

The reverse proxy authenticates API requests. For a POST, PATCH, or DELETE request, first call `GET /api/csrf` and keep its cookie. Send that cookie and the returned token in the `X-CSRF-Token` header on writes. The browser UI handles this automatically. Send any authentication required by your proxy on both requests. The internal health probe at `GET /healthz` returns a minimal status.

| Method and path | Purpose |
| --- | --- |
| `GET /api/csrf` | Obtain the request token and its matching cookie for API writes. |
| `GET /api/jobs?limit=50&offset=0` | List job summaries. Page size is capped at 100. |
| `POST /api/jobs` | Submit multipart fields `format` (`audio`, `ebook`, `combined`), `author`, `title`, and the applicable `epub` file and/or `torrent`. Audio-only forms may also be URL encoded. |
| `GET /api/jobs/{id}` | Retrieve job status, destinations, progress from 0 to 1, and extracted tags. |
| `GET /api/jobs/{id}/cover` | Retrieve a sanitized JPEG cover when available. |
| `DELETE /api/jobs/{id}` | Remove a queue job, its uploaded EPUB, and any matching unpublished staging directories. Returns 204 on success or 409 when the worker is busy or the job is currently importing or already completed. |
| `POST /api/jobs/{id}/action` | Send JSON with `intent` set to `retry` or `approve`, optional `author` and `title` for review, and optional `torrent` to supply a full magnet with the original info hash. |
| `GET /api/destinations` | Preview destination paths with query fields `format`, `author`, and `title`. |
| `POST /api/epub/inspect` | Inspect a multipart `epub` and return suggested metadata. |
| `GET /api/settings` | Read current settings and identify environment overrides. Passwords are omitted. |
| `PATCH /api/settings` | Send a JSON object of lower-case runtime setting keys. Set `qbit_password` to an empty string to clear it. |
| `POST /api/settings/test-qbittorrent` | Check access using saved settings and return the Web API version. |

For the loopback-only development server, the following example uses `curl` and `jq`:

```sh
base=http://127.0.0.1:8080
cookies=$(mktemp)
trap 'rm -f "$cookies"' EXIT
csrf=$(curl -fsS -c "$cookies" "$base/api/csrf" | jq -er .token)
curl -fsS -b "$cookies" -H "X-CSRF-Token: $csrf" "$base/api/jobs" \
  -F format=ebook -F 'author=Example Author' -F 'title=Example Book' \
  -F 'epub=@example.epub;type=application/epub+zip'
```
