# Payload GC concurrency prototype results

PROTOTYPE ONLY. Each row used fresh temporary SQLite/filesystem state and deterministic event barriers.

## Protocol summary

| Protocol | Immediate invariant failures | Eventual invariant failures |
| --- | ---: | ---: |
| 1-delete-commit-unlink-recheck | 2 | 2 |
| 2-tombstone-owner-phase | 0 | 0 |
| 3-generation-check-before-unlink | 1 | 1 |
| 4-main-db-transaction-through-unlink | 1 | 0 |
| 5-sidecar-sqlite-mutation-mutex | 0 | 0 |

## 1-delete-commit-unlink-recheck

| Interleaving | DB row state | Generation/tombstone | File state | Publisher | GC | Invariant | Eventual cleanup | Eventual state | Eventual invariant |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ordinary_orphan_collection | A:absent | A:none | A:canonical=no:quarantine=0 | not_run | collected:gc1 | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=skipped:referenced_or_absent | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |
| republication_before_row_deletion | A:live:g1:refs=1 | A:none | A:canonical=yes:quarantine=0 | published:g1;repeat=idempotent | skipped:referenced_or_absent | PASS:all_live_rows_have_verified_files | A:published_retained;recovery=skipped:referenced_or_absent | A:live:g1:refs=1;A:none;A:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| republication_after_row_deletion_before_unlink | A:live:g2:refs=1 | A:none | A:canonical=no:quarantine=0 | published:g2;repeat=idempotent | race_detected_too_late:g2:refs=1 | FAIL:missing_or_invalid=0d032b9c | A:BROKEN_reference_without_file;recovery=skipped:referenced_or_absent | A:live:g2:refs=1;A:none;A:canonical=no:quarantine=0 | FAIL:missing_or_invalid=0d032b9c |
| republication_immediately_before_unlink | A:live:g2:refs=1 | A:none | A:canonical=no:quarantine=0 | published:g2;repeat=idempotent | race_detected_too_late:g2:refs=1 | FAIL:missing_or_invalid=0d032b9c | A:BROKEN_reference_without_file;recovery=skipped:referenced_or_absent | A:live:g2:refs=1;A:none;A:canonical=no:quarantine=0 | FAIL:missing_or_invalid=0d032b9c |
| republication_immediately_after_unlink | A:live:g2:refs=1 | A:none | A:canonical=yes:quarantine=0 | published:g2;repeat=idempotent | race_detected_too_late:g2:refs=1 | PASS:all_live_rows_have_verified_files | A:published_retained;recovery=skipped:referenced_or_absent | A:live:g2:refs=1;A:none;A:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| two_concurrent_gc_workers | A:absent | A:none | A:canonical=no:quarantine=0 | not_run | gc1=collected:gc1;gc2=collected:gc2 | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=skipped:referenced_or_absent | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |
| publisher_and_gc_different_digests | A:absent;B:live:g1:refs=1 | A:none;B:none | A:canonical=no:quarantine=0;B:canonical=yes:quarantine=0 | published:g1;repeat=idempotent | collected:gc1 | PASS:all_live_rows_have_verified_files | A:orphan_collected;B:published_retained;recovery=skipped:referenced_or_absent,skipped:referenced_or_absent | A:absent;B:live:g1:refs=1;A:none;B:none;A:canonical=no:quarantine=0;B:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| unlink_failure_before_removal | A:absent | A:none | A:canonical=yes:quarantine=0 | not_run | unlink_failed:PermissionError | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=collected:recovery | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |
| interrupted_after_successful_unlink | A:absent | A:none | A:canonical=no:quarantine=0 | not_run | interrupted:after_unlink | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=skipped:referenced_or_absent | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |

## 2-tombstone-owner-phase

