# dsh-plugin-atuowexin 安装脚本（Windows PowerShell 5.1+）
#
# 作用：按本机目录生成 cordis.patch.yml（DSH 的 bundle 补丁层需要绝对路径），
#       并在需要时从 config.example.json 准备一份 config.json。
#
# 用法（在本目录执行）：
#     powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# 可选参数：
#     -Profile desktop   只影响提示文字
#     -Quiet             只生成文件，不打印说明

[CmdletBinding()]
param(
    [string]$Profile = "desktop",
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$lib = Join-Path $root "lib\index.js"
$tpl = Join-Path $root "cordis.patch.yml.tpl"
$out = Join-Path $root "cordis.patch.yml"
$workdir = ""
$config = ""

if (-not (Test-Path $lib)) { throw "找不到实现文件：$lib" }
if (-not (Test-Path $tpl)) { throw "找不到模板：$tpl" }

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

$workdir = Join-Path $root "data"
$config = Join-Path $root "config.json"
New-Item -ItemType Directory -Force -Path $workdir | Out-Null
if (-not (Test-Path $config)) {
    Copy-Item (Join-Path $root "config.example.json") $config
    $cfg = Get-Content $config -Raw -Encoding UTF8 | ConvertFrom-Json
    $cfg.workdir = $workdir
    [System.IO.File]::WriteAllText($config, ($cfg | ConvertTo-Json -Depth 10), (New-Object System.Text.UTF8Encoding($false)))
}

$text = Get-Content -Path $tpl -Raw -Encoding UTF8
$text = $text.Replace('{{LIB}}', $lib)
$text = $text.Replace('{{WORKDIR}}', $workdir)
$text = $text.Replace('{{CONFIG}}', $config)
Write-Utf8NoBom $out $text
Write-Host "[ok] 已生成：$out"

if ($Quiet) { return }

Write-Host ""
Write-Host "下一步："
Write-Host "  1) 让 DSH 里的 agent 执行：plugin_manager install_bundle  file:$root"
Write-Host "  2) 或在 GUI「设置 → 插件 → 安装 → 本地路径」填：$root"
Write-Host ""
Write-Host "装完在 GUI「设置 → 插件」里可整体启用/停用。"
