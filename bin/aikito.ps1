#!/usr/bin/env pwsh
$scriptPath = Join-Path $PSScriptRoot "aikito"
python $scriptPath @args
