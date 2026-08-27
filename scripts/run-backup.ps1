[CmdletBinding()]
param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot)
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-EnvValue {
    param([string]$Name)

    $envPath = Join-Path $ProjectDir ".env"
    if (-not (Test-Path -LiteralPath $envPath)) {
        return ""
    }
    $prefix = "$Name="
    foreach ($line in [IO.File]::ReadAllLines($envPath)) {
        if ($line.StartsWith($prefix, [StringComparison]::Ordinal)) {
            return $line.Substring($prefix.Length).Trim()
        }
    }
    return ""
}

$python = Get-Command py.exe -ErrorAction SilentlyContinue
$pythonArgs = @("-3")
if ($null -eq $python) {
    $python = Get-Command python.exe -ErrorAction Stop
    $pythonArgs = @()
}
$retentionValue = Get-EnvValue "IMAGEGEN_BACKUP_RETENTION_DAYS"
$retentionDays = 0
if (-not [int]::TryParse($retentionValue, [ref]$retentionDays) -or $retentionDays -lt 1) {
    $retentionDays = 30
}
$arguments = @(
    "scripts/backup.py",
    "--output", "backups",
    "--env-file", ".env",
    "--retention-days", [string]$retentionDays
)
$mirror = Get-EnvValue "IMAGEGEN_BACKUP_MIRROR"
if (-not [string]::IsNullOrWhiteSpace($mirror)) {
    $arguments += @("--mirror", $mirror)
}

Push-Location $ProjectDir
try {
    & $python.Source @pythonArgs @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Backup command failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}
