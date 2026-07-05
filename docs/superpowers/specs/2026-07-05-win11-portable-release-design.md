# Win11 Portable Release Design

Date: 2026-07-05

## Context

The current Windows launcher, `start_tradingagents.ps1`, expects a project-local
`.venv` and falls back to `python -m cli.main`. That means a target Windows
machine must already have a compatible Python and pip environment, or someone
must be allowed to install one.

The target deployment model is different:

- Target machines run Windows 11.
- Target machines may not have Python or pip installed.
- If Python is installed, its version may not satisfy this project.
- Target machines may not allow installing Python, pip packages, or system-level
  dependencies.
- Network access and editable API-key configuration are allowed.
- All runtime output must stay under the portable release directory, not under
  `C:\Users\<user>\.tradingagents` or another user-profile location.

The project has two required user-facing entry points:

- `start_tradingagents.ps1`, which starts the TradingAgents CLI.
- `ak_pick_a_stock.py`, which picks China A-share candidates and writes
  `ak_candidates.csv`.

## Goals

1. Produce a Windows 11 portable release that runs without installing Python,
   pip, virtualenv, or project dependencies on the target machine.
2. Preserve the existing TradingAgents CLI entry point and the candidate-picker
   entry point.
3. Provide `.cmd` and `.ps1` launchers for both entry points.
4. Keep API keys and runtime configuration editable in a plain `.env` file.
5. Keep all generated files under the portable release directory:
   - reports
   - logs
   - data cache
   - memory log
   - candidate CSV output
6. Make the build process repeatable from a development machine.
7. Add verification that the generated package does not depend on system Python.

## Non-goals

- Do not support non-Windows targets in this release format.
- Do not create a full installer, MSI, or system-wide application registration.
- Do not install or modify Python on the target machine.
- Do not write to system `PATH`, the registry, or global site-packages.
- Do not remove the existing Python development workflow.
- Do not make the app work offline; LLM providers, market data sources, and API
  endpoints still require network access.
- Do not compile API keys into executables.

## Recommended Approach

Use PyInstaller in `onedir` mode to create a portable folder release with two
executables:

- `tradingagents.exe`
- `ak_pick_a_stock.exe`

The release folder owns its private Python runtime and dependency files in
PyInstaller's internal directory. Target machines run normal Windows executables
and do not need Python or pip.

Recommended release layout:

```text
TradingAgents-Win64/
  start_tradingagents.cmd
  start_tradingagents.ps1
  ak_pick_a_stock.cmd
  ak_pick_a_stock.ps1
  tradingagents.exe
  ak_pick_a_stock.exe
  .env
  data/
    cache/
    memory/
  logs/
  reports/
    runs/
    ak_candidates.csv
  _internal/
```

Alternatives considered:

- PyInstaller `onefile`: simpler to copy but slower to start, harder to debug,
  and more fragile with pandas, AkShare, LangChain, and dynamic imports.
- Embedded Python plus wheelhouse: debuggable and close to the development
  workflow, but it creates a more complex runtime layout and still feels like
  shipping a Python environment instead of a sealed application folder.

The `onedir` approach is the best first release because it is explicit, stable,
and easy to inspect when a target machine has a missing DLL, blocked executable,
or antivirus quarantine.

## Component Design

### 1. Build Script

Add a Windows build script, likely `scripts/build_windows_release.ps1`, that:

1. Verifies it is running on Windows.
2. Verifies the active Python is compatible with the project requirement.
3. Installs or verifies build-time dependencies in the development environment,
   including PyInstaller.
4. Builds `tradingagents.exe` from a small PyInstaller entrypoint that imports
   and runs `cli.main:app`.
5. Builds `ak_pick_a_stock.exe` from a small PyInstaller entrypoint that calls
   `ak_pick_a_stock.main()`.
6. Collects package data required at runtime, including `cli/static/*`.
7. Creates the portable release directory.
8. Copies launchers, `.env` or `.env.example`, and any required static assets.
9. Creates `data/cache`, `data/memory`, `logs`, and `reports/runs`.
10. Runs post-build smoke checks.

API-key handling:

- A private internal release may copy the developer machine's `.env` into the
  release folder when explicitly requested by the build command.
- The default build should copy `.env.example` to `.env` or create a sanitized
  `.env`, so secrets are not accidentally distributed.
- API keys must remain plain editable configuration and must not be embedded in
  either executable.

### 2. TradingAgents Launcher

Replace the target behavior of `start_tradingagents.ps1` so the release launcher
does not activate `.venv` and does not call system Python.

Launcher responsibilities:

- Resolve the directory containing the launcher as `AppDir`.
- Create `data/cache`, `data/memory`, `logs`, and `reports/runs` if missing.
- Set the current directory to `AppDir`.
- Load or leave available the root `.env` file for the Python app to consume.
- Set path environment overrides before starting the executable:
  - `TRADINGAGENTS_RESULTS_DIR=<AppDir>\reports\runs`
  - `TRADINGAGENTS_CACHE_DIR=<AppDir>\data\cache`
  - `TRADINGAGENTS_MEMORY_LOG_PATH=<AppDir>\data\memory\trading_memory.md`
- Start `<AppDir>\tradingagents.exe`.
- Return the executable's exit code.

