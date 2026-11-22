# Shell output with RTK

Use RTK for supported routine commands, for example `rtk git status` or `rtk ruff check <paths>`. This is an output-efficiency preference, not an execution permission or evidence transformation rule.

Use `rtk proxy <command> <arguments>` when raw output matters. Use the underlying command directly if RTK is unavailable, cannot represent the invocation, or would affect an exact byte/protocol/exit-status observation. Preserve raw evidence independently of display filtering. Do not install or modify tools merely to satisfy this preference.

Use `pwsh` for ordinary Windows shell work. A contract-bound executable/version (including Windows PowerShell 5.1) takes precedence. Keep filesystem operations within one shell using literal, verified paths. A wrapper/setup failure is not an underlying experiment result: establish whether the target launched before deciding whether another invocation is permitted.
