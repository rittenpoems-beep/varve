<#
install.ps1 — Varve 安装器：让一个陌生用户从零到"记忆自动生效"。

做五件事：
  1. 环境检查（Python 3.10+ / SQLite FTS5+trigram / PowerShell 7+）
  2. 创建数据目录（默认 %USERPROFILE%\.varve；-DataRoot 可覆盖）
  3. 生成 .codex/hooks.json（用本机 Python 路径 + 当前安装路径，写入目标项目）
  4. 安装 Skill（templates\skill-varve-memory.md -> ~/.codex/skills/varve-memory）
  5. 输出 AGENTS.md 规则句（打印；-AppendAgents 可自动追加到用户 AGENTS.md）

用法：
  pwsh -NoProfile -File scripts\install.ps1
      # 默认 -Scope user：写 ~/.codex/hooks.json，对所有工作区生效（推荐，与 install-claude.ps1 对称）
  pwsh -NoProfile -File scripts\install.ps1 -Scope project -Project D:\my-project
      # 只对该项目生效
  pwsh -NoProfile -File scripts\install.ps1 -DataRoot D:\varve-data -AppendAgents
      # -DataRoot 非默认目录时会**持久化**用户环境变量 VARVE_DATA（否则脚本找不到数据根）；
      # 不想动环境变量就加 -NoSetEnv（此时会以 [FAIL] 提示手动设置）

退出码：0 = 全部检查通过；1 = 存在 [FAIL] 项（供 CI / 自动化感知，2026-09-23 修）。
装完后唯一的手动步骤：在 Codex UI 里点一次 hooks 信任（New hook - review required）。
#>
param(
    [string]$Project = ".",
    [string]$DataRoot = "",
    [switch]$AppendAgents,
    [switch]$Force,
    [switch]$NoSetEnv,
    [ValidateSet("user", "project")][string]$Scope = "user"
)

$ErrorActionPreference = "Stop"
$installDir = Split-Path -Parent $PSScriptRoot
$ok = $true
$givenRoot = $DataRoot          # 分清"用户显式传了 -DataRoot"和"用了默认值"

function Step($msg) { Write-Output ("`n== " + $msg) }
function Good($msg) { Write-Output ("  [OK]   " + $msg) }
function Warn($msg) { Write-Output ("  [WARN] " + $msg) }
function Bad($msg)  { Write-Output ("  [FAIL] " + $msg); $script:ok = $false }

function Get-PyVer($s) {
    # check-env.py 没跑起来时 $E["python"] 是 $null，直接 [version]$null 会抛异常 ——
    # 把"环境不合规"变成"安装器崩溃"（2026-09-26 修）
    if ([string]::IsNullOrWhiteSpace([string]$s)) { return $null }
    try { return [version]([string]$s) } catch { return $null }
}

# ---------- 1. 环境检查 ----------
Step "1/5 环境检查"

$py = (Get-Command python -ErrorAction SilentlyContinue)
if ($py) {
    $envLines = & python -X utf8 "$PSScriptRoot\check-env.py" 2>$null
    $E = @{}
    foreach ($l in $envLines) { $kv = $l -split "=", 2; if ($kv.Count -eq 2) { $E[$kv[0]] = $kv[1] } }
    $pv = Get-PyVer $E["python"]
    if (-not $pv) {
        Bad ("check-env.py 未返回可用版本号（输出：" + (($envLines -join " / ")) + "）")
    } elseif ($pv -ge [version]"3.10") {
        Good ("Python " + $E["python"] + " @ " + $py.Source)
    } else { Bad ("Python 版本过低: " + $E["python"] + "（需要 3.10+）") }
    if ($E["trigram"] -eq "ok") { Good ("SQLite " + $E["sqlite"] + " 含 FTS5 + trigram") }
    else { Bad "SQLite 缺少 FTS5/trigram —— 索引不可用" }
} else { Bad "未找到 python（需要 3.10+，且 sqlite 带 FTS5）" }

if ($PSVersionTable.PSVersion.Major -ge 7) { Good ("PowerShell " + $PSVersionTable.PSVersion) }
else { Warn "建议 PowerShell 7+（pwsh）" }

# ---------- 2. 数据目录 ----------
Step "2/5 数据目录"

