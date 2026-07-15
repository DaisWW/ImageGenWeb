[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 18081,

    [ValidatePattern("^[A-Za-z0-9_.-]+$")]
    [string]$AdminUsername = "admin",

    [switch]$Lan,
    [switch]$LocalOnly,
    [switch]$SkipFirewall,
    [switch]$SkipAutostart,
    [switch]$NoBuild,
    [switch]$FirewallOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($LocalOnly) {
    $Lan = $false
}

$projectDir = $PSScriptRoot
$envPath = Join-Path $projectDir ".env"
$autostartScript = Join-Path $projectDir "docker-autostart.ps1"
$firewallRuleName = "Snow AI Studio LAN"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Resolve-PowerShellHost {
    $windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    if (Test-Path -LiteralPath $windowsPowerShell) {
        return $windowsPowerShell
    }

    return (Get-Process -Id $PID).Path
}

function Install-FirewallRule {
    param([int]$ListenPort)

    if (-not (Test-IsAdministrator)) {
        throw "配置 Windows 防火墙需要管理员权限。"
    }

    $rule = Get-NetFirewallRule -DisplayName $firewallRuleName -ErrorAction SilentlyContinue
    if ($rule) {
        $rule | Set-NetFirewallRule -Enabled True -Profile Any -Direction Inbound -Action Allow | Out-Null
        $rule | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort $ListenPort | Out-Null
        $rule | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress LocalSubnet | Out-Null
    } else {
        New-NetFirewallRule `
            -DisplayName $firewallRuleName `
            -Description "允许本地子网访问 Snow AI Studio。" `
            -Direction Inbound `
            -Action Allow `
            -Protocol TCP `
            -LocalPort $ListenPort `
            -RemoteAddress LocalSubnet `
            -Profile Any | Out-Null
    }
    Write-Host "Windows 防火墙已允许专用网络访问 TCP 端口 $ListenPort。" -ForegroundColor Green
}

if ($FirewallOnly) {
    Install-FirewallRule -ListenPort $Port
    exit 0
}

function New-RandomSecret {
    param([ValidateRange(16, 128)][int]$ByteLength = 32)

    $bytes = New-Object byte[] $ByteLength
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    } finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Set-EnvValue {
    param(
        [System.Collections.Generic.List[string]]$Lines,
        [string]$Name,
        [string]$Value,
        [switch]$ReplaceBlank,
        [switch]$ReplacePlaceholder
    )

    $prefix = "$Name="
    for ($index = 0; $index -lt $Lines.Count; $index++) {
        if (-not $Lines[$index].StartsWith($prefix, [StringComparison]::Ordinal)) {
            continue
        }

        $currentValue = $Lines[$index].Substring($prefix.Length)
        $shouldReplace = $ReplaceBlank -and [string]::IsNullOrWhiteSpace($currentValue)
        $shouldReplace = $shouldReplace -or ($ReplacePlaceholder -and $currentValue.StartsWith("CHANGE_ME", [StringComparison]::OrdinalIgnoreCase))
        if ($shouldReplace -or $Name -in @("IMAGEGEN_PORT", "IMAGEGEN_BIND_HOST")) {
            $Lines[$index] = "$Name=$Value"
            return $true
        }
        return $false
    }

    $Lines.Add("$Name=$Value") | Out-Null
    return $true
}

function Initialize-EnvironmentFile {
    $lines = New-Object "System.Collections.Generic.List[string]"
    if (Test-Path -LiteralPath $envPath) {
        [IO.File]::ReadAllLines($envPath) | ForEach-Object { $lines.Add($_) | Out-Null }
    } else {
        $lines.Add("# 由 deploy-docker.ps1 生成。请妥善保密并定期备份。") | Out-Null
    }

    Set-EnvValue $lines "POSTGRES_DB" "imagegen" | Out-Null
    Set-EnvValue $lines "POSTGRES_USER" "imagegen" | Out-Null
    Set-EnvValue $lines "POSTGRES_PASSWORD" (New-RandomSecret) -ReplaceBlank -ReplacePlaceholder | Out-Null
    Set-EnvValue $lines "SECRET_KEY" (New-RandomSecret -ByteLength 48) -ReplaceBlank -ReplacePlaceholder | Out-Null
    Set-EnvValue $lines "CONFIG_ENCRYPTION_KEY" (New-RandomSecret -ByteLength 32) -ReplaceBlank | Out-Null
    Set-EnvValue $lines "ADMIN_USERNAME" $AdminUsername -ReplaceBlank | Out-Null
    $adminPassword = New-RandomSecret -ByteLength 24
    $adminPasswordChanged = Set-EnvValue $lines "ADMIN_PASSWORD" $adminPassword -ReplaceBlank -ReplacePlaceholder
    Set-EnvValue $lines "PYTHON_IMAGE" "docker.m.daocloud.io/library/python:3.12-slim" | Out-Null
    Set-EnvValue $lines "POSTGRES_IMAGE" "docker.m.daocloud.io/library/postgres:17-alpine" | Out-Null

    Set-EnvValue $lines "GPT_CHAT_API_BASE_URL" "" | Out-Null
    Set-EnvValue $lines "GPT_CHAT_API_KEY" "" | Out-Null
    Set-EnvValue $lines "GPT_CHAT_MODEL" "gpt-5.6-sol" | Out-Null
    Set-EnvValue $lines "GPT_CHAT_REASONING_EFFORT" "max" | Out-Null
    Set-EnvValue $lines "IMAGE_API_BASE_URL" "" | Out-Null
    Set-EnvValue $lines "IMAGE_API_KEY" "" | Out-Null
    Set-EnvValue $lines "LUCEN_API_BASE_URL" "https://lucen.plus" | Out-Null
    Set-EnvValue $lines "LUCEN_API_KEY" "" | Out-Null
    Set-EnvValue $lines "IMAGEGEN_PORT" ([string]$Port) | Out-Null
    $bindHost = if ($Lan) { "0.0.0.0" } else { "127.0.0.1" }
    Set-EnvValue $lines "IMAGEGEN_BIND_HOST" $bindHost | Out-Null
    Set-EnvValue $lines "COOKIE_SECURE" "false" | Out-Null
    Set-EnvValue $lines "TRUST_PROXY_HEADERS" "false" | Out-Null

    [IO.File]::WriteAllLines($envPath, $lines, (New-Object Text.UTF8Encoding($false)))
    if ($adminPasswordChanged) {
        return $adminPassword
    }
    return $null
}

function Test-PortAvailable {
    param([int]$ListenPort)

    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, $ListenPort)
    try {
        $listener.Start()
        return $true
    } catch [Net.Sockets.SocketException] {
        return $false
    } finally {
        $listener.Stop()
    }
}

