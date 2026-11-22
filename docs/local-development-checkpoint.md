# Local development checkpoint (2026-11-22)

This branch is a source-only work-in-progress snapshot, not a release or a
self-contained acceptance bundle. It captures the current mainland analysis
launcher, Full intake and reporting work, accompanying tests, and Codex agent
definitions. It is separate from the default branch.

Host-specific manifests, local configuration, retained provider responses and
credentials, run artifacts, logs, caches, and local backups are intentionally
excluded. Some tests refer to those local-only inputs and cannot be treated as
passing on a fresh clone. The bounded launcher requires an appropriately
generated and verified local manifest.

The latest live calendar-only diagnostic stopped on an unclassified response
field. It made one calendar request and did not start Full or call a model.
The 36 offline checks of the calendar classifier do not count as live
verification. No current-date Full acceptance is claimed here.
