# Isolated finite disclosure runtime

This worktree is detached at d343d8a7b69caf80223fc591d834305732e7ed3b. Explicit profile 601328-2026H1-v2 is required; global defaults do not select it.

Publication paths: configs/finite-disclosure/601328-2026H1-v2.json, active-601328-v2.json; tradingagents/dataflows/finite-disclosure-compatibility-v2.json; tradingagents/market_history/current_cache_compatibility_finite_v2.json; releases/finite-601328-v2/<release digest>/manifest.json; data/finite-financial-disclosures/<pinned namespace>/finite-view.json.

The original earliest observation expires at 2026-10-22T17:56:25.556279Z. Review, cache copy and approval never renew it. Research evidence remains available after expiry, but financial runtime reads stop.

Rollback: disable the new activation pointer by an authorized atomic replacement with enabled=false, then stop selecting this profile. Retain the release, cache, qualification and migration audit. The old checkout and entrypoints require no restoration because they were not changed. Never delete consumed markers, reset quota or reuse old attempts.

The new market store may contain only the explicitly authorized SSE revision 30547d4ba90760ac3b07702b506ca3f7a0617d776a7a7433b5bee50d18b87048, 1827 natural dates, 1212 open and 615 closed. The migration preserves source payload hash and observation time and does not copy prices, financial data or quota. Coordinator authority remains the existing main-tree provider-requests database; no new independent quota is authorized.

Offline qualification uses Python -I -S -B, explicitly inserts this worktree and shared site-packages, and bypasses editable .pth hooks. Network, model factories and real database access are blocked in tests. Only named tests are collected; the protected persistence-isolation test is neither read nor collected. A synthetic graph success does not authorize real analysis or demonstrate price availability.
