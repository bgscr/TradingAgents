# Same-instance enrollment admission v2 preparation

Authorized on 2026-10-02: isolated candidate development on a new worktree, build,
local commit, independent static review, one frozen synthetic TEST batch and a
new preparation packet. Historical baseline/business candidate is
33337b24b5aa07d11c5b9c29485f8fb3604fc2e9; new implementation identity must differ.

Required behavior is documented in
[the v2 contract](../../docs/windows-policy-owner-admission-v2.md). Preserve the
original role, instance/root/service SID/ledger identity and DB objects/data.
Only ELIGIBLE post-cut state can migrate and reserve a new attempt. No v1 fallback,
automatic retry, recreation, cleanup or signature rebinding is permitted.

Preparation deadline: 2026-10-02 09:37:47 UTC, 120 minutes from start. One declared
batch only, cumulative 900 seconds and 60 seconds per actual chain including
cleanup. Unexpected failure stops the batch without a source fix/rerun.

Deployment, actual service/registry/ACL migration, kernel reboot, installed-state
collection and genuine authentication are excluded. Original C and subsequent A
retain their STOP states and historical UNKNOWN. Policy/current-source/113-root
B0/qualification/Ticket-07/live remain excluded. The final packet must state actual
outcomes, unexecuted platform paths and the next distinct authorization boundaries.
