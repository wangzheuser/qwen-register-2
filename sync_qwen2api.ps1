param(
    [switch]$DryRun,
    [string]$ConfigPath = "",
    [string]$AccountsFile = ""
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

$ResolvedConfigPath = Resolve-ProjectPath $ConfigPath ".start-config.json"
$DefaultAccountsFile = "qwen_accounts.json"
if (-not [string]::IsNullOrWhiteSpace($AccountsFile)) {
    $DefaultAccountsFile = $AccountsFile
}
$PythonScriptPath = Join-Path $ProjectRoot "sync_qwen2api_accounts.py"
$VenvPython = Join-Path (Join-Path $ProjectRoot ".venv") "Scripts\python.exe"

function Write-Info([string]$Message) {
    Write-Host "[sync-qwen2api] $Message"
}

function Read-LineValue([string]$PromptText) {
    Write-Host -NoNewline $PromptText
    $line = [Console]::ReadLine()
    if ($null -eq $line) {
        return ""
    }
    return $line.Trim()
}

function Load-SyncConfig([string]$Path) {
    $config = [ordered]@{
        qwen2api_base_url = "http://127.0.0.1:7860"
        qwen2api_admin_key = "admin"
        qwen2api_timeout = 30
    }

    if (Test-Path -LiteralPath $Path) {
        try {
            $loaded = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($key in @("qwen2api_base_url", "qwen2api_admin_key", "qwen2api_timeout")) {
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

function Format-CommandForDisplay([string[]]$CommandArgs) {
    $escaped = foreach ($arg in $CommandArgs) {
        if ($arg -match '[\s"]') {
            '"' + ($arg -replace '"', '\"') + '"'
        } else {
            $arg
        }
    }
    return ($escaped -join " ")
}

function Get-PythonExecutable() {
    if (Test-Path -LiteralPath $VenvPython) {
        return $VenvPython
    }
    return "python"
}

if (-not (Test-Path -LiteralPath $PythonScriptPath)) {
    throw "缺少 sync_qwen2api_accounts.py: $PythonScriptPath"
}

$config = Load-SyncConfig $ResolvedConfigPath

$accountsFileValue = Prompt-String "账号文件路径" $DefaultAccountsFile
$qwen2ApiBaseUrl = Prompt-String "qwen2API 地址" ([string]$config.qwen2api_base_url)
$qwen2ApiAdminKey = Prompt-String "qwen2API 管理密钥" ([string]$config.qwen2api_admin_key)
$qwen2ApiTimeout = Prompt-PositiveInt "qwen2API 同步超时秒数" ([int]$config.qwen2api_timeout)
$force = Prompt-Bool "强制重新同步已成功账号?" $false
$pythonDryRun = Prompt-Bool "只预览不发请求?" $false

$pythonExe = Get-PythonExecutable
$scriptArgs = @(
    $PythonScriptPath,
    "--accounts-file", $accountsFileValue,
    "--qwen2api-base-url", $qwen2ApiBaseUrl,
    "--qwen2api-admin-key", $qwen2ApiAdminKey,
    "--qwen2api-timeout", [string]$qwen2ApiTimeout
)
if ($force) {
    $scriptArgs += "--force"
}
if ($pythonDryRun) {
    $scriptArgs += "--dry-run"
}

$displayCommand = Format-CommandForDisplay (@($pythonExe) + $scriptArgs)
Write-Host "最终命令: $displayCommand"
Write-Info "本脚本不会保存输入到 $ResolvedConfigPath"

if ($DryRun) {
    Write-Info "包装层 DryRun：不会执行同步。"
    exit 0
}

& $pythonExe @scriptArgs
exit $LASTEXITCODE
