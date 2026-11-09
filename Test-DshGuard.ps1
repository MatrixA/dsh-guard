[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Node,
    [string]$Output = (Join-Path $env:LOCALAPPDATA ('dsh-guard-acceptance-' + [Guid]::NewGuid().ToString('N') + '\report.json')),
    [switch]$Scheduled
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Use native Windows, not WSL or Wine.' }
$Python = (Resolve-Path -LiteralPath $Python).Path
$Node = (Resolve-Path -LiteralPath $Node).Path
if ([IO.Path]::GetExtension($Python) -ne '.exe' -or [IO.Path]::GetExtension($Node) -ne '.exe') { throw 'Select native .exe files.' }
$Output = [IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath $Output) { throw 'Choose a new report path; previous evidence is preserved.' }
$driver = Join-Path $PSScriptRoot 'windows_acceptance.py'
if (-not $Scheduled) {
    & $Python -I $driver --node $Node --output $Output
    if ($LASTEXITCODE -ne 0) { throw ('Checks incomplete or failed. Report: ' + $Output) }
} else {
    # A separate disposable task tests real background job inheritance. Its
    # fixtures use fresh ports and never read the production guard config.
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $name = 'DSH Guard acceptance - ' + [Guid]::NewGuid().ToString('N')
    $arguments = '-I "' + $driver + '" --node "' + $Node + '" --output "' + $Output + '"'
    $action = New-ScheduledTaskAction -Execute $Python -Argument $arguments -WorkingDirectory $PSScriptRoot
    $principal = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $settings.AllowHardTerminate = $false
    Register-ScheduledTask -TaskName $name -Action $action -Principal $principal -Settings $settings | Out-Null
    $finished = $false
    try {
        Start-ScheduledTask -TaskName $name
        $deadline = [DateTime]::UtcNow.AddMinutes(8)
        do {
            Start-Sleep -Milliseconds 500
            $task = Get-ScheduledTask -TaskName $name
            $info = Get-ScheduledTaskInfo -TaskName $name
            $finished = $info.LastRunTime.Year -gt 2000 -and $task.State -ne 'Running'
        } while (-not $finished -and [DateTime]::UtcNow -lt $deadline)
        if (-not $finished) { throw ('Fixture task timed out; retained for diagnosis: ' + $name) }
        if ($info.LastTaskResult -ne 0 -or -not (Test-Path -LiteralPath $Output)) {
            throw ('Background checks failed with exit ' + $info.LastTaskResult + '. Report if produced: ' + $Output)
        }
    } finally {
        if ($finished) { Unregister-ScheduledTask -TaskName $name -Confirm:$false }
    }
}
$report = Get-Content -Raw -Encoding UTF8 -LiteralPath $Output | ConvertFrom-Json
if (-not $report.native_windows_fixture_checks_passed) { throw ('Native fixture checks did not pass: ' + $Output) }
Write-Output ('Fixture checks passed. Evidence: ' + $Output)
Write-Output 'This does not certify real DSH shutdown, its browser UI or candidate isolation. See WINDOWS.md.'
