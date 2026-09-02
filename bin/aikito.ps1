#!/usr/bin/env pwsh
$scriptPath = Join-Path $PSScriptRoot "aikito"
$pythonExe = $null

if (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonExe = "py"
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
    $pythonExe = "python3"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonExe = "python"
} else {
    Write-Error "Python interpreter not found. Please install Python and add it to PATH."
    exit 1
}

if ($pythonExe -eq "py") {
    & py -3 $scriptPath @args
} else {
    & $pythonExe $scriptPath @args
}

$exitCode = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
exit $exitCode
