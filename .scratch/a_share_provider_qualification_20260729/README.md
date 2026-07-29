# PROTOTYPE — mainland A-share provider qualification

Question: which capability-specific provider routing is supported by measured data completeness, stability, point-in-time metadata, and current account entitlement, without changing production code or purchasing a service?

Everything in this directory is disposable. The probe performs live, read-only provider calls and writes only under `outputs/` and the local Yahoo cache. It loads the project `.env` into process memory but never prints or persists credential values.

Run from the repository root:

```powershell
& '.scratch\a_share_provider_qualification_20260729\envs\baseline\Scripts\python.exe' '.scratch\a_share_provider_qualification_20260729\qualify.py'
```

The harness executes each provider/capability/symbol twice and produces raw JSON, normalized JSON, provider and per-symbol CSV matrices, and the two policy scores.

After a Tushare account reaches 2,000 points, rerun only the seven capabilities
whose entitlement changed (four symbols, two passes, 56 calls) with:

```powershell
& '.scratch\a_share_provider_qualification_20260729\envs\baseline\Scripts\python.exe' '.scratch\a_share_provider_qualification_20260729\requalify_tushare_2000.py'
```

This preserves the original `tushare-run-*.json` baseline and writes the new
raw passes as `tushare-2000-run-*.json` plus `tushare-2000-summary.json`.
