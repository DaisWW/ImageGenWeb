[CmdletBinding()]
param(
    [string]$TaskName = "Snow AI Studio Daily Backup",
    [ValidatePattern("^([01]\d|2[0-3]):[0-5]\d$")]
    [string]$At = "03:00"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$runner = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "run-backup.ps1")).Path
$projectDir = (Split-Path -Parent $PSScriptRoot)
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -ProjectDir `"$projectDir`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$account = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }
$principal = New-ScheduledTaskPrincipal `
    -UserId $account `
    -LogonType S4U `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Daily verified Snow AI Studio backup and restore drill." `
    -Force | Out-Null

Write-Host "Scheduled task installed: $TaskName at $At"
