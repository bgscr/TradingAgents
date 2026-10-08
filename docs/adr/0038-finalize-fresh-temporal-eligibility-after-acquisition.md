---
status: accepted
---

# Finalize fresh temporal eligibility after acquisition

Fresh current-analysis metadata will use one run-owned Eligibility Acquisition Window: `eligibility_started_at` pins the exact Market Session Calendar revision before provider work, while one later `eligibility_evaluated_at` bounds the truthful selected artifacts and derives the terminal completed-session frontier before context binding. One content-derived transaction binding ties the canonical run, routing plan, mode, requested session, start binding, and strict cutoff to every staged proof and final decision. Staged proof records whether its unchanged provider evidence is fresh or retained: only fresh proof makes the start-origin claim, while both origins remain bounded by the terminal instant. The terminal context is admitted only when every fresh proof satisfies `started <= observed_at <= retrieved_at <= evaluated`, both instants use the same pinned calendar revision, and the requested session is still the latest completed session at evaluation; crossing the next session close fails closed. This forward-only policy keeps strict-PIT cutoffs unchanged, preserves plan-1.3/context-v2 replay without reinterpretation, and avoids timestamp rewriting, grace windows, future cutoffs, provider fan-out, or authority changes.