$defaultRoot = [System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE ".varve"))
if (-not $DataRoot) { $DataRoot = $defaultRoot }
$DataRoot = [System.IO.Path]::GetFullPath($DataRoot)
foreach ($d in @("", "index", "records", "staging")) {
    $p = if ($d) { Join-Path $DataRoot $d } else { $DataRoot }
    if (-not (Test-Path -LiteralPath $p)) { New-Item -ItemType Directory -Path $p | Out-Null }
}
Good ("数据目录: " + $DataRoot)

# 数据根必须让**所有**脚本都找得到（靠 $env:VARVE_DATA）。旧实现只打一句 Warn ——
# 用户传了 -DataRoot 却没设环境变量时，hook / recall 会去 ~/.varve 找，
# 表现成"记忆没生效"，且没有任何报错（静默的根目录不匹配，2026-09-26 修）。
$curRoot = [Environment]::GetEnvironmentVariable("VARVE_DATA", "User")
if ($DataRoot -ne $defaultRoot) {
    if ($givenRoot -and -not $NoSetEnv) {
        if ($curRoot -and [System.IO.Path]::GetFullPath($curRoot) -ne $DataRoot) {
            Warn ("用户环境变量 VARVE_DATA 原指向 " + $curRoot + "，本次覆盖为 " + $DataRoot)
        }
        try {
            [Environment]::SetEnvironmentVariable("VARVE_DATA", $DataRoot, "User")
            $env:VARVE_DATA = $DataRoot
            Good ("已持久化用户环境变量 VARVE_DATA=" + $DataRoot + "（本次会话立即生效；撤销：删掉该环境变量）")
        } catch {
            Bad ("无法写入用户环境变量（" + $_.Exception.Message + "）：请手动设置 VARVE_DATA=" + $DataRoot)
        }
    } elseif ($curRoot -and [System.IO.Path]::GetFullPath($curRoot) -eq $DataRoot) {
        Good ("VARVE_DATA 已指向 " + $DataRoot + "（-NoSetEnv：未改动环境变量）")
    } else {
        Bad ("数据根不是默认目录，但 VARVE_DATA 未指向它 —— 脚本会去 " + $defaultRoot +
             " 找数据。请手动设置 VARVE_DATA=" + $DataRoot + "，或去掉 -NoSetEnv 重跑")
    }
} elseif ($curRoot -and [System.IO.Path]::GetFullPath($curRoot) -ne $defaultRoot) {
    Bad ("用户环境变量 VARVE_DATA 指向 " + $curRoot + "，但本次安装用默认目录 " +
         $defaultRoot + " —— 两者必须一致（改环境变量或传 -DataRoot 指向 " + $curRoot + "）")
} else {
    Good ("VARVE_DATA 未设置，全部脚本将使用默认目录 " + $defaultRoot)
}

$globalStatus = Join-Path $DataRoot "STATUS.md"
if (-not (Test-Path -LiteralPath $globalStatus)) {
    $tmpl = Join-Path $installDir "templates\global-status.template.md"
    if (Test-Path -LiteralPath $tmpl) {
        Copy-Item -LiteralPath $tmpl -Destination $globalStatus
        Good ("已创建全局卡: " + $globalStatus)
    } else { Warn "templates\global-status.template.md 缺失，跳过全局卡" }
} else {
    Good ("全局卡已存在: " + $globalStatus)
}

# ---------- 3. hooks.json ----------
Step ("3/5 hooks.json（scope=" + $Scope + "）")

# 没有可用的 python 就**不要**写 hooks.json：旧实现会写出解释器路径为空的配置，
# 而 hook 失败是静默的 —— 用户以为装好了，其实一条都没跑（2026-09-26 修）。
if (-not $py) {
    Write-Output ""
    Write-Output "== 安装中止：未找到 python（3.10+）。hooks.json 需要绝对解释器路径，先装好 Python 再重跑。"
    exit 1
}

# 默认 user：写 ~/.codex/hooks.json，对所有工作区生效（与 install-claude.ps1 对称）
# project：只写 <Project>/.codex/hooks.json，仅该项目生效
if ($Scope -eq "project") {
    $projAbs = [System.IO.Path]::GetFullPath($Project)
    if (-not (Test-Path -LiteralPath $projAbs)) { Bad ("目标项目不存在: " + $projAbs); exit 1 }
    $codexDir = Join-Path $projAbs ".codex"
} else {
    $codexDir = Join-Path $env:USERPROFILE ".codex"
}
$hooksPath = Join-Path $codexDir "hooks.json"
$pyExe = $py.Source -replace "\\", "/"
$scriptDir = ($PSScriptRoot -replace "\\", "/")

