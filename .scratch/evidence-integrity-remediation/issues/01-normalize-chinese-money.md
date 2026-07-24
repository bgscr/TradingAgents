# Normalize Chinese monetary source facts

Status: completed

Implement boundary extraction and normalization for `元`, `万元`, and `亿元`, preserving raw spans, ranges, currency, normalized values, source references, and deterministic rendered annotations. Cover the exact audit cases and ambiguous/invalid expressions without silently guessing.

## Acceptance

- `50亿元 == 5_000_000_000 CNY` and `200.62亿元 == 20_062_000_000 CNY`.
- A `50亿元~55亿元` range preserves both normalized endpoints and original text.
- China news, announcement, and fundamentals tool outputs expose the structured facts before LLM consumption.
- Tests cover success, ranges, malformed expressions, and non-CNY text.

## Comments