| Interleaving | DB row state | Generation/tombstone | File state | Publisher | GC | Invariant | Eventual cleanup | Eventual state | Eventual invariant |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ordinary_orphan_collection | A:absent | A:none | A:canonical=no:quarantine=0 | not_run | collected:gc1 | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=skipped:referenced_or_absent | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |
| republication_before_row_deletion | A:live:g1:refs=1 | A:none | A:canonical=yes:quarantine=0 | published:g1;repeat=idempotent | skipped:referenced_or_absent | PASS:all_live_rows_have_verified_files | A:published_retained;recovery=skipped:referenced_or_absent | A:live:g1:refs=1;A:none;A:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| republication_after_row_deletion_before_unlink | A:live:g2:refs=1 | A:none | A:canonical=yes:quarantine=0 | blocked:tombstone->published:g2;repeat=idempotent | collected:gc1 | PASS:all_live_rows_have_verified_files | A:published_retained;recovery=skipped:referenced_or_absent | A:live:g2:refs=1;A:none;A:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| republication_immediately_before_unlink | A:live:g2:refs=1 | A:none | A:canonical=yes:quarantine=0 | blocked:tombstone->published:g2;repeat=idempotent | collected:gc1 | PASS:all_live_rows_have_verified_files | A:published_retained;recovery=skipped:referenced_or_absent | A:live:g2:refs=1;A:none;A:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| republication_immediately_after_unlink | A:live:g2:refs=1 | A:none | A:canonical=yes:quarantine=0 | blocked:tombstone->published:g2;repeat=idempotent | collected:gc1 | PASS:all_live_rows_have_verified_files | A:published_retained;recovery=skipped:referenced_or_absent | A:live:g2:refs=1;A:none;A:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| two_concurrent_gc_workers | A:absent | A:none | A:canonical=no:quarantine=0 | not_run | gc1=collected:gc1;gc2=skipped:tombstone_owned->skipped:referenced_or_absent | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=skipped:referenced_or_absent | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |
| publisher_and_gc_different_digests | A:absent;B:live:g1:refs=1 | A:none;B:none | A:canonical=no:quarantine=0;B:canonical=yes:quarantine=0 | published:g1;repeat=idempotent | collected:gc1 | PASS:all_live_rows_have_verified_files | A:orphan_collected;B:published_retained;recovery=skipped:referenced_or_absent,skipped:referenced_or_absent | A:absent;B:live:g1:refs=1;A:none;B:none;A:canonical=no:quarantine=0;B:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| unlink_failure_before_removal | A:absent | A:g1:gc1-token:renamed | A:canonical=no:quarantine=1 | not_run | unlink_failed_tombstoned:PermissionError | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=collected:recovery | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |
| interrupted_after_successful_unlink | A:absent | A:g1:gc1-token:renamed | A:canonical=no:quarantine=0 | not_run | interrupted_tombstoned:after_unlink | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=collected:recovery | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |

## 3-generation-check-before-unlink

| Interleaving | DB row state | Generation/tombstone | File state | Publisher | GC | Invariant | Eventual cleanup | Eventual state | Eventual invariant |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ordinary_orphan_collection | A:absent | A:none | A:canonical=no:quarantine=0 | not_run | collected:gc1:checked_g1 | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=skipped:referenced_or_absent | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |
| republication_before_row_deletion | A:live:g1:refs=1 | A:none | A:canonical=yes:quarantine=0 | published:g1;repeat=idempotent | skipped:referenced_or_absent | PASS:all_live_rows_have_verified_files | A:published_retained;recovery=skipped:referenced_or_absent | A:live:g1:refs=1;A:none;A:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| republication_after_row_deletion_before_unlink | A:live:g2:refs=1 | A:none | A:canonical=yes:quarantine=0 | published:g2;repeat=idempotent | skipped:generation_changed:g1->g2 | PASS:all_live_rows_have_verified_files | A:published_retained;recovery=skipped:referenced_or_absent | A:live:g2:refs=1;A:none;A:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| republication_immediately_before_unlink | A:live:g2:refs=1 | A:none | A:canonical=no:quarantine=0 | published:g2;repeat=idempotent | collected:gc1:checked_g1 | FAIL:missing_or_invalid=0d032b9c | A:BROKEN_reference_without_file;recovery=skipped:referenced_or_absent | A:live:g2:refs=1;A:none;A:canonical=no:quarantine=0 | FAIL:missing_or_invalid=0d032b9c |
| republication_immediately_after_unlink | A:live:g2:refs=1 | A:none | A:canonical=yes:quarantine=0 | published:g2;repeat=idempotent | collected:gc1:checked_g1 | PASS:all_live_rows_have_verified_files | A:published_retained;recovery=skipped:referenced_or_absent | A:live:g2:refs=1;A:none;A:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| two_concurrent_gc_workers | A:absent | A:none | A:canonical=no:quarantine=0 | not_run | gc1=collected:gc1:checked_g1;gc2=collected:gc2:checked_g1 | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=skipped:referenced_or_absent | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |
| publisher_and_gc_different_digests | A:absent;B:live:g1:refs=1 | A:none;B:none | A:canonical=no:quarantine=0;B:canonical=yes:quarantine=0 | published:g1;repeat=idempotent | collected:gc1:checked_g1 | PASS:all_live_rows_have_verified_files | A:orphan_collected;B:published_retained;recovery=skipped:referenced_or_absent,skipped:referenced_or_absent | A:absent;B:live:g1:refs=1;A:none;B:none;A:canonical=no:quarantine=0;B:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| unlink_failure_before_removal | A:absent | A:none | A:canonical=yes:quarantine=0 | not_run | unlink_failed:PermissionError | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=collected:recovery:checked_g1 | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |
| interrupted_after_successful_unlink | A:absent | A:none | A:canonical=no:quarantine=0 | not_run | interrupted:after_unlink | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=skipped:referenced_or_absent | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |

