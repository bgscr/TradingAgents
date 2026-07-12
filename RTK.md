# RTK - Rust Token Killer (Codex CLI)

**Usage**: Token-optimized CLI proxy for shell commands.

## Rule

1. Always prefix shell commands with `rtk`.
2. **PowerShell Mandatory Rule**: Whenever you need to use PowerShell to execute a command, **you MUST use `pwsh`**. Do not use the legacy `powershell` command under any circumstances.

Examples:

```bash
rtk git status
rtk cargo test
rtk npm run build
rtk pytest -q
```

## Meta Commands

```bash
rtk gain            # Token savings analytics
rtk gain --history  # Recent command savings history
rtk proxy <cmd>     # Run raw command without filtering
```

## Verification

```bash
rtk --version
rtk gain
which rtk
```