Also add `start_tradingagents.cmd` as the double-click friendly wrapper. The
`.cmd` wrapper should call the PowerShell launcher using a local script path and
should pause on failure so non-technical users can read the error.

### 3. Candidate Picker Launcher

Add `ak_pick_a_stock.ps1` and `ak_pick_a_stock.cmd`.

Launcher responsibilities:

- Resolve `AppDir`.
- Create `reports` and `logs` if missing.
- Set the current directory to `AppDir`.
- Run `<AppDir>\ak_pick_a_stock.exe`.
- Return the executable's exit code.

The candidate picker currently writes `ak_candidates.csv` to the current working
directory. For a cleaner portable layout, update the implementation or launcher
behavior so the final CSV lands at:

```text
<AppDir>\reports\ak_candidates.csv
```

If the implementation is changed, keep the old behavior available only when an
explicit output path is passed later. This release does not require a full CLI
argument parser for the candidate picker; a fixed portable output path is enough.

### 4. Runtime Path Control

The existing config already supports these environment overrides:

- `TRADINGAGENTS_RESULTS_DIR`
- `TRADINGAGENTS_CACHE_DIR`
- `TRADINGAGENTS_MEMORY_LOG_PATH`

Use these instead of changing the default config globally. This keeps the normal
developer workflow unchanged while making the portable launchers deterministic.

Any new runtime logs introduced for the packaged app should go under:

```text
<AppDir>\logs
```

The implementation should audit obvious file writes in the two entry points and
ensure none intentionally target `~/.tradingagents` when started by the portable
launchers.

### 5. PyInstaller Configuration

Use checked-in PyInstaller spec files or generate them from the build script.
Checked-in spec files are preferred if the hidden imports or data collection
rules become non-trivial.

Expected packaging needs:

- collect project packages `tradingagents` and `cli`
- include `cli/static/*`
- include dependencies used through dynamic imports by LangChain, providers,
  pandas, AkShare, yfinance, baostock, Rich, Typer, and requests
- include certificate bundle support for HTTPS requests
- keep console mode enabled because both entry points are CLI tools

The first implementation should favor explicit hidden imports discovered during
smoke testing over broad speculative collection. If PyInstaller misses dynamic
dependencies, add them in the smallest working set.

## Data Flow

### Build-time

1. Developer runs `scripts/build_windows_release.ps1`.
2. Build script verifies the development Python environment.
3. PyInstaller freezes the TradingAgents CLI executable.
4. PyInstaller freezes the candidate-picker executable.
5. Build script assembles `TradingAgents-Win64`.
6. Build script copies launchers and editable config.
7. Build script runs smoke checks against the assembled folder.

### Runtime

1. User runs `start_tradingagents.cmd` or `start_tradingagents.ps1`.
2. Launcher sets `AppDir`-local output paths.
3. Launcher changes the current directory to `AppDir`.
4. `tradingagents.exe` starts and reads `.env` / environment configuration.
5. Reports, cache, memory log, and run artifacts are written under `AppDir`.

Candidate picker runtime:

1. User runs `ak_pick_a_stock.cmd` or `ak_pick_a_stock.ps1`.
2. Launcher changes the current directory to `AppDir`.
3. `ak_pick_a_stock.exe` fetches market data.
4. Candidate output is written under `AppDir\reports`.

## Error Handling

- If the expected executable is missing, the launcher should print a clear
  message and exit non-zero.
- If the `.env` file is missing, the launcher should warn but still start; the
  app can then prompt or fail with its existing provider-specific API-key checks.
- If the portable folders cannot be created, the launcher should print the
  failing path and exit non-zero.
- If the packaged executable exits non-zero, the launcher should propagate that
  exit code.
- The `.cmd` wrapper should pause on failure to keep the console visible.
- Build failures should stop immediately and identify whether the failure came
  from dependency installation, PyInstaller, file assembly, or smoke checks.

## Verification Plan

Build verification:

- Run the build script on a clean development checkout.
- Confirm both executables are present in the release folder.
- Confirm `_internal` is present and the release folder does not require `.venv`.
- Confirm `.env` is editable text in the release root.

Runtime smoke checks:

- Copy the release folder to a temporary path outside the repository.
- Temporarily modify `PATH` so system Python is not discoverable.
- Run `start_tradingagents.ps1` in a non-interactive smoke mode if one exists,
  or run a help/version command once such a command is added.
- Run `ak_pick_a_stock.ps1` with network access and confirm
  `reports\ak_candidates.csv` is created.
- Confirm no new files are written under `C:\Users\<user>\.tradingagents`.
- Confirm `reports`, `logs`, and `data` are populated under the release folder.

Suggested tests:

- Add focused tests for path resolution helpers if launcher logic is factored
  into testable PowerShell functions.
- Add a Python test or smoke helper for candidate-picker output path behavior.
- Add a build-script dry run that validates expected paths without invoking
  PyInstaller.

## Review Checklist

- The target machine does not need Python, pip, or virtualenv.
- The target machine is not modified globally.
- API keys are editable and not embedded in executables.
- The release works from an arbitrary folder path with spaces.
- All runtime output remains under the release folder.
- The normal source checkout development workflow still works.
