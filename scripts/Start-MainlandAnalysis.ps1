param(
    [switch]$CheckOnly,
    [switch]$Full,
    [ValidateSet('portfolio-research-v3')][string]$DecisionMode,
    [switch]$Run,
    [switch]$FiveSnapshotsOnce,
    [switch]$CalendarOnly,
    [switch]$CalendarNineOnce,
    [string[]]$Enhancement,
    [switch]$Guided,
    [switch]$UseProjectEnv,
    [switch]$Moneyflow,
    [switch]$PowerShellNews,
    [switch]$PowerShellFundFlow,
    [switch]$NewsSourceIsolation,
    [AllowEmptyString()][string]$MarginDate,
    [string]$BankSnapshot,
    [AllowEmptyString()][string]$Ticker,
    [AllowEmptyString()][string]$AnalysisDate,
    [AllowEmptyString()][string]$SupplementalReportPeriod,
    [AllowEmptyString()][string]$AnnouncementFile,
    [AllowEmptyString()][string]$AnnouncementDate,
    [AllowEmptyString()][string]$AnnouncementSourceUrl,
    [ValidateSet('retrospective','as_known_on_date','disabled')][string]$ObservationCacheMode,
    [ValidatePattern('^[0-9a-f]{64}$')][string]$FundflowObservationSha256,
    [ValidateRange(1,60)][int]$SessionMaxCalls = 60
)
$ErrorActionPreference = 'Stop'
$hasTicker = $PSBoundParameters.ContainsKey('Ticker')
$hasAnalysisDate = $PSBoundParameters.ContainsKey('AnalysisDate')
if ($PSBoundParameters.ContainsKey('MarginDate') -and [string]::IsNullOrWhiteSpace($MarginDate)) {
    throw 'MarginDate cannot be empty.'
}
$hasSupplementalReportPeriod = $PSBoundParameters.ContainsKey('SupplementalReportPeriod')
if ($hasSupplementalReportPeriod -and [string]::IsNullOrWhiteSpace($SupplementalReportPeriod)) {
    throw 'SupplementalReportPeriod cannot be empty.'
}
$announcementKeys = @('AnnouncementFile', 'AnnouncementDate', 'AnnouncementSourceUrl')
$announcementCount = @($announcementKeys | Where-Object { $PSBoundParameters.ContainsKey($_) }).Count
if ($announcementCount -ne 0 -and $announcementCount -ne 3) {
    throw 'AnnouncementFile, AnnouncementDate and AnnouncementSourceUrl must all be supplied.'
}
if ($announcementCount -eq 3 -and (-not $hasTicker -or -not $hasAnalysisDate -or
    [string]::IsNullOrWhiteSpace($AnnouncementFile) -or
    [string]::IsNullOrWhiteSpace($AnnouncementDate) -or
    [string]::IsNullOrWhiteSpace($AnnouncementSourceUrl))) {
    throw 'Local announcement requires explicit Ticker/AnalysisDate and three nonempty announcement arguments.'
}
$autoLatest = $Full -and $hasTicker -and -not $hasAnalysisDate
if ($PSBoundParameters.ContainsKey('DecisionMode') -and (-not $autoLatest -or $CalendarOnly -or $FiveSnapshotsOnce -or $Guided)) { throw 'DecisionMode requires ordinary latest Full.' }
if ($Run -and (-not $autoLatest -or $CheckOnly -or -not $UseProjectEnv)) {
    throw '-Run requires default latest Full with -UseProjectEnv and no -CheckOnly.'
}
if ($CalendarNineOnce -or $FiveSnapshotsOnce) { throw 'Historical launch unavailable.' }
if ($CalendarOnly) {
    if (-not $Run -or ($PSBoundParameters.ContainsKey('SessionMaxCalls') -and $SessionMaxCalls -ne 1)) {
        throw '-CalendarOnly requires -Run and fixes the request budget at 1.'
    }
    $SessionMaxCalls = 1
}
if ($autoLatest -and -not $CheckOnly -and -not $Run) {
    throw 'Add -Run -UseProjectEnv to start latest Full, or use -CheckOnly for an offline check.'
}
if ($autoLatest) { $AnalysisDate = 'latest'; $hasAnalysisDate = $true }
if ($hasTicker -ne $hasAnalysisDate) {
    throw 'Ticker and AnalysisDate must both be supplied.'
}
if ($PowerShellNews -and -not $hasTicker -and -not $Guided) {
    throw 'PowerShellNews requires explicit Ticker/AnalysisDate.'
}
if ($PowerShellFundFlow -and -not $hasTicker -and -not $Guided) {
    throw 'PowerShellFundFlow requires explicit Ticker/AnalysisDate or Guided.'
}
if ($NewsSourceIsolation -and -not $PowerShellNews -and -not $Guided) {
    throw 'NewsSourceIsolation requires PowerShellNews or Guided.'
}
if ($hasTicker -and ([string]::IsNullOrWhiteSpace($Ticker) -or [string]::IsNullOrWhiteSpace($AnalysisDate))) {
    throw 'Ticker and AnalysisDate cannot be empty.'
}
$sourceRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $sourceRoot '.venv/Scripts/python.exe'
$entryPath = Join-Path $PSScriptRoot 'analyze_mainland_bounded.py'
foreach ($boundPath in @($pythonPath, $entryPath)) {
    $entry = Get-Item -LiteralPath $boundPath -Force
    if ($entry.PSIsContainer -or ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Bound entry must be an ordinary file.'
    }
}
$entryArguments = @('-I', '-B', $entryPath)
if ($CalendarOnly -or $PSBoundParameters.ContainsKey('SessionMaxCalls')) { $entryArguments += @('--session-max-calls', [string]$SessionMaxCalls) }
if ($Full) { $entryArguments += '--full' }
if ($PSBoundParameters.ContainsKey('DecisionMode')) { $entryArguments += @('--decision-mode', $DecisionMode) }
if ($autoLatest) { $entryArguments += '--auto-latest' }
if ($Run) { $entryArguments += '--run' }
if ($CalendarOnly) { $entryArguments += '--calendar-only' }
foreach ($name in $Enhancement) { $entryArguments += @('--enhancement', $name) }
if ($CheckOnly) { $entryArguments += '--check-only' }
if ($Guided) { $entryArguments += @('--guided', '--guided-powershell-executable', (Join-Path $PSHOME 'pwsh.exe')) }
if ($UseProjectEnv) { $entryArguments += '--use-project-env' }
if ($Moneyflow) { $entryArguments += '--moneyflow' }
if ($NewsSourceIsolation) { $entryArguments += '--news-source-isolation' }
if ($PowerShellNews) {
    $entryArguments += @('--powershell-news', '--powershell-news-executable', (Join-Path $PSHOME 'pwsh.exe'))
}
if ($PowerShellFundFlow) {
    $entryArguments += @('--powershell-fundflow', '--powershell-fundflow-executable', (Join-Path $PSHOME 'pwsh.exe'))
}
if ($PSBoundParameters.ContainsKey('MarginDate')) { $entryArguments += @('--margin-date', $MarginDate) }
if ($PSBoundParameters.ContainsKey('ObservationCacheMode')) { $entryArguments += @('--observation-cache-mode', $ObservationCacheMode) }
if ($PSBoundParameters.ContainsKey('FundflowObservationSha256')) { $entryArguments += @('--fundflow-observation-sha256', $FundflowObservationSha256) }
if ($BankSnapshot) { $entryArguments += @('--bank-snapshot', $BankSnapshot) }
if ($hasTicker) { $entryArguments += @('--ticker', $Ticker, '--analysis-date', $AnalysisDate) }
if ($hasSupplementalReportPeriod) { $entryArguments += @('--supplemental-report-period', $SupplementalReportPeriod) }
if ($announcementCount -eq 3) {
    $entryArguments += @('--announcement-file', $AnnouncementFile, '--announcement-date', $AnnouncementDate,
                         '--announcement-source-url', $AnnouncementSourceUrl)
}
# Inherit the caller's standard handles, including pipes. Shell/native-command
# dispatch may detach in a hosted Windows runner; own and wait for this process.
$startInfo = [System.Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $pythonPath
$startInfo.WorkingDirectory = $sourceRoot
$startInfo.UseShellExecute = $false
# Supply only the current host to the Python child. load_policy validates the
# executable and helper against the release before registering either producer.
if ($Full -and $PSVersionTable.PSVersion.Major -eq 7 -and
    !$startInfo.Environment.ContainsKey('TRADINGAGENTS_ORDINARY_FULL_PWSH')) {
    $startInfo.Environment['TRADINGAGENTS_ORDINARY_FULL_PWSH'] = [Environment]::ProcessPath
}
foreach ($argument in $entryArguments) {
    $startInfo.ArgumentList.Add($argument)
}
$child = [System.Diagnostics.Process]::new()
$child.StartInfo = $startInfo
try {
    if (-not $child.Start()) {
        throw 'Bound Python process did not start.'
    }
    $child.WaitForExit()
    $exitCode = $child.ExitCode
} finally {
    # Disposing the handle never terminates an independently running process.
    $child.Dispose()
}
exit $exitCode
