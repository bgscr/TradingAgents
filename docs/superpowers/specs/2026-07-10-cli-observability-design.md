# CLI Observability Design

Date: 2026-07-10

## Context

The TradingAgents CLI renders a Rich live layout while `run_analysis()` consumes
LangGraph state with `stream_mode="values"`. Users currently see an analysis
panel containing only `Waiting for analysis report...` until an analyst produces
its final response. That initial delay is normally one to three minutes because
the analyst nodes use synchronous LLM and tool calls and intentionally keep their
report field empty during tool-call rounds.

Recent artifacts prove report state does not wait for the whole graph to finish.
In run `20260709_031311`, the market report was first written 139 seconds after
start and 902 seconds before completion. Across eight recent completed runs, the
first report appeared 83-159 seconds after start while total run time was
979-1409 seconds.

Two additional problems make the experience look worse:

- Rich emits live frames in an interactive TTY, but a non-TTY or captured output
  host may receive only the final frame when `Live` closes. The Windows launcher
  calls the CLI directly without intentional redirection, but PowerShell hosts
  can still differ in how stdout is exposed.
- The progress events added in commit `492a25d` do not work with the configured
  stream shape. Every cumulative `values` chunk contains `messages`, while
  `_state_progress_events()` returns immediately whenever `messages` is present.
  Recent logs consequently contain no Research, Risk, or Portfolio events and
  include silent spans of six to nine minutes.

The existing `spinner_text` is constructed but never rendered. The run loop also
retains every cumulative state in `trace` and repeatedly rewrites unchanged
report sections, even though the latest `values` chunk is already the full state.

## Goals

1. Show meaningful activity immediately in interactive PowerShell instead of an
   empty waiting panel.
2. Display each finalized report as soon as its state field becomes available.
3. Provide durable, timestamped progress when stdout is non-interactive or
   captured.
4. Record Research, Risk, and Portfolio state transitions in both the display
   and `message_tool.log`.
5. Deduplicate cumulative messages, state events, and unchanged report writes.
6. Ensure display failures never interrupt graph execution or report generation.
7. Preserve finalized reports as the only authoritative report content.

## Non-goals

- Do not add token-by-token LLM streaming.
- Do not display or persist partial structured-output JSON, tool-call arguments,
  or unfinished LLM content as a report.
- Do not change agent prompts, trading recommendations, or report schemas.
- Do not fix report-integrity findings such as contradictory actions,
  unsupported arithmetic, duplicated financial years, or unreconciled metrics.
- Do not change China A-share data vendors, retry behavior, degradation status,
  or the incorrect `weekend or holiday` wording in this workstream.
- Do not redesign the completed report tree or post-run interaction flow.

The report-integrity and market-data findings require separate designs because
they affect trading semantics and source validation rather than CLI
observability.

## Approaches Considered

### Recommended: dual-mode display with state-derived progress

Keep node-level `values` streaming, add a small display boundary, and choose a
Rich or plain renderer from `console.is_terminal`. Derive progress by comparing
state fingerprints instead of treating the presence of cumulative messages as
evidence that a state update is already represented.

This approach fixes both terminal modes without changing provider behavior or
mixing provisional output with finalized reports.

### True token streaming

Request both `messages` and `values` stream modes and assemble token chunks into
a provisional preview. This offers richer feedback but has a substantially
larger compatibility surface: structured-output agents may stream JSON,
tool-call fragments differ by provider, and non-TTY output still needs a plain
fallback.

Token streaming is deferred until node-level observability is reliable.

### Minimal Rich-only patch

Render `spinner_text`, remove the incorrect progress guard, and explicitly
refresh the current Rich layout. This is smaller but does not solve the
final-frame-only behavior of non-TTY PowerShell hosts and leaves display logic
tightly coupled to the run loop.

## Component Design

### 1. Progress Extraction

Add a focused progress module, `cli/run_progress.py`, containing:

