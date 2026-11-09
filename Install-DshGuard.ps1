[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Node,
    [Parameter(Mandatory=$true)][string]$Runtime,
    [string]$DshHome = $(if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $env:USERPROFILE '.dsh' }),
    [string]$Base = (Join-Path $env:LOCALAPPDATA 'dsh-guard'),
    [int]$Port = 3080,
    [int]$ConsolePort = 3081,
    [switch]$RegisterStartup,
    [switch]$Start
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this package on native Windows.' }
if ([IO.Path]::GetExtension($Python) -ne '.exe') { throw 'Select an absolute python.exe, not a command wrapper.' }
$Python = (Resolve-Path -LiteralPath $Python).Path
if ($Base -notmatch '^[A-Za-z]:[\\/]') { throw 'Use a local absolute NTFS installation directory.' }
$Base = [IO.Path]::GetFullPath($Base)
$drive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($Base))
if ($drive.DriveFormat -ne 'NTFS') { throw 'This preview requires NTFS for locking and exclusive queue publication.' }
$cursor = $Base
while ($cursor) {
    if (Test-Path -LiteralPath $cursor) {
        $item = Get-Item -Force -LiteralPath $cursor
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reparse point in installation path.' }
    }
    $parent = [IO.Directory]::GetParent($cursor)
    if ($null -eq $parent) { break }
    $cursor = $parent.FullName
}
if ((Test-Path -LiteralPath $Base) -and @(Get-ChildItem -Force -LiteralPath $Base).Count -gt 0) {
    throw 'Installation directory must be new or empty; existing installations are preserved.'
}
$manifestPath = Join-Path $PSScriptRoot 'windows-package.json'
if (Test-Path -LiteralPath $manifestPath) {
    $manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifestPath | ConvertFrom-Json
    foreach ($file in $manifest.files.PSObject.Properties) {
        if ($file.Name.Contains('..') -or [IO.Path]::IsPathRooted($file.Name)) { throw 'Invalid package member.' }
        $path = Join-Path $PSScriptRoot $file.Name
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $file.Value) {
            throw ('Package hash mismatch: ' + $file.Name)
        }
    }
}
[void][IO.Directory]::CreateDirectory($Base)
# A private per-user install. No credentials, administrator account or service password.
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = [Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
$acl.SetOwner($sid)
foreach ($account in @($sid, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new($account, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    [void]$acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $Base -AclObject $acl
& $Python -I (Join-Path $PSScriptRoot 'windows_setup.py') --source $PSScriptRoot --base $Base --home $DshHome --runtime $Runtime --node $Node --port $Port --console-port $ConsolePort
if ($LASTEXITCODE -ne 0) { throw 'Enrollment failed. DSH configuration was not changed; keep the partial install for diagnosis.' }
$info = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Base 'installation.json') | ConvertFrom-Json
$taskName = 'DSH Guard - ' + $sid.Value
if ($RegisterStartup) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'Startup task already exists; refusing to replace it.' }
    $action = New-ScheduledTaskAction -Execute $info.pythonw -Argument $info.arguments -WorkingDirectory $Base
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $sid.Value
    $principal = New-ScheduledTaskPrincipal -UserId $sid.Value -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $settings.AllowHardTerminate = $false
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
}
if ($Start) {
    if ($RegisterStartup) { Start-ScheduledTask -TaskName $taskName }
    else { Start-Process -FilePath $info.pythonw -ArgumentList $info.arguments -WorkingDirectory $Base | Out-Null }
}
Write-Output ('Installed in observation mode: ' + $Base)
Write-Output ('Console after startup: http://127.0.0.1:' + $ConsolePort)
Write-Output ('CLI: & "' + (Join-Path $Base 'dsh-guard.ps1') + '" status')
Write-Output 'Windows real-machine acceptance is pending. Run Test-DshGuard.ps1 before managing DSH.'
Write-Output 'Snapshots, rescue, candidate validation and Claude repair are disabled in this preview.'
