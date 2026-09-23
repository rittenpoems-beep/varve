<#
doctor.ps1 — Varve 环境体检（只读，任何用户可跑）

检查：运行环境 / 数据目录 / 会话日志 / hooks 安装 / 索引状态 / AGENTS 契约。
用法：pwsh -NoProfile -File scripts\doctor.ps1 [-Project <检查哪个项目的 hooks>]
#>
param(
    [string]$Project = ".",
    [string]$DataRoot = $(if ($env:VARVE_DATA) { $env:VARVE_DATA } else { Join-Path $env:USERPROFILE ".varve" })
)
$ErrorActionPreference = "Continue"
$rows = New-Object System.Collections.ArrayList
function Chk($name, $ok, $detail) { [void]$rows.Add([pscustomobject]@{ OK = [bool]$ok; Name = $name; Detail = $detail }) }

# 1. 运行环境
Chk "PowerShell 7+" ($PSVersionTable.PSVersion.Major -ge 7) ("v" + $PSVersionTable.PSVersion)
$py = Get-Command python -ErrorAction SilentlyContinue
Chk "Python 可执行" ([bool]$py) $(if ($py) { $py.Source } else { "未找到 python" })
if ($py) {
    $envLines = & python -X utf8 "$PSScriptRoot\check-env.py" 2>$null
    $E = @{}
    foreach ($l in $envLines) { $kv = $l -split "=", 2; if ($kv.Count -eq 2) { $E[$kv[0]] = $kv[1] } }
    Chk "Python >= 3.10" ([version]$E["python"] -ge [version]"3.10") $E["python"]
    Chk "SQLite FTS5+trigram" ($E["trigram"] -eq "ok") $(if ($E["trigram"] -eq "ok") { "sqlite " + $E["sqlite"] } else { "不支持（索引不可用）" })
}

# 2. 数据目录
Chk "数据目录存在" (Test-Path -LiteralPath $DataRoot) $DataRoot
foreach ($d in @("index", "records")) {
    Chk ("  子目录 " + $d) (Test-Path -LiteralPath (Join-Path $DataRoot $d)) ""
}

# 3. 会话日志源
$sess = Join-Path $env:USERPROFILE ".codex\sessions"
$n = if (Test-Path -LiteralPath $sess) { (Get-ChildItem $sess -Recurse -Filter *.jsonl -ErrorAction SilentlyContinue | Measure-Object).Count } else { 0 }
Chk "Codex 会话日志" ($n -gt 0) ("$n 个 jsonl" + $(if ($n -eq 0) { "（尚无会话可索引）" } else { "" }))

# 4. hooks 安装
$projAbs = [System.IO.Path]::GetFullPath($Project)
$hooks = Join-Path $projAbs ".codex\hooks.json"
Chk "hooks.json 已安装" (Test-Path -LiteralPath $hooks) $hooks

# 5. 索引状态（--stats 走只读路径，不会触发 rebuild）
$db = Join-Path $DataRoot "index\sessions.db"
if (Test-Path -LiteralPath $db) {
    $sz = [math]::Round((Get-Item $db).Length / 1MB, 1)
    $line = ((& python -X utf8 (Join-Path $PSScriptRoot "build-search-index.py") --stats --data $DataRoot 2>&1) -join " ")
    $okIdx = ($LASTEXITCODE -eq 0) -and ($line -match "turns=")
    Chk "检索索引" $okIdx ($sz.ToString() + " MB | " + $line)
} else {
    Chk "检索索引" $false "未建立（跑 build-search-index.py）"
}

# 6. 检索契约（Skill 优先；AGENTS.md 规则句为可选双保险）
$skillMd = Join-Path $env:USERPROFILE ".codex\skills\varve-memory\SKILL.md"
$ag = Join-Path $env:USERPROFILE ".codex\AGENTS.md"
$hasSkill = Test-Path -LiteralPath $skillMd
$hasAgents = if (Test-Path -LiteralPath $ag) { [bool](Select-String -Path $ag -Pattern "recall.py" -SimpleMatch -Quiet) } else { $false }
Chk "检索契约" ($hasSkill -or $hasAgents) $(if ($hasSkill) { "Skill 已装" } elseif ($hasAgents) { "AGENTS.md 规则句" } else { "缺 —— 跑 install.ps1" })

# 输出
Write-Output ""
foreach ($r in $rows) {
    $tag = if ($r.OK) { "[ OK ]" } else { "[FAIL]" }
    Write-Output ("{0} {1,-26} {2}" -f $tag, $r.Name, $r.Detail)
}
$bad = ($rows | Where-Object { -not $_.OK }).Count
Write-Output ""
Write-Output ("体检完成：" + $rows.Count + " 项，失败 " + $bad + " 项")
exit $bad
