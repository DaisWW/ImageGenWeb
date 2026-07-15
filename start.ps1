param(
    [switch]$Lan,
    [int]$Port = 7860
)

$projectDir = $PSScriptRoot
$venvPython = Join-Path $projectDir ".venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { "py" }
$chatKeyWasSet = -not [string]::IsNullOrWhiteSpace($env:GPT_CHAT_API_KEY)
$chatBaseUrlWasSet = -not [string]::IsNullOrWhiteSpace($env:GPT_CHAT_API_BASE_URL)
$lucenKeyWasSet = -not [string]::IsNullOrWhiteSpace($env:LUCEN_API_KEY)
$lucenBaseUrlWasSet = -not [string]::IsNullOrWhiteSpace($env:LUCEN_API_BASE_URL)
$lucenKeyImported = $false
$lucenBaseUrlImported = $false
$chatKeyImported = $false
$chatBaseUrlImported = $false

$env:IMAGE_API_BASE_URL = if ($env:IMAGE_API_BASE_URL) {
    $env:IMAGE_API_BASE_URL
} else {
    "https://fuck.codexapis.com"
}

if (-not $chatKeyWasSet -and $env:IMAGE_API_KEY) {
    $env:GPT_CHAT_API_KEY = $env:IMAGE_API_KEY
    $chatKeyImported = $true
}

if (-not $chatBaseUrlWasSet) {
    $env:GPT_CHAT_API_BASE_URL = $env:IMAGE_API_BASE_URL
    $chatBaseUrlImported = $true
}

if (-not $lucenKeyWasSet) {
    $api2imgConfigPath = Join-Path $HOME ".api2img\config.json"
    $api2imgSecretPath = Join-Path $HOME ".api2img\secret.json"
    if ((Test-Path -LiteralPath $api2imgConfigPath) -and (Test-Path -LiteralPath $api2imgSecretPath)) {
        try {
            $api2imgConfig = Get-Content -Raw -LiteralPath $api2imgConfigPath | ConvertFrom-Json
            $api2imgSecret = Get-Content -Raw -LiteralPath $api2imgSecretPath | ConvertFrom-Json
            if (-not [string]::IsNullOrWhiteSpace([string]$api2imgSecret.apiKey)) {
                $env:LUCEN_API_KEY = [string]$api2imgSecret.apiKey
                $lucenKeyImported = $true
            }
            if (-not $lucenBaseUrlWasSet -and -not [string]::IsNullOrWhiteSpace([string]$api2imgConfig.baseUrl)) {
                $env:LUCEN_API_BASE_URL = [string]$api2imgConfig.baseUrl
                $lucenBaseUrlImported = $true
            }
        } catch {
            Write-Warning "无法读取本机 api2img 配置，Lucen 渠道将保持未配置"
        }
    }
}

$env:IMAGE_WEB_HOST = if ($Lan) { "0.0.0.0" } else { "127.0.0.1" }
$env:IMAGE_WEB_PORT = [string]$Port

Push-Location $projectDir
$worker = $null
try {
    & $python -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        throw "数据库迁移失败"
    }
    $worker = Start-Process -FilePath $python `
        -ArgumentList "run_worker.py" `
        -WorkingDirectory $projectDir `
        -WindowStyle Hidden `
        -PassThru
    & $python app.py
} finally {
    if ($worker -and -not $worker.HasExited) {
        Stop-Process -Id $worker.Id -ErrorAction SilentlyContinue
        $worker.WaitForExit(5000)
    }
    Pop-Location
    if ($lucenKeyImported) {
        Remove-Item Env:LUCEN_API_KEY -ErrorAction SilentlyContinue
    }
    if ($lucenBaseUrlImported) {
        Remove-Item Env:LUCEN_API_BASE_URL -ErrorAction SilentlyContinue
    }
    if ($chatKeyImported) {
        Remove-Item Env:GPT_CHAT_API_KEY -ErrorAction SilentlyContinue
    }
    if ($chatBaseUrlImported) {
        Remove-Item Env:GPT_CHAT_API_BASE_URL -ErrorAction SilentlyContinue
    }
}