$hooksJson = @"
{
  "description": "Varve hooks - session status injection + search index rebuild + history recall reminder",
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear|compact",
        "hooks": [
          {
            "type": "command",
            "command": "$pyExe \"$scriptDir/hook-session-start.py\"",
            "timeout": 15,
            "statusMessage": "加载工程状态",
            "additionalContextLimit": 4000
          },
          {
            "type": "command",
            "command": "$pyExe -X utf8 \"$scriptDir/hook-build-index.py\"",
            "async": true,
            "timeout": 120,
            "statusMessage": "重建检索索引"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "$pyExe -X utf8 \"$scriptDir/hook-user-prompt.py\"",
            "timeout": 10,
            "statusMessage": "历史信号检测"
          }
        ]
      }
    ]
  }
}
"@

if ((Test-Path -LiteralPath $hooksPath) -and -not $Force) {
    Warn "已存在 hooks.json（未覆盖）。请手动合并以下 SessionStart 条目："
    Write-Output $hooksJson
} else {
    if (-not (Test-Path -LiteralPath $codexDir)) { New-Item -ItemType Directory -Path $codexDir | Out-Null }
    Set-Content -LiteralPath $hooksPath -Value $hooksJson -Encoding UTF8
    Good ("已写入: " + $hooksPath)
}

# ---------- 4. Skill 安装（推荐路径：不写全局 AGENTS.md） ----------
Step "4/5 Skill 安装（替代全局契约）"

$skillSrc = Join-Path $installDir "templates\skill-varve-memory.md"
$skillDir = Join-Path $env:USERPROFILE ".codex\skills\varve-memory"
if (Test-Path -LiteralPath $skillSrc) {
    if (-not (Test-Path -LiteralPath $skillDir)) { New-Item -ItemType Directory -Path $skillDir | Out-Null }
    $skillText = (Get-Content -LiteralPath $skillSrc -Raw) -replace "<VARVE_HOME>", $installDir -replace "<VARVE_DATA>", $DataRoot
    Set-Content -LiteralPath (Join-Path $skillDir "SKILL.md") -Value $skillText -Encoding UTF8
    Good ("已安装 Skill: " + (Join-Path $skillDir "SKILL.md"))
    Write-Output "         （Skill 的 description 会自动出现在每个会话的技能列表里 —— 这就是检索契约，不需要动全局 AGENTS.md）"
} else {
    Warn ("templates\skill-varve-memory.md 缺失，跳过 Skill 安装")
}

# ---------- 5. AGENTS.md 规则句（可选，双保险） ----------
Step "5/5 AGENTS.md 规则句（可选）"

$snippetPath = Join-Path $installDir "templates\agents-snippet.md"
$snippet = if (Test-Path -LiteralPath $snippetPath) {
    (Get-Content -LiteralPath $snippetPath -Raw) -replace "<VARVE_HOME>", $installDir -replace "<VARVE_DATA>", $DataRoot
} else { "（templates\agents-snippet.md 缺失）" }

$agentsPath = Join-Path $env:USERPROFILE ".codex\AGENTS.md"
if ($AppendAgents) {
    if (Test-Path -LiteralPath $agentsPath) {
        Add-Content -LiteralPath $agentsPath -Value ("`n" + $snippet) -Encoding UTF8
        Good ("已追加到 " + $agentsPath)
    } else { Warn ("用户 AGENTS.md 不存在: " + $agentsPath + "（请手动创建并粘贴下列内容）"); Write-Output $snippet }
} else {
    Warn "请把以下规则句加进你的 AGENTS.md（或用 -AppendAgents 自动追加）："
    Write-Output $snippet
}

Write-Output ""
if ($ok) {
    Write-Output "== 安装完成。最后一步（无法自动化）：在 Codex 里打开任意会话，按提示点一次 hooks 信任。"
    Write-Output "   验证：开一个新会话，看到「工程状态」注入即生效；或跑 doctor.ps1。"
    exit 0
} else {
    Write-Output "== 安装未完成：请先解决上面 [FAIL] 项。"
    exit 1
}
