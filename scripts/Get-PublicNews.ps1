# Fixed child worker. Body is returned only on anonymous stdout to the parent;
# it is never a terminal output, log, temporary file or executable expression.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$VerbosePreference = 'SilentlyContinue'
$DebugPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$result = [ordered]@{status='input_invalid'; http_status=$null; body=$null; cleanup='not_opened'; request_attempted=$false}
$response = $null
try {
    # The parent writes one small JSON line; never evaluate input as PowerShell.
    $line = [Console]::ReadLine()
    if ($null -eq $line -or $line.Length -gt 2048) { throw 'input' }
    $inputData = ConvertFrom-Json -InputObject $line -Depth 8 -AsHashtable
    if ($inputData.Count -ne 2 -or -not $inputData.ContainsKey('url') -or -not $inputData.ContainsKey('timeout_seconds')) { throw 'input' }
    $uri = [Uri]$inputData.url
    if ($inputData.url -isnot [string] -or $uri.Scheme -cne 'https' -or
        $uri.Host -cne 'search-api-web.eastmoney.com' -or $uri.AbsolutePath -cne '/search/jsonp' -or
        $uri.Port -ne 443 -or $uri.UserInfo -ne '' -or $uri.Fragment -ne '' -or
        $inputData.url -notmatch '^https://search-api-web\.eastmoney\.com/search/jsonp\?cb=[A-Za-z_][A-Za-z0-9_]*&param=[A-Za-z0-9%._~-]+&_=[0-9]{13}$' -or
        $inputData.timeout_seconds -isnot [long] -or $inputData.timeout_seconds -lt 1 -or $inputData.timeout_seconds -gt 20) { throw 'input' }
    $result.status = 'runtime_unsupported'
    if ($PSVersionTable.PSVersion.Major -ne 7 -or $PSVersionTable.PSVersion -lt [version]'7.4') { throw 'version' }
    # A compiled built-in command only; no profile function or user module.
    $result.status = 'cmdlet_invalid'
    $command = Get-Command Microsoft.PowerShell.Utility\Invoke-WebRequest -CommandType Cmdlet -ErrorAction Stop
    $assembly = [IO.Path]::GetFullPath($command.ImplementingType.Assembly.Location)
    if ($command.ModuleName -cne 'Microsoft.PowerShell.Utility' -or
        -not $assembly.StartsWith(([IO.Path]::GetFullPath($PSHOME) + [IO.Path]::DirectorySeparatorChar), [StringComparison]::OrdinalIgnoreCase) -or
        -not $command.Parameters.ContainsKey('OperationTimeoutSeconds')) { throw 'cmdlet' }
    $result.status = 'request_failed'
    $result.request_attempted = $true
    $response = & $command -Uri $uri -Method Get -MaximumRedirection 0 -MaximumRetryCount 0 `
        -ConnectionTimeoutSeconds $inputData.timeout_seconds -OperationTimeoutSeconds $inputData.timeout_seconds `
        -SkipHttpErrorCheck -ErrorAction Stop
    $result.http_status = [int]$response.StatusCode
    $result.cleanup = 'pending'
    $result.status = 'http_error'
    if ($result.http_status -eq 200) {
        # IWR buffers in its owned process. Bound the body before copying into
        # IPC; parent also caps stdout and enforces the absolute child deadline.
        $result.status = 'response_limit'
        if ($null -ne $response.RawContentStream -and $response.RawContentStream.Length -le 1048576) {
            $bytes = $response.RawContentStream.ToArray()
            $result.body = [Convert]::ToBase64String($bytes)
            $result.status = 'ok'
        }
    }
} catch {
    # Never serialize ErrorRecord, exception messages, response or request data.
    $result.body = $null
} finally {
    if ($null -ne $response) {
        try {
            if ($null -ne $response.RawContentStream) { $response.RawContentStream.Dispose() }
            if ($null -ne $response.BaseResponse) { $response.BaseResponse.Dispose() }
            $result.cleanup = 'closed'
        } catch {
            $result.cleanup = 'close_failed'
            $result.body = $null
        }
    }
}
[Console]::Write(($result | ConvertTo-Json -Compress -Depth 3))
if ($result.status -eq 'ok' -and $result.cleanup -eq 'closed') { exit 0 }
exit 1
