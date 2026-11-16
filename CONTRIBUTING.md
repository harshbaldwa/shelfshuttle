# Contributing to ShelfShuttle

Use the [local development setup](README.md#local-development) and run `ruff check .`, `ruff format --check .`, and `pytest` before opening a pull request. Describe the problem, your design, and how you verified it. For UI changes, include screenshots at relevant viewport sizes.

Use temporary directories, generated synthetic media, and mocked qBittorrent in tests. Keep private configuration, real books, torrents, library contents, and database files out of commits and issue attachments.

When changing the worker or installer, account for restart recovery, existing destination conflicts, and restricted mounts. Document any configuration changes in the [configuration reference](docs/configuration.md). For security-sensitive reports, follow the [security policy](SECURITY.md).
