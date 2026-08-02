# RTK - Shell Output Compression Policy

RTK is a token-optimized proxy for verbose external shell commands.

## 1. Decision Matrix
| Execution Mode | Trigger / Condition | Examples |
| :--- | :--- | :--- |
| **Use `rtk <cmd>`** | Supported external CLI commands with large or repetitive output. | `rtk git status`<br>`rtk pytest -q`<br>`rtk ruff check .`<br>`rtk rg "pattern" path` |
| **Direct Execution** | PowerShell built-ins/pipelines, unsupported commands, concise output, or when exact raw output is strictly required. | `Get-ChildItem`<br>`git log -1`<br>PowerShell native pipelines |
| **Raw Proxy Mode (`rtk proxy`)** | Command is supported by RTK, but output filtering is undesirable. | `rtk proxy <command>` |

## 2. Core Execution Rules
1. **PowerShell Launcher**: If explicitly launching PowerShell as a subprocess, use `pwsh`. **Do not launch legacy `powershell.exe`.**
2. **Fallback Safety**: If RTK is unavailable or fails, execute the direct command immediately. Do not treat RTK failure as a blocker.
3. **Diagnostics (Optional)**: Use `rtk --version` or `rtk gain` only when diagnostic token savings information is explicitly requested or relevant.