function Test-CurrentStackOwnsPort {
    param([int]$ListenPort)

    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        $publishedPort = docker compose --project-directory $projectDir port web 7860 2>$null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    return $exitCode -eq 0 -and ($publishedPort -match ":$ListenPort$")
}

function Install-LoginAutostart {
    $startupDir = [Environment]::GetFolderPath("Startup")
    if ([string]::IsNullOrWhiteSpace($startupDir)) {
        throw "找不到 Windows 启动文件夹。"
    }

    $powerShellPath = Resolve-PowerShellHost
    $shortcutPath = Join-Path $startupDir "Snow AI Studio Docker.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $powerShellPath
    $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$autostartScript`""
    $shortcut.WorkingDirectory = $projectDir
    $shortcut.WindowStyle = 7
    $shortcut.Description = "Windows 登录后启动 Docker Desktop 和 Snow AI Studio。"
    $shortcut.Save()
    Write-Host "已注册 Windows 登录自启动：$shortcutPath" -ForegroundColor Green
}

function Get-LanAddresses {
    return Get-NetIPConfiguration -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPv4DefaultGateway -and
            $_.IPv4Address -and
            $_.InterfaceDescription -notmatch "TAP-Windows|Hyper-V|Docker|WSL"
        } |
        ForEach-Object { $_.IPv4Address.IPAddress } |
        Where-Object { $_ -and $_ -ne "127.0.0.1" -and -not $_.StartsWith("169.254.") } |
        Select-Object -Unique
}

Push-Location $projectDir
try {
    if ($Lan) {
        Write-Warning "局域网模式会通过明文 HTTP 暴露登录和会话流量。请仅在可信网络使用，或在服务前配置 TLS。"
    }
    if (-not (Get-Command docker.exe -ErrorAction SilentlyContinue)) {
        throw "找不到 docker.exe，请先安装 Docker Desktop。"
    }
    if (-not (Test-Path -LiteralPath $autostartScript)) {
        throw "找不到自启动脚本：$autostartScript"
    }

    $generatedAdminPassword = Initialize-EnvironmentFile
    & $autostartScript -WaitForDockerOnly

    if (-not (Test-PortAvailable -ListenPort $Port) -and -not (Test-CurrentStackOwnsPort -ListenPort $Port)) {
        throw "端口 $Port 已被其他进程占用。请停止占用进程，或使用 -Port 选择其他端口。"
    }

    $composeArguments = @("compose", "--project-directory", $projectDir, "up", "-d", "--remove-orphans")
    if (-not $NoBuild) {
        $composeArguments += "--build"
    }
    & docker @composeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose 启动失败。"
    }

    $healthUrl = "http://127.0.0.1:$Port/health"
    $deadline = (Get-Date).AddMinutes(3)
    $healthy = $false
    do {
        try {
            $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) {
                $healthy = $true
                break
            }
        } catch {
            Start-Sleep -Seconds 3
        }
    } while ((Get-Date) -lt $deadline)
    if (-not $healthy) {
        docker compose --project-directory $projectDir ps
        throw "服务未能在 3 分钟内通过健康检查。请运行：docker compose logs web worker"
    }

    if (-not $SkipAutostart) {
        Install-LoginAutostart
    }

    if ($Lan -and -not $SkipFirewall) {
        if (Test-IsAdministrator) {
            Install-FirewallRule -ListenPort $Port
        } else {
            $elevationArguments = @(
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-File", "`"$PSCommandPath`"",
                "-FirewallOnly",
                "-Lan",
                "-Port", $Port
            )
            $process = Start-Process `
                -FilePath (Resolve-PowerShellHost) `
                -ArgumentList $elevationArguments `
                -Verb RunAs `
                -Wait `
                -PassThru
            if ($process.ExitCode -ne 0) {
                Write-Warning "Windows 防火墙配置失败。本机访问仍可使用，但局域网访问可能被阻止。"
            }
        }
    }

    Write-Host ""
    Write-Host "Snow AI Studio 已启动。" -ForegroundColor Green
    Write-Host "本机地址：http://127.0.0.1:$Port"
    if ($Lan) {
        foreach ($address in Get-LanAddresses) {
            Write-Host "局域网地址（明文 HTTP）：http://${address}:$Port"
        }
    } else {
        Write-Host "局域网访问未启用。如确需共享，请使用 -Lan 重新运行。"
    }
    if ($generatedAdminPassword) {
        Write-Host "初始管理员：$AdminUsername" -ForegroundColor Yellow
        Write-Host "初始密码：$generatedAdminPassword" -ForegroundColor Yellow
        Write-Host "请在登录后修改密码；该密码也保存在 .env 中。" -ForegroundColor Yellow
    }
} finally {
    Pop-Location
}
