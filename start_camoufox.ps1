param(
    [switch]$DryRun,
    [switch]$SkipDependencyInstall,
    [string]$ConfigPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Get-Location).Path
}

function Resolve-ProjectPath([string]$PathValue, [string]$DefaultName) {
    if ([string]::IsNullOrWhiteSpace($PathValue)) {
        return (Join-Path $ProjectRoot $DefaultName)
    }
    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return $PathValue
    }
    return (Join-Path $ProjectRoot $PathValue)
}

$ResolvedConfigPath = Resolve-ProjectPath $ConfigPath ".start-camoufox-config.json"
$RequirementsPath = Join-Path $ProjectRoot "requirements.txt"
$ScriptPath = Join-Path $ProjectRoot "qwenv4_camoufox.py"
$VenvPath = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"

function Write-Info([string]$Message) {
    Write-Host "[start-camoufox] $Message"
}

function Read-LineValue([string]$PromptText) {
    Write-Host -NoNewline $PromptText
    $line = [Console]::ReadLine()
    if ($null -eq $line) {
        return ""
    }
    return $line.Trim()
}

function Load-StartConfig([string]$Path) {
    $config = [ordered]@{
        count = 1
        email_provider = "mailtm"
        api_proxy = ""
        concurrency = 1
        captcha_timeout = 600
        verbose = $false
        sync_qwen2api = $false
        qwen2api_base_url = "http://127.0.0.1:7860"
        qwen2api_admin_key = "admin"
        qwen2api_timeout = 30
        strict = $true
    }

    if (Test-Path -LiteralPath $Path) {
        try {
            $loaded = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($key in @("count", "email_provider", "api_proxy", "concurrency", "captcha_timeout", "verbose", "sync_qwen2api", "qwen2api_base_url", "qwen2api_admin_key", "qwen2api_timeout", "strict")) {
                if ($loaded.PSObject.Properties.Name -contains $key) {
                    $config[$key] = $loaded.$key
                }
            }
        } catch {
            Write-Host "⚠️  配置文件读取失败，将使用内置默认值: $($_.Exception.Message)"
        }
    }

    return $config
}

function Save-StartConfig([string]$Path, [hashtable]$Config) {
    $parent = Split-Path -Parent $Path
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $payload = [ordered]@{
        count = [int]$Config.count
        email_provider = [string]$Config.email_provider
        api_proxy = [string]$Config.api_proxy
        concurrency = [int]$Config.concurrency
        captcha_timeout = [int]$Config.captcha_timeout
        verbose = [bool]$Config.verbose
        sync_qwen2api = [bool]$Config.sync_qwen2api
        qwen2api_base_url = [string]$Config.qwen2api_base_url
        qwen2api_admin_key = [string]$Config.qwen2api_admin_key
        qwen2api_timeout = [int]$Config.qwen2api_timeout
        strict = [bool]$Config.strict
    }
    $json = $payload | ConvertTo-Json -Depth 3
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $json, $utf8NoBom)
}

function Prompt-PositiveInt([string]$Label, [int]$DefaultValue) {
    while ($true) {
        $raw = Read-LineValue "$Label [$DefaultValue]: "
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return $DefaultValue
        }
        $parsed = 0
        if ([int]::TryParse($raw, [ref]$parsed) -and $parsed -gt 0) {
            return $parsed
        }
        Write-Host "请输入正整数。"
    }
}

function Prompt-RangedInt([string]$Label, [int]$DefaultValue, [int]$MinValue, [int]$MaxValue) {
    while ($true) {
        $raw = Read-LineValue "$Label [$DefaultValue]: "
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return $DefaultValue
        }
        $parsed = 0
        if ([int]::TryParse($raw, [ref]$parsed) -and $parsed -ge $MinValue -and $parsed -le $MaxValue) {
            return $parsed
        }
        Write-Host "请输入 $MinValue 到 $MaxValue 之间的整数。"
    }
}

function Normalize-Provider([string]$RawValue) {
    $value = $RawValue.Trim().ToLowerInvariant()
    switch ($value) {
        "1" { return "generator.email" }
        "generator" { return "generator.email" }
        "generator.email" { return "generator.email" }
        "2" { return "mailtm" }
        "mailtm" { return "mailtm" }
        "mail.tm" { return "mailtm" }
        "3" { return "mailporary" }
        "mailporary" { return "mailporary" }
        default { return $null }
    }
}

function Prompt-Provider([string]$DefaultValue) {
    $normalizedDefault = Normalize-Provider $DefaultValue
    if ($null -eq $normalizedDefault) {
        $normalizedDefault = "generator.email"
    }

    while ($true) {
        Write-Host "邮箱服务:"
        Write-Host "  1) generator.email"
        Write-Host "  2) mailtm"
        Write-Host "  3) mailporary"
        $raw = Read-LineValue "请选择邮箱服务 [$normalizedDefault]: "
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return $normalizedDefault
        }
        $provider = Normalize-Provider $raw
        if ($null -ne $provider) {
            return $provider
        }
        Write-Host "请输入 1/2/3 或 generator.email/mailtm/mailporary。"
    }
}

