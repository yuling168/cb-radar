# One-command daily refresh: download the latest DB snapshot from GitHub, then
# regenerate exports/ (Markdown per CB + all_cb_daily.csv + _index.md).
# Read-only for the project: the snapshot goes to .latest-snapshot/, never to data/cb_history.db.
# Usage (from anywhere):  powershell -File scripts\update_exports.ps1 [-Since 2026-01-01]
param([string]$Since = "2026-01-01")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:PYTHONIOENCODING = "utf-8"
$py = Join-Path $root ".venv311\Scripts\python.exe"
$work = Join-Path $root ".latest-snapshot"
$repo = "yuling168/cb-radar"

New-Item -ItemType Directory -Force $work | Out-Null

# 1. Latest manifest straight from GitHub main (the local copy may be stale).
$manifestPath = Join-Path $work "manifest.json"
$manifestText = (gh api -H "Accept: application/vnd.github.raw" "repos/$repo/contents/data/cb_history.manifest.json?ref=main") -join "`n"
if ($LASTEXITCODE -ne 0) { throw "failed to fetch manifest" }
# Write without BOM: Windows PowerShell's -Encoding utf8 adds one, which breaks json.loads.
[System.IO.File]::WriteAllText($manifestPath, $manifestText, (New-Object System.Text.UTF8Encoding($false)))
$snap = (Get-Content $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json).snapshot
Write-Host "Latest snapshot: $($snap.asset_name) (trade_date $($snap.trade_date))"

# 2. Download + verify + decompress with the project's own restore script.
$asset = Join-Path $work $snap.asset_name
if (-not (Test-Path $asset)) {
    Get-ChildItem $work -Filter *.db.gz | Remove-Item -Force
    gh release download $snap.release_tag --repo $repo --pattern $snap.asset_name --dir $work
    if ($LASTEXITCODE -ne 0) { throw "snapshot download failed" }
}
$db = Join-Path $work "cb_history.db"
if (Test-Path $db) { Remove-Item $db -Force }
& $py scripts\restore_db_snapshot.py --manifest $manifestPath --asset $asset --destination $db
if ($LASTEXITCODE -ne 0) { throw "snapshot restore/verification failed" }

# 3. Regenerate exports from the fresh DB.
& $py export_cb.py --all --since $Since --days 400 --database $db --output-dir exports
if ($LASTEXITCODE -ne 0) { throw "export failed" }
Write-Host "Done. Data through $($snap.trade_date). Files in $root\exports"
