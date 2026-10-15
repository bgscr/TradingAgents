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
& 'D:/prj/TradingAgents/TradingAgents/.venv/Scripts/python.exe' -I -S -B scripts/accept_financial_statements.py --output 'C:/Users/56922/Documents/Codex/2026-10-14/task-3/EXAMPLE-NEW-OFFLINE-RUN' --destination https://api.tushare.pro --account synthetic-offline --as-of 2026-10-15
```

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

The original one-shot GET returned HTTP 200 and 706 bytes but parsing failed. Its
raw body was not retained. That exhausted attempt cannot be replayed or treated as
production recovery. These successor tools do not renew its request budget.

`scripts/diagnose_public_news.py` accepts either `--replay CAPTURE_DIRECTORY` or
the explicit future authorization flag `--authorize-one-public-get-and-save`.
Both require `--output NEW_DIRECTORY --start YYYY-MM-DD --end YYYY-MM-DD`.
Select the exit-status goal with `--acceptance-mode news-content|original-text`.
Omitting this option preserves the legacy `original-text` behavior.
Offline regression uses synthetic captures and replay. Live mode requires its own
explicit authorization; neither this tool nor an offline pass grants one.

The live adapter, when separately authorized, is constrained to one GET of
`https://search-api-web.eastmoney.com/search/jsonp`, keyword `600895`, page 1,
size 10, zero redirects/retries/details/model calls. It uses the installed SDK's
curl transport, parameters, fixed `_` value, static SDK headers and Response.text
decoding, with an explicit 20-second timeout and redirects disabled. Static SDK
cookies are not operator credentials; no browser cookies or user credential store
are consulted. Request and SDK hashes are saved, not the headers/cookies themselves.
The real SDK parser and production private pandas binding/news acquisition route
are executed; article links come from `code`, not a required wire `url` column.

Independent `checks` record network, HTTP, decoding/size, retention, SDK,
production window/admission, capture-receipt writing and replay status. Each failed
check keeps its safe reason and exception type, without exception messages or
response excerpts. `stage` identifies the first failed check even when production
returns an available artifact. `capture_passed` requires production, body retention
and receipt writing; it does not imply replay acceptance. `acceptance_passed` is
false until a successful offline replay of a passed exact-text capture. These
original-text fields retain their meanings in both acceptance modes.
Import/dependency or guard failure remains a setup failure and can stop
before a normal receipt. HTTP 200 alone never means recovery.

The CLI records `acceptance.mode`, `acceptance.passed` and both independent
`acceptance.goals` in its new `result.json` and stdout. The reservation records
`acceptance_mode`. Exit 0 means the selected goal passed for this invocation's
capture/replay phase; exit 1 means it failed. Invalid arguments exit 2. Failure
reasons and original-text flags are never rewritten to match the selected goal.
`acceptance.content_source_passed` separately reports whether the source's network,
HTTP, decode, SDK, production, receipt and selected-representation retention stages
all passed. The content goal requires this for both capture and replay; a failed or
missing source stage cannot be rescued by matching output hashes. For projection
replay, the selected source retention stage is `projection`, not failed exact-text
`retention`. The original-text mode keeps its legacy contract.

| Mode | Capture phase | Offline replay phase |
| --- | --- | --- |
| `original-text` | Existing `capture_passed`: exact text, production and receipt saved | Existing `acceptance_passed`: successful exact-text capture and replay |
| `news-content` | Validated exact text or projection, SDK/production/receipt success and full SDK frame fingerprint | Successful exact-text or projection replay with matching full SDK frame and production artifact |

A capture success does not claim that offline replay has happened. A projection
can pass the content goal while `body_saved=false`, exact retention failed and
`acceptance_passed=false`; inspect the selected goal and each stage separately.
Missing frame evidence fails the new content goal. Old exact-text receipts without
that optional historical fingerprint keep their original-text behavior. Existing
receipts are read without requiring new mode fields and are never edited in place.

Only a recognized public envelope containing `result.cmsArticleWebOld` and at most
ten rows of public `date/mediaName/code/title/content/image` string fields is saved
as exact UTF-8 SDK-decoded text (`image` is optional). The SDK itself reads that
article list, selects/renames columns and derives links from `code`; it does not
enforce the helper's closed field sets. The helper now also permits integer
`code/hits/total/count` metadata from 0 through 2^53 at the top and result levels,
and an optional top-level `success: true`. These are bounded supported variations,
not a reconstruction of any lost live response. String metadata, unknown fields,
duplicate JSON keys, recognizable credential markers in article text and unexpected
bodies are rejected for exact retention. No arbitrary response, header, exception
text, or transformed body is written as the original. Text screening is conservative and does
not establish that every possible secret embedded in public fields is detectable.
Empty recognized arrays are retained even if the SDK subsequently fails. Existing
files are never overwritten; a failed/partial write is not a retained replay body.