function Prompt-String([string]$Label, [string]$DefaultValue) {
    $displayDefault = $DefaultValue
    if ($null -eq $displayDefault) {
        $displayDefault = ""
    }
    $raw = Read-LineValue "$Label [$displayDefault]: "
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $displayDefault
    }
    if ($raw.ToLowerInvariant() -in @("none", "clear", "null")) {
        return ""
    }
    return $raw
}

function Prompt-Bool([string]$Label, [bool]$DefaultValue) {
    $defaultText = "n"
    if ($DefaultValue) {
        $defaultText = "Y"
    }
    while ($true) {
        $raw = Read-LineValue "$Label [$defaultText]: "
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return $DefaultValue
        }
        switch ($raw.Trim().ToLowerInvariant()) {
            { $_ -in @("y", "yes", "true", "1") } { return $true }
            { $_ -in @("n", "no", "false", "0") } { return $false }
            default { Write-Host "请输入 y/n。" }
        }
    }
}

function Get-PythonLauncher() {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $py) {
        return @{ Command = $py.Source; Args = @("-3") }
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        return @{ Command = $python.Source; Args = @() }
    }
    throw "未找到 Python。请先安装 Python 3。"
}

function Invoke-Checked([string]$Command, [string[]]$Arguments, [string]$FailureMessage) {
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit code $LASTEXITCODE)"
    }
}

function Ensure-VenvPython() {
    if ($SkipDependencyInstall) {
        $launcher = Get-PythonLauncher
        if ($launcher.Args.Count -gt 0) {
            return "$($launcher.Command) $($launcher.Args -join ' ')"
        }
        return $launcher.Command
    }

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        Write-Info "创建本地虚拟环境: $VenvPath"
        $launcher = Get-PythonLauncher
        $args = @($launcher.Args) + @("-m", "venv", $VenvPath)
        Invoke-Checked $launcher.Command $args "创建虚拟环境失败"
    }

    return $VenvPython
}

function Invoke-Python([string]$PythonExe, [string[]]$Arguments) {
    $previousPythonUnbuffered = $env:PYTHONUNBUFFERED
    $env:PYTHONUNBUFFERED = "1"
    try {
        if ($PythonExe.Contains(" ") -and -not (Test-Path -LiteralPath $PythonExe)) {
            $parts = $PythonExe.Split(" ", 2)
            $launcherArgs = @()
            if ($parts.Count -gt 1 -and -not [string]::IsNullOrWhiteSpace($parts[1])) {
                $launcherArgs = @($parts[1] -split "\s+")
            }
            & $parts[0] @launcherArgs @Arguments
        } else {
            & $PythonExe @Arguments
        }
        $script:InvokePythonExitCode = $LASTEXITCODE
    } finally {
        if ($null -eq $previousPythonUnbuffered) {
            Remove-Item Env:PYTHONUNBUFFERED -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONUNBUFFERED = $previousPythonUnbuffered
        }
    }
}

function Ensure-PythonDependencies([string]$PythonExe) {
    if ($SkipDependencyInstall) {
        Write-Info "已跳过依赖安装检测 (-SkipDependencyInstall)"
        return
    }
    if (-not (Test-Path -LiteralPath $RequirementsPath)) {
        throw "缺少 requirements.txt: $RequirementsPath"
    }

    $checkCode = @'
import importlib.util
import sys
modules = ["httpx", "bs4", "lxml", "portalocker", "playwright", "camoufox"]
missing = [name for name in modules if importlib.util.find_spec(name) is None]
if missing:
    print(",".join(missing))
    sys.exit(1)
'@
    $checkFile = [System.IO.Path]::GetTempFileName() + ".py"
    Set-Content -LiteralPath $checkFile -Value $checkCode -Encoding UTF8
    try {
        $missingOutput = & $PythonExe $checkFile 2>&1
        $missingExit = $LASTEXITCODE
    } finally {
        Remove-Item -LiteralPath $checkFile -Force -ErrorAction SilentlyContinue
    }

    if ($missingExit -ne 0) {
        Write-Info "检测到缺失依赖: $missingOutput"
        Invoke-Checked $PythonExe @("-m", "pip", "install", "-r", $RequirementsPath) "安装 Python 依赖失败"
    } else {
        Write-Info "Python 依赖已满足"
    }
}

