# Troubleshooting

## I cannot stay signed in

Confirm that you are browsing the correct origin and that the browser accepts cookies. Secure cookies require HTTPS in ordinary deployments. For a loopback-only HTTP instance, set `SHELFSHUTTLE_COOKIE_SECURE=false` and recreate the service. If a login form was open while credentials changed, reload it to acquire a new request token.

## qBittorrent connection test fails

Check the Web UI URL from the container's network, including any proxy path. `localhost` inside ShelfShuttle refers to ShelfShuttle itself. Save credentials before testing. For HTTPS, use a certificate trusted by the container. Check any proxy host validation and qBittorrent Web UI allow-list settings. Keep authentication and CSRF checks enabled.

## A hash or magnet will not finish

A bare v1 hash requires a torrent already in qBittorrent. For a new job, provide a full magnet. Check qBittorrent for paused torrents, missing metadata, unavailable peers, or stalled trackers. A submitted job waits for qBittorrent to list the torrent before offering a retry. For an existing torrent in another category, confirm that you want ShelfShuttle to monitor it without modifying it.

## qBittorrent says complete but ShelfShuttle cannot read the files

Compare qBittorrent's save path and the actual file path with both download mapping settings. For example, qBittorrent's `/srv/qbit/ready/Disc 1/01.mp3` maps to `/downloads/ready/Disc 1/01.mp3` when the remote prefix is `/srv/qbit` and the mapped directory is `/downloads`. The mapped mount must contain the downloaded files with read and directory traversal permissions for the container user. Symbolic links are rejected.

## Import failed with a permissions or atomic rename error

Check that the container UID/GID can write each library parent, state directory, intake directory, and staging root. Use one mount for a library and its staging sibling. Separate bind mounts of directories on the same host disk can still fail with a cross-device rename error. Some network and virtual filesystems lack exclusive rename or directory synchronization. For new imports, prefer a local filesystem that supports both.

## An import needs review

- **Metadata differs.** Inspect the embedded metadata and confirm or edit the author and title.
- **Destination already exists.** Keep the existing folder, choose a distinct name for an unprepared job, or resolve the conflict elsewhere and retry. The app never merges an existing book directory.
- **Prepared files are missing or changed.** Restore the recorded staging directory or published destination from the same filesystem. If the process published the first half of a pairing and a second destination conflicts, resolve the conflict and retry.
- **Multiple M4Bs or mixed formats.** Select one M4B or the appropriate MP3 tracks in qBittorrent and retry.

## How should I back up and update the app?

Stop the ShelfShuttle service before making a filesystem copy of its state, intake, and any staging directories with pending jobs. Keep SQLite files and the private key together. Back up libraries separately. For an update, pull the desired release from your checkout and run `docker compose up -d --build`. Keep the same mount paths and storage.
