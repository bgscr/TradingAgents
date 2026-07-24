# Windows Portable Release

This guide builds a Windows 11 portable TradingAgents folder that does not
require Python, pip, virtualenv, or package installation on the target machine.

## Build Machine Requirements

- Windows 11
- Python 3.10 or newer available on the build machine
- Network access for Python package installation

## Build

From the repository root:

```powershell
.\scripts\build_windows_release.ps1
```

The output folder is:

```text
dist\TradingAgents-Win64
```

Copy the entire `dist\TradingAgents-Win64` folder to the target Windows 11
machine. Keep the folder layout intact, including `_internal`, `.env`, the
launchers, and the `data`, `logs`, and `reports` directories. Run the launchers
from inside that copied folder.

For a private internal package that copies the local `.env` into the release:

```powershell
.\scripts\build_windows_release.ps1 -IncludeLocalEnv
```

## Target Machine Requirements

- Windows 11
- Network access to the selected LLM provider and market data sources
- Editable `.env` file in the release folder

The target machine does not need Python, pip, or virtualenv.

## Run TradingAgents

Double-click:

```text
start_tradingagents.cmd
```

Advanced users may run:

```powershell
.\start_tradingagents.ps1
```

## Run Candidate Picker

Double-click:

```text
ak_pick_a_stock.cmd
```

Advanced users may run:

```powershell
.\ak_pick_a_stock.ps1
```

The candidate CSV is written to:

```text
reports\ak_candidates.csv
```

## Runtime Output Locations

By default, all runtime output is written under the release folder:

```text
data\cache
data\memory\trading_memory.md
logs
reports
reports\runs
```

The portable launchers set these environment variables before starting the app:

```text
TRADINGAGENTS_RESULTS_DIR
TRADINGAGENTS_CACHE_DIR
TRADINGAGENTS_MEMORY_LOG_PATH
AK_PICK_OUTPUT_PATH
```

Values already exported by the caller are preserved, allowing an operator to
place runtime state elsewhere explicitly.

## Console Encoding

The CLI normalizes stdout and stderr to UTF-8 before rendering progress and
reports. This prevents model-generated Unicode text, including `¥`, from
failing when Windows starts Python with a legacy console encoding such as GBK.
Stored reports and runtime artifacts remain UTF-8 and are not rewritten.

## API Keys

Edit `.env` in the release folder with Notepad or another text editor. API keys
are not compiled into the executables.

## Smoke Check

From the release folder:

```powershell
.\start_tradingagents.ps1 -DryRun
.\ak_pick_a_stock.ps1 -DryRun
```

Both commands should print paths under the release folder.
