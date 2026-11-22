# Isolated data diagnostics

These entry points do not load the operator's local profile, `.env`, production
databases, reports or checkpoints. Use a new output directory outside the checkout
for every invocation. Failed reservations are retained and cannot be resumed.
Python must be the existing project virtual environment with `-I -S -B`; no
installation is performed. The checked-in instrument registry and its checksum
are the authority source; the operator's runtime identity configuration is not read.

## Financial statements

From the checkout, an entirely offline example is:

```powershell
$diagnosticParent = (Get-Item -LiteralPath .).Parent.FullName
$offlineOutput = Join-Path $diagnosticParent 'EXAMPLE-NEW-OFFLINE-RUN'
& ./.venv/Scripts/python.exe -I -S -B scripts/accept_financial_statements.py --output $offlineOutput --destination https://api.tushare.pro --account synthetic-offline --as-of 2026-10-15 --ticker 600895.SH
```

Choose an approved, writable external parent and a new output name. The example
derives an absolute sibling path without a `..` component; the output directory
must not already exist. The ticker labels synthetic inputs in this offline mode.

Without `--authorize-live`, the first worker substitutes only the HTTPS connection
with synthetic statement responses. It exercises the production HTTPS JSON parser,
configured FinancialToolDispatcher, CURRENT adapter, coordinator, normalizer and
SQLite cache. The second worker is a new interpreter after the first has exited.
It repeats the same window's quarterly and annual views for all three tables,
compares hashes and `retrieved_at`/`observed_at`, and forbids client creation.
Its environment excludes inherited credentials. Before application imports, audit
guards deny sockets, credential environment/file access, subprocesses, native
security discovery and writes outside the output. Native curl HTTP is also blocked.
Guard self-probes and import-time blocked operations are reported separately from
application access. These are defense-in-depth guards, not an OS sandbox or native
qualification. `result.json` must report `passed: true`; setup failure is not a pass.

For a future user-authorized live acceptance, use the same command with a fresh
directory, the explicitly authorized `--destination`, a non-secret `--account`
scope label, and `--authorize-live`. An interactive hidden prompt asks for the
token only in the first worker. There is no environment/token-file discovery,
token argument, credential migration or saved token. Do not execute this mode
without fresh authorization for the exact destination and account credential use.

The selected destination is mandatory and has no default:

| Destination | Physical POST shape | Evidence limit |
| --- | --- | --- |
| `https://api.tushare.pro` | `/`, body `api_name=balancesheet/income/cashflow` | Supported HTTPS implementation; account entitlement/live success not established |
| `https://api.waditu.com/dataapi` | `/dataapi/balancesheet`, `/dataapi/income`, `/dataapi/cashflow`, with `ts_type_name` | Existing market-client protocol precedent; not evidence that credentials or entitlement are interchangeable |

The two destinations remain separate cache identities. Neither the SDK's HTTP
default nor either HTTPS host is chosen on the user's behalf. A synthetic example
does not establish a live destination preference. See [CURRENT statements](current-financial-statements.md).

Live mode is fixed to `600895.SH`, three statement endpoints, one shared six-year
announcement window ending at `--as-of`, at most three physical POST invocations,
one per endpoint. TLS verification remains enabled; redirects/retries are disabled.
The first unavailable or degraded view stops the whole acceptance, including empty,
permission, rate limit, malformed response, timeout or persistence failure. It does
not call a model or ordinary analysis CLI. Only a fully successful acquisition
starts the offline worker. All config, cache, coordinator state and receipts remain
in the new output directory. No runtime configuration is adopted automatically.

## News response diagnosis

An explicitly authorized live run may instead select
`--authorize-one-public-get-metadata-only`. This is a live GET, not offline replay.
The default capture and replay modes are unchanged and mutually exclusive with
this option. The exit-status goal defaults to `metadata-only` for this mode.
Its separate `news-live-metadata-only-v1` receipt records closed status/shape
categories, selection counts, source bindings and public request metadata. It
does not retain response bytes, article text, arbitrary fields or their hashes,
response/frame/artifact digests, or replayable content. A passing metadata run
does not pass the original-byte or news-content replay goals. Failed eligibility
still stops before production parsing; retention rules are not bypassed merely
because writing is disabled. Existing size, stock/date/business and close rules
continue to apply. Response length means decoded `response.content` bytes; the
existing size rejection happens after Requests has read the response, not as a
streaming download cap.

