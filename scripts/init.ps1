<#
init.ps1 — agent-memory 首次初始化
创建数据目录与首批文件。幂等：可重复运行，已存在的不动。

用法:
  pwsh -NoProfile -File scripts\init.ps1
  pwsh -NoProfile -File scripts\init.ps1 -DataRoot D:\varve-data
#>
param(
    [string]$DataRoot = $(if ($env:VARVE_DATA) { $env:VARVE_DATA } else { Join-Path $env:USERPROFILE ".varve" })
)

$ErrorActionPreference = "Stop"
$created = @()

foreach ($d in @($DataRoot, (Join-Path $DataRoot "index"), (Join-Path $DataRoot "records"))) {
    if (-not (Test-Path -LiteralPath $d)) {
        New-Item -ItemType Directory -Path $d | Out-Null
        $created += $d
    }
}

$projects = Join-Path $DataRoot "projects.md"
if (-not (Test-Path -LiteralPath $projects)) {
    $tpl = Join-Path $PSScriptRoot "..\templates\projects.template.md"
    if (Test-Path -LiteralPath $tpl) {
        Copy-Item -LiteralPath $tpl -Destination $projects
    } else {
        Set-Content -LiteralPath $projects -Value "# 项目索引（跨区路由表）`n" -Encoding utf8NoBOM
    }
    $created += $projects
}

"init 完成。新建项: " + $created.Count
$created | ForEach-Object { "  + " + $_ }
