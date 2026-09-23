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
                if ($h["command"] -notmatch "hook-(session-start|user-prompt)\.py") { $kept += $h }
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

Set-VarveHook "SessionStart" ($pyExe + ' "' + $dir + '/hook-session-start.py"')
Set-VarveHook "UserPromptSubmit" ($pyExe + ' -X utf8 "' + $dir + '/hook-user-prompt.py" --json-output')

$json = $settings | ConvertTo-Json -Depth 12
Set-Content -LiteralPath $cfgPath -Value $json -Encoding UTF8

Write-Output ""
Write-Output ("== 已写入: " + $cfgPath)
Write-Output "   SessionStart      -> hook-session-start.py（只标记，零输出）"
Write-Output "   UserPromptSubmit  -> hook-user-prompt.py --json-output（尾部追加注入）"
Write-Output ""
Write-Output "验证：新开一个 Claude Code 会话，看到「记忆系统 · 全局工程状态」即生效。"
Write-Output "注意：Claude Code 的 additionalContext 有 10,000 字符上限（当前状态卡约 2.5k，安全）。"
