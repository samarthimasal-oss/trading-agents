# Install and optionally compile the native TradingAgents EA.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_ea.ps1 -Compile
#
# Copies only mql5\Include\TradingAgents and mql5\Experts\TradingAgents into the
# MT5 data folder. It never touches other experts or includes.

param(
    [string]$DataFolder = "",
    [string]$MetaEditor = "",
    [switch]$Compile
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sourceInclude = Join-Path $repoRoot "mql5\Include\TradingAgents"
$sourceExpert = Join-Path $repoRoot "mql5\Experts\TradingAgents"
if (-not (Test-Path $sourceInclude) -or -not (Test-Path $sourceExpert)) {
    throw "MQL5 sources not found under $repoRoot\mql5"
}

function Find-DataFolder {
    if ($DataFolder) {
        if (-not (Test-Path (Join-Path $DataFolder "MQL5"))) {
            throw "-DataFolder does not contain an MQL5 folder: $DataFolder"
        }
        return (Resolve-Path $DataFolder).Path
    }
    $root = Join-Path $env:APPDATA "MetaQuotes\Terminal"
    $candidates = Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName "MQL5\Experts") }
    if (-not $candidates) {
        throw "No MT5 data folder found under $root. Pass -DataFolder explicitly."
    }
    if ($candidates.Count -gt 1) {
        $list = ($candidates | ForEach-Object { "  " + $_.FullName }) -join "`n"
        throw "Multiple MT5 data folders found. Pass -DataFolder explicitly:`n$list"
    }
    return $candidates[0].FullName
}

function Find-MetaEditor {
    if ($MetaEditor) {
        if (-not (Test-Path $MetaEditor)) { throw "MetaEditor not found: $MetaEditor" }
        return (Resolve-Path $MetaEditor).Path
    }
    $candidates = @(
        "C:\Program Files\MetaTrader 5\MetaEditor64.exe",
        "C:\Program Files\MetaTrader 5\MetaEditor.exe"
    )
    foreach ($path in $candidates) {
        if (Test-Path $path) { return $path }
    }
    throw "MetaEditor64.exe not found. Pass -MetaEditor explicitly."
}

$data = Find-DataFolder
Write-Host "MT5 data folder: $data"

$targetInclude = Join-Path $data "MQL5\Include\TradingAgents"
$targetExpert = Join-Path $data "MQL5\Experts\TradingAgents"
New-Item -ItemType Directory -Force -Path $targetInclude | Out-Null
New-Item -ItemType Directory -Force -Path $targetExpert | Out-Null

Copy-Item (Join-Path $sourceInclude "*.mqh") $targetInclude -Force
Copy-Item (Join-Path $sourceExpert "*.mq5") $targetExpert -Force
Write-Host "Copied $((Get-ChildItem $targetInclude -Filter *.mqh).Count) includes and EA source."

if ($Compile) {
    $editor = Find-MetaEditor
    $ea = Join-Path $targetExpert "TradingAgentsEA.mq5"
    $log = Join-Path $env:TEMP "trading_agents_ea_compile.log"
    Write-Host "Compiling with: $editor"
    & $editor /compile:"$ea" /log:"$log" | Out-Null
    Start-Sleep -Seconds 2
    if (-not (Test-Path $log)) { throw "MetaEditor produced no log: $log" }
    $text = Get-Content $log -Encoding Unicode
    $result = ($text | Select-String -Pattern "Result:" | Select-Object -Last 1).Line
    if (-not $result) {
        $text | Select-Object -Last 20
        throw "Compile result not found in $log"
    }
    Write-Host $result
    if ($result -notmatch "0 errors") {
        $text | Select-String -Pattern "error|warning" | Select-Object -Last 30
        throw "EA compile failed; fix the errors above."
    }
}

$pythonToken = Join-Path $repoRoot "state\ea_token.txt"
$commonToken = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files\TradingAgents\token.txt"
if (-not (Test-Path $pythonToken) -or -not (Test-Path $commonToken)) {
    Write-Warning "EA token missing. Run: python main.py ea-token"
} else {
    Write-Host "EA token files present (Python + MT5 common files)."
}

Write-Host ""
Write-Host "Next:"
Write-Host "  1. MT5: Tools > Options > Expert Advisors, enable"
Write-Host "     'Allow WebRequest for listed URL' and add"
Write-Host "     http://127.0.0.1:8765. The list is stored encrypted;"
Write-Host "     editing common.ini by hand is ignored."
Write-Host "  2. python main.py ea-server"
Write-Host "  3. Attach TradingAgentsEA to XAUUSD M15 with InpMode=PAPER"
Write-Host "     and InpTokenFile=TradingAgents\token.txt"
