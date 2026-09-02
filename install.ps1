#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Aikito Windows one-liner installer.

.DESCRIPTION
    Downloads the latest Aikito release from GitHub, installs to
    %LOCALAPPDATA%\Programs\aikito, and adds the bin directory to the
    current user's PATH environment variable.

.EXAMPLE
    irm https://raw.githubusercontent.com/lsaint/aikito/main/install.ps1 | iex
#>
[CmdletBinding()]
param (
    # Install a specific version tag instead of the latest release.
    [string] $Version = "",

    # Install to a custom directory instead of the default.
    [string] $InstallDir = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --- Constants ----------------------------------------------------------------

$Repo        = "lsaint/aikito"
$ApiBase     = "https://api.github.com/repos/$Repo"
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

# --- Banner -------------------------------------------------------------------

Write-Host ""
Write-Host "  $DisplayName Installer for Windows" -ForegroundColor White
Write-Host "  -----------------------------------------" -ForegroundColor DarkGray
Write-Host ""

# --- Check Python -------------------------------------------------------------

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

After installing Python, re-run this installer.
"@
}

Write-Ok "Python $PythonVer found ($PythonExe)"

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

# --- Resolve install dir and version ------------------------------------------

if (-not $InstallDir) {
    $InstallDir = $DefaultDir
}

Write-Step "Fetching release information from GitHub ..."

if ($Version) {
    $Tag = $Version.TrimStart("v")
    $ApiUrl = "$ApiBase/releases/tags/v$Tag"
} else {
    $ApiUrl = "$ApiBase/releases/latest"
}

try {
    $Release = Invoke-RestMethod -Uri $ApiUrl -Headers @{ "User-Agent" = "aikito-installer/1.0" }
} catch {
    Write-Fail "Could not fetch release info from GitHub. Check your internet connection."
}

$TagName     = $Release.tag_name
$VersionNum  = $TagName.TrimStart("v")
$AssetName   = "aikito-$VersionNum.zip"
$DownloadUrl = $null

foreach ($asset in $Release.assets) {
    if ($asset.name -eq $AssetName) {
        $DownloadUrl = $asset.browser_download_url
        break
    }
}

# Fall back to GitHub source zipball
if (-not $DownloadUrl) {
    $DownloadUrl = $Release.zipball_url
    $AssetName   = "aikito-source-$TagName.zip"
}

Write-Ok "Version: $TagName"

# --- Download -----------------------------------------------------------------

$TempDir  = Join-Path ([System.IO.Path]::GetTempPath()) "aikito-install-$([System.Guid]::NewGuid())"
$ZipPath  = Join-Path $TempDir $AssetName
New-Item -ItemType Directory -Path $TempDir -Force | Out-Null

Write-Step "Downloading $AssetName ..."

try {
    Invoke-WebRequest -Uri $DownloadUrl -OutFile $ZipPath -UseBasicParsing
} catch {
    Remove-Item $TempDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Fail "Download failed: $_"
}

# --- Extract & Install --------------------------------------------------------

Write-Step "Installing to $InstallDir ..."

if (Test-Path $InstallDir) {
    Remove-Item $InstallDir -Recurse -Force
}

$ExtractDir = Join-Path $TempDir "extracted"
Expand-Archive -Path $ZipPath -DestinationPath $ExtractDir -Force

# GitHub zipball nests content under a single top-level directory
$TopLevel = Get-ChildItem -Path $ExtractDir -Directory | Select-Object -First 1
if ($TopLevel) {
    Move-Item -Path $TopLevel.FullName -Destination $InstallDir
} else {
    Move-Item -Path $ExtractDir -Destination $InstallDir
}

Remove-Item $TempDir -Recurse -Force -ErrorAction SilentlyContinue
Write-Ok "Installed to $InstallDir"

# --- PATH registration --------------------------------------------------------

Write-Step "Updating User PATH ..."

$BinDir     = Join-Path $InstallDir "bin"
$CurrentPath = [Environment]::GetEnvironmentVariable("PATH", "User")

if ($CurrentPath -notlike "*$BinDir*") {
    $NewPath = "$BinDir;$CurrentPath"
    [Environment]::SetEnvironmentVariable("PATH", $NewPath, "User")
    $env:PATH = "$BinDir;$env:PATH"
    Write-Ok "Added $BinDir to User PATH"
} else {
    Write-Ok "$BinDir is already in PATH"
}

# --- Smoke test ---------------------------------------------------------------

Write-Step "Verifying installation ..."

$AikitoPs1 = Join-Path $BinDir "aikito.ps1"
try {
    $VerLine = & pwsh -NoProfile -File $AikitoPs1 version 2>&1
    if ($LASTEXITCODE -ne 0) { throw "non-zero exit" }
    Write-Ok $VerLine.ToString().Trim()
} catch {
    Write-Warn "Could not run 'aikito version'. Open a new terminal and try manually."
}

# --- Done ---------------------------------------------------------------------

Write-Host ""
Write-Host "  $DisplayName $TagName installed successfully!" -ForegroundColor Green
Write-Host ""
Write-Host "  Next steps:" -ForegroundColor White
Write-Host ""
Write-Host "    1. Open a NEW terminal (to pick up the updated PATH)." -ForegroundColor DarkGray
Write-Host ""
Write-Host "    2. Initialize your workspace:" -ForegroundColor DarkGray
Write-Host "         aikito init workspace" -ForegroundColor Cyan
Write-Host ""
Write-Host "    3. Sync global resources:" -ForegroundColor DarkGray
Write-Host "         aikito sync global" -ForegroundColor Cyan
Write-Host ""
Write-Host "    4. (Optional) Enable PowerShell tab completion." -ForegroundColor DarkGray
Write-Host "       Run this to append it to your `$PROFILE automatically:" -ForegroundColor DarkGray
Write-Host '         if (!(Test-Path $PROFILE)) { New-Item -ItemType File -Path $PROFILE -Force | Out-Null }; Add-Content $PROFILE "`nInvoke-Expression (& aikito completion powershell | Out-String)"' -ForegroundColor Cyan
Write-Host ""
