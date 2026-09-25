"""Run only a pinned setup action's non-mutating prefix in fresh WinPS 5.1.

The first native invocation and the entire remaining tail are excluded before
PowerShell parses the test input. No native setup, enrollment or Hello test exists.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

BOUNDARY = "Invoke-PinnedSetupNative -Path $nativePath -Sha256 $nativeSha -Arguments @('setup',$proposalPath,$proposalSha) -InProcessSetup"


def check(action: Path, expected_sha: str, output: Path) -> dict:
    raw = action.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("exact setup action bytes changed")
    text = raw.decode("utf-8", errors="strict")
    if text.count(BOUNDARY) != 1 or text.count("\nInvoke-PinnedSetupNative ") != 3:
        raise ValueError("unknown setup action topology; refuse to execute any prefix")
    prefix = text[: text.index(BOUNDARY)]
    if "\nInvoke-PinnedSetupNative " in prefix:
        raise ValueError("native invocation occurs before the test boundary")
    if not prefix.rstrip().endswith(
        "Write-Host 'Review the exact principal, dedicated virtual service account, new root and permissions in the native setup form.'"
    ):
        raise ValueError("non-mutating boundary changed")
    source = Path(__file__).resolve().parents[2]
    output = output.resolve(strict=False)
    if output.exists() or output == source or output.is_relative_to(source):
        raise ValueError("fresh external test output required")
    output.mkdir()
    (output / "executed-prefix.ps1").write_bytes(prefix.encode("utf-8"))
    prefix_path = "'" + str(output / "executed-prefix.ps1").replace("'", "''") + "'"
    prefix_sha = hashlib.sha256(prefix.encode("utf-8")).hexdigest()
    inspect = r"""
$result.prefix_passed=$true
$commands=@()
foreach($name in @('Get-FileHash','Test-Path','Join-Path','Write-Host')) {
 $command=Get-Command -Name $name -ListImported -ErrorAction Stop
 $commands+=@{name=$command.Name;kind=[string]$command.CommandType;module=$command.ModuleName;path=$command.Module.Path}
}
$result.commands=$commands
$modules=@();foreach($loaded in (Get-Module)){$modules+=@{name=$loaded.Name;path=$loaded.Path}}
$result.modules_after=$modules
"""
    encoded_inspect = base64.b64encode(inspect.encode("utf-8")).decode("ascii")
    harness = (
        r"""
$ErrorActionPreference='Stop'
[void][Reflection.Assembly]::LoadWithPartialName('System.Web.Extensions')
$serializer=[System.Web.Script.Serialization.JavaScriptSerializer]::new()
$modules=@();foreach($loaded in (Get-Module)){$modules+=@{name=$loaded.Name;path=$loaded.Path}}
$preference=$ExecutionContext.SessionState.PSVariable.Get('PSModuleAutoLoadingPreference')
$preferenceBefore=if($null -eq $preference){'UNSET'}else{[string]$preference.Value}
$pathBefore=$env:PSModulePath
$result=@{version=$PSVersionTable.PSVersion.ToString();x64=([IntPtr]::Size -eq 8);pshome=$PSHOME;modules_before=$modules;preference_before=$preferenceBefore;prefix_passed=$false;boundary='BEFORE_FIRST_NATIVE_INVOCATION';native_tail_excluded=$true}
$prefixBytes=[IO.File]::ReadAllBytes(@@PREFIX_PATH@@)
$prefixHasher=[Security.Cryptography.SHA256]::Create()
try { $actualPrefix=[BitConverter]::ToString($prefixHasher.ComputeHash($prefixBytes)).Replace('-','').ToLowerInvariant() } finally { $prefixHasher.Dispose() }
if($actualPrefix -ne '@@PREFIX_SHA@@'){throw 'Bounded test prefix changed'}
$prefix=[Text.Encoding]::UTF8.GetString($prefixBytes)
$inspect=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('@@INSPECT@@'))
try { & ([ScriptBlock]::Create($prefix+$inspect)) }
catch { $result.prefix_passed=$false;$result.error=$_.ToString();$result.error_id=$_.FullyQualifiedErrorId;$result.line=$_.InvocationInfo.ScriptLineNumber }
$preference=$ExecutionContext.SessionState.PSVariable.Get('PSModuleAutoLoadingPreference')
$result.preference_after=if($null -eq $preference){'UNSET'}else{[string]$preference.Value}
$result.module_path_unchanged=($pathBefore -ceq $env:PSModulePath)
[Console]::WriteLine('INITIALIZATION_RESULT='+$serializer.Serialize($result))
""".replace("@@PREFIX_PATH@@", prefix_path)
        .replace("@@PREFIX_SHA@@", prefix_sha)
        .replace("@@INSPECT@@", encoded_inspect)
    )
    results = []
    for mode, prelude in (
        ("fresh-default", ""),
        ("fresh-no-autoload", "$PSModuleAutoLoadingPreference='None'\n"),
    ):
        command = prelude + harness
        (output / (mode + ".command.ps1")).write_text(command, encoding="utf-8")
        process = subprocess.run(
            [
                str(
                    Path(os.environ["SYSTEMROOT"])
                    / "System32/WindowsPowerShell/v1.0/powershell.exe"
                ),
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                base64.b64encode(command.encode("utf-16-le")).decode("ascii"),
            ],
            capture_output=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        (output / (mode + ".stdout.bin")).write_bytes(process.stdout)
        (output / (mode + ".stderr.bin")).write_bytes(process.stderr)
        lines = [
            line.split("INITIALIZATION_RESULT=", 1)[1]
            for line in process.stdout.decode("utf-8", errors="strict").splitlines()
            if line.startswith("INITIALIZATION_RESULT=")
        ]
        if process.returncode or len(lines) != 1:
            raise RuntimeError(
                "fresh initialization harness did not return its bounded observation"
            )
        result = json.loads(lines[0])
        commands = result.get("commands", [])
        expected_modules = {
            "Get-FileHash": "Microsoft.PowerShell.Utility",
            "Write-Host": "Microsoft.PowerShell.Utility",
            "Test-Path": "Microsoft.PowerShell.Management",
            "Join-Path": "Microsoft.PowerShell.Management",
        }
        result["passed"] = (
            result["prefix_passed"]
            and result["x64"]
            and result["version"].startswith("5.1.")
            and not result["modules_before"]
            and result["module_path_unchanged"]
            and result["preference_before"] == result["preference_after"]
            and len(commands) == len(expected_modules)
            and all(
                item["module"] == expected_modules[item["name"]]
                and item["path"]
                .casefold()
                .startswith((result["pshome"] + "\\Modules\\").casefold())
                for item in commands
            )
            and any(
                item["name"] == "Get-FileHash" and item["kind"] == "Function" for item in commands
            )
        )
        results.append({"mode": mode, **result})
    report = {
        "action": str(action),
        "action_sha256": expected_sha,
        "prefix_sha256": hashlib.sha256(prefix.encode("utf-8")).hexdigest(),
        "cases": results,
        "passed": all(item["passed"] for item in results),
        "provisioning_executed": False,
        "enrollment_executed": False,
    }
    (output / "result.json").write_text(
        json.dumps(report, sort_keys=True, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    if os.name != "nt" or not (
        sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode
    ):
        raise ValueError("use the existing Windows interpreter with -I -B -S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = check(args.action, args.sha256, args.output)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
