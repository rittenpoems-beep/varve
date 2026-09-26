<#
sync-projects.ps1 — 从 Codex config.toml 发现工作区，登记进 projects.md 观察区

原理: Codex 在首次打开工作区时会把 [projects.'<path>'] 写入 config.toml，
      本脚本读它即可发现新工作区，不依赖任何 hook（hook 只决定何时跑）。

幂等: 已登记过的路径不会重复添加。可被 SessionStart hook 调用（-Quiet）。

用法:
  pwsh -NoProfile -File scripts\sync-projects.ps1
  pwsh -NoProfile -File scripts\sync-projects.ps1 -Quiet
#>
param(
    [string]$DataRoot = $(if ($env:VARVE_DATA) { $env:VARVE_DATA } else { Join-Path $env:USERPROFILE ".varve" }),
    [string]$ConfigPath = (Join-Path $env:USERPROFILE ".codex\config.toml"),
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

$projectsFile = Join-Path $DataRoot "projects.md"
if (-not (Test-Path -LiteralPath $projectsFile)) {
    if (-not $Quiet) { "projects.md 不存在，请先运行 init.ps1" }
    exit 0
}
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    if (-not $Quiet) { "config.toml 不存在: " + $ConfigPath }
    exit 0
}

$lines = Get-Content -LiteralPath $ConfigPath -Encoding UTF8
$paths = @()
foreach ($l in $lines) {
    # 兼容单/双引号（盲测 #8：只认单引号会静默漏掉工作区）。
    # 正则必须**只认紧跟引号就闭合**的表头：旧写法 `^\[projects\.(.+)\]$` 会把
    # 子表 [projects.'D:\x'.trust] 也抓进来，登记出 `D:\x'.trust` 这种垃圾路径
    # （2026-09-26 修；与 varve_hooks_common.py 的 sync_projects 保持同一判据）。
    $m = [regex]::Match($l, "^\s*\[projects\.(?:'([^']+)'|`"([^`"]+)`")\]\s*(?:#.*)?$")
    if ($m.Success) {
        $p = if ($m.Groups[1].Success) { $m.Groups[1].Value } else { $m.Groups[2].Value }
        if ($p) { $paths += $p }
    }
}
$paths = $paths | Sort-Object -Unique

$content = Get-Content -LiteralPath $projectsFile -Raw -Encoding UTF8
$new = @()
foreach ($p in $paths) {
    if ($content -notmatch [regex]::Escape($p)) {
        $new += $p
    }
}

if ($new.Count -gt 0) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
    $sb = New-Object System.Text.StringBuilder
    foreach ($p in $new) {
        [void]$sb.AppendLine("- " + $stamp + " 发现: " + $p)
    }
    Add-Content -LiteralPath $projectsFile -Value $sb.ToString().TrimEnd() -Encoding utf8NoBOM
}

if (-not $Quiet) {
    "扫描到 " + $paths.Count + " 个工作区；新增登记 " + $new.Count + " 个"
    $new | ForEach-Object { "  + " + $_ }
} elseif ($new.Count -gt 0) {
    "memory: 新登记 " + $new.Count + " 个工作区到 projects.md"
}