function Ensure-CamoufoxBrowser([string]$PythonExe) {
    if ($SkipDependencyInstall) {
        return
    }

    $checkCode = @'
from camoufox.pkgman import installed_verstr
try:
    installed_verstr()
    raise SystemExit(0)
except FileNotFoundError:
    raise SystemExit(1)
'@
    $checkFile = [System.IO.Path]::GetTempFileName() + ".py"
    Set-Content -LiteralPath $checkFile -Value $checkCode -Encoding UTF8
    try {
        & $PythonExe $checkFile | Out-Null
        $camoufoxExit = $LASTEXITCODE
    } finally {
        Remove-Item -LiteralPath $checkFile -Force -ErrorAction SilentlyContinue
    }

    if ($camoufoxExit -ne 0) {
        Write-Info "安装 Camoufox 浏览器"
        Invoke-Checked $PythonExe @("-m", "camoufox", "fetch") "安装 Camoufox 浏览器失败"
    } else {
        Write-Info "Camoufox 浏览器已可用"
    }
}

function Format-CommandForDisplay([string[]]$Arguments) {
    return ($Arguments -join " ")
}

try {
    Set-Location -LiteralPath $ProjectRoot

    if (-not (Test-Path -LiteralPath $ScriptPath)) {
        throw "缺少 qwenv4_camoufox.py: $ScriptPath"
    }

    $pythonExe = Ensure-VenvPython
    Ensure-PythonDependencies $pythonExe
    Ensure-CamoufoxBrowser $pythonExe

    $config = Load-StartConfig $ResolvedConfigPath

    $count = Prompt-PositiveInt "账号数量" ([int]$config.count)
    $provider = Prompt-Provider ([string]$config.email_provider)
    $apiProxy = Prompt-String "API 代理（输入 none 可清空）" ([string]$config.api_proxy)
    $verbose = Prompt-Bool "启用详细日志?" ([bool]$config.verbose)
    $concurrency = Prompt-RangedInt "并发数量" ([int]$config.concurrency) 1 10
    $captchaTimeout = Prompt-PositiveInt "滑块验证等待秒数" ([int]$config.captcha_timeout)
    $syncQwen2Api = Prompt-Bool "同步到 qwen2API?" ([bool]$config.sync_qwen2api)
    $qwen2ApiBaseUrl = [string]$config.qwen2api_base_url
    $qwen2ApiAdminKey = [string]$config.qwen2api_admin_key
    $qwen2ApiTimeout = [int]$config.qwen2api_timeout
    if ($syncQwen2Api) {
        $qwen2ApiBaseUrl = Prompt-String "qwen2API 地址" $qwen2ApiBaseUrl
        $qwen2ApiAdminKey = Prompt-String "qwen2API 管理密钥" $qwen2ApiAdminKey
        $qwen2ApiTimeout = Prompt-PositiveInt "qwen2API 同步超时秒数" $qwen2ApiTimeout
    }
    $strict = Prompt-Bool "严格模式?" ([bool]$config.strict)
    if (-not $strict) {
        Write-Host "⚠️  qwenv4_camoufox.py 当前不支持关闭严格模式，将继续启用。"
        $strict = $true
    }

    $newConfig = [ordered]@{
        count = $count
        email_provider = $provider
        api_proxy = $apiProxy
        concurrency = $concurrency
        captcha_timeout = $captchaTimeout
        verbose = $verbose
        sync_qwen2api = $syncQwen2Api
        qwen2api_base_url = $qwen2ApiBaseUrl
        qwen2api_admin_key = $qwen2ApiAdminKey
        qwen2api_timeout = $qwen2ApiTimeout
        strict = $true
    }
    Save-StartConfig $ResolvedConfigPath $newConfig
    Write-Info "已保存本次参数: $ResolvedConfigPath"

    $scriptArgs = @("qwenv4_camoufox.py", [string]$count, "--email-provider", $provider)
    if (-not [string]::IsNullOrWhiteSpace($apiProxy)) {
        $scriptArgs += @("--api-proxy", $apiProxy)
    }
    $scriptArgs += @("--concurrency", [string]$concurrency, "--captcha-timeout", [string]$captchaTimeout)
    if ($syncQwen2Api) {
        $scriptArgs += @(
            "--sync-qwen2api",
            "--qwen2api-base-url", $qwen2ApiBaseUrl,
            "--qwen2api-admin-key", $qwen2ApiAdminKey,
            "--qwen2api-timeout", [string]$qwen2ApiTimeout
        )
    }
    if ($verbose) {
        $scriptArgs += "--verbose"
    }
    $scriptArgs += "--strict"

    $displayCommand = Format-CommandForDisplay $scriptArgs
    Write-Host "最终命令: $displayCommand"

    if ($DryRun) {
        Write-Info "试运行模式（DryRun）已启用，不启动 qwenv4_camoufox.py"
        exit 0
    }

    $script:InvokePythonExitCode = 0
    Invoke-Python $pythonExe $scriptArgs
    exit $script:InvokePythonExitCode
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
