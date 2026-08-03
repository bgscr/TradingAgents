# RTK - Rust Token Killer (Codex CLI)

Token-optimized CLI proxy for shell commands.

## Core Rule
ALWAYS prefix shell commands with `rtk`.

**Examples:**
`rtk git status` | `rtk cargo test` | `rtk npm run build` | `rtk pytest -q` | `rtk pwsh -c "Get-Process"`

## Shell Environment
* Use `pwsh` instead of `powershell` for PowerShell commands.

## Meta Commands
* `rtk gain` — Token savings analytics
* `rtk gain --history` — Recent command savings history
* `rtk proxy <cmd>` — Run raw command without filtering

## Verification
* `rtk --version`
* `rtk gain`
* `which rtk`