Metadata-only receipts additionally carry `closed_shape` (`news-closed-shape-v1`).
It labels strict production-decoded JSONP as `expected_jsonp` or
`other_valid_jsonp`; otherwise only a complete plain-JSON syntax check is tried
for observation, with the same duplicate-key/nonfinite-number rejection. That
fallback never admits plain JSON to the production parser. Unsupported/malformed
wrappers are `invalid_or_unknown`. This classification does not identify actual
callback text, error responsibility, access restrictions or supplier success.

The only inspected locations are root, root.result and root.data. At each, a
fixed set of known fields receives presence/type labels; code receives only
zero/nonzero/wrong-type/absent categories, msg/bizMsg only exact `OK`, empty string,
unknown/wrong-type/absent categories. No normalization or error-message inference
is used. Each location's cmsArticleWebOld array length is capped at 65535. No
unknown key, value, callback, raw path or content digest is retained. `error`
presence/type is descriptive only, not proof of a provider error contract.
The metadata classifier never recursively projects unknown nesting; the JSON
decoder still parses the complete size-bounded input in memory. Limits remain
the existing 1 MiB post-read boundary and strict decoder; recursion errors or malformed syntax produce unavailable
classification. An unexpected optional-observer failure yields fixed unavailable
metadata with empty scopes and does not change admission or the first error;
cancellation remains terminal and response ownership is closed before propagation.

The separate opt-in `--public-structure-metadata` adds `news-public-structure-v1`
to the metadata-only receipt and is recorded in its reservation. It is limited
to the fixed production request or the single `_` variant below, `%20` URL and
`600919.SH`; no header variant is combined with it. The actual PreparedRequest must pass the existing URL/parameter,
no-body and no-credential-header checks, plus GET, empty session auth/cookies,
verified TLS and disabled environment/proxy discovery checks, before observation.
The default modes and all production admission rules are unchanged.

