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
This task executed only synthetic captures and offline replay, never live mode.

The live adapter, when separately authorized, is constrained to one GET of
`https://search-api-web.eastmoney.com/search/jsonp`, keyword `600895`, page 1,
size 10, zero redirects/retries/details/model calls. It uses the installed SDK's
curl transport, parameters, fixed `_` value, static SDK headers and Response.text
decoding, with an explicit 20-second timeout and redirects disabled. Static SDK
cookies are not operator credentials; no browser cookies or user credential store
are consulted. Request and SDK hashes are saved, not the headers/cookies themselves.
The real SDK parser and production private pandas binding/news acquisition route
are executed; article links come from `code`, not a required wire `url` column.

Stages distinguish transport, HTTP, decode, response size, JSONP wrapper, JSON,
schema, SDK/dataframe, window/admission and available artifact. Exception types
and safe response metadata are recorded without arbitrary exception text. An HTTP
200 alone never means recovery. Import/dependency or guard failure remains a setup
failure and can stop before a normal receipt.

Only a strictly recognized public envelope containing `result.cmsArticleWebOld`
and at most ten rows of public `date/mediaName/code/title/content/image` string
fields is saved as exact UTF-8 SDK-decoded text. Unknown/extra fields or unexpected
bodies retain only classification, length and hash, and have no replay body. Empty
recognized news arrays are retained even if the SDK subsequently fails. Public
content remains local. Content bytes are curl-decoded/decompressed bytes, not a
claim to retain compressed wire bytes. Replay validates the retained text digest
and reports SDK/request fingerprint equality; source/dependency changes must not be
described as an identical historical replay. Old failed evidence is never overwritten.

Example replay of an already approved capture:

```powershell
& 'D:/prj/TradingAgents/TradingAgents/.venv/Scripts/python.exe' -I -S -B scripts/diagnose_public_news.py --replay 'C:/path/to/approved-capture' --output 'C:/path/to/NEW-replay' --start 2026-10-05 --end 2026-10-12
```
