# RTK — Optional Shell Output Compression

RTK is a token-optimized proxy for supported external shell commands. It can reduce noisy command output, but it is not required for every shell operation.

## Usage Policy

1. Use RTK when the external command is supported and its output is likely to be large or repetitive.
2. Run commands directly when:
   - using PowerShell built-ins or pipelines;
   - RTK does not support the command;
   - exact, unfiltered output is required;
   - RTK would add complexity without reducing output.
3. If explicitly launching PowerShell as a subprocess, use `pwsh`. Do not launch legacy `powershell.exe`.
4. If RTK is unavailable or fails to handle a command, continue with the direct command without treating that as a blocker.

## Examples

```text
rtk git status
rtk pytest -q
rtk ruff check .
rtk rg "pattern" path
```

Direct execution remains appropriate for concise commands and PowerShell-native operations.

## Raw Output

Use the proxy mode when RTK supports the command but its filtering is undesirable:

```text
rtk proxy <command>
```

## Diagnostics

```text
rtk --version
rtk gain
rtk gain --history
```

`rtk gain` reports estimated token savings. These diagnostic commands are optional and should only be used when their information is relevant to the task.