Only after the strict decoder accepts the complete JSONP wrapper may this view
retain a common `jQuery` identifier with 6–25 digits, underscore and a 13-digit
2000–2100 timestamp. This is an explicitly permitted public correlation identifier,
not authentication, proof of source, or a guarantee that any numeric string is
non-sensitive. The format is grounded in jQuery 3.5.1's
[JSONP callback generation](https://raw.githubusercontent.com/jquery/jquery/3.5.1/src/ajax/jsonp.js),
[expando](https://raw.githubusercontent.com/jquery/jquery/3.5.1/src/core.js), and
[nonce](https://raw.githubusercontent.com/jquery/jquery/3.5.1/src/ajax/var/nonce.js).
Other callback forms remain hidden. Observing the name does not repair a callback
mismatch. Existing profile-specific checks remain unchanged: the observed public
profile rejects a mismatch; the legacy profile records it without that rejection.

For `result`, at most 32 direct fields are inspected. Names must be ASCII business
identifiers of at most 40 characters and in the explicit reviewed-name set;
credential-like and unreviewed names are hidden in fixed counters. Nonempty direct
string values hide their entire field. Allowed fields expose only name, built-in
JSON type and empty/nonempty/not-applicable labels. No values, unknown-name hashes,
article counts or nested fields are copied by this view; container contents are
not inspected or certified safe. An oversized object exposes no sampled names.
Fixed business-status categories remain in `closed_shape`. No body/digest is kept,
so this observation cannot provide byte replay, establish cache identity, or
reconstruct an earlier rejected response. All prior read/size/cleanup limitations
above still apply.

`--request-variant fresh-cachebuster-v1` additionally requires that public-structure
opt-in. It changes only the final `_` query value to one freshly generated local
UTC millisecond value, with a fixed numeric range and rejection of the baseline
value. It never changes `cb`, `param`, query order/encoding, headers, method, body,
TLS, redirects, retries or timeout. It does not reserialize the whole query.
The actual changed URL and fixed query delta are recorded; `decoded_query_equal`
is false by design for this variant. The request fingerprint explicitly remains
the production request **before** the diagnostic variant. There is no automatic
iteration, retry, arbitrary query input or change to production defaults.

The production adapter already sets both `Cache-Control: no-cache` and
`Pragma: no-cache`. [RFC 9111](https://www.rfc-editor.org/rfc/rfc9111.html#section-5.2.1.4)
defines request no-cache as a preference for origin validation before reuse;
request directives are advisory. Pragma is deprecated HTTP/1.0 compatibility.
Cache keys normally include the target URI, but this does not prove a particular
server or intermediary implements that correctly or that a new URI clears every
cache. [jQuery 3.5.1](https://raw.githubusercontent.com/jquery/jquery/3.5.1/src/ajax.js)
uses a fresh `_` for its cache=false option. This supports a normal single-variable
observation, not a verified Eastmoney server contract or a cache-bypass guarantee.
Changing response shape supports a possible relationship to query/URL variation;
no change does not determine the cause or eliminate caching. Neither outcome alone
establishes a fix, cache hit/miss, equivalent route or long-term reliability.

Only this variant adds `news-cache-metadata-v1`: Date/Expires/Last-Modified must be
exact valid IMF-fixdate/GMT; Age must be a bounded nonnegative decimal. Cache-Control
is limited to 256 characters and 16 recognized directives with bounded numeric
arguments. Unsupported/quoted/duplicate or public+private directives hide the
whole field. ETag/Vary/Via/Cache-Status/X-Cache/CF-Cache-Status disclose presence
only, never values, hosts, validator identifiers or hashes. Unexpected observer
failure is unavailable; terminal errors close owned responses and propagate.
No content or arbitrary headers are retained. Old receipts did not retain these
headers, so their values are unknown rather than evidence of server absence.

Only in this mode, `--percent-encoded-spaces` compares a public browser link by
replacing the prepared query's space encoding (`+` to `%20`). The fixed decoded
parameters and absence of credentials/body are checked before the sole send.
Both public URLs and their semantic/byte equality are recorded. Production
headers, verified TLS, `trust_env=False`, timeout 15, zero retries and disabled
redirects remain unchanged. This option does not imitate a browser or identify
the actual network route. Only closed response negotiation fields are retained;
unknown values are labeled `other`. No browser access or second request occurs.

A separate explicit `--request-variant omit-referer-v1` is restricted to live
metadata-only mode, `%20` encoding and `600919.SH`. It deletes only the existing
stock Referer from the PreparedRequest, verifies every other header plus method,
URL and body are unchanged, and records that fixed deletion. It does not copy a
browser identity, change TLS or provide an arbitrary-header interface. The default
`production` variant leaves all headers unchanged. The existing request fingerprint
still binds the production input before the diagnostic deletion; the variant name,
verified delta and tool/CLI source bindings describe the actual diagnostic change.
Each invocation still has its own single-send limit; this option grants no request
authority and never schedules subsequent variants. A changed result cannot alone
prove causality across different acquisition times or unconfirmed network routes.

`--request-variant accept-javascript-v1` has the same explicit scope restrictions
and replaces only `Accept: */*` with `Accept: text/javascript` relative to the
unchanged production-header baseline, including its Referer. It cannot be combined
with omit-referer-v1. This expresses response media preference, not a browser
identity. [RFC 9110 sections 12.4.1 and 12.5.1](https://www.rfc-editor.org/rfc/rfc9110.html#section-12.5.1)
define this negotiation and permit a server to ignore it or return406 if unavailable.
Neither a media-type match nor HTTP200 overrides the existing JSONP, business,
stock/date or retention requirements. An unchanged result does not prove that all
content negotiation is irrelevant; a changed result is not a supplier-wide fix.

`--request-variant accept-edge-value-v1` instead selects the user's observed full
Accept value, exactly:

```text
text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7
```

This is a separate single-field experiment against the original production
baseline, not the earlier `text/javascript` preference. The same metadata-only,
`%20`, ticker and one-send restrictions apply. Referer, UA, client hints, fetch
context, encoding, language, callback, timestamp, verified TLS and proxy settings
are unchanged; no other variant can be combined. Matching this one header does
not recreate Edge or prove a common network route. HTML/error responses remain
subject to the original decode/JSONP/schema admission and are never news merely
because the request accepts HTML. Body, arbitrary fields and their digests remain
unretained on both success and failure; only the fixed Accept delta is recorded.

The original one-shot GET returned HTTP 200 and 706 bytes but parsing failed. Its
raw body was not retained. That exhausted attempt cannot be replayed or treated as
production recovery. These successor tools do not renew its request budget.

In addition to metadata-only mode, `scripts/diagnose_public_news.py` retains
`--replay CAPTURE_DIRECTORY` and `--authorize-one-public-get-and-save`.
All three modes require `--output NEW_DIRECTORY --start YYYY-MM-DD --end YYYY-MM-DD`
and explicit `--ticker 600895.SH|600919.SH` and `--transport direct`;
there is no default stock or transport choice.
The target choices do not grant live authorization for either instrument.
Select the exit-status goal with `--acceptance-mode news-content|original-text`.
Omitting this option defaults to `original-text` within the current receipt contract.
Offline regression uses synthetic captures and replay. Live mode requires its own
explicit authorization; neither this tool nor an offline pass grants one.

Current receipts use `news-production-diagnostic-v4`. The helper exercises the
actual `route_to_vendor_acquired -> get_news -> _stock_news_em -> parse_news_page`
chain. It intercepts the adapter's requests namespace, not the obsolete SDK call.
The CLI source hash and explicit transport choice are bound in v4 receipts.
Live capture creates a fresh Requests Session with `trust_env=False`, empty
proxies and `verify=True`. It does not discover system/environment proxies,
netrc credentials or environment CA overrides. The installed default HTTPS
retry count must remain zero; timeout 15 and redirects disabled are unchanged.
The session closes after the attempt, without fallback or global changes.
The production Requests path advertises only `gzip, deflate`, whose decoding
does not require optional Brotli/Zstandard packages. This is an encoding-policy
fix, not a switch to the SDK's curl transport. Plain responses remain supported.
Requests decodes content before `response.content` is inspected: retained bytes,
lengths and hashes describe that body, not compressed bytes on the connection.
Synthetic compatibility tests keep real Requests/urllib3 decoding and replace
only the network send. They do not replay the missing historical response or
establish why that response lacked the target news collection.
Replay is offline; the transport field describes the bound capture policy,
not a network replay or independent proof of network delivery.
The selected ticker binds the request keyword, production symbol, capture,
reservation and replay. Only `600895.SH` and `600919.SH` are supported.
The existing transport scope remains page 1, size 10, one supplied GET, no redirects, no additional details/model calls, and the production 15-second
timeout. No default live getter exists in the library. The CLI still requires
separate explicit authorization for capture, `-I -S -B`, and a fresh directory
outside the source tree. Offline replay retains the original network guard.

The installed production parser, not HTTP 200 or the SDK's article shape, decides
business status, dates and instrument association. No authoritative success-code
contract is established here: `code=0` alone remains `business_status_unverified`.

The 2026-11-13 local compatibility profile additionally recognizes the complete
shape observed in two user-pasted browser responses: exactly `bizCode`, `bizMsg`,
`code`, `extra`, `hitsTotal`, `msg`, `result`, `searchId`; integer code zero, exact
`OK`, empty business strings and extra object, bounded integer hit count, UUIDv4
search ID, matching callback, and only the target collection of 1–10 articles.
All seven observed article fields must have the checked types; image is empty,
article ID/date formats are checked, sensitive text is rejected, and each URL
must match that article ID on the observed stock/finance/bank Eastmoney hosts.
Unknown extensions, other status combinations and mismatching callbacks do not
gain this compatibility exception. The shared parser/retention validator does
not remove metadata or re-encode the original body. Existing legacy status-absent
behavior is unchanged; partial observed-profile markers remain unverified.

This is a closed local compatibility policy, not an authoritative supplier-wide
success protocol. Date and stock selection still follow independently; the two
browser pastes each contain ten rows but only one in the October 31–November 7
window, with unknown coverage despite hitsTotal=196. They are user-supplied chat
text, not program captures: browser acquisition time, HTTP status and headers
are unknown. Local sample tests use explicitly simulated transports with origin
sidecars, and cannot qualify those simulated HTTP statuses as live observations.
Explicit failure signals remain failures. Query keywords and article IDs cannot
prove stock association. Inclusive Shanghai calendar dates and authoritative
symbol/alias checks are unchanged. One page leaves `news_window_coverage_unknown`;
publication dates alone do not establish historical availability or strict PIT.

The retention check distinguishes the target `result.cmsArticleWebOld` collection:
`articles_missing` means the field is absent, `articles_null` means an explicit
JSON null, and `articles_not_list` means a present non-null value has another
type. These remain failed capture outcomes, with no accepted-count or coverage
claim. An empty array is distinct from a missing collection; it still must pass
the independent business-status and production-parser checks. An unrelated
collection must never be substituted for the target or reported as zero news.

CLI stdout includes the existing `callback_matches` metadata: true/false after
successful decoding, or null when decoding has not established a comparison.
The callback name is not printed. Legacy status-absent envelopes retain their
existing callback observation behavior; the closed browser-observed profile
requires a match. Neither behavior overrides failed business, schema, date,
stock-association or source checks.
Old receipts and their reason labels are preserved. When original response bytes
were not saved, new diagnostics cannot reconstruct them or provide a real replay;
code-defined synthetic fixtures establish only the tested behavior.

Transport exceptions now add an optional `transport_detail` block under the
network check, with sub-contract `news-transport-error-v1`. The outer v4 capture
contract and all admission/retention rules remain unchanged. This diagnostic
block is not evidence of supplier success or the root cause of a historical run.
It records only exact known class identities as fixed labels/categories and
whitelisted integer errno/winerror values. Unknown types become `other`;
unknown codes, booleans and strings become null. No messages, traceback, host,
URL, request, response, credentials, unknown class names or arbitrary attributes
are serialized. DNS/TLS errno allowlists remain distinct from OS error codes.

The selected chain follows explicit cause, known wrapper exception reason,
exception-valued argument, then unsuppressed context, in that order. Traversal
is capped at eight nodes, the first four arguments and eight fields of an exact
ordinary wrapper dictionary. Dictionary subclasses and hash-based lookups are
not used; unavailable references, cycles and stopping reasons are explicit.
Known wrapper references include Requests/urllib3 args,
MaxRetryError.reason, ProxyError.original_error and NameResolutionError._reason.
This is one bounded path, not an exhaustive exception graph. MaxRetryError does
not prove retries occurred: it can wrap a failed first attempt when retries=0.
Metadata failure leaves a fixed unavailable detail and the original failure;
it does not change the request, timeout, retry, TLS, budget or acceptance result.
Previously lost exception chains cannot be reconstructed from outer type alone,
and old captures/packets remain unchanged. Changed tool-source hashes continue
to prevent old receipts from being silently qualified against new code.

Independent checks record network, HTTP, decoder, retention, parser, adapter,
production admission, receipt and replay outcomes. Safe exception types/reasons
and existing bounded business-status diagnostics contain no exception messages,
headers, request bodies, addresses or article text. A later successful check
cannot erase an earlier failed check. Setup/import/guard failures remain distinct.

`selection.excluded_counts` comes from the actual parser in validation order:
`malformed`, `undated`, `outside_window`, then `wrong_stock`. Counts are mutually
exclusive, saturate at 65535 with an explicit flag, and accompany accepted counts.
They are unavailable (null, not zero) when parsing fails before article selection.
The observed raw count is separate from the requested page size. The safe shape
summary remains bounded to 32 fields per known object and 10 article shapes;
unknown names are hashed and values are not copied.

Exact retention uses unchanged `response.content` bytes in `public-response.bin`,
not re-encoded `Response.text` and not compressed wire bytes. The production
nonexecuting decoder validates JSONP, duplicate keys, nonfinite numbers and UTF-8.
The separate conservative public-data whitelist still requires at most ten rows
of known string fields, screens sensitive text and rejects unknown envelope/row
fields. Optional fields accepted by production do not relax retention. Recognized
negative responses may be retained but cannot pass capture or replay acceptance.
Existing files are never overwritten; failed or partial writes are not retained
evidence. Text screening cannot identify every possible embedded secret.

No projected body is generated or accepted by v4. Removing a business-status field
would change an input the current production parser evaluates. The compatibility
flags `projection_saved` and `projection_replay_passed` therefore remain false.
Earlier v2/v3, legacy SDK and projection receipts fail with an unsupported-contract
reason and remain unchanged; success cannot be retroactively manufactured.

Replay checks original-byte digest plus current tool, adapter, parser, acquisition,
route and diagnostic-source bindings, explicit ticker, request settings, date window, adapter frame
and rendered artifact fingerprints. Missing or failed source checks cannot be
rescued by matching output. `capture_passed` requires original retention,
production and receipt success; it does not claim replay occurred.
`acceptance_passed` requires a passed source capture and successful bound replay.

The CLI retains `--acceptance-mode original-text|news-content` for compatibility.
Despite its legacy spelling, `original-text` now concerns the original bytes in
the v4 contract. The content goal additionally requires the adapter frame hash.
Neither mode accepts a projection or bypasses production/retention failure.
Exit 0 means the selected goal passed for this phase; 1 means a failed diagnostic;
2 means invalid invocation/input. Results retain explicit missing-data/PIT limits.

Historical SDK-era observations below describe their original contracts only.
They are not evidence that the current pipeline or a future supplier request works.

Missing bindings or a failed source capture cannot pass exact acceptance. These
checks do not prove an identical environment or qualify native
isolation. Two earlier distinct live captures on 2026-10-15 each returned 4,881 bytes. The
earlier receipt lacks article counts and date ranges. The later separately
authorized receipt records raw/SDK counts of 10, dates from 2026-08-20 09:29:18 to
2026-10-14 17:47:00, and one filtered row dated 2026-10-14 17:47:00 for the October
9–15 window. Those source timestamps have no timezone offset. Its retained failure
is `unknown_envelope_fields`; the unknown field names, types and structure were
not recorded. Both original bodies are absent. Neither capture nor the older
706-byte response can be replayed from synthetic fixtures. A later v3 capture also
returned 4,881 bytes but retained a complete safe shape: ten rows each had the six
original known fields plus a name hash matching the SDK's `url` literal. The v3
projection rejected that extra column. Its body and projection were not saved,
so the new field policy cannot retroactively replay or pass that attempt either.
Structural synthetic fixtures use invented contents, not reconstructed live text.
Old failed evidence is never overwritten, and this repair grants no further GETs.

Example replay of an already approved capture:

```powershell
$diagnosticParent = (Get-Item -LiteralPath .).Parent.FullName
$captureRoot = Join-Path $diagnosticParent 'approved-capture'
$replayOutput = Join-Path $diagnosticParent 'NEW-replay'
& ./.venv/Scripts/python.exe -I -S -B scripts/diagnose_public_news.py --replay $captureRoot --output $replayOutput --start 2026-10-05 --end 2026-10-12 --ticker 600895.SH --transport direct --acceptance-mode news-content
```

Replace the capture path with an existing, permitted retained capture; this
example does not download one. Use a fresh output path in an approved external
directory, with no `..` components or links. All replay acceptance checks remain.