- `ProgressEvent`: a small immutable value containing timestamp-independent
  event type, display text, source key, and source fingerprint.
- `StateProgressTracker`: stateful fingerprint tracking that accepts each full
  values chunk and returns only newly observed events.
- Message fingerprint support for messages without stable IDs.

The tracker recognizes these transitions:

- analyst report finalized
- investment debate response changed
- research manager plan finalized
- trader plan finalized
- risk speaker response changed
- portfolio decision finalized

The tracker must inspect state fields even when cumulative `messages` is
non-empty. Source fingerprints prevent repeated values chunks from emitting the
same event.

For final decisions, the event should include a concise stable label when one is
available, for example `Final decision ready: Underweight`. The first non-empty
line of the rendered decision is sufficient; if it cannot be summarized, use
`Final decision ready` rather than parsing arbitrary free text aggressively.

### 2. Display Boundary

Add `cli/run_display.py` with a narrow display interface and two implementations.
The interface owns display lifecycle and rendering only; it does not mutate
graph state or write reports.

Required operations:

- start the selected display mode
- render the current message buffer and run statistics
- publish one progress event
- close cleanly at completion or failure

`RichRunDisplay`:

- uses the existing module-level `Console`
- owns the `Live` context and passes `console=console` explicitly
- refreshes after meaningful chunk transitions
- reuses the current progress, messages, report, and footer panels
- renders an informative in-progress state before the first finalized report

The initial analysis panel should contain:

- active in-progress agent, derived from `agent_status`
- current activity or latest tool/event
- an animated indicator
- a clear explanation that the report appears after the current tool/LLM round

Example:

```text
Market Analyst - retrieving and validating market data
The report will appear when this analyst's tool/LLM round completes.
```

Once a report is finalized, Rich mode displays the full current report exactly
as today.

`PlainRunDisplay`:

- performs no cursor movement or screen rewriting
- prints one timestamped line per meaningful transition
- prints a concise report preview and its artifact path when a report becomes
  available
- leaves the existing post-run complete-report flow unchanged

The preview should be bounded so captured logs remain usable; it is not a second
report format.

### 3. Renderer Selection and Fallback

After selections and run artifacts are prepared, construct the display once:

- use `RichRunDisplay` when `console.is_terminal` is true
- otherwise use `PlainRunDisplay`

No new end-user flag or environment variable is required for the first version.
Tests can inject the renderer directly.

If Rich setup or refresh fails:

1. report the display error once
2. close the Rich live context safely
3. switch to plain output
4. continue the graph without changing run status to failed

Only rendering failures are fail-open. Report-file and run-status write failures
retain their existing error behavior because artifacts are part of the run's
contract.

### 4. Run Loop Integration

`cli/main.py` remains the orchestrator:

1. prepare artifacts and graph state
2. choose a display implementation
3. consume cumulative values chunks
4. ingest only new messages and tool calls
5. derive state progress events
6. update changed report sections and agent statuses
7. publish the same progress events to display and run log
8. render the current view

The display and audit log must consume the same `ProgressEvent` objects so they
cannot silently diverge.

Keep `latest_state` rather than appending every cumulative values chunk to
`trace`. Initialize it to an empty dictionary and replace it with each
cumulative values chunk. After stream exhaustion, `latest_state` is the final
state. If the stream yields no chunk, it remains empty, preserving the current
empty-stream completion and report-writing behavior rather than redefining graph
semantics in this observability change.

Only call `update_report_section()` when a report fingerprint changes. This
preserves immediate report writes while avoiding repeated writes on every later
cumulative chunk.

### 5. Message and Agent Status Semantics

Message IDs remain the primary deduplication key. When an ID is absent, use a
stable fingerprint of message class/type and content so cumulative chunks do not
repeat it indefinitely.

The active agent shown before the first report must be derived from the agent
whose status is `in_progress`. Do not rely on `MessageBuffer.current_agent`,
because the current implementation updates that field for pending and completed
status writes as well.