## 4-main-db-transaction-through-unlink

| Interleaving | DB row state | Generation/tombstone | File state | Publisher | GC | Invariant | Eventual cleanup | Eventual state | Eventual invariant |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ordinary_orphan_collection | A:absent | A:none | A:canonical=no:quarantine=0 | not_run | collected:gc1 | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=skipped:referenced_or_absent | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |
| republication_before_row_deletion | A:live:g1:refs=1 | A:none | A:canonical=yes:quarantine=0 | published:g1;repeat=idempotent | skipped:referenced_or_absent | PASS:all_live_rows_have_verified_files | A:published_retained;recovery=skipped:referenced_or_absent | A:live:g1:refs=1;A:none;A:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| republication_after_row_deletion_before_unlink | A:live:g2:refs=1 | A:none | A:canonical=yes:quarantine=0 | blocked:metadata_writer->published:g2;repeat=idempotent | collected:gc1 | PASS:all_live_rows_have_verified_files | A:published_retained;recovery=skipped:referenced_or_absent | A:live:g2:refs=1;A:none;A:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| republication_immediately_before_unlink | A:live:g2:refs=1 | A:none | A:canonical=yes:quarantine=0 | blocked:metadata_writer->published:g2;repeat=idempotent | collected:gc1 | PASS:all_live_rows_have_verified_files | A:published_retained;recovery=skipped:referenced_or_absent | A:live:g2:refs=1;A:none;A:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| republication_immediately_after_unlink | A:live:g2:refs=1 | A:none | A:canonical=yes:quarantine=0 | blocked:metadata_writer->published:g2;repeat=idempotent | collected:gc1 | PASS:all_live_rows_have_verified_files | A:published_retained;recovery=skipped:referenced_or_absent | A:live:g2:refs=1;A:none;A:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| two_concurrent_gc_workers | A:absent | A:none | A:canonical=no:quarantine=0 | not_run | gc1=collected:gc1;gc2=blocked:metadata_writer->skipped:referenced_or_absent | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=skipped:referenced_or_absent | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |
| publisher_and_gc_different_digests | A:absent;B:live:g1:refs=1 | A:none;B:none | A:canonical=no:quarantine=0;B:canonical=yes:quarantine=0 | blocked:metadata_writer->published:g1;repeat=idempotent | collected:gc1 | PASS:all_live_rows_have_verified_files | A:orphan_collected;B:published_retained;recovery=skipped:referenced_or_absent,skipped:referenced_or_absent | A:absent;B:live:g1:refs=1;A:none;B:none;A:canonical=no:quarantine=0;B:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| unlink_failure_before_removal | A:live:g1:refs=0 | A:none | A:canonical=yes:quarantine=0 | not_run | unlink_failed_rolled_back:PermissionError | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=collected:recovery | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |
| interrupted_after_successful_unlink | A:live:g1:refs=0 | A:none | A:canonical=no:quarantine=0 | not_run | interrupted_rolled_back:after_unlink | FAIL:missing_or_invalid=0d032b9c | A:orphan_collected;recovery=collected:recovery | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |

## 5-sidecar-sqlite-mutation-mutex

