# Security policy

Use the latest tagged release once available. Report suspected vulnerabilities through the repository's private vulnerability reporting feature if enabled. If it is unavailable, open a public issue asking maintainers for a private reporting channel without publishing vulnerability details or credentials.

Include the affected version, a concise description, and a reproduction using synthetic data. Redact hostnames, tokens, cookies, paths identifying your library, and other personal details. Coordinate publication with maintainers so users can update.

Use HTTPS and require reverse-proxy authentication on every route to ShelfShuttle. Keep its container port on a trusted Docker network and protect the state directory, backups, and mounted libraries.
