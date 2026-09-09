#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Aikito Windows one-liner installer.

.DESCRIPTION
    Installs Aikito on Windows. Validates Windows Developer Mode (required for
    symlink support), cleans up legacy bin/ installations, and installs Aikito
    using uv tool (preferred) or an isolated Python virtual environment.

.EXAMPLE
    irm https://raw.githubusercontent.com/lsaint/aikito/main/install.ps1 | iex

.EXAMPLE
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/lsaint/aikito/main/install.ps1))) -InstallDir "D:\aikito"
#>
[CmdletBinding()]
param (
    # Install a specific version instead of the latest release (e.g. "1.30.0").
    [string] $Version = "",

    # Install to a custom directory instead of the default when using a virtual environment.
    [string] $InstallDir = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --- Constants ----------------------------------------------------------------

$DisplayName = "Aikito"
$DefaultDir  = Join-Path $env:LOCALAPPDATA "Programs\aikito"

# --- Helpers ------------------------------------------------------------------

function Write-Step([string] $Msg) {
    Write-Host "  $Msg" -ForegroundColor Cyan
}

function Write-Ok([string] $Msg) {
    Write-Host "  [OK] $Msg" -ForegroundColor Green
}

function Write-Warn([string] $Msg) {
    Write-Host "  [WARN] $Msg" -ForegroundColor Yellow
}

function Write-Fail([string] $Msg) {
    Write-Host ""
    Write-Host "  [ERROR] $Msg" -ForegroundColor Red
    Write-Host ""
    exit 1
}

function Clean-LegacyInstallation([string] $BaseDir) {
    $legacyBin = Join-Path $BaseDir "bin"

    # 1. Clean legacy aikito\bin entries from User PATH (matches DefaultDir, InstallDir, or any past *\aikito\bin)
    $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if ($userPath) {
        $parts = $userPath -split ';' | Where-Object {
            $entry = $_.Trim().TrimEnd('\')
            $entry -and ($entry -ne $legacyBin.TrimEnd('\')) -and ($entry -notmatch '[\\/]aikito[\\/]bin$')
        }
        $cleanedUserPath = $parts -join ';'
        if ($cleanedUserPath -ne $userPath) {
            [Environment]::SetEnvironmentVariable("PATH", $cleanedUserPath, "User")
            Write-Ok "Removed legacy bin entry from User PATH"
        }
    }

    # Also clean current session env:PATH
    $sessionParts = $env:PATH -split ';' | Where-Object {
        $entry = $_.Trim().TrimEnd('\')
        $entry -and ($entry -ne $legacyBin.TrimEnd('\')) -and ($entry -notmatch '[\\/]aikito[\\/]bin$')
    }
    $env:PATH = $sessionParts -join ';'

    # 2. Check specifically for legacy Aikito stub scripts before removing legacy files
    $isLegacyAikito = (Test-Path (Join-Path $legacyBin "aikito.cmd")) -or `
                      (Test-Path (Join-Path $legacyBin "aikito")) -or `
                      (Test-Path (Join-Path $legacyBin "aikito.ps1"))

    if ($isLegacyAikito) {
        Write-Step "Cleaning up legacy installation stubs in $legacyBin ..."
        try {
            Remove-Item $legacyBin -Recurse -Force -ErrorAction Stop
            Write-Ok "Removed legacy $legacyBin"
        } catch {
            Write-Warn "Could not remove legacy $legacyBin ($_); continuing..."
        }
    }
}

# --- Banner -------------------------------------------------------------------

Write-Host ""
Write-Host "  $DisplayName Installer for Windows" -ForegroundColor White
Write-Host "  -----------------------------------------" -ForegroundColor DarkGray
Write-Host ""

# --- Check Developer Mode (symlink support) -----------------------------------

Write-Step "Checking Windows Developer Mode (required for symlinks) ..."

$developerModeOk = $false

# 1. Try registry check first
try {
    $regPath = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock"
    $val = (Get-ItemProperty -Path $regPath -Name "AllowDevelopmentWithoutDevLicense" -ErrorAction Stop).AllowDevelopmentWithoutDevLicense
    $developerModeOk = ($val -eq 1)
} catch {}

# 2. Fall back: probe actual symlink creation (works for Administrator sessions)
if (-not $developerModeOk) {
    try {
        $tmpDir = [System.IO.Path]::GetTempPath()
        $probeTarget = Join-Path $tmpDir "aikito-probe-target-$([System.Guid]::NewGuid())"
        $probeSrc    = Join-Path $tmpDir "aikito-probe-link-$([System.Guid]::NewGuid())"
        [void][System.IO.File]::WriteAllText($probeTarget, "probe")
        New-Item -ItemType SymbolicLink -Path $probeSrc -Target $probeTarget -ErrorAction Stop | Out-Null
        $developerModeOk = $true
        Remove-Item $probeSrc    -ErrorAction SilentlyContinue
        Remove-Item $probeTarget -ErrorAction SilentlyContinue
    } catch {}
}

if (-not $developerModeOk) {
    Write-Fail @"
Windows Developer Mode is required to create symbolic links without Administrator privileges.

  How to enable (choose one):

  Option 1 - Settings GUI:
    Settings -> System -> For developers -> Developer Mode (toggle ON)

  Option 2 - PowerShell as Administrator:
    reg add "HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock" /t REG_DWORD /f /v "AllowDevelopmentWithoutDevLicense" /d "1"

After enabling Developer Mode, re-run this installer.
"@
}

Write-Ok "Developer Mode / symlink support confirmed"

# --- Cleanup Legacy Installation ---------------------------------------------

Clean-LegacyInstallation $DefaultDir
if ($InstallDir -and ($InstallDir -ne $DefaultDir)) {
    Clean-LegacyInstallation $InstallDir
}

# --- Install Package ----------------------------------------------------------

$pkgSpec = if ($Version) { "aikito==$($Version.TrimStart('v'))" } else { "aikito" }
$installedViaUv = $false
$ScriptsDir = $null

if (-not $InstallDir -and (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Step "Found uv, installing via 'uv tool install' ..."
    try {
        & uv tool install --force $pkgSpec
        if ($LASTEXITCODE -eq 0) {
            $installedViaUv = $true
            Write-Ok "Installed $pkgSpec via uv tool"
            try { & uv tool update-shell 2>$null } catch {}

            # Temporarily add candidate uv bin directories to session PATH for immediate verification
            $uvBinCandidates = @(
                $env:UV_TOOL_BIN_DIR,
                (Join-Path $env:USERPROFILE ".local\bin")
            ) | Where-Object { $_ -and (Test-Path $_) }

            foreach ($cand in $uvBinCandidates) {
                if ($env:PATH -notlike "*$cand*") {
                    $env:PATH = "$cand;$env:PATH"
                }
            }
        }
    } catch {
        Write-Warn "uv tool install failed ($_); falling back to Python virtualenv..."
    }
}

if (-not $installedViaUv) {
    # --- Check Python 3.12+ ---
    Write-Step "Checking Python 3.12+ ..."

    $PythonExe = $null
    $PythonVer = $null
    foreach ($candidate in @("py", "python3", "python")) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($found) {
            try {
                $invokeArgs = if ($candidate -eq "py") { @("-3", "-c") } else { @("-c") }
                $ver = & $candidate @invokeArgs "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
                if ($ver -match "^(\d+)\.(\d+)$") {
                    $major = [int]$Matches[1]
                    $minor = [int]$Matches[2]
                    if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 12)) {
                        $PythonExe = $candidate
                        $PythonVer = $ver.Trim()
                        break
                    }
                }
            } catch {}
        }
    }

    if (-not $PythonExe) {
        Write-Fail @"
Python 3.12 or later is required but was not found in PATH.

  Download from: https://www.python.org/downloads/
  Or via winget:  winget install Python.Python.3.12

Alternatively, install Astral uv (https://docs.astral.sh/uv/) and re-run this installer.
"@
    }

    Write-Ok "Python $PythonVer found ($PythonExe)"

    if (-not $InstallDir) {
        $InstallDir = $DefaultDir
    }

    Write-Step "Setting up virtual environment at $InstallDir ..."
    if (-not (Test-Path $InstallDir)) {
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    }

    $invokeArgs = if ($PythonExe -eq "py") { @("-3", "-m", "venv", $InstallDir) } else { @("-m", "venv", $InstallDir) }
    & $PythonExe @invokeArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Failed to create Python virtual environment at $InstallDir."
    }

    $ScriptsDir = Join-Path $InstallDir "Scripts"
    $VenvPython = Join-Path $ScriptsDir "python.exe"
    if (-not (Test-Path $VenvPython)) {
        Write-Fail "Virtual environment Python executable not found at $VenvPython."
    }

    Write-Step "Installing $pkgSpec via pip ..."
    & $VenvPython -m pip install --upgrade --no-warn-script-location pip
    & $VenvPython -m pip install --upgrade --no-warn-script-location $pkgSpec
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Failed to install $pkgSpec via pip."
    }
    Write-Ok "Installed $pkgSpec to $InstallDir"

    # --- PATH registration ---
    Write-Step "Updating User PATH ..."
    $CurrentPath = [Environment]::GetEnvironmentVariable("PATH", "User")

    if ($CurrentPath -notlike "*$ScriptsDir*") {
        $NewPath = if ($CurrentPath) { "$ScriptsDir;$CurrentPath" } else { $ScriptsDir }
        [Environment]::SetEnvironmentVariable("PATH", $NewPath, "User")
        $env:PATH = "$ScriptsDir;$env:PATH"
        Write-Ok "Added $ScriptsDir to User PATH"
    } else {
        Write-Ok "$ScriptsDir is already in PATH"
    }
}

# --- Smoke test ---------------------------------------------------------------

Write-Step "Verifying installation ..."

$verified = $false
try {
    $verLine = & aikito version 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Ok $verLine.ToString().Trim()
        $verified = $true
    }
} catch {}

if (-not $verified) {
    $candidates = @()
    if ($ScriptsDir) {
        $candidates += (Join-Path $ScriptsDir "aikito.exe")
    }
    if ($env:UV_TOOL_BIN_DIR) {
        $candidates += (Join-Path $env:UV_TOOL_BIN_DIR "aikito.exe")
    }
    if ($env:USERPROFILE) {
        $candidates += (Join-Path $env:USERPROFILE ".local\bin\aikito.exe")
    }

    foreach ($exe in $candidates) {
        if ($exe -and (Test-Path $exe)) {
            try {
                $verLine = & $exe version 2>&1
                if ($LASTEXITCODE -eq 0) {
                    Write-Ok $verLine.ToString().Trim()
                    $verified = $true
                    break
                }
            } catch {}
        }
    }
}

if (-not $verified) {
    Write-Warn "Could not verify 'aikito version' in current session. Open a new terminal to check."
}

# Check for stale resolution shadowing the new install
$resolved = (Get-Command aikito -ErrorAction SilentlyContinue).Source
$expectedLocation = $false
if ($resolved) {
    if ($ScriptsDir -and ($resolved -like "$ScriptsDir*")) {
        $expectedLocation = $true
    }
    if ($env:USERPROFILE -and ($resolved -like "*\.local\bin\*")) {
        $expectedLocation = $true
    }
    if ($env:UV_TOOL_BIN_DIR -and ($resolved -like "$($env:UV_TOOL_BIN_DIR)*")) {
        $expectedLocation = $true
    }
    if (-not $expectedLocation) {
        Write-Warn "'aikito' currently resolves to '$resolved' — a stale or conflicting PATH entry may shadow the new installation."
    }
}

# --- Done ---------------------------------------------------------------------

Write-Host ""
Write-Host "  $DisplayName installed successfully!" -ForegroundColor Green
Write-Host ""
Write-Host "  Next steps:" -ForegroundColor White
Write-Host ""
Write-Host "    1. Open a NEW terminal (to pick up the updated PATH)." -ForegroundColor DarkGray
Write-Host ""
Write-Host "    2. Set up your workspace:" -ForegroundColor DarkGray
Write-Host "       - Brand new workspace:" -ForegroundColor DarkGray
Write-Host "           aikito init workspace" -ForegroundColor Cyan
Write-Host "       - Existing workspace:" -ForegroundColor DarkGray
Write-Host "           aikito init workspace <workspace-path>" -ForegroundColor Cyan
Write-Host ""
Write-Host "    3. Sync your workspace:" -ForegroundColor DarkGray
Write-Host "         aikito sync" -ForegroundColor Cyan
Write-Host ""
Write-Host "    4. (Optional) Enable PowerShell tab completion." -ForegroundColor DarkGray
Write-Host "       Run this to append it to your `$PROFILE automatically:" -ForegroundColor DarkGray
Write-Host '         if (!(Test-Path $PROFILE)) { New-Item -ItemType File -Path $PROFILE -Force | Out-Null }; Add-Content $PROFILE "`nInvoke-Expression (& aikito completion powershell | Out-String)"' -ForegroundColor Cyan
Write-Host ""
