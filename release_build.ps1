[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [switch]$RunAllTests,
    [switch]$FailFast,
    [string]$TestPath = "tests/test_security_regressions.py"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptStart = Get-Date
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )
    Write-Host "[$Label] Started..." -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "[$Label] Failed with exit code $LASTEXITCODE."
    }
    Write-Host "[$Label] OK" -ForegroundColor Green
}

function Resolve-PythonCommand {
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @{ Exe = "python"; Args = @() }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @{ Exe = "py"; Args = @("-3") }
    }
    throw "Python runtime was not found in PATH."
}

$pyCmd = Resolve-PythonCommand
$pyExe = [string]$pyCmd.Exe
$pyBaseArgs = @($pyCmd.Args)

function Invoke-Python {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    & $pyExe @pyBaseArgs @Args
}

$resolvedTestTarget = $TestPath
if ($RunAllTests -and $TestPath -eq "tests/test_security_regressions.py") {
    $resolvedTestTarget = "tests"
}

if (-not (Test-Path -Path $resolvedTestTarget)) {
    throw "Test target not found: $resolvedTestTarget"
}

if (-not $SkipBuild) {
    if (-not (Test-Path -Path "VidDownloader.spec")) {
        throw "VidDownloader.spec was not found in repo root."
    }
}

Invoke-Step -Label "Preflight: pytest module" -Action {
    Invoke-Python -c "import pytest"
}

if (-not $SkipBuild) {
    Invoke-Step -Label "Preflight: PyInstaller module" -Action {
        Invoke-Python -c "import PyInstaller"
    }
}

$pytestArgs = @("-m", "pytest", $resolvedTestTarget, "-q")
if ($FailFast) {
    $pytestArgs += "-x"
}

Write-Host "[Gate] Running test gate target: $resolvedTestTarget" -ForegroundColor Cyan
Invoke-Python @pytestArgs
if ($LASTEXITCODE -ne 0) {
    throw "Test gate failed. Release build is blocked."
}

if ($SkipBuild) {
    $elapsed = (Get-Date) - $scriptStart
    Write-Host ("[Gate] Passed. Build skipped by flag. Elapsed: {0:n1}s" -f $elapsed.TotalSeconds) -ForegroundColor Green
    exit 0
}

Write-Host "[Build] Test gate passed. Starting PyInstaller release build..." -ForegroundColor Cyan
Invoke-Python -m PyInstaller --noconfirm VidDownloader.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

$elapsed = (Get-Date) - $scriptStart
Write-Host ("[Done] Release build completed successfully in {0:n1}s." -f $elapsed.TotalSeconds) -ForegroundColor Green