When exact validation rejects a parsed article list, the helper can separately
save `public-projection.txt`: newly serialized JSONP containing only
`result.cmsArticleWebOld`. Its field policy is derived from all parser/output
operations in the pinned SDK, not a gradually expanded exact-body whitelist:

| Input field | SDK use | Projection |
| --- | --- | --- |
| `date`, `mediaName` | Renamed and selected | Retain required safe strings |
| `title`, `content` | Renamed, selected and text-cleaned | Retain required safe strings |
| `code` | Builds the output news URL, then discarded | Retain required safe string |
| `url` | Overwritten from `code` before selection | Omit original value |
| `关键词` | Overwritten with the symbol before selection | Omit original value |
| `image` | Renamed to an unselected intermediate column | Omit original value |
| Other row columns | Can collide with renamed output columns | Reject |
| Other top/result metadata | Not read by this SDK | Omit, without endorsing original status |

The SDK builds a DataFrame from row dictionaries and selects six output names;
there is no fixed input column-count/order requirement. Unknown columns are not
generally safe to drop: for example, an incoming `新闻标题` can duplicate a renamed
column. Only `url/image/关键词` are removed, then the existing field, string,
sensitive-text and count validator checks the remaining five-field core. Original
values of these three omitted fields, including suspicious URLs or nested values,
are never serialized. Sensitive or malformed retained fields still reject the
projection. Exact-body validation is unchanged and never admits new URL values.

The fallback is pinned to the reviewed SDK function SHA256. It does not depend on
guessing envelope field names or allowing their values. Rejected metadata or status
flags remain failed exact-retention evidence; projected replay only compares SDK
output after original SDK/production success. Changed SDK code, duplicate/invalid
JSON, invalid core rows, other unknown columns or an expanded projection over 1 MiB
cannot produce a usable projection. `projection_saved` and its distinct digest
never imply `body_saved`, `capture_passed` or exact acceptance.

New receipts also retain a bounded `structure` summary: known diagnostic labels,
JSON types, field counts, sizes and hashes/lengths of unknown names. No unknown
literal names or field values are copied. At most 32 fields per known object and
ten article-row shapes are shown; truncation is explicit, and arbitrary nested
metadata is not traversed. JSONP prefix/suffix checks are booleans only. The existing
older failed live receipts predate this summary and cannot be retroactively supplemented.

`articles.raw`, `articles.sdk` and `articles.filtered` record observed counts,
invalid-date counts and valid timestamp minima/maxima. Raw observations come from
the JSON article list even when metadata is rejected. SDK observations come from
the real returned DataFrame; filtered observations wrap the actual production
date filter, including its inclusive end day. Dates use production pandas coercion;
unknown observations remain null and empty observed sets have count zero. Requested
page size is never used as an observed count. Summary failure does not change the
production result.

Public content remains local. Content bytes are curl-decoded/decompressed bytes,
not compressed wire bytes. Replay checks the retained text digest, reruns the SDK
and production route with offline transport, and compares SDK, request, window and
artifact fingerprints. A new full SDK DataFrame fingerprint additionally covers
all rows, index/column order and dtypes, including out-of-window data. A projection
replay must match that fingerprint as well as the SDK, request, window and rendered
artifact, and requires successful original SDK/production observations. Only then
is `projection_replay_passed` true. This is equivalence of the tested parsed output,
not original-text preservation or proof about ignored envelope semantics. Projection
replay always leaves `acceptance_passed` false. With `--acceptance-mode news-content`,
successful projection replay exits 0; `original-text` still exits 1. Inspect
`replay_kind`, `projection_replay_passed` and `acceptance` for the separate outcomes. Missing full-frame
evidence, changed code, or changed rows cannot be rescued by a matching final article.

`--replay` selects only the fixed exact-body or projection filename declared in an
approved capture receipt. No supplier request is needed. A future separately
authorized single capture can thus retain safe structure and a validated projection
before original production parsing, then compare it in the offline guarded worker.
Unexpected article structure may still prevent projection; retain that precise
reason and summary rather than guessing fields through repeated live requests.

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
& 'D:/prj/TradingAgents/TradingAgents/.venv/Scripts/python.exe' -I -S -B scripts/diagnose_public_news.py --replay 'C:/path/to/approved-capture' --output 'C:/path/to/NEW-replay' --start 2026-10-05 --end 2026-10-12 --acceptance-mode news-content
```
