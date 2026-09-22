<#
build-docs-index.ps1 — 文档索引生成（L3 扩展：把现有文档纳入可检索索引）

扫描指定目录下的 .md 文件，提取首行标题，生成 index\docs.md。
幂等：每次都全量重建（文档数量少，重建成本低）。

用法:
  pwsh -NoProfile -File scripts\build-docs-index.ps1
  pwsh -NoProfile -File scripts\build-docs-index.ps1 -ScanPaths "D:\my-project","%USERPROFILE%\Documents\Codex"
#>
param(
    [string]$DataRoot = $(if ($env:VARVE_DATA) { $env:VARVE_DATA } else { Join-Path $env:USERPROFILE ".varve" }),
    [string[]]$ScanPaths = @((Get-Location).Path)
)

$out = Join-Path $DataRoot "index\docs.md"
$rows = New-Object System.Collections.ArrayList

foreach ($root in $ScanPaths) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    Get-ChildItem -LiteralPath $root -Filter "*.md" -File -ErrorAction SilentlyContinue | ForEach-Object {
        $title = ""
        $head = Get-Content -LiteralPath $_.FullName -Encoding UTF8 -TotalCount 6 -ErrorAction SilentlyContinue
        foreach ($l in $head) {
            if ($l -match "^#\s+(.+)$") { $title = $Matches[1].Trim(); break }
        }
        if ($title -eq "") { $title = "(无标题)" }
        [void]$rows.Add([pscustomobject]@{
            Root    = $root
            File    = $_.Name
            Title   = $title
            KB      = [math]::Round($_.Length / 1KB, 1)
            Updated = $_.LastWriteTime.ToString("yyyy-MM-dd")
        })
    }
}

$sorted = $rows | Sort-Object Updated -Descending

$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine("# 文档索引")
[void]$sb.AppendLine("")
[void]$sb.AppendLine("> 由 ``build-docs-index.ps1`` 生成（可重建）。检索：``rg ""关键词"" docs.md``。")
[void]$sb.AppendLine("> 覆盖目录：" + ($ScanPaths -join " ; "))
[void]$sb.AppendLine("")
[void]$sb.AppendLine("| 文件 | 标题 | KB | 更新 | 所在目录 |")
[void]$sb.AppendLine("|---|---|---|---|---|")
foreach ($r in $sorted) {
    [void]$sb.AppendLine("| " + $r.File + " | " + $r.Title + " | " + $r.KB + " | " + $r.Updated + " | " + $r.Root + " |")
}

Set-Content -LiteralPath $out -Value $sb.ToString() -Encoding utf8NoBOM

"扫描 " + $ScanPaths.Count + " 个目录；收录 " + $sorted.Count + " 个文档"
"out=" + $out
