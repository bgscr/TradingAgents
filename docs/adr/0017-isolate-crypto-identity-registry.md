---
status: accepted
---

# Isolate crypto identity in a dedicated registry

TradingAgents will establish Crypto Instrument identities from a separately versioned and digest-pinned registry rather than extending the mainland exchange registry or promoting mutable provider metadata to identity authority. Candidate routing may select a registry, but only a matching registry row establishes identity; this preserves current Mainland Instrument behavior and allows crypto provenance and support to evolve independently.

The crypto registry will follow the existing transactional refresh lifecycle: build and validate a deterministic candidate offline, publish the registry, checksum, and configuration atomically, run focused or full verification, and restore every prior file if publication or verification fails. Analysis runs are read-only consumers of the pinned registry and never discover or promote identity at runtime.
