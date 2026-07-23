[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 18081,

    [ValidatePattern("^[A-Za-z0-9_.-]+$")]
    [string]$AdminUsername = "admin",

    [switch]$Lan,
    [switch]$LocalOnly,
    [switch]$SkipFirewall,
    [switch]$NoBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($LocalOnly) {
    $Lan = $false
}

$projectDir = $PSScriptRoot
$envPath = Join-Path $projectDir ".env"
$firewallRuleName = "Snow AI Studio LAN"

function Get-FirewallRule {
    try {
        $policy = New-Object -ComObject HNetCfg.FwPolicy2
        return $policy.Rules.Item($firewallRuleName)
    } catch [IO.FileNotFoundException] {
        return $null
    }
}

function Test-FirewallRule {
    param($Rule, [int]$ListenPort)

    # HNetCfg values: inbound=1, allow=1, all profiles=0x7fffffff, TCP=6.
    return (
        $null -ne $Rule -and
        $Rule.Enabled -and
        $Rule.Direction -eq 1 -and
        $Rule.Action -eq 1 -and
        $Rule.Profiles -eq [int]0x7FFFFFFF -and
        $Rule.Protocol -eq 6 -and
        $Rule.LocalPorts -eq [string]$ListenPort -and
        $Rule.RemoteAddresses -eq "LocalSubnet"
    )
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
    Set-EnvValue $lines "LUCIDA_IMAGE" "snow-ai-studio-lucida:latest" -ReplaceBlank | Out-Null
    Set-EnvValue $lines "LUCIDA_MATTING_URL" "http://lucida:8000" -ReplaceBlank | Out-Null
    Set-EnvValue $lines "LUCIDA_MATTING_MODEL" "lucida" -ReplaceBlank | Out-Null
    Set-EnvValue $lines "LUCIDA_MATTING_TIMEOUT_SECONDS" "120" -ReplaceBlank | Out-Null
    Set-EnvValue $lines "LUCIDA_TORCH_INDEX_URL" "https://download.pytorch.org/whl/cu124" -ReplaceBlank | Out-Null
    Set-EnvValue $lines "LUCIDA_MODEL_PATH" "./.tmp-lucida-src/lucida-main/.model/lucida" -ReplaceBlank | Out-Null
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

function Get-LanAddresses {
    return [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() |
        Where-Object {
            $properties = $_.GetIPProperties()
            $_.OperationalStatus -eq [Net.NetworkInformation.OperationalStatus]::Up -and
            $_.NetworkInterfaceType -notin @(
                [Net.NetworkInformation.NetworkInterfaceType]::Loopback,
                [Net.NetworkInformation.NetworkInterfaceType]::Tunnel
            ) -and
            $_.Description -notmatch "TAP-Windows|Hyper-V|Docker|WSL" -and
            @($properties.GatewayAddresses | Where-Object {
                $_.Address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork -and
                -not $_.Address.Equals([Net.IPAddress]::Any)
            }).Count -gt 0
        } |
        ForEach-Object { $_.GetIPProperties().UnicastAddresses } |
        ForEach-Object { $_.Address } |
        Where-Object {
            $_.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork -and
            -not [Net.IPAddress]::IsLoopback($_) -and
            -not $_.IPAddressToString.StartsWith("169.254.")
        } |
        ForEach-Object { $_.IPAddressToString } |
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

    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        docker info *> $null
        $dockerReady = $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if (-not $dockerReady) {
        throw "Docker 引擎未运行。请启动 Docker Desktop 后重试。"
    }

    $generatedAdminPassword = Initialize-EnvironmentFile

    $firewallRule = Get-FirewallRule
    if ($Lan -and -not $SkipFirewall -and -not (Test-FirewallRule -Rule $firewallRule -ListenPort $Port)) {
        Write-Host "正在配置 Windows 防火墙，需要确认一次管理员权限。" -ForegroundColor Yellow
        $operation = if ($null -eq $firewallRule) { "add" } else { "set" }
        $firewallArguments = @(
            "advfirewall", "firewall", $operation, "rule",
            "name=`"$firewallRuleName`""
        )
        if ($operation -eq "set") {
            $firewallArguments += "new"
        }
        $firewallArguments += @(
            "dir=in", "action=allow", "enable=yes", "profile=any",
            "protocol=TCP", "localport=$Port", "remoteip=LocalSubnet"
        )
        $process = Start-Process `
            -FilePath (Join-Path $env:SystemRoot "System32\netsh.exe") `
            -ArgumentList $firewallArguments `
            -Verb RunAs `
            -WindowStyle Hidden `
            -Wait `
            -PassThru
        if ($process.ExitCode -ne 0 -or -not (Test-FirewallRule -Rule (Get-FirewallRule) -ListenPort $Port)) {
            throw "Windows 防火墙规则未正确生效。"
        }
        Write-Host "Windows 防火墙已允许本地子网访问 TCP 端口 $Port。" -ForegroundColor Green
    }

    if (-not (Test-PortAvailable -ListenPort $Port) -and -not (Test-CurrentStackOwnsPort -ListenPort $Port)) {
        throw "端口 $Port 已被其他进程占用。请停止占用进程，或使用 -Port 选择其他端口。"
    }


    $lucidaSource = Join-Path $projectDir ".tmp-lucida-src\lucida-main"
    $lucidaModel = Join-Path $lucidaSource ".model\lucida\config.json"
    $lucidaServing = Join-Path $lucidaSource "serving\app.py"
    if (-not (Test-Path -LiteralPath $lucidaServing) -or -not (Test-Path -LiteralPath $lucidaModel)) {
        throw "缺少 Lucida 源码/权重：请准备 .tmp-lucida-src\lucida-main（含 serving 与 .model\lucida）。"
    }

    $lucidaImage = "snow-ai-studio-lucida:latest"
    $envLines = if (Test-Path -LiteralPath $envPath) { [IO.File]::ReadAllLines($envPath) } else { @() }
    foreach ($line in $envLines) {
        if ($line.StartsWith("LUCIDA_IMAGE=", [StringComparison]::Ordinal)) {
            $configuredImage = $line.Substring("LUCIDA_IMAGE=".Length).Trim()
            if (-not [string]::IsNullOrWhiteSpace($configuredImage)) {
                $lucidaImage = $configuredImage
            }
            break
        }
    }

    function Test-LocallyUsableLucidaImage {
        param([string]$Image)

        docker image inspect $Image *> $null
        if ($LASTEXITCODE -ne 0) {
            return $false
        }

        docker run --rm --gpus all --entrypoint python $Image -c "import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)" *> $null
        return $LASTEXITCODE -eq 0
    }

    if (-not $NoBuild) {
        Write-Host "正在构建主站、Worker 与 GPU Lucida 镜像（已缓存层会自动复用）..."
        & docker compose --project-directory $projectDir --profile lucida build web worker lucida
        if ($LASTEXITCODE -ne 0) {
            throw "Docker 镜像构建失败。"
        }
    }

    if (-not (Test-LocallyUsableLucidaImage -Image $lucidaImage)) {
        if ($NoBuild) {
            throw "找不到可用的 GPU Lucida 镜像 $lucidaImage；请去掉 -NoBuild 让脚本先构建。"
        }
        throw "GPU Lucida 镜像 CUDA 检查失败。请确认 Docker Desktop 已启用 NVIDIA runtime。"
    } else {
        Write-Host "GPU Lucida 镜像 CUDA 检查通过：$lucidaImage" -ForegroundColor Green
    }

    & docker compose --project-directory $projectDir --profile lucida up -d --no-build --force-recreate --remove-orphans
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose 启动失败。"
    }

    $healthUrl = "http://127.0.0.1:$Port/health"
    $deadline = (Get-Date).AddMinutes(15)
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
        throw "服务未能在 15 分钟内通过健康检查。请运行：docker compose --profile lucida logs web worker lucida"
    }

    Write-Host ""
    Write-Host "Snow AI Studio 已启动（含 Docker Lucida GPU 抠图）。" -ForegroundColor Green
    Write-Host "本机地址：http://127.0.0.1:$Port"
    Write-Host "透明背景：勾选后自动经 Lucida 后处理（LUCIDA_MATTING_URL=http://lucida:8000）"
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
