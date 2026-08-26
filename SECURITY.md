# Security

## Reporting

Use the repository host's private vulnerability-reporting feature when it is available. Do not publish credentials, session files, account identifiers, signed media URLs, downloaded content, or proof-of-concept data that belongs to another person.

Include only the minimum reproduction steps needed to explain the issue. Replace real account and post identifiers with synthetic placeholders.

## Sensitive local files

The following files and directories must remain local:

- dedicated browser session directories
- Cookie or storage-state exports
- environment and credential files
- downloaded media and generated manifests
- local databases, logs, traces, and screenshots

The default ignore rules cover common names, but custom output paths remain the user's responsibility.

Generated references are pseudonyms, not anonymity guarantees. Stable hashes can correlate the same source across runs, file hashes can correlate identical media, and downloaded originals may retain embedded metadata. Treat both media and manifests as private data even though direct source identifiers and signed URLs are omitted by default.
