# dsh-plugin-atuowexin 环境自检 / 自动安装脚本
#
# 检查 Python（以及本插件需要的微信/QQ/PyMuPDF/NapCat），
# 默认静默安装；只想检查时加 -CheckOnly。
#
# 用法：
#     powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1
#     powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1 -CheckOnly
#     powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1 -WhatIf

[CmdletBinding()]
param(
    [switch]$AutoInstall,
    [switch]$CheckOnly,
    [switch]$Quiet,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# 默认静默安装缺失环境；只想检查时加 -CheckOnly
if (-not $CheckOnly) { $AutoInstall = $true }

$AppName = "微信（WeChat）"
$WingetId = "Tencent.WeChat.Universal"
$AltWingetId = "Tencent.WeChat"
$NeedsPyMuPDF = $false
$NeedsNapCat = $false

function Say([string]$Message) {
    if (-not $Quiet) { Write-Host $Message }
}
function Warn([string]$Message) {
    Write-Host $Message -ForegroundColor Yellow
}

function Get-DshHome {
    if ($env:DSH_HOME) { return $env:DSH_HOME }
    if ($env:USERPROFILE) { return (Join-Path $env:USERPROFILE ".dsh") }
    return (Join-Path $HOME ".dsh")
}

function Find-Python {
    foreach ($candidate in @($env:DSH_PYTHON, $env:PYTHON)) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $dshHome = Get-DshHome
    $pattern = Join-Path $dshHome "dsh-runtimes\*\dependencies\python\python.exe"
    $found = Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($found) { return $found.FullName }
    foreach ($name in @("python", "py", "python3")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { return $command.Source }
    }
    return $null
}

function Test-WingetPackage([string]$Id) {
    if (-not $Id) { return $false }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return $false }
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $output = & winget list --id $Id --exact --accept-source-agreements 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    if ($code -ne 0) { return $false }
    return (($output -join "`n") -match [regex]::Escape($Id))
}

function Install-Winget([string]$Id, [string]$Label, [string[]]$Extra = @()) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Warn "[!] 未找到 winget，无法自动安装 $Label；请手动安装。"
        return $false
    }
    $wingetArgs = @("install", "--id", $Id, "--exact", "--silent", "--accept-package-agreements", "--accept-source-agreements") + $Extra
    if ($WhatIf) {
        Write-Host ("[whatif] winget " + ($wingetArgs -join " "))
        return $true
    }
    Write-Host "正在自动安装 $Label ..."
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & winget @wingetArgs
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $old
    if ($ok) { Write-Host "[ok] $Label 安装完成" } else { Warn "[!] $Label 安装失败（exit $LASTEXITCODE）" }
    return $ok
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($machine, $user) | Where-Object { $_ }
    $env:Path = $parts -join ";"
}

function Test-App {
    if (Test-WingetPackage $WingetId) { return $true }
    if ($AltWingetId -and (Test-WingetPackage $AltWingetId)) { return $true }
    $paths = @()
if ($env:ProgramFiles) { $paths += (Join-Path $env:ProgramFiles "Tencent\WeChat\WeChat.exe") }
if (${env:ProgramFiles(x86)}) { $paths += (Join-Path ${env:ProgramFiles(x86)} "Tencent\WeChat\WeChat.exe") }
if ($env:LOCALAPPDATA) { $paths += (Join-Path $env:LOCALAPPDATA "Tencent\WeChat\WeChat.exe") }
    foreach ($path in $paths) {
        if ($path -and (Test-Path -LiteralPath $path)) { return $true }
    }
    return $false
}

$missing = 0
Say "== 环境检查（dsh-plugin-atuowexin）=="

$python = Find-Python
if (-not $python) {
    Warn "[缺少] Python 3.11+"
    $missing += 1
    if ($AutoInstall) {
        if (Install-Winget "Python.Python.3.12" "Python 3.12" @("--scope", "user")) {
            Refresh-Path
            $python = Find-Python
        }
    } else {
        Say "  自动安装：powershell -ExecutionPolicy Bypass -File .\install.ps1（去掉 -CheckOnly）"
    }
} else {
    Say "[ok] Python：$python"
}