| Interleaving | DB row state | Generation/tombstone | File state | Publisher | GC | Invariant | Eventual cleanup | Eventual state | Eventual invariant |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ordinary_orphan_collection | A:absent | A:none | A:canonical=no:quarantine=0 | not_run | collected:gc1 | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=skipped:referenced_or_absent | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |
| republication_before_row_deletion | A:live:g1:refs=1 | A:none | A:canonical=yes:quarantine=0 | published:g1;repeat=idempotent | skipped:referenced_or_absent | PASS:all_live_rows_have_verified_files | A:published_retained;recovery=skipped:referenced_or_absent | A:live:g1:refs=1;A:none;A:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| republication_after_row_deletion_before_unlink | A:live:g2:refs=1 | A:none | A:canonical=yes:quarantine=0 | blocked:payload_mutation_mutex->published:g2;repeat=idempotent | collected:gc1 | PASS:all_live_rows_have_verified_files | A:published_retained;recovery=skipped:referenced_or_absent | A:live:g2:refs=1;A:none;A:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| republication_immediately_before_unlink | A:live:g2:refs=1 | A:none | A:canonical=yes:quarantine=0 | blocked:payload_mutation_mutex->published:g2;repeat=idempotent | collected:gc1 | PASS:all_live_rows_have_verified_files | A:published_retained;recovery=skipped:referenced_or_absent | A:live:g2:refs=1;A:none;A:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| republication_immediately_after_unlink | A:live:g2:refs=1 | A:none | A:canonical=yes:quarantine=0 | blocked:payload_mutation_mutex->published:g2;repeat=idempotent | collected:gc1 | PASS:all_live_rows_have_verified_files | A:published_retained;recovery=skipped:referenced_or_absent | A:live:g2:refs=1;A:none;A:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| two_concurrent_gc_workers | A:absent | A:none | A:canonical=no:quarantine=0 | not_run | gc1=collected:gc1;gc2=blocked:payload_mutation_mutex->skipped:referenced_or_absent | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=skipped:referenced_or_absent | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |
| publisher_and_gc_different_digests | A:absent;B:live:g1:refs=1 | A:none;B:none | A:canonical=no:quarantine=0;B:canonical=yes:quarantine=0 | blocked:payload_mutation_mutex->published:g1;repeat=idempotent | collected:gc1 | PASS:all_live_rows_have_verified_files | A:orphan_collected;B:published_retained;recovery=skipped:referenced_or_absent,skipped:referenced_or_absent | A:absent;B:live:g1:refs=1;A:none;B:none;A:canonical=no:quarantine=0;B:canonical=yes:quarantine=0 | PASS:all_live_rows_have_verified_files |
| unlink_failure_before_removal | A:absent | A:none | A:canonical=yes:quarantine=0 | not_run | unlink_failed_row_absent:PermissionError | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=collected:recovery | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |
| interrupted_after_successful_unlink | A:absent | A:none | A:canonical=no:quarantine=0 | not_run | interrupted_lock_released:after_unlink | PASS:all_live_rows_have_verified_files | A:orphan_collected;recovery=skipped:referenced_or_absent | A:absent;A:none;A:canonical=no:quarantine=0 | PASS:all_live_rows_have_verified_files |

## Selected-protocol separate-process probe

| Field | Result |
| --- | --- |
| probe | 1 |
| protocol | 5-sidecar-sqlite-mutation-mutex |
| interleaving | separate_process_republication_immediately_before_unlink |
| blocked_publisher_attempt | blocked:payload_mutation_mutex |
| state_while_child_holds_mutex | A:absent;A:none;A:canonical=yes:quarantine=0 |
| child_gc_result | collected:gc-child |
| publisher_result | blocked:payload_mutation_mutex->published:g2;repeat=idempotent |
| database_row_state | A:live:g2:refs=1 |
| generation_tombstone_state | A:none |
| payload_file_state | A:canonical=yes:quarantine=0 |
| invariant_result | PASS:all_live_rows_have_verified_files |
| probe | 2 |
| protocol | 5-sidecar-sqlite-mutation-mutex |
| interleaving | separate_process_termination_after_row_delete_before_unlink |
| state_after_gc_process_termination | A:absent;A:none;A:canonical=yes:quarantine=0 |
| publisher_result | published:g2;repeat=idempotent |
| database_row_state | A:live:g2:refs=1 |
| generation_tombstone_state | A:none |
| payload_file_state | A:canonical=yes:quarantine=0 |
| invariant_result | PASS:all_live_rows_have_verified_files |
