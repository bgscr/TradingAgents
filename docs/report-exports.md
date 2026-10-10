# Current and historical report exports

`complete_report.md` is the current report entrypoint. Its first line and
visible manifest link identify one immutable presentation version beneath
`report_exports/<export-id>/`. That version contains the terminal report,
the exact set of advisory stage documents, a complete report, and
`manifest.json`.

The manifest binds the version to its run ID, immutable decision-audit digest,
presentation language, and SHA-256 of every report file. Advisory text remains
outside the validated decision contract. Changing or removing advisory content
creates another presentation version without rewriting the decision audit.
Repeating the same export reuses the identical version.

Readers should follow the current entrypoint and manifest instead of assuming
that root-level `1_analysts` through `5_portfolio` directories describe the
current run. Existing root-level directories and old stage aliases are retained
history, as declared in each new manifest. Streaming CLI stage revisions are
also retained separately under `advisory_drafts/`. Unknown user files are not
deleted. A pre-versioning complete report is copied byte-for-byte to a
content-addressed legacy version before the current entrypoint is replaced.
Changed versioned entrypoints, including manual edits, are also retained
byte-for-byte under `report_exports/entry-history/`.

The exporter writes the immutable files and manifest first, then atomically
replaces the current entrypoint. An interrupted export does not make a partial
version current. Unreferenced partial versions are retained; there is no
automatic cleanup.

Publication rejects links, reparse points and aliased file targets. On Windows,
ordinary local-drive ancestors and parent directories remain held against
write/delete sharing during publication; on POSIX, operations use no-follow
directory descriptors. Files are published from exclusive temporary files
without opening existing targets for truncation. Filesystems which cannot
create the immutable hard-link publication fail closed rather than falling
back to an unsafe overwrite. This is a report I/O boundary, not policy
approval, native qualification, or an execution grant.