if ($AppName) {
    if (Test-App) {
        Say "[ok] $AppName：已安装"
    } else {
        Warn "[缺少] $AppName"
        $missing += 1
        if ($AutoInstall) {
            Install-Winget $WingetId $AppName | Out-Null
            if ($AltWingetId -and -not (Test-App)) {
                Install-Winget $AltWingetId $AppName | Out-Null
            }
        } else {
            Say "  自动安装：powershell -ExecutionPolicy Bypass -File .\install.ps1（去掉 -CheckOnly）"
            Say "  或手动：winget install --id $WingetId -e"
        }
    }
}

if ($NeedsPyMuPDF) {
    $pymupdfOk = $false
    if ($python) {
        $old = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & $python -c "import pymupdf" 2>&1 | Out-Null
        $pymupdfOk = ($LASTEXITCODE -eq 0)
        $ErrorActionPreference = $old
    }
    if (-not $python) {
        Warn "[跳过] PyMuPDF：先解决 Python"
    } elseif ($pymupdfOk) {
        Say "[ok] PyMuPDF：已安装"
    } else {
        Warn "[缺少] PyMuPDF（PDF 翻译）"
        $missing += 1
        if ($AutoInstall) {
            # DSH 自带运行时：直接装进它的 site-packages；外部 Python：装到 user site
            if ($python -like "*dsh-runtimes*") {
                $pipArgs = @("-m", "pip", "install", "--disable-pip-version-check", "pymupdf")
            } else {
                $pipArgs = @("-m", "pip", "install", "--disable-pip-version-check", "--user", "pymupdf")
            }
            if ($WhatIf) {
                Write-Host ("[whatif] " + $python + " " + ($pipArgs -join " "))
            } else {
                $old = $ErrorActionPreference
                $ErrorActionPreference = "Continue"
                & $python @pipArgs
                $pipCode = $LASTEXITCODE
                $ErrorActionPreference = $old
                if ($pipCode -eq 0) { Write-Host "[ok] PyMuPDF 安装完成" } else { Warn "[!] PyMuPDF 安装失败（exit $pipCode）" }
            }
        } else {
            Say "  自动安装：powershell -ExecutionPolicy Bypass -File .\install.ps1（去掉 -CheckOnly）"
            Say "  或手动：& `"$python`" -m pip install --user pymupdf"
        }
    }
}

if ($NeedsNapCat) {
    $napcatOk = $false
    if ($env:NAPCAT_HOME -and (Test-Path -LiteralPath $env:NAPCAT_HOME)) { $napcatOk = $true }
    if (-not $napcatOk -and $env:USERPROFILE) {
        if (Test-Path -LiteralPath (Join-Path $env:USERPROFILE "napcat")) { $napcatOk = $true }
    }
    if (-not $napcatOk -and (Test-Path -LiteralPath (Join-Path $root "tools\napcat\shell"))) { $napcatOk = $true }
    if ($napcatOk) {
        Say "[ok] NapCat：已检测到"
    } else {
        Warn "[缺少] NapCat（QQ 发送的 OneBot 通道）"
        $missing += 1
        Say "  NapCat 不通过 winget 安装；请从 https://github.com/NapNeko/NapCatQQ 安装 Shell 版，"
        Say "  然后在 config.json 的 napcat.dir 指向安装目录（或设置 NAPCAT_HOME）。"
    }
}

if ($missing -gt 0) {
    Say ""
    if ($CheckOnly) {
        Say "有 $missing 项缺失。去掉 -CheckOnly 即可自动安装。"
    } else {
        Say "自动安装已尝试；若仍有缺少项，请按上面的提示手动处理。"
    }
}

# 检查结果不应让安装脚本失败
exit 0
