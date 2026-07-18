# Runtime artifact buffering benchmark — 2026-07-17

## Scope

This microbenchmark compares the former synchronous `message_tool.log` behavior
with the bounded background runtime-artifact writer. It sends 200 identical
524,327-byte tool payloads to the run log and its `latest` mirror.

The legacy case formats the complete payload, opens both logs, and appends the
complete line on every call. The buffered case enqueues the payload, stores the
complete canonical JSON as a gzip-compressed SHA-256 object, and writes bounded
previews plus references in batches.

Command:

```text
rtk python .scratch/evidence-integrity-remediation/benchmark_runtime_artifacts.py
```

## Result

| Measure | Legacy synchronous logs | Buffered artifacts | Change |
|---|---:|---:|---:|
| Producer/caller time | 0.980867 s | 0.001170 s | 838.0× faster |
| Total time through final flush | 0.980867 s | 0.548666 s | 1.79× faster |
| Physical payload/log bytes | 209,742,828 | 271,014 | 773.9× smaller |
| Final content-addressed objects | N/A | 1 | 199 duplicates reused |
| Compression operations | N/A | 1 | One per unique digest |
| Maximum queue depth | N/A | 201 / 512 | Bounded |
| Dropped/coalesced events | N/A | 0 / 0 | No loss |
| Maximum flush latency | N/A | 0.534076 s | Final durability boundary |

## Behavioral evidence

- Complete payload bytes round-trip from the gzip object and match the
  independently calculated canonical JSON digest.
- Both tool inputs and tool results use bounded previews with SHA-256 references.
- Identical input is stored and compressed once without scanning or rereading
  artifact contents during the run.
- Queue saturation is nonblocking for producers and emits dropped/coalesced
  details at the next flush.
- Status transitions and fatal-error lines remain synchronous.
- Graph phases cover analysis, research debate, trading, risk debate, portfolio
  synthesis, and report writing; tool, model, and report durations are separate.
- Offline GC checks all run/latest references, defaults to dry-run, and refuses
  to operate while any run status is `running`.

## Interpretation

The benchmark is intentionally deduplication-heavy because repeated full-state
tool payloads were the audited write-amplification pattern. It demonstrates the
critical-path and storage behavior of the new design; it is not a provider or
end-to-end analysis benchmark.
