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

    throw "找不到 docker.exe，请先安装 Docker Desktop。"
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
        throw "Docker 未运行，并且找不到 Docker Desktop。"
    }

    Start-Process -FilePath $desktopPath -WindowStyle Hidden
}

function Invoke-ComposeUp {
    param([string]$DockerCommand)

    $previousPreference = $ErrorActionPreference
    try {
        # Compose 会把正常进度写入标准错误流，因此同时捕获两个输出流，
        # 并使用原生命令退出码，避免 Windows PowerShell 把进度误判为失败。
        $ErrorActionPreference = "Continue"
        $output = @(
            & $DockerCommand compose --project-directory $projectDir up -d --remove-orphans 2>&1
        )
        $composeExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }

    if ($composeExitCode -eq 0) {
        return
    }

    $details = @(
        $output |
            ForEach-Object { ([string]$_).Trim() } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            Select-Object -Last 8
    ) -join " | "
    if ($details) {
        throw "docker compose 启动失败，退出码 ${composeExitCode}：$details"
    }
    throw "docker compose 启动失败，退出码为 $composeExitCode。"
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
        throw "Docker 未能在 $DockerWaitSeconds 秒内就绪。"
    }

    if ($WaitForDockerOnly) {
        return
    }

    Invoke-ComposeUp -DockerCommand $docker
    Write-AutostartLog "Docker Compose 服务已启动。"
} catch {
    Write-AutostartLog "启动失败：$($_.Exception.Message)"
    throw
}
