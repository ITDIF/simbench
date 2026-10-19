<#
打包成 exe。用法（在仓库根跑）：

    .\packaging\build.ps1
    .\packaging\build.ps1 -Python C:\Python313\python.exe

产物：

    dist\simbench\simbench.exe      控制台
    dist\simbench\simbench-gui.exe  无控制台（双击开界面）
    dist\simbench-onefile.exe       单文件

口径与验收步骤见 docs\superpowers\plans\2026-10-13-packaging.md 与 packaging\SMOKE.md。
Python 解释器默认用仓库里的 .venv（没有就用 PATH 上的 `python`），可用 `-Python` 换。
#>
param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $candidate = Join-Path $root ".venv\Scripts\python.exe"
    $Python = if (Test-Path $candidate) { $candidate } else { "python" }
}

& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller 没装：先跑 pip install -r requirements-dev.txt（打包工具在开发依赖里）"
    exit 1
}

Push-Location $root
try {
    $pyiArgs = @("-m", "PyInstaller", "--noconfirm", "--clean", (Join-Path $PSScriptRoot "simbench.spec"))
    & $Python @pyiArgs
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 退出码 $LASTEXITCODE" }
} finally {
    Pop-Location
}

# 演示点表放到 exe 旁边（不是 `_internal\` 里）：它是给人看的，不是运行时数据。
$out = Join-Path $root "dist\simbench"
if (Test-Path $out) {
    $demo = Join-Path $out "examples"
    New-Item -ItemType Directory -Force -Path $demo | Out-Null
    # **收 examples/ 下所有点表**，不写死名单：2026-10-19 加了 Modbus Poll 用的两份测试表，
    # 写死的话每加一份都要记得改这里，而漏了不会报错、只会让人以为"exe 里没带"。
    Copy-Item -Path (Join-Path $root "examples\*.csv") -Destination $demo -Force
}

Write-Host "onedir:  dist\simbench\simbench.exe / dist\simbench\simbench-gui.exe"
Write-Host "onefile: dist\simbench-onefile.exe"