This design does not otherwise redefine agent-transition rules.

## Runtime Data Flow

1. User starts `start_tradingagents.ps1` in PowerShell.
2. The launcher invokes the TradingAgents CLI directly.
3. The CLI prepares run artifacts and determines terminal capability.
4. The selected display starts and immediately shows or prints the current
   active agent.
5. LangGraph yields a cumulative values chunk.
6. New messages and tool calls enter `MessageBuffer` once.
7. `StateProgressTracker` emits events for changed state fields.
8. Changed reports update `MessageBuffer`, write their Markdown artifact, and
   become visible immediately.
9. Each event is published to both the renderer and `message_tool.log`.
10. The latest cumulative state replaces `latest_state`.
11. At stream completion, `latest_state` is written through the existing report
    tree and the display closes.

Representative plain-mode output:

```text
03:13:12 [System] Analysis started for 601658.SS
03:13:19 [Tool] Market Analyst requested get_stock_data
03:15:30 [Analysis] Market report ready: ...\market_report.md
03:19:12 [Research] Bull Researcher updated investment debate
03:25:08 [Risk] Aggressive Analyst updated risk debate
03:30:32 [Portfolio] Final decision ready: Underweight
```

## Error Handling

- A Rich rendering exception switches the run to plain output and does not mark
  the analysis failed.
- A malformed or absent optional state field produces no display event and does
  not interrupt graph execution.
- Event and message fingerprinting must tolerate non-JSON-native values by using
  the existing string fallback behavior.
- Repeated cumulative state is ignored after its first event.
- Partial LLM or tool content never updates `current_report`.
- Graph, report-writing, and status-writing exceptions continue through the
  existing `_mark_run_failed()` path.

## Acceptance Criteria

1. Interactive PowerShell shows an active analysis state immediately.
2. The first finalized analyst report is displayed before graph completion.
3. Non-TTY execution emits progress before stream exhaustion rather than only a
   final frame.
4. Research, Risk, and Portfolio events appear in both the display and
   `message_tool.log`.
5. Repeated cumulative chunks do not duplicate messages, events, or report
   writes.
6. The final portfolio rating is represented in progress output when its stable
   rendered label is available.
7. A display failure does not stop analysis or artifact generation.
8. Saved report contents and final-state semantics remain unchanged.

## Verification Plan

Focused unit tests:

- derive events from realistic cumulative values chunks that contain messages
- emit analyst, research, trader, risk, and portfolio transitions
- suppress repeated state fingerprints
- deduplicate messages with and without IDs
- transition a Rich layout from informative waiting state to finalized report
- emit plain progress before stream exhaustion
- switch from Rich to plain after a simulated rendering failure
- update reports only when content changes

Run-level tests:

- use a fake graph that yields cumulative values states and assert that the first
  report is displayed and written before the generator finishes
- assert that Research, Risk, and Portfolio lines reach both display output and
  `message_tool.log`
- assert final state is the latest values chunk without retaining a trace list

Manual Windows verification:

1. Run `start_tradingagents.ps1` in Windows Terminal PowerShell and confirm the
   active-agent state appears immediately and reports update during the run.
2. Run the launcher with captured or redirected output and confirm timestamped
   progress is emitted throughout the run without ANSI screen-control noise.
3. Confirm report files are created at the same state transitions shown in the
   console and log.

Regression verification:

- run the focused CLI and progress tests
- run the complete test suite
- confirm the primary checkout remains clean and all implementation changes stay
  in the dedicated worktree branch

## Review Checklist

- Terminal mode is detected once and can fall back safely.
- The waiting state explains active work instead of implying nothing is
  happening.
- Finalized reports remain the sole authoritative report content.
- Cumulative messages no longer suppress state-only progress.
- Display and log consume the same progress events.
- Plain output remains readable when redirected.
- No report-reliability or market-data semantics are changed in this scope.
