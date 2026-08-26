# 賽前個別更新（給 Windows 工作排程器每 5 分鐘呼叫一次）
# 腳本自己判斷有沒有比賽進入 T-60 / T-30 窗口，沒有就立刻結束。

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "data\logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$Log = Join-Path $LogDir ("live_" + (Get-Date -Format "yyyyMMdd") + ".log")

# numpy/scipy 的 Intel Fortran runtime 會攔截 console 關閉事件，排程環境要關掉
$env:FOR_DISABLE_CONSOLE_CTRL_HANDLER = "1"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$Py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Py) { "找不到 python" | Out-File $Log -Append -Encoding utf8; exit 1 }

Push-Location (Join-Path $Root "analysis")
try {
    & $Py "live_update.py" "--push" 2>&1 | Out-File $Log -Append -Encoding utf8
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

# 記錄檔只留最近 7 天
Get-ChildItem $LogDir -Filter "live_*.log" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 7 |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $code
