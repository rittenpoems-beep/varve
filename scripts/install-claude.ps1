<#
install-claude.ps1 — 把 Varve 挂到 Claude Code 上（第二个 Adapter）。

为什么脚本本体能复用：Claude Code 的 hook 契约与 Codex 高度一致——
  · 配置结构：{ "hooks": { "<EventName>": [ { "matcher"?, "hooks": [ { "type": "command", "command": ... } ] } ] } }
  · stdin payload 字段：session_id / cwd / source / prompt（与 Codex 同名）
  · 注入位置：UserPromptSubmit 的 additionalContext 追加在用户消息**之后**（缓存安全，与我们的铁律一致）
唯一差异：输出形态。Claude Code 侧用 `--json-output` 产出 hookSpecificOutput.additionalContext。

写什么：默认写用户级 ~/.claude/settings.json（对所有项目生效）；
        -Scope project 时写 <Project>/.claude/settings.json。
        只合并 hooks 段，不动 settings.json 里的其他设置；重复安装会先移除旧条目再写。
        两个事件都装：SessionStart（标记 + **重建检索索引**）与 UserPromptSubmit（尾部追加注入）。
        索引重建这条原先漏了 —— 只装标记/注入的话，Claude Code 侧索引永不刷新，
        检索会停在装的那一天且**没有任何报错**（2026-09-26 修）。

用法：
  pwsh -NoProfile -File scripts\install-claude.ps1
  pwsh -NoProfile -File scripts\install-claude.ps1 -Scope project -Project D:\my-project
#>
param(
    [ValidateSet("user", "project")][string]$Scope = "user",
    [string]$Project = "."
)

$ErrorActionPreference = "Stop"
$installDir = Split-Path -Parent $PSScriptRoot
$scriptDir = Join-Path $installDir "scripts"

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Write-Output "[FAIL] 未找到 python（需要 3.10+）"; exit 1 }

# Python 版本 / FTS5 必须校验：版本不够时 hook 会静默失败，装出来的是一套"看着在、实际不工作"的配置
$envLines = & python -X utf8 (Join-Path $PSScriptRoot "check-env.py") 2>$null
$E = @{}
foreach ($l in $envLines) { $kv = $l -split "=", 2; if ($kv.Count -eq 2) { $E[$kv[0]] = $kv[1] } }
$pv = $null
if (-not [string]::IsNullOrWhiteSpace([string]$E["python"])) {
    try { $pv = [version]([string]$E["python"]) } catch { $pv = $null }
}
if (-not $pv -or $pv -lt [version]"3.10") { Write-Output ("[FAIL] Python 版本过低/无法识别: " + $E["python"] + "（需要 3.10+）"); exit 1 }
if ($E["trigram"] -ne "ok") { Write-Output ("[FAIL] SQLite 缺少 FTS5/trigram（sqlite " + $E["sqlite"] + "）—— 检索索引不可用"); exit 1 }
$pyExe = $py.Source -replace "\\", "/"
$dir = $scriptDir -replace "\\", "/"

if ($Scope -eq "project") {
    $projAbs = [System.IO.Path]::GetFullPath($Project)
    if (-not (Test-Path -LiteralPath $projAbs)) { Write-Output ("[FAIL] 项目不存在: " + $projAbs); exit 1 }
    $cfgPath = Join-Path $projAbs ".claude\settings.json"
} else {
    $cfgPath = Join-Path $env:USERPROFILE ".claude\settings.json"
}

$cfgDir = Split-Path -Parent $cfgPath
if (-not (Test-Path -LiteralPath $cfgDir)) { New-Item -ItemType Directory -Path $cfgDir | Out-Null }

$settings = @{}
if (Test-Path -LiteralPath $cfgPath) {
    try {
        $raw = Get-Content -LiteralPath $cfgPath -Raw -Encoding UTF8
        if ($raw.Trim()) { $settings = $raw | ConvertFrom-Json -AsHashtable }
    } catch {
        Write-Output ("[FAIL] 现有 settings.json 解析失败（未改动）: " + $_.Exception.Message)
        exit 1
    }
}

if (-not $settings.ContainsKey("hooks") -or -not $settings["hooks"]) { $settings["hooks"] = @{} }
$hooks = $settings["hooks"]

function Set-VarveHook($eventName, $command) {
    $existing = @()
    if ($hooks.ContainsKey($eventName)) {
        foreach ($group in $hooks[$eventName]) {
            $kept = @()
            foreach ($h in $group["hooks"]) {
                # 清理本工具此前的条目（重装不叠加）。正则要覆盖**全部**我们要装的脚本，
                # 漏掉一个就会让重装后出现重复条目（2026-09-26 加 build-index）。
                if ($h["command"] -notmatch "hook-(session-start|user-prompt|build-index)\.py") { $kept += $h }
            }
            if ($kept.Count -gt 0) {
                $group["hooks"] = $kept
                $existing += $group
            }
        }
    }
    $existing += @{ hooks = @(@{ type = "command"; command = $command; timeout = 15 }) }
    $hooks[$eventName] = $existing
}

# SessionStart 要挂两条：标记（本进程）+ 后台重建索引（另起进程、异步）
# 同一事件多条目 -> 用一条 group 装两个 hook，避免产生两个 group 让重装清理变复杂
$ssGroups = @()
if ($hooks.ContainsKey("SessionStart")) {
    foreach ($group in $hooks["SessionStart"]) {
        $kept = @()
        foreach ($h in $group["hooks"]) {
            if ($h["command"] -notmatch "hook-(session-start|user-prompt|build-index)\.py") { $kept += $h }
        }
        if ($kept.Count -gt 0) { $group["hooks"] = $kept; $ssGroups += $group }
    }
}
$ssGroups += @{ hooks = @(
    @{ type = "command"; command = ($pyExe + ' "' + $dir + '/hook-session-start.py"'); timeout = 15 },
    @{ type = "command"; command = ($pyExe + ' -X utf8 "' + $dir + '/hook-build-index.py"'); timeout = 120 }
) }
$hooks["SessionStart"] = $ssGroups
Set-VarveHook "UserPromptSubmit" ($pyExe + ' -X utf8 "' + $dir + '/hook-user-prompt.py" --json-output')

$json = $settings | ConvertTo-Json -Depth 12
Set-Content -LiteralPath $cfgPath -Value $json -Encoding UTF8

Write-Output ""
Write-Output ("== 已写入: " + $cfgPath)
Write-Output "   SessionStart      -> hook-session-start.py（只标记，零输出）"
Write-Output "                     -> hook-build-index.py（后台重建检索索引）"
Write-Output "   UserPromptSubmit  -> hook-user-prompt.py --json-output（尾部追加注入）"
Write-Output ""
Write-Output "验证：新开一个 Claude Code 会话，看到「记忆系统 · 全局工程状态」即生效。"
$dataRoot = if ($env:VARVE_DATA) { $env:VARVE_DATA } else { Join-Path $env:USERPROFILE ".varve" }
$statusPath = Join-Path $dataRoot "STATUS.md"
$szNote = if (Test-Path -LiteralPath $statusPath) {
    ((Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8).Length.ToString() + " 字符")
} else { "STATUS.md 尚未创建" }
Write-Output ("注意：Claude Code 的 additionalContext 有 10,000 字符上限（当前注入源 " + $szNote + "）。")
