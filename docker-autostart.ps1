[CmdletBinding()]
param(
    [ValidateRange(30, 600)]
    [int]$DockerWaitSeconds = 180,

    [switch]$WaitForDockerOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectDir = $PSScriptRoot
$logDir = Join-Path $env:LOCALAPPDATA "SnowAIStudio"
$logPath = Join-Path $logDir "docker-autostart.log"

function Write-AutostartLog {
    param([string]$Message)

    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logPath -Value "$timestamp $Message" -Encoding UTF8
}

function Resolve-DockerCommand {
    $command = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $bundledCommand = Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe"
    if (Test-Path -LiteralPath $bundledCommand) {
        return $bundledCommand
    }

    throw "docker.exe was not found. Install Docker Desktop first."
}

function Test-DockerReady {
    param([string]$DockerCommand)

    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        & $DockerCommand info *> $null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    return $exitCode -eq 0
}

function Start-DockerDesktop {
    if (Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue) {
        return
    }

    $desktopPaths = @(
        (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
        (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe")
    )
    $desktopPath = $desktopPaths | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $desktopPath) {
        throw "Docker is not running and Docker Desktop was not found."
    }

    Start-Process -FilePath $desktopPath -WindowStyle Hidden
}

try {
    $docker = Resolve-DockerCommand
    if (-not (Test-DockerReady -DockerCommand $docker)) {
        Start-DockerDesktop
        $deadline = (Get-Date).AddSeconds($DockerWaitSeconds)
        do {
            Start-Sleep -Seconds 3
            if (Test-DockerReady -DockerCommand $docker) {
                break
            }
        } while ((Get-Date) -lt $deadline)
    }

    if (-not (Test-DockerReady -DockerCommand $docker)) {
        throw "Docker did not become ready within $DockerWaitSeconds seconds."
    }

    if ($WaitForDockerOnly) {
        return
    }

    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        & $docker compose --project-directory $projectDir up -d --remove-orphans *> $null
        $composeExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($composeExitCode -ne 0) {
        throw "docker compose up failed with exit code $composeExitCode."
    }
    Write-AutostartLog "Docker Compose services started."
} catch {
    Write-AutostartLog "Startup failed: $($_.Exception.Message)"
    throw
}
