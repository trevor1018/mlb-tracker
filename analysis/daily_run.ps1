# 每日自動更新（給 Windows 工作排程器呼叫）
# 手動測試：powershell -ExecutionPolicy Bypass -File D:\python\mlb-tracker\analysis\daily_run.ps1

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "data\logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Log = Join-Path $LogDir "daily_$Stamp.log"

"=== MLB 每日更新 開始 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $Log -Encoding utf8

# 找 python（優先用 PATH 上的）
$Py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Py) { $Py = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $Py) {
    "找不到 python，中止" | Out-File $Log -Append -Encoding utf8
    exit 1
}
"python: $Py" | Out-File $Log -Append -Encoding utf8

Push-Location (Join-Path $Root "analysis")
try {
    # --push：跑完自動 git commit + push 到 GitHub Pages
    & $Py "daily_update.py" "--push" 2>&1 | Out-File $Log -Append -Encoding utf8
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

"=== 結束 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') exit=$code ===" | Out-File $Log -Append -Encoding utf8

# 只保留最近 14 份記錄
Get-ChildItem $LogDir -Filter "daily_*.log" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 14 |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $code
