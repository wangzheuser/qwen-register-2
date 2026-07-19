param(
    [string]$Mode = "",
    [switch]$DryRun,
    [switch]$SkipDependencyInstall,
    [switch]$InterruptCleanupSelfTest,
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

$RequirementsPath = Join-Path $ProjectRoot "requirements.txt"
$VenvPath = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
$PythonInterruptGraceSeconds = 15

function Write-Info([string]$Message) {
    Write-Host "[start] $Message"
}

function Read-LineValue([string]$PromptText) {
    Write-Host -NoNewline $PromptText
    $line = [Console]::ReadLine()
    if ($null -eq $line) {
        return ""
    }
    return $line.Trim()
}

function Select-BrowserMode([string]$RequestedMode) {
    $normalized = ([string]$RequestedMode).Trim().ToLowerInvariant()
    switch ($normalized) {
        { $_ -in @("playwright", "chromium", "1") } { return "playwright" }
        { $_ -in @("camoufox", "2") } { return "camoufox" }
        "" {
            Write-Host "浏览器模式:"
            Write-Host "  1) Playwright Chromium"
            Write-Host "  2) Camoufox"
            $selected = (Read-LineValue "请选择浏览器模式 [1]: ").ToLowerInvariant()
            if ($selected -in @("", "1", "playwright", "chromium")) {
                return "playwright"
            }
            if ($selected -in @("2", "camoufox")) {
                return "camoufox"
            }
            throw "请输入 1/2 或 playwright/camoufox。"
        }
        default { throw "不支持的浏览器模式: $RequestedMode" }
    }
}

function Load-StartConfig([string]$Path) {
    $config = [ordered]@{
        count = 1
        email_provider = "mailtm"
        api_proxy = ""
        browser_proxy = ""
        concurrency = 1
        captcha_timeout = 600
        captcha_solver = "ddddocr"
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
            foreach ($key in @("count", "email_provider", "api_proxy", "browser_proxy", "concurrency", "captcha_timeout", "captcha_solver", "verbose", "sync_qwen2api", "qwen2api_base_url", "qwen2api_admin_key", "qwen2api_timeout", "strict")) {
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
        browser_proxy = [string]$Config.browser_proxy
        concurrency = [int]$Config.concurrency
        captcha_timeout = [int]$Config.captcha_timeout
        captcha_solver = [string]$Config.captcha_solver
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
        "4" { return "gonebox" }
        "gonebox" { return "gonebox" }
        "5" { return "tempmail_lol" }
        "tempmail_lol" { return "tempmail_lol" }
        "tempmail.lol" { return "tempmail_lol" }
        "6" { return "freecustom" }
        "freecustom" { return "freecustom" }
        "freecustom.email" { return "freecustom" }
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
        Write-Host "  4) gonebox"
        Write-Host "  5) tempmail_lol"
        Write-Host "  6) freecustom"
        $raw = Read-LineValue "请选择邮箱服务 [$normalizedDefault]: "
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return $normalizedDefault
        }
        $provider = Normalize-Provider $raw
        if ($null -ne $provider) {
            return $provider
        }
        Write-Host "请输入 1-6 或 generator.email/mailtm/mailporary/gonebox/tempmail_lol/freecustom。"
    }
}

function Normalize-CaptchaSolver([string]$RawValue) {
    $value = $RawValue.Trim().ToLowerInvariant()
    switch ($value) {
        "1" { return "ddddocr" }
        "ddddocr" { return "ddddocr" }
        "ocr" { return "ddddocr" }
        "2" { return "ai" }
        "ai" { return "ai" }
        "滑块ai" { return "ai" }
        "3" { return "manual" }
        "manual" { return "manual" }
        "人工" { return "manual" }
        default { return $null }
    }
}

function Prompt-CaptchaSolver([string]$DefaultValue) {
    $normalizedDefault = Normalize-CaptchaSolver $DefaultValue
    if ($null -eq $normalizedDefault) {
        $normalizedDefault = "ddddocr"
    }

    while ($true) {
        Write-Host "滑块处理方式:"
        Write-Host "  1) ddddocr（本地自动，默认）"
        Write-Host "  2) 滑块AI"
        Write-Host "  3) 人工"
        $raw = Read-LineValue "请选择滑块处理方式 [$normalizedDefault]: "
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return $normalizedDefault
        }
        $solver = Normalize-CaptchaSolver $raw
        if ($null -ne $solver) {
            return $solver
        }
        Write-Host "请输入 1/2/3 或 ddddocr/ai/manual。"
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

function Resolve-PythonInvocation([string]$PythonExe) {
    if ($PythonExe.Contains(" ") -and -not (Test-Path -LiteralPath $PythonExe)) {
        $parts = $PythonExe.Split(" ", 2)
        $launcherArgs = @()
        if ($parts.Count -gt 1 -and -not [string]::IsNullOrWhiteSpace($parts[1])) {
            $launcherArgs = @($parts[1] -split "\s+")
        }
        return @{ Command = $parts[0]; Args = $launcherArgs }
    }
    return @{ Command = $PythonExe; Args = @() }
}

function Quote-ProcessArgument([string]$Argument) {
    if ($null -eq $Argument) {
        return '""'
    }
    if ($Argument -notmatch '[\s"]') {
        return $Argument
    }
    return '"' + ($Argument -replace '\', '\' -replace '"', '\"') + '"'
}

function Join-ProcessArguments([string[]]$Arguments) {
    return (($Arguments | ForEach-Object { Quote-ProcessArgument $_ }) -join " ")
}

function Stop-ProcessTree([int]$ProcessId) {
    $children = @()
    try {
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue
    } catch {
        try {
            $children = Get-WmiObject Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue
        } catch {
            $children = @()
        }
    }

    foreach ($child in @($children)) {
        if ($null -ne $child.ProcessId) {
            Stop-ProcessTree -ProcessId ([int]$child.ProcessId)
        }
    }

    try {
        $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if ($null -ne $process) {
            Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
        }
    } catch {
    }
}

function Wait-PythonGracefulShutdown($Process, [int]$GraceSeconds) {
    if ($null -eq $Process -or $Process.HasExited) {
        return $true
    }

    Write-Host ""
    Write-Info "收到 Ctrl+C，已通知 Python 停止；等待 Python 停止新任务并关闭当前 $BrowserDisplayName 浏览器..."
    $deadline = [DateTime]::UtcNow.AddSeconds($GraceSeconds)
    while (-not $Process.HasExited -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 200
    }

    if ($Process.HasExited) {
        Write-Info "Python 已完成中断清理。"
        return $true
    }

    Write-Info "Python 未在 ${GraceSeconds}s 内退出，强制终止 Python 子进程及其 $BrowserDisplayName 浏览器进程..."
    Stop-ProcessTree -ProcessId $Process.Id
    return $false
}

function Request-PythonGracefulStop([string]$StopFilePath) {
    if ([string]::IsNullOrWhiteSpace($StopFilePath)) {
        return
    }
    try {
        $parent = Split-Path -Parent $StopFilePath
        if (-not [string]::IsNullOrWhiteSpace($parent)) {
            New-Item -ItemType Directory -Force -Path $parent | Out-Null
        }
        [System.IO.File]::WriteAllText($StopFilePath, "stop", [System.Text.Encoding]::UTF8)
    } catch {
        Write-Info "写入 Python 停止信号失败: $($_.Exception.Message)"
    }
}

function Invoke-Python([string]$PythonExe, [string[]]$Arguments) {
    $invocation = Resolve-PythonInvocation $PythonExe
    $allArguments = @($invocation.Args) + @($Arguments)
    $process = $null
    $handler = $null
    $script:InvokePythonExitCode = 130
    $script:CancelRequested = $false
    $stopFilePath = Join-Path ([System.IO.Path]::GetTempPath()) ("qwen-register-$BrowserMode-stop-{0}.signal" -f ([guid]::NewGuid().ToString("N")))
    try {
        $handler = [ConsoleCancelEventHandler]{
            param($sender, $eventArgs)
            $eventArgs.Cancel = $true
            $script:CancelRequested = $true
            Request-PythonGracefulStop $stopFilePath
        }
        [Console]::add_CancelKeyPress($handler)

        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = [string]$invocation.Command
        $startInfo.Arguments = Join-ProcessArguments $allArguments
        $startInfo.WorkingDirectory = $ProjectRoot
        $startInfo.UseShellExecute = $false
        $startInfo.RedirectStandardOutput = $false
        $startInfo.RedirectStandardError = $false
        $startInfo.EnvironmentVariables["PYTHONUNBUFFERED"] = "1"
        $startInfo.EnvironmentVariables["QWEN_REGISTER_MANAGED_BY_START"] = "1"
        $startInfo.EnvironmentVariables["QWEN_REGISTER_STOP_FILE"] = $stopFilePath

        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        [void]$process.Start()
        $script:CurrentPythonProcessId = $process.Id
        $script:CurrentPythonStopFile = $stopFilePath

        while (-not $process.HasExited) {
            if ($script:CancelRequested) {
                Request-PythonGracefulStop $stopFilePath
                [void](Wait-PythonGracefulShutdown $process $PythonInterruptGraceSeconds)
                break
            }
            Start-Sleep -Milliseconds 200
        }
        if ($process.HasExited) {
            $script:InvokePythonExitCode = $process.ExitCode
        } else {
            $script:InvokePythonExitCode = 130
        }
    } finally {
        if ($null -ne $handler) {
            [Console]::remove_CancelKeyPress($handler)
        }
        if ($null -ne $process -and -not $process.HasExited) {
            Write-Info "正在终止 Python 子进程及其浏览器进程..."
            Stop-ProcessTree -ProcessId $process.Id
            $script:InvokePythonExitCode = 130
        }
        $script:CurrentPythonProcessId = $null
        $script:CurrentPythonStopFile = $null
        if ($null -ne $process) {
            $process.Dispose()
        }
        Remove-Item -LiteralPath $stopFilePath -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-InterruptCleanupSelfTest {
    $tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("qwen-start-$BrowserMode-interrupt-selftest-{0}" -f ([guid]::NewGuid().ToString("N")))
    New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
    $childPath = Join-Path $tempDir "child.py"
    $stopFilePath = Join-Path $tempDir "stop.signal"
    $startedPath = Join-Path $tempDir "started.txt"
    $gracefulPath = Join-Path $tempDir "graceful.txt"
    $childCode = @'
import os
import time
from pathlib import Path

stop_file = Path(os.environ["QWEN_REGISTER_STOP_FILE"])
started = Path(os.environ["QWEN_REGISTER_SELFTEST_STARTED"])
graceful = Path(os.environ["QWEN_REGISTER_SELFTEST_GRACEFUL"])

started.write_text("ready", encoding="utf-8")
while not stop_file.exists():
    time.sleep(0.05)
time.sleep(0.2)
graceful.write_text("ok", encoding="utf-8")
raise SystemExit(130)
'@
    Set-Content -LiteralPath $childPath -Value $childCode -Encoding UTF8
    $launcher = Get-PythonLauncher
    $allArguments = @($launcher.Args) + @($childPath)
    $process = $null
    try {
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = [string]$launcher.Command
        $startInfo.Arguments = Join-ProcessArguments $allArguments
        $startInfo.WorkingDirectory = $tempDir
        $startInfo.UseShellExecute = $false
        $startInfo.RedirectStandardOutput = $false
        $startInfo.RedirectStandardError = $false
        $startInfo.EnvironmentVariables["QWEN_REGISTER_STOP_FILE"] = $stopFilePath
        $startInfo.EnvironmentVariables["QWEN_REGISTER_SELFTEST_STARTED"] = $startedPath
        $startInfo.EnvironmentVariables["QWEN_REGISTER_SELFTEST_GRACEFUL"] = $gracefulPath

        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        [void]$process.Start()

        $deadline = [DateTime]::UtcNow.AddSeconds(5)
        while (-not (Test-Path -LiteralPath $startedPath) -and [DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 50
        }
        if (-not (Test-Path -LiteralPath $startedPath)) {
            throw "中断清理自检失败：测试 Python 子进程未启动"
        }

        Request-PythonGracefulStop $stopFilePath
        [void](Wait-PythonGracefulShutdown $process 5)
        if (-not (Test-Path -LiteralPath $gracefulPath)) {
            throw "中断清理自检失败：Python 未收到停止信号"
        }
        return 0
    } finally {
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-ProcessTree -ProcessId $process.Id
        }
        if ($null -ne $process) {
            $process.Dispose()
        }
        Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Ensure-PythonDependencies([string]$PythonExe, [string]$SelectedMode) {
    if ($SkipDependencyInstall) {
        Write-Info "已跳过依赖安装检测 (-SkipDependencyInstall)"
        return
    }
    if (-not (Test-Path -LiteralPath $RequirementsPath)) {
        throw "缺少 requirements.txt: $RequirementsPath"
    }

    $modules = @("httpx", "bs4", "lxml", "portalocker", "playwright", "ddddocr")
    if ($SelectedMode -eq "camoufox") {
        $modules += "camoufox"
    }
    $checkCode = @'
import importlib.util
import importlib.metadata
import sys
modules = sys.argv[1:]
missing = [name for name in modules if importlib.util.find_spec(name) is None]
if "playwright" not in missing:
    version = importlib.metadata.version("playwright")
    if tuple(int(part) for part in version.split(".")[:2]) >= (1, 60):
        missing.append(f"playwright {version}（需要 <1.60.0）")
if missing:
    print(",".join(missing))
    sys.exit(1)
'@
    $checkFile = [System.IO.Path]::GetTempFileName() + ".py"
    Set-Content -LiteralPath $checkFile -Value $checkCode -Encoding UTF8
    try {
        $missingOutput = & $PythonExe $checkFile @modules 2>&1
        $missingExit = $LASTEXITCODE
    } finally {
        Remove-Item -LiteralPath $checkFile -Force -ErrorAction SilentlyContinue
    }

    if ($missingExit -ne 0) {
        Write-Info "检测到缺失或版本不兼容的依赖: $missingOutput"
        Invoke-Checked $PythonExe @("-m", "pip", "install", "-r", $RequirementsPath) "安装 Python 依赖失败"
    } else {
        Write-Info "Python 依赖已满足"
    }
}

function Ensure-BrowserRuntime([string]$PythonExe, [string]$SelectedMode) {
    if ($SkipDependencyInstall) {
        return
    }

    if ($SelectedMode -eq "camoufox") {
        $checkCode = @'
from camoufox.pkgman import installed_verstr
try:
    installed_verstr()
    raise SystemExit(0)
except FileNotFoundError:
    raise SystemExit(1)
'@
        $installArgs = @("-m", "camoufox", "fetch")
        $installLabel = "Camoufox 浏览器"
    } else {
        $checkCode = @'
from pathlib import Path
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    executable = Path(p.chromium.executable_path)
    raise SystemExit(0 if executable.exists() else 1)
'@
        $installArgs = @("-m", "playwright", "install", "chromium")
        $installLabel = "Playwright Chromium"
    }
    $checkFile = [System.IO.Path]::GetTempFileName() + ".py"
    Set-Content -LiteralPath $checkFile -Value $checkCode -Encoding UTF8
    try {
        & $PythonExe $checkFile | Out-Null
        $browserExit = $LASTEXITCODE
    } finally {
        Remove-Item -LiteralPath $checkFile -Force -ErrorAction SilentlyContinue
    }

    if ($browserExit -ne 0) {
        Write-Info "安装 $installLabel"
        Invoke-Checked $PythonExe $installArgs "安装 $installLabel 失败"
    } else {
        Write-Info "$installLabel 已可用"
    }
}

function Format-CommandForDisplay([string[]]$Arguments) {
    return ($Arguments -join " ")
}

try {
    Set-Location -LiteralPath $ProjectRoot

    $BrowserMode = Select-BrowserMode $Mode
    if ($BrowserMode -eq "camoufox") {
        $EntryScript = "qwenv4_camoufox.py"
        $DefaultConfigName = ".start-camoufox-config.json"
        $LogName = "qwenv4-camoufox.log"
        $BrowserDisplayName = "Camoufox"
    } else {
        $EntryScript = "qwenv4.py"
        $DefaultConfigName = ".start-config.json"
        $LogName = "qwenv4.log"
        $BrowserDisplayName = "Playwright"
    }
    $ResolvedConfigPath = Resolve-ProjectPath $ConfigPath $DefaultConfigName
    $ScriptPath = Join-Path $ProjectRoot $EntryScript

    if ($InterruptCleanupSelfTest) {
        exit (Invoke-InterruptCleanupSelfTest)
    }

    if (-not (Test-Path -LiteralPath $ScriptPath)) {
        throw "缺少 ${EntryScript}: $ScriptPath"
    }

    $pythonExe = Ensure-VenvPython
    Ensure-PythonDependencies $pythonExe $BrowserMode
    Ensure-BrowserRuntime $pythonExe $BrowserMode

    $config = Load-StartConfig $ResolvedConfigPath

    $count = Prompt-PositiveInt "账号数量" ([int]$config.count)
    $provider = Prompt-Provider ([string]$config.email_provider)
    $apiProxy = Prompt-String "API 代理（输入 none 可清空）" ([string]$config.api_proxy)
    $browserProxy = Prompt-String "浏览器代理（输入 none 可清空）" ([string]$config.browser_proxy)
    $verbose = Prompt-Bool "启用详细日志?" ([bool]$config.verbose)
    $concurrency = Prompt-RangedInt "并发数量" ([int]$config.concurrency) 1 10
    $captchaSolver = Prompt-CaptchaSolver ([string]$config.captcha_solver)
    $captchaTimeout = [int]$config.captcha_timeout
    if ($captchaSolver -in @("ai", "manual")) {
        $captchaTimeout = Prompt-PositiveInt "滑块验证等待/超时秒数" $captchaTimeout
    }
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
        Write-Host "⚠️  $EntryScript 当前不支持关闭严格模式，将继续启用。"
        $strict = $true
    }

    $newConfig = [ordered]@{
        count = $count
        email_provider = $provider
        api_proxy = $apiProxy
        browser_proxy = $browserProxy
        concurrency = $concurrency
        captcha_timeout = $captchaTimeout
        captcha_solver = $captchaSolver
        verbose = $verbose
        sync_qwen2api = $syncQwen2Api
        qwen2api_base_url = $qwen2ApiBaseUrl
        qwen2api_admin_key = $qwen2ApiAdminKey
        qwen2api_timeout = $qwen2ApiTimeout
        strict = $true
    }
    Save-StartConfig $ResolvedConfigPath $newConfig
    Write-Info "已保存本次参数: $ResolvedConfigPath"

    $logDir = Join-Path $ProjectRoot "logs"
    $logFile = Join-Path $logDir $LogName

    $scriptArgs = @($EntryScript, [string]$count, "--email-provider", $provider)
    if (-not [string]::IsNullOrWhiteSpace($apiProxy)) {
        $scriptArgs += @("--api-proxy", $apiProxy)
    }
    if (-not [string]::IsNullOrWhiteSpace($browserProxy)) {
        $scriptArgs += @("--browser-proxy", $browserProxy)
    }
    $scriptArgs += @("--concurrency", [string]$concurrency, "--account-retries", "3")
    if ($captchaSolver -eq "ddddocr") {
        $scriptArgs += @("--captcha-solver", "ddddocr", "--captcha-ai-attempts", "3", "--no-captcha-ai-fallback-manual", "--captcha-drag-backend", "os", "--captcha-drag-strategy", "fast_quadratic")
    } elseif ($captchaSolver -eq "ai") {
        if ([string]::IsNullOrWhiteSpace($env:CAPTCHA_AI_API_KEY)) {
            throw "选择滑块AI模式需要先设置环境变量 CAPTCHA_AI_API_KEY"
        }
        $scriptArgs += @("--captcha-solver", "ai", "--captcha-timeout", [string]$captchaTimeout, "--captcha-ai-attempts", "3", "--no-captcha-ai-fallback-manual", "--captcha-drag-backend", "os", "--captcha-drag-strategy", "fast_quadratic")
    } else {
        $scriptArgs += @("--captcha-solver", "manual", "--captcha-timeout", [string]$captchaTimeout)
    }
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
    $scriptArgs += @("--log-file", $logFile)
    $scriptArgs += "--strict"

    $displayCommand = Format-CommandForDisplay $scriptArgs
    Write-Host "最终命令: $displayCommand"
    if ($DryRun) {
        Write-Info "试运行模式（DryRun）已启用，不启动 $EntryScript"
        exit 0
    }

    $script:InvokePythonExitCode = 0
    $previousCaptchaAiApiKey = $env:CAPTCHA_AI_API_KEY
    try {
        Invoke-Python $pythonExe $scriptArgs
    } finally {
        if ($null -eq $previousCaptchaAiApiKey) {
            Remove-Item Env:CAPTCHA_AI_API_KEY -ErrorAction SilentlyContinue
        } else {
            $env:CAPTCHA_AI_API_KEY = $previousCaptchaAiApiKey
        }
    }
    exit $script:InvokePythonExitCode
} catch [System.Management.Automation.PipelineStoppedException] {
    if ($script:CurrentPythonProcessId) {
        if ($script:CurrentPythonStopFile) {
            Request-PythonGracefulStop ([string]$script:CurrentPythonStopFile)
        }
        Write-Info "正在终止 Python 子进程及其浏览器进程..."
        Stop-ProcessTree -ProcessId ([int]$script:CurrentPythonProcessId)
    }
    exit 130
} catch {
    Write-Error $_.Exception.Message
    exit 1
}


