<#
doctor.ps1 — Varve 环境体检（只读，任何用户可跑）

检查：运行环境 / 数据目录 / 会话日志 / hooks 安装 / 索引状态 / AGENTS 契约。
用法：pwsh -NoProfile -File scripts\doctor.ps1 [-Project <检查哪个项目的 hooks>]
      -Project 只在**显式传入**时才检查项目级 hooks：默认安装是用户级（~/.codex/hooks.json），
      旧实现默认 -Project . 会把"当前目录没有 .codex"报成假 [FAIL]（2026-09-26 修）。
#>
param(
    [string]$Project = "",
    [string]$DataRoot = $(if ($env:VARVE_DATA) { $env:VARVE_DATA } else { Join-Path $env:USERPROFILE ".varve" })
)
$ErrorActionPreference = "Continue"
$rows = New-Object System.Collections.ArrayList
function Chk($name, $ok, $detail) { [void]$rows.Add([pscustomobject]@{ OK = [bool]$ok; Name = $name; Detail = $detail }) }

function Get-PyVer($s) {
    # check-env.py 无输出时 $E["python"] 是 $null：直接 [version]$null 会抛异常，
    # 体检项会消失且控制台喷红（2026-09-26 修）
    if ([string]::IsNullOrWhiteSpace([string]$s)) { return $null }
    try { return [version]([string]$s) } catch { return $null }
}

# 1. 运行环境
Chk "PowerShell 7+" ($PSVersionTable.PSVersion.Major -ge 7) ("v" + $PSVersionTable.PSVersion)
$py = Get-Command python -ErrorAction SilentlyContinue
Chk "Python 可执行" ([bool]$py) $(if ($py) { $py.Source } else { "未找到 python" })
if ($py) {
    $envLines = & python -X utf8 "$PSScriptRoot\check-env.py" 2>$null
    $E = @{}
    foreach ($l in $envLines) { $kv = $l -split "=", 2; if ($kv.Count -eq 2) { $E[$kv[0]] = $kv[1] } }
    $pv = Get-PyVer $E["python"]
    Chk "Python >= 3.10" ($pv -and $pv -ge [version]"3.10") $(if ($pv) { $E["python"] } else { "check-env.py 无输出/版本号不可解析" })
    Chk "SQLite FTS5+trigram" ($E["trigram"] -eq "ok") $(if ($E["trigram"] -eq "ok") { "sqlite " + $E["sqlite"] } else { "不支持（索引不可用）" })
}

# 2. 数据目录
Chk "数据目录存在" (Test-Path -LiteralPath $DataRoot) $DataRoot
foreach ($d in @("index", "records", "staging")) {
    Chk ("  子目录 " + $d) (Test-Path -LiteralPath (Join-Path $DataRoot $d)) ""
}

# 3. 会话日志源
$sess = Join-Path $env:USERPROFILE ".codex\sessions"
$n = if (Test-Path -LiteralPath $sess) { (Get-ChildItem $sess -Recurse -Filter *.jsonl -ErrorAction SilentlyContinue | Measure-Object).Count } else { 0 }
Chk "Codex 会话日志" ($n -gt 0) ("$n 个 jsonl" + $(if ($n -eq 0) { "（尚无会话可索引）" } else { "" }))

# 4. hooks 安装（项目级 / 全局级）
# 项目级只在显式传 -Project 时检查；否则"跳过"（记为 OK，以保持 exit code = 失败项数的语义）
if ($Project) {
    $projAbs = [System.IO.Path]::GetFullPath($Project)
    $hooks = Join-Path $projAbs ".codex\hooks.json"
    Chk "hooks（项目级）" (Test-Path -LiteralPath $hooks) $hooks
    $hookFiles = @($hooks)
} else {
    Chk "hooks（项目级）" $true "未指定 -Project，跳过（默认装用户级；要查项目级请传 -Project <路径>）"
    $hookFiles = @()
}
$globalHooks = Join-Path $env:USERPROFILE ".codex\hooks.json"
$hasGlobalHooks = if (Test-Path -LiteralPath $globalHooks) {
    [bool](Select-String -Path $globalHooks -Pattern "hook-session-start" -SimpleMatch -Quiet)
} else { $false }
Chk "hooks（全局级）" $hasGlobalHooks $(if ($hasGlobalHooks) { $globalHooks } else { "未装（全局卡模式下推荐装到 ~/.codex/hooks.json）" })
if (Test-Path -LiteralPath $globalHooks) { $hookFiles += $globalHooks }

# hook 失败一律静默 → 指向不存在的脚本时没人会发现。这里把 hooks.json 里的 .py 路径
# 逐个验在（含"装了但指向已移动的旧安装目录"这种），2026-09-26 加。
$pyRefs = @{}
foreach ($hf in $hookFiles) {
    if (-not (Test-Path -LiteralPath $hf)) { continue }
    $raw = Get-Content -LiteralPath $hf -Raw -Encoding UTF8
    foreach ($m in [regex]::Matches($raw, '([A-Za-z]:[\\/][^"]*?\.py)')) {
        $pyRefs[$m.Groups[1].Value] = $hf
    }
}
if ($pyRefs.Count -gt 0) {
    $missingRefs = @($pyRefs.Keys | Where-Object { -not (Test-Path -LiteralPath $_) })
    Chk "hook 脚本路径有效" ($missingRefs.Count -eq 0) $(if ($missingRefs.Count -eq 0) {
        "$($pyRefs.Count) 个引用全部存在"
    } else { "指向不存在: " + ($missingRefs -join ", ") })
}

# 4.5 全局卡
$globalStatus = Join-Path $DataRoot "STATUS.md"
Chk "全局卡 STATUS.md" (Test-Path -LiteralPath $globalStatus) $globalStatus

# 5. 索引状态（--stats 走只读路径，不会触发 rebuild）
$db = Join-Path $DataRoot "index\sessions.db"
if (Test-Path -LiteralPath $db) {
    $sz = [math]::Round((Get-Item $db).Length / 1e6, 1)   # 与 audit.py / build-search-index.py 统一：MB = 10^6 字节
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
