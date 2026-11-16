# Architecture

ShelfShuttle runs as one FastAPI process. Jinja renders the web UI. Browser JavaScript progressively adds EPUB suggestions, live paths, and dashboard refreshes. The UI and HTTP API call the same intake and retry functions.

## Durable intake workflow

1. Intake validates the confirmed book fields and supported torrent input. It validates the EPUB ZIP and required package files without extracting anything. It writes the EPUB to a private intake folder and commits a queued job to SQLite.
2. A polling worker takes a state-volume file lock so one worker processes the queue. SQLite uses WAL and full synchronization. The worker looks up the torrent by hash and persists a submission timestamp before adding a new one. A bare v1 hash becomes a trackerless magnet. The worker associates new torrents with a job-specific tag and rechecks the hash before any retry.
3. The worker checks actual torrent progress and an eligible completed state. It reads qBittorrent's file list, translates its absolute save path under the configured mapping, and validates selected M4B or MP3 media. The worker opens files with no-follow traversal of every path component.
4. Metadata readers extract EPUB and audio tags, verify available fields against the user-confirmed author and title, and store a sanitized JPEG cover when possible. Missing tags are allowed. A disagreement requires confirmation. Implementations of the provider protocol can extend this in a future release.
5. The importer checks destination conflicts, stages copies on the destination filesystem outside watched directories, and syncs the files. It also syncs directories where the filesystem supports it. It records the prepared directories' filesystem identities in SQLite before publishing. The preferred publication method is an atomic, exclusive rename. If the filesystem rejects that operation, ShelfShuttle creates the book folder and files exclusively and records each identity in SQLite. On retry it resumes a partial file only if its identity and existing bytes match the staged copy.
6. If a process stops before the prepare commit, the worker can rebuild that job's known private staging directories. If it stops after an exclusive rename, the next run recognizes the same directory at the destination. A conflict blocks further publication until review.

The worker never modifies or removes torrent content. Existing library entries are left in place. A new paired import publishes one audiobook folder containing the audio and EPUB. Queued paired imports without a prepared plan adopt that destination when the audiobook path still matches. Jobs with a prepared plan resume their recorded paths, including two-library jobs from earlier releases. In the copy fallback, a crash between creating an entry and recording its identity can require manual review. Preserve staged folders and already-published directories when recovering a prepared import.

## State and trust

- The state volume holds the encrypted qBittorrent password, a separate private key file, and SQLite state. ShelfShuttle derives a separate CSRF signing key from that file. Back up the state and intake together and restrict access to the backups.
- The reverse proxy authenticates requests. The application uses an HTTP-only, same-site cookie and CSRF tokens for changes. Keep the backend reachable only by trusted network peers.
- Users admitted by the proxy can manage jobs and configure the qBittorrent endpoint. Apply your access rules at the proxy and use network controls to limit which services ShelfShuttle can reach.
- Mount downloaded files read-only. Library directories, staging roots, and the state and intake volumes must be writable by the process within the configured mount boundaries. Keep other applications from writing to private staging.
- Filename normalization can map different titles to the same path. A collision enters review. Imports require a filesystem that supports atomic exclusive directory rename and directory synchronization.
