[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 18081,

    [ValidatePattern("^[A-Za-z0-9_.-]+$")]
    [string]$AdminUsername = "admin",

    # LAN is the product default for this deployment. Use -LocalOnly to
    # opt out; keep -Lan for backwards-compatible explicit invocation.
    [switch]$Lan,
    [switch]$LocalOnly,
    [switch]$SkipFirewall,
    [switch]$NoBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# LISTENER POLICY - DO NOT CHANGE: LAN is the default; loopback is explicit.
if ($LocalOnly) {
    $Lan = $false
} else {
    $Lan = $true
}

$projectDir = $PSScriptRoot
$envPath = Join-Path $projectDir ".env"
$firewallRuleName = "Snow AI Studio LAN"
$httpsFirewallRuleName = "Snow AI Studio HTTPS LAN"

# LISTENER CONTRACT - DO NOT CHANGE for LAN deployments: HTTP publishes on
# every host interface; loopback is only for explicit -LocalOnly or HTTPS Web.
$lanBindHost = "0.0.0.0"
$loopbackBindHost = "127.0.0.1"

function Get-FirewallRule {
    param([string]$RuleName = $firewallRuleName)

    try {
        $policy = New-Object -ComObject HNetCfg.FwPolicy2
        return $policy.Rules.Item($RuleName)
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
        if ($shouldReplace -or $Name -in @("IMAGEGEN_PORT", "IMAGEGEN_BIND_HOST", "IMAGEGEN_HTTPS_BIND_HOST")) {
            $Lines[$index] = "$Name=$Value"
            return $true
        }
        return $false
    }

    $Lines.Add("$Name=$Value") | Out-Null
    return $true
}

function Get-EnvFlag {
    param(
        [System.Collections.IEnumerable]$Lines,
        [string]$Name
    )

    $prefix = "$Name="
    foreach ($line in $Lines) {
        if ($line.StartsWith($prefix, [StringComparison]::Ordinal)) {
            return $line.Substring($prefix.Length).Trim().ToLowerInvariant() -in @("1", "true", "yes", "on")
        }
    }
    return $false
}

function Get-EnvValue {
    param(
        [System.Collections.IEnumerable]$Lines,
        [string]$Name
    )

    $prefix = "$Name="
    foreach ($line in $Lines) {
        if ($line.StartsWith($prefix, [StringComparison]::Ordinal)) {
            return $line.Substring($prefix.Length).Trim()
        }
    }
    return ""
}

function Initialize-EnvironmentFile {
    param([bool]$LanMode)

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
    Set-EnvValue $lines "BACKGROUND_REMOVAL_CONCURRENCY" "2" -ReplaceBlank | Out-Null
    Set-EnvValue $lines "LUCIDA_TORCH_INDEX_URL" "https://download.pytorch.org/whl/cu124" -ReplaceBlank | Out-Null
    Set-EnvValue $lines "LUCIDA_MODEL_PATH" "./.tmp-lucida-src/lucida-main/.model/lucida" -ReplaceBlank | Out-Null
    Set-EnvValue $lines "IMAGEGEN_PORT" ([string]$Port) | Out-Null
    $httpsProxyEnabled = Get-EnvFlag -Lines $lines -Name "IMAGEGEN_HTTPS_ENABLED"
    # Keep the public HTTP listener explicit. Never silently fall back to a
    # loopback bind when the deployment was requested for the LAN.
    $bindHost = if ($httpsProxyEnabled -or -not $LanMode) { $loopbackBindHost } else { $lanBindHost }
    Set-EnvValue $lines "IMAGEGEN_BIND_HOST" $bindHost | Out-Null
    $httpsBindHost = if ($httpsProxyEnabled -and $LanMode) { $lanBindHost } else { $loopbackBindHost }
    Set-EnvValue $lines "IMAGEGEN_HTTPS_BIND_HOST" $httpsBindHost | Out-Null
    Set-EnvValue $lines "COOKIE_SECURE" "false" -ReplaceBlank | Out-Null
    Set-EnvValue $lines "TRUST_PROXY_HEADERS" "false" -ReplaceBlank | Out-Null
    Set-EnvValue $lines "IMAGEGEN_HTTPS_ENABLED" "false" -ReplaceBlank | Out-Null
    Set-EnvValue $lines "IMAGEGEN_HTTPS_HOST" "localhost" -ReplaceBlank | Out-Null
    Set-EnvValue $lines "IMAGEGEN_HTTPS_PORT" "18443" -ReplaceBlank | Out-Null
    Set-EnvValue $lines "IMAGEGEN_CADDY_TLS" "tls internal" | Out-Null
    Set-EnvValue $lines "CADDY_IMAGE" "caddy:2-alpine" -ReplaceBlank | Out-Null
    Set-EnvValue $lines "IMAGEGEN_BACKUP_RETENTION_DAYS" "30" -ReplaceBlank | Out-Null
    Set-EnvValue $lines "IMAGEGEN_BACKUP_TIME" "03:00" -ReplaceBlank | Out-Null
    Set-EnvValue $lines "IMAGEGEN_BACKUP_MIRROR" "" | Out-Null

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
    param(
        [int]$ListenPort,
        [string]$Service = "web",
        [int]$ContainerPort = 7860,
        [switch]$HttpsProfile
    )

    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        $composeArguments = @("--project-directory", $projectDir)
        if ($HttpsProfile) {
            $composeArguments += @("--profile", "https")
        }
        $publishedPort = docker compose @composeArguments port $Service $ContainerPort 2>$null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    return $exitCode -eq 0 -and ($publishedPort -match ":$ListenPort$")
}

function Assert-PublishedListener {
    param(
        [string]$Service,
        [int]$ContainerPort,
        [string]$ExpectedHost,
        [int]$ExpectedPort,
        [string]$Label,
        [switch]$HttpsProfile
    )

    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        $composeArguments = @("--project-directory", $projectDir)
        if ($HttpsProfile) {
            $composeArguments += @("--profile", "https")
        }
        $publishedPort = docker compose @composeArguments port $Service $ContainerPort 2>$null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }

    $publishedPort = @($publishedPort | ForEach-Object { "$_".Trim() } | Where-Object { $_ })[0]
    $expectedPublished = "$ExpectedHost`:$ExpectedPort"
    if ($exitCode -ne 0 -or $publishedPort -ne $expectedPublished) {
        throw "监听校验失败（$Label）：Docker 当前发布为 '$publishedPort'，预期为 '$expectedPublished'。LAN 请使用 -Lan；仅本机访问请显式使用 -LocalOnly。"
    }
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

    $existingBindHost = ""
    if (Test-Path -LiteralPath $envPath) {
        $existingLines = [IO.File]::ReadAllLines($envPath)
        $existingBindHost = Get-EnvValue -Lines $existingLines -Name "IMAGEGEN_BIND_HOST"
        if ($existingBindHost -and $existingBindHost -notin @($lanBindHost, $loopbackBindHost)) {
            throw "IMAGEGEN_BIND_HOST 只能是 $lanBindHost 或 $loopbackBindHost，当前值 '$existingBindHost' 无效。"
        }
    }
    $effectiveLan = $Lan

    $generatedAdminPassword = Initialize-EnvironmentFile -LanMode $effectiveLan
    $envLines = [IO.File]::ReadAllLines($envPath)
    $httpsProxyEnabled = Get-EnvFlag -Lines $envLines -Name "IMAGEGEN_HTTPS_ENABLED"
    $expectedWebBindHost = if ($httpsProxyEnabled) { $loopbackBindHost } elseif ($effectiveLan) { $lanBindHost } else { $loopbackBindHost }
    $actualWebBindHost = Get-EnvValue -Lines $envLines -Name "IMAGEGEN_BIND_HOST"
    if ($actualWebBindHost -ne $expectedWebBindHost) {
        throw "监听配置不一致：Web 预期绑定 $expectedWebBindHost，实际为 $actualWebBindHost。请使用 -Lan 或显式 -LocalOnly。"
    }
    [int]$backupRetentionDays = 30
    $configuredRetention = Get-EnvValue -Lines $envLines -Name "IMAGEGEN_BACKUP_RETENTION_DAYS"
    [int]$parsedRetention = 0
    if ([int]::TryParse($configuredRetention, [ref]$parsedRetention) -and $parsedRetention -ge 1) {
        $backupRetentionDays = $parsedRetention
    }
    [int]$httpsPort = 18443
    $httpsHost = "localhost"
    foreach ($line in $envLines) {
        if ($line.StartsWith("IMAGEGEN_HTTPS_PORT=", [StringComparison]::Ordinal)) {
            $configuredHttpsPort = $line.Substring("IMAGEGEN_HTTPS_PORT=".Length).Trim()
            if (-not [int]::TryParse($configuredHttpsPort, [ref]$httpsPort) -or $httpsPort -lt 1 -or $httpsPort -gt 65535) {
                throw "IMAGEGEN_HTTPS_PORT 必须是 1 到 65535 之间的端口。"
            }
        } elseif ($line.StartsWith("IMAGEGEN_HTTPS_HOST=", [StringComparison]::Ordinal)) {
            $httpsHost = $line.Substring("IMAGEGEN_HTTPS_HOST=".Length).Trim()
        }
    }

    $preDeployBackup = $null
    $runningServices = docker compose --project-directory $projectDir ps --services --status running
    if ($LASTEXITCODE -eq 0 -and $runningServices -contains "db") {
        $python = Get-Command py.exe -ErrorAction SilentlyContinue
        $pythonArguments = @("-3")
        if ($null -eq $python) {
            $python = Get-Command python.exe -ErrorAction Stop
            $pythonArguments = @()
        }
        Write-Host "正在创建并演练部署前备份..."
        $backupOutput = & $python.Source @pythonArguments scripts/backup.py `
            --output backups `
            --env-file .env `
            --retention-days $backupRetentionDays
        if ($LASTEXITCODE -ne 0) {
            throw "部署前备份或恢复演练失败，已停止部署。"
        }
        $preDeployBackup = @($backupOutput)[-1]
        Write-Host "部署前备份：$preDeployBackup" -ForegroundColor Green
    }

    if ($effectiveLan -and $httpsProxyEnabled) {
        Write-Host "局域网模式将通过 HTTPS 反向代理提供访问。" -ForegroundColor Green
    } elseif ($effectiveLan) {
        Write-Warning "局域网模式会通过明文 HTTP 暴露登录和会话流量。请仅在可信网络使用，或在服务前配置 TLS。"
    }

    $firewallPort = if ($httpsProxyEnabled) { $httpsPort } else { $Port }
    $firewallRuleNameForMode = if ($httpsProxyEnabled) { $httpsFirewallRuleName } else { $firewallRuleName }
    $firewallRule = Get-FirewallRule -RuleName $firewallRuleNameForMode
    if ($effectiveLan -and -not $SkipFirewall -and -not (Test-FirewallRule -Rule $firewallRule -ListenPort $firewallPort)) {
        Write-Host "正在配置 Windows 防火墙，需要确认一次管理员权限。" -ForegroundColor Yellow
        $operation = if ($null -eq $firewallRule) { "add" } else { "set" }
        $firewallArguments = @(
            "advfirewall", "firewall", $operation, "rule",
            "name=`"$firewallRuleNameForMode`""
        )
        if ($operation -eq "set") {
            $firewallArguments += "new"
        }
        $firewallArguments += @(
            "dir=in", "action=allow", "enable=yes", "profile=any",
            "protocol=TCP", "localport=$firewallPort", "remoteip=LocalSubnet"
        )
        $process = Start-Process `
            -FilePath (Join-Path $env:SystemRoot "System32\netsh.exe") `
            -ArgumentList $firewallArguments `
            -Verb RunAs `
            -WindowStyle Hidden `
            -Wait `
            -PassThru
        if ($process.ExitCode -ne 0 -or -not (Test-FirewallRule -Rule (Get-FirewallRule -RuleName $firewallRuleNameForMode) -ListenPort $firewallPort)) {
            throw "Windows 防火墙规则未正确生效。"
        }
        $firewallProtocol = if ($httpsProxyEnabled) { "HTTPS" } else { "HTTP" }
        Write-Host "Windows 防火墙已允许本地子网访问 $firewallProtocol TCP 端口 $firewallPort。" -ForegroundColor Green
    }

    if (-not (Test-PortAvailable -ListenPort $Port) -and -not (Test-CurrentStackOwnsPort -ListenPort $Port)) {
        throw "端口 $Port 已被其他进程占用。请停止占用进程，或使用 -Port 选择其他端口。"
    }
    if ($httpsProxyEnabled) {
        if ($httpsPort -eq $Port) {
            throw "IMAGEGEN_HTTPS_PORT 不能与 IMAGEGEN_PORT 相同。"
        }
        if (-not (Test-PortAvailable -ListenPort $httpsPort) -and -not (Test-CurrentStackOwnsPort -ListenPort $httpsPort -Service "proxy" -ContainerPort 443 -HttpsProfile)) {
            throw "HTTPS 端口 $httpsPort 已被其他进程占用。请停止占用进程，或修改 IMAGEGEN_HTTPS_PORT。"
        }
    }

    $lucidaImage = "snow-ai-studio-lucida:latest"
    $lucidaBaseImage = "snow-ai-studio-lucida-base:cu124"
    foreach ($line in $envLines) {
        if ($line.StartsWith("LUCIDA_IMAGE=", [StringComparison]::Ordinal)) {
            $configuredImage = $line.Substring("LUCIDA_IMAGE=".Length).Trim()
            if (-not [string]::IsNullOrWhiteSpace($configuredImage)) {
                $lucidaImage = $configuredImage
            }
            break
        }
    }

    $lucidaSource = Join-Path $projectDir ".tmp-lucida-src\lucida-main"
    $lucidaModel = Join-Path $lucidaSource ".model\lucida\config.json"
    $lucidaRegistry = Join-Path $lucidaSource "bgr\registry.py"
    $lucidaBenchmark = Join-Path $lucidaSource "benchmark\__init__.py"
    $lucidaServing = Join-Path $lucidaSource "serving\app.py"

    function Test-LocallyUsableLucidaImage {
        param([string]$Image)

        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "SilentlyContinue"
            docker image inspect $Image *> $null
            if ($LASTEXITCODE -ne 0) {
                return $false
            }

            docker run --rm --gpus all --entrypoint python $Image -c "import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)" *> $null
            return $LASTEXITCODE -eq 0
        } finally {
            $ErrorActionPreference = $previousPreference
        }
    }

    $lucidaImageAvailable = Test-LocallyUsableLucidaImage -Image $lucidaImage
    if (-not $NoBuild) {
        Write-Host "正在构建主站与 Worker 镜像..."
        & docker compose --project-directory $projectDir build web
        if ($LASTEXITCODE -ne 0) {
            throw "主站/Worker 镜像构建失败。"
        }

        if ($lucidaImageAvailable) {
            Write-Host "复用现有 GPU Lucida 镜像，跳过源码/权重检查与构建：$lucidaImage" -ForegroundColor Green
        } else {
            if (@($lucidaRegistry, $lucidaBenchmark, $lucidaServing, $lucidaModel) |
                Where-Object { -not (Test-Path -LiteralPath $_) }) {
                throw "缺少 Lucida 源码/权重：请准备 .tmp-lucida-src\lucida-main（含 bgr、benchmark、serving 与 .model\lucida）。"
            }

            $lucidaBuildBase = $lucidaBaseImage
            if (-not (Test-LocallyUsableLucidaImage -Image $lucidaBuildBase)) {
                $lucidaBuildBase = "docker.m.daocloud.io/library/python:3.12-slim"
                Write-Host "未找到 GPU 底座，将执行首次 CUDA 依赖构建（可能需要较长时间）。"
            } else {
                Write-Host "复用 GPU Lucida 构建底座：$lucidaBaseImage" -ForegroundColor Green
            }

            Write-Host "正在构建 Lucida 运行镜像..."
            & docker compose --project-directory $projectDir build `
                --build-arg "LUCIDA_BASE_IMAGE=$lucidaBuildBase" lucida
            if ($LASTEXITCODE -ne 0) {
                throw "GPU Lucida 镜像构建失败。"
            }
            if ($lucidaBuildBase -eq "docker.m.daocloud.io/library/python:3.12-slim") {
                docker tag $lucidaImage $lucidaBaseImage
                if ($LASTEXITCODE -ne 0) {
                    throw "无法保存 GPU Lucida 构建底座。"
                }
            }
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

    $composeProfiles = @("--profile", "lucida")
    if ($httpsProxyEnabled) {
        $composeProfiles += @("--profile", "https")
    }
    & docker compose --project-directory $projectDir @composeProfiles up -d --no-build --remove-orphans
    if ($LASTEXITCODE -ne 0) {
        if ($preDeployBackup) {
            Write-Warning "可用以下备份恢复：py scripts/restore.py --backup-dir `"$preDeployBackup`" --confirm RESTORE"
        }
        throw "docker compose 启动或数据库迁移失败。"
    }

    # A localhost health check cannot detect a wrongly scoped host port. Check
    # Compose's published endpoint so LAN mode fails before it is announced.
    Assert-PublishedListener `
        -Service "web" `
        -ContainerPort 7860 `
        -ExpectedHost $expectedWebBindHost `
        -ExpectedPort $Port `
        -Label "Web HTTP"
    if ($httpsProxyEnabled) {
        $expectedHttpsBindHost = if ($effectiveLan) { $lanBindHost } else { $loopbackBindHost }
        Assert-PublishedListener `
            -Service "proxy" `
            -ContainerPort 443 `
            -ExpectedHost $expectedHttpsBindHost `
            -ExpectedPort $httpsPort `
            -Label "HTTPS proxy" `
            -HttpsProfile
    }
    Write-Host "监听校验通过：Web $expectedWebBindHost`:$Port" -ForegroundColor Green
    if ($httpsProxyEnabled) {
        Write-Host "监听校验通过：HTTPS $expectedHttpsBindHost`:$httpsPort" -ForegroundColor Green
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
        $logProfiles = if ($httpsProxyEnabled) { "--profile lucida --profile https" } else { "--profile lucida" }
        $logServices = if ($httpsProxyEnabled) { "web worker lucida proxy" } else { "web worker lucida" }
        throw "服务未能在 15 分钟内通过健康检查。请运行：docker compose $logProfiles logs $logServices"
    }

    if ($httpsProxyEnabled) {
        $proxyContainerId = docker compose --project-directory $projectDir --profile lucida --profile https ps -q proxy 2>$null
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($proxyContainerId)) {
            docker compose --project-directory $projectDir --profile lucida --profile https ps
            throw "HTTPS 反向代理容器未能启动。请运行：docker compose --profile lucida --profile https logs proxy"
        }
        $proxyRunning = docker inspect --format "{{.State.Running}}" $proxyContainerId 2>$null
        if ($LASTEXITCODE -ne 0 -or $proxyRunning.Trim() -ne "true") {
            docker compose --project-directory $projectDir --profile lucida --profile https ps
            throw "HTTPS 反向代理容器未处于运行状态。请运行：docker compose --profile lucida --profile https logs proxy"
        }
    }

    Write-Host ""
    Write-Host "Snow AI Studio 已启动（含 Docker Lucida GPU 抠图）。" -ForegroundColor Green
    if ($httpsProxyEnabled) {
        Write-Host "本机 HTTPS 地址：https://${httpsHost}:${httpsPort}" -ForegroundColor Green
    } else {
        Write-Host "本机地址：http://127.0.0.1:$Port"
    }
    Write-Host "背景透明化：在图片详情页选择 Lucida 或其他已配置模型并行比较（LUCIDA_MATTING_URL=http://lucida:8000）"
    if ($httpsProxyEnabled) {
        Write-Host "HTTPS 反向代理：https://${httpsHost}:${httpsPort}" -ForegroundColor Green
    }
    if ($effectiveLan) {
        foreach ($address in Get-LanAddresses) {
            if ($httpsProxyEnabled) {
                Write-Host "局域网地址（HTTPS，请确保主机名解析到此机器）：https://${httpsHost}:${httpsPort}"
                break
            }
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
    $backupTime = "03:00"
    foreach ($line in $envLines) {
        if ($line.StartsWith("IMAGEGEN_BACKUP_TIME=", [StringComparison]::Ordinal)) {
            $configuredBackupTime = $line.Substring("IMAGEGEN_BACKUP_TIME=".Length).Trim()
            if ($configuredBackupTime -match "^([01]\d|2[0-3]):[0-5]\d$") {
                $backupTime = $configuredBackupTime
            }
            break
        }
    }
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass `
            -File (Join-Path $projectDir "scripts\install-backup-task.ps1") `
            -At $backupTime
        if ($LASTEXITCODE -ne 0) {
            throw "任务计划程序返回 $LASTEXITCODE"
        }
    } catch {
        Write-Warning "每日备份任务未能自动安装：$($_.Exception.Message)"
    }
} finally {
    Pop-Location
}
