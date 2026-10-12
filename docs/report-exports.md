# Independent report exports

`write_report_tree` returns the exact `complete_report.md` path for that call:
`report_exports/<export-id>/complete_report.md`. The CLI records that path
relative to the run directory in `reports_written`; consumers resolve it
against that run directory. Manual exports display the returned path. There is no
mutable root-level "current report" alias and no automatic selection of the
newest directory.

Every call allocates a fresh version. Its `manifest.json` binds the run ID,
unchanged decision-audit digest, language, exact stage set and file hashes.
Identical re-exports have the same content identity but distinct paths.
Removed or changed stages cannot contaminate another version. Model prose
remains explicitly advisory and does not change the validated decision.

Existing root reports, aliases, previous versions and manual edits are left
in place, without reading their bodies for backup or replacing them.
Streaming CLI revisions likewise use independent `advisory_drafts/` paths.
Consumers must use the returned report path and its sibling manifest.

New files are created exclusively at their final names. They are written,
flushed, and checked for object identity and exact content through the
original open descriptor. Descriptors remain open until the entire export
has been verified; the manifest is written last. There is no rename or link
from a closed temporary source. Failure raises instead of returning a path;
partial versions are retained for inspection, not automatically retried or
cleaned up.

The Windows adapter uses CREATE_NEW, OPEN_REPARSE_POINT and FILE_SHARE_READ
only: write/delete sharing is denied while each file handle is held.
Directory ancestors are also held against substitution. The POSIX adapter
uses O_EXCL/O_NOFOLLOW and directory-relative operations; these descriptors
do not impose Windows-style mandatory sharing restrictions. It checks the
named object and descriptor content before close and rejects observed
interference. Neither backend claims tamper-proof storage after handles close.
Callers inspecting stored reports later can compare them with manifest hashes.

Links, reparse points and aliased file targets are rejected. The targeted
development checks simulate Windows handles and interference; they do not
certify real Windows sharing or the POSIX backend. Report publication grants
no native policy, service, authentication, qualification or B1 authority.
