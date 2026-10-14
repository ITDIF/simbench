<#
重新生成 `requirements.lock.txt`（`pip freeze` 的原文）。

用法（仓库根）：

    powershell -File packaging\lock.ps1

口径：先 `pip install -r requirements-dev.txt`（把该装的都装上），再跑这个——
锁文件是"这一个 venv 的当下快照"，所以它记录的必须是那一套。
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

$frozen = & $python -m pip freeze
$target = Join-Path $root "requirements.lock.txt"
$old = Get-Content -Raw $target
# 头注释保留（它是给人看的说明），正文整段替换
$header = ($old -split "\r?\n" | Where-Object { $_ -like "#*" -or $_ -eq "" }) -join "`n"
$body = ($frozen | Where-Object { $_ -and -not $_.StartsWith("-e ") } | Sort-Object) -join "`n"
Set-Content -Path $target -Value ($header.TrimEnd() + "`n" + $body + "`n") -Encoding UTF8
Write-Host "已刷新 requirements.lock.txt（$((($frozen)).Count) 条）"
