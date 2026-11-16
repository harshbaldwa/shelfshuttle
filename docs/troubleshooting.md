# Troubleshooting

## I cannot stay signed in

Confirm that you are browsing the correct origin and that the browser accepts cookies. Secure cookies require HTTPS in ordinary deployments. For a loopback-only HTTP instance, set `SHELFSHUTTLE_COOKIE_SECURE=false` and recreate the service. If a login form was open while credentials changed, reload it to acquire a new request token.

## qBittorrent connection test fails

Check the Web UI URL from the container's network, including any proxy path. `localhost` inside ShelfShuttle refers to ShelfShuttle itself. Save credentials before testing. For HTTPS, use a certificate trusted by the container. Check any proxy host validation and qBittorrent Web UI allow-list settings. Keep authentication and CSRF checks enabled.

## An EPUB fails upload validation

ShelfShuttle checks the ZIP and reads the EPUB package and its reading order. If the package is readable and the archive passes its safety checks, ShelfShuttle can repair a missing, misplaced, compressed, or incorrect `mimetype` entry on upload. The repair keeps the publication resources intact. If the package or reading order is missing or damaged, export a fresh EPUB from your source and upload it again.

## A hash or magnet will not finish

A new download submitted with a bare v1 hash relies on peer discovery, such as DHT. Check qBittorrent for paused torrents, missing metadata, unavailable peers, or stalled trackers. If the download requires a tracker, add it in qBittorrent or provide the full magnet when creating the job. A submitted job waits for qBittorrent to list the torrent before offering a retry. For a magnet that matches an existing torrent, confirm that you want ShelfShuttle to monitor it.

If a ShelfShuttle job says the submission failed while qBittorrent shows it downloading, use **Retry** on the existing job. ShelfShuttle checks for the same hash before submitting again.

## qBittorrent says complete but ShelfShuttle cannot read the files

Compare qBittorrent's save path and the actual file path with both download mapping settings. For example, qBittorrent's `/srv/qbit/ready/Disc 1/01.mp3` maps to `/downloads/ready/Disc 1/01.mp3` when the remote prefix is `/srv/qbit` and the mapped directory is `/downloads`. The mapped mount must contain the downloaded files with read and directory traversal permissions for the container user. Symbolic links are rejected.

## Import failed with a permissions or atomic rename error

Check that the container UID/GID can write each library parent, state directory, intake directory, and staging root. Use one mount for a library and its staging sibling. Separate bind mounts of directories on the same host disk can still fail with a cross-device rename error. Some network and virtual filesystems lack exclusive rename or directory synchronization. For new imports, prefer a local filesystem that supports both.

Read the operation, container path, and errno on the failed job after retrying. If the filesystem rejects the no-replace rename flag, ShelfShuttle reserves a new book folder and copies files with exclusive creation. This can make a folder visible before all its files arrive. The fallback applies to existing staged jobs on retry and requires write access to the library. Resolve an existing destination conflict before retrying. On Linux, `findmnt -T /path/to/your/library-parent` shows the backing filesystem.

If directory synchronization is unsupported, ShelfShuttle still syncs each file. Recovery after a power loss then depends on when the filesystem persists its directory entries.

With the Compose read-only root enabled, a mount directly at `/media/ebooks/library` leaves the default sibling `/media/ebooks/.shelfshuttle` on the read-only container filesystem. Mount the host parent at `/media/ebooks` and set `SHELFSHUTTLE_EBOOK_LIBRARY` to the mounted path of the existing book directory. If the directory is named `books` on the host, use `/media/ebooks/books`. ShelfShuttle creates staging beside it.

After changing container library paths, recreate the ShelfShuttle container. Retry affected unprepared jobs, then confirm the updated destination in the review queue. A prepared job retains the paths it recorded for recovery.

## An import needs review

- **Metadata differs.** Inspect the embedded metadata and confirm or edit the author and title.
- **Destination already exists.** Keep the existing folder, choose a distinct name for an unprepared job, or resolve the conflict elsewhere and retry. The app never merges an existing book directory.
- **Prepared files are missing or changed.** Restore the recorded staging directory or published destination from the same filesystem. For an older job with two recorded destinations, resolve any remaining destination conflict and retry.
- **Multiple M4Bs or mixed formats.** Select one M4B or the appropriate MP3 tracks in qBittorrent and retry.

Use **Delete from queue** on a failed or queued job. Confirm to remove the job, its uploaded EPUB, and any matching unpublished staging directories. If cleanup is interrupted, the worker retries it when it resumes.

## How should I back up and update the app?

Stop the ShelfShuttle service before making a filesystem copy of its state, intake, and any staging directories with pending jobs. Keep SQLite files and the private key together. Back up libraries separately. For an update, pull the desired release from your checkout and run `docker compose up -d --build`. Keep the same mount paths and